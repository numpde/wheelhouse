SHELL := /bin/sh
.SHELLFLAGS := -eu -c
.ONESHELL:
.DEFAULT_GOAL := proof

ENGINE_CONTEXT ?=
IMAGE := wheelhouse:proof
DOCKER = docker --context "$(ENGINE_CONTEXT)"

.PHONY: image proof

image:
	@test -n "$(ENGINE_CONTEXT)" || { echo 'Set ENGINE_CONTEXT explicitly, e.g. make proof ENGINE_CONTEXT=science' >&2; exit 2; }
	$(DOCKER) build --tag "$(IMAGE)" "$(CURDIR)"

proof: image
	@volume="wheelhouse-proof-$$(date +%s)-$$$$"
	$(DOCKER) volume create "$$volume" >/dev/null
	trap '$(DOCKER) volume rm "$$volume" >/dev/null || true' 0

	# Cold pool: metadata-only lock, then acquire the exact missing wheels.
	$(DOCKER) run --rm -i --pull never --network bridge \
	  --mount "type=volume,src=$$volume,dst=/trial" \
	  --entrypoint sh "$(IMAGE)" -es <<'SH'
	set -u
	mkdir /trial/wheels
	printf '%s\n' '[project]' 'dependencies = ["requests==2.32.3", "numpy==2.2.2"]' \
	  '[tool.wheelhouse]' 'index = "https://pypi.org/simple"' > /trial/pyproject.toml
	python /opt/wheelhouse/wheelhouse.py lock --project /trial/pyproject.toml \
	  --pool /trial/wheels --lock /trial/wheelhouse.lock
	test -z "$$(ls -A /trial/wheels)"
	python /opt/wheelhouse/wheelhouse.py acquire \
	  --lock /trial/wheelhouse.lock --pool /trial/wheels
	echo 'PASS cold pool: metadata-only resolution and real acquisition'
	SH

	# A wider requirement must retain the eligible existing selection.
	# The pool and original lock are read-only; the warm lock is disposable.
	$(DOCKER) run --rm -i --pull never --network bridge \
	  --mount "type=volume,src=$$volume,dst=/trial,readonly" \
	  --entrypoint sh "$(IMAGE)" -es <<'SH'
	set -u
	python -c 'import pathlib, tomllib, tomli_w; project = tomllib.loads(pathlib.Path("/trial/pyproject.toml").read_text()); project["project"]["dependencies"] = ["requests>=2.32,<3", "numpy>=2.2,<3"]; pathlib.Path("/tmp/pyproject.toml").write_text(tomli_w.dumps(project))'
	python /opt/wheelhouse/wheelhouse.py lock --project /tmp/pyproject.toml \
	  --pool /trial/wheels --lock /tmp/warm.lock
	python -c 'import sys; sys.path.insert(0, "/opt/wheelhouse"); import wheelhouse; selected = lambda path: {artifact[:3] for artifact in wheelhouse.load_locked(path)[2]}; assert selected("/trial/wheelhouse.lock") == selected("/tmp/warm.lock"), "Wider requirements replaced an eligible cached artifact"'
	python /opt/wheelhouse/wheelhouse.py acquire \
	  --lock /tmp/warm.lock --pool /trial/wheels > /tmp/acquisition.json
	python -c 'import json; assert json.load(open("/tmp/acquisition.json"))["downloaded"] == []'
	echo 'PASS warm pool: local selection retained and zero wheel downloads'
	SH

	# Fresh container, no network, read-only inputs, no originating tool cache.
	$(DOCKER) run --rm -i --pull never --network none \
	  --mount "type=volume,src=$$volume,dst=/trial,readonly" \
	  --entrypoint sh "$(IMAGE)" -es <<'SH'
	set -u
	python /opt/wheelhouse/wheelhouse.py check --lock /trial/wheelhouse.lock \
	  --pool /trial/wheels --local-lock /tmp/pylock.toml
	uv --no-config --no-cache venv /opt/venv \
	  --python /usr/local/bin/python --no-python-downloads
	uv --no-config --no-cache pip sync --offline --no-index \
	  --no-python-downloads --only-binary=:all: --require-hashes \
	  --python /opt/venv/bin/python /tmp/pylock.toml
	/opt/venv/bin/python -I -c 'import numpy, requests; assert numpy.arange(3).sum() == 3; print("PASS offline installation:", "NumPy", numpy.__version__, "Requests", requests.__version__)'

	# Failed checks must not produce an installation projection.
	mkdir /tmp/empty
	if python /opt/wheelhouse/wheelhouse.py check --lock /trial/wheelhouse.lock \
	  --pool /tmp/empty --local-lock /tmp/pylock.failed.toml; then
	  echo 'FAIL: accepted an empty pool' >&2; exit 1
	fi
	test ! -e /tmp/pylock.failed.toml
	python -c 'import json, pathlib; lock = json.loads(pathlib.Path("/trial/wheelhouse.lock").read_text()); lock["metadata"] = {}; pathlib.Path("/tmp/missing-evidence.lock").write_text(json.dumps(lock))'
	if python /opt/wheelhouse/wheelhouse.py check --lock /tmp/missing-evidence.lock \
	  --pool /trial/wheels --local-lock /tmp/pylock.failed.toml; then
	  echo 'FAIL: accepted missing metadata evidence' >&2; exit 1
	fi
	test ! -e /tmp/pylock.failed.toml
	python -c 'import json, pathlib; lock = json.loads(pathlib.Path("/trial/wheelhouse.lock").read_text()); lock["metadata"] = dict.fromkeys(lock["metadata"], "0" * 64); pathlib.Path("/tmp/conflicting-evidence.lock").write_text(json.dumps(lock))'
	if python /opt/wheelhouse/wheelhouse.py check --lock /tmp/conflicting-evidence.lock \
	  --pool /trial/wheels --local-lock /tmp/pylock.failed.toml; then
	  echo 'FAIL: accepted conflicting metadata evidence' >&2; exit 1
	fi
	test ! -e /tmp/pylock.failed.toml
	echo 'PASS offline failures: missing wheels and missing/conflicting metadata evidence rejected'
	SH
