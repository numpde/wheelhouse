ifndef ENGINE_CONTEXT
$(error Set ENGINE_CONTEXT, for example: make proof ENGINE_CONTEXT=science)
endif

IMAGE := wheelhouse:proof
DOCKER := docker --context "$(ENGINE_CONTEXT)"
LOCK_ARGS := --lock /project/wheelhouse.lock --pool /wheels
run = $(DOCKER) run --rm --pull never \
    -v "$(CURDIR)/.proof/project:/project:$(1)" \
    -v "$(CURDIR)/.proof/wheels:/wheels:$(2)"

.PHONY: proof image lock acquire offline

proof:
	$(MAKE) image
	$(MAKE) lock
	$(MAKE) acquire
	$(MAKE) offline

image:
	$(DOCKER) build --tag $(IMAGE) .

lock:
	mkdir -p .proof/project .proof/wheels
	$(call run,rw,ro) -v "$(CURDIR)/proof.toml:/input.toml:ro" $(IMAGE) \
	    lock --project /input.toml $(LOCK_ARGS)

acquire:
	$(call run,ro,rw) $(IMAGE) acquire $(LOCK_ARGS)

offline:
	$(call run,ro,ro) --network none --entrypoint sh $(IMAGE) -ec '\
	    python /opt/wheelhouse/wheelhouse.py acquire $(LOCK_ARGS) && \
	    python /opt/wheelhouse/wheelhouse.py check $(LOCK_ARGS) --local-lock /tmp/pylock.toml && \
	    uv --no-config --no-cache venv /opt/venv \
	        --python /usr/local/bin/python --no-python-downloads && \
	    uv --no-config --no-cache pip sync --offline --no-index \
	        --no-python-downloads --only-binary=:all: --require-hashes \
	        --python /opt/venv/bin/python /tmp/pylock.toml && \
	    uv --no-config --no-cache pip check --python /opt/venv/bin/python'
