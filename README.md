# Wheelhouse

A small implementation of [the desiderata](notes/000_desiderata.txt).
uv resolves dependencies; the released wheelhouse SDK selects locked artifacts,
checks their hashes, downloads them, and projects locks for offline installation.
This adapter supplies the local preference and mandatory metadata checks.

| Operation | Project | Shared wheel folder | Network |
| --- | --- | --- | --- |
| `lock` | Read declarations; atomically write `wheelhouse.lock` | Read-only | Publisher indexes and separate metadata only |
| `acquire` | Read the existing lock | Reuse verified files; publish missing verified wheels | Locked artifact URLs |
| `check` | Read the existing lock; optionally write a disposable local pylock | Read-only | None needed |

Lock creation never fetches remote wheel contents or byte ranges. It reads
metadata from verified local wheels or the publisher's exact wheel sidecar.
Missing required metadata is an error, including when encountered by uv's
prefetching. Existing locks survive failed resolution.

## Run the proof

Choose an existing Docker context explicitly. For example:

```sh
cd /home/ra/repos/wheelhouse &&
make proof ENGINE_CONTEXT=science
```

The [Makefile](Makefile) contains the proof commands directly. It explicitly
builds `wheelhouse:proof`, resolves and acquires Requests and NumPy from PyPI,
then widens their requirements and checks that the existing selection is
retained with zero further wheel downloads. A fresh container checks and
installs the closure with networking disabled and inputs mounted read-only.
It also checks failures for missing wheels and missing or conflicting metadata
evidence. All Python and packaging tools run inside containers.

Trial files and downloads live at `/trial` inside a temporary Docker volume
named `wheelhouse-proof-...`. The volume is removed afterward; the prepared
tools image remains. The proof does not need the operator's existing wheel
folder. [GitHub Actions](https://github.com/numpde/wheelhouse/actions/workflows/proof.yml)
runs this same Make target. `make image ENGINE_CONTEXT=science` prepares only
the tools image.

## Use an existing shared wheel folder

In the consuming project's `pyproject.toml`, declare static dependencies and
their publishers once. For example:

```toml
[project]
dependencies = ["requests>=2.32,<3"]

[tool.wheelhouse]
index = "https://pypi.org/simple"

# Optional package-specific publisher, used for all candidates of that package:
# [tool.wheelhouse.sources]
# torch = "https://download.pytorch.org/whl/cpu"
```

Set `project` and `pool` below to existing absolute paths. The interpreter and
platform of the tools container define the target; use the same prepared tools
image for these operations and the offline consumer.

```sh
context=science &&
project=/home/ra/repos/my-project &&
pool=/absolute/path/to/the/existing/wheel-folder &&
image=wheelhouse:tools &&
docker --context "$context" build --tag "$image" /home/ra/repos/wheelhouse &&
docker --context "$context" run --rm --pull never \
  --mount "type=bind,src=$project,dst=/project" \
  --mount "type=bind,src=$pool,dst=/wheels,readonly" \
  "$image" lock --project /project/pyproject.toml \
  --pool /wheels --lock /project/wheelhouse.lock &&
docker --context "$context" run --rm --pull never \
  --mount "type=bind,src=$project,dst=/project,readonly" \
  --mount "type=bind,src=$pool,dst=/wheels" \
  "$image" acquire --lock /project/wheelhouse.lock --pool /wheels
```

Commit `wheelhouse.lock`. Downloads go into the existing host folder mounted
at `/wheels`. Missing files are staged privately on that filesystem, checked
against both hashes, then published atomically. Conflicting existing files
are retained and reported. A complete pool also permits `acquire` with a
read-only mount. Acquisition never resolves again or updates the lock.

## Offline consumer

An offline container or Docker build must mount the project and wheel folder
read-only, disable networking, and use already prepared images. Inside that
container, run:

```sh
python /opt/wheelhouse/wheelhouse.py check \
  --lock /project/wheelhouse.lock --pool /wheels \
  --local-lock /tmp/pylock.toml &&
uv --no-config --no-cache venv /opt/venv \
  --python /usr/local/bin/python --no-python-downloads &&
uv --no-config --no-cache pip sync --offline --no-index \
  --no-python-downloads --only-binary=:all: --require-hashes \
  --python /opt/venv/bin/python /tmp/pylock.toml
```

`check` verifies every selected wheel before producing `/tmp/pylock.toml`.
That disposable file contains local paths for the installer; the committed
lock remains authoritative. In an application build, retain `/opt/venv` and
the application code in the resulting image. The runtime application needs
the installed environment; the wheel folder is only a build input.

## Lock contract and scope

`wheelhouse.lock` is a versioned JSON envelope containing a standard pylock
resolution, the target environment, and a required mapping from each wheel's
SHA-256 to the SHA-256 of the exact metadata used during resolution. Required
evidence is part of this format, rather than optional pylock tool annotations.
Consumption rejects absent evidence before selecting or downloading artifacts.

uv can resolve a version using one representative wheel's metadata. Every
wheel retained for that version is therefore bound to the same metadata
expectation. Acquisition and offline checking compare its complete embedded
`METADATA` bytes against that expectation. Hashing covers all headers; semantic
validation covers identity and dependency fields, including malformed or
duplicated dependency fields. Unrelated historical licence headers do not
prevent resolution.

The current scope is static named dependencies, wheels, and declared HTTPS
Simple API publishers. Direct dependency URLs, source builds, and dynamic
project dependencies fail explicitly. Artifact hashes must come from the
publisher. Publisher redirects are checked; lock-time redirects to wheel
URLs are blocked. HTTP is available only through an internal proof hook.

The lock records the running interpreter, ABI, OS and architecture. Kernel
marker values are recorded only when consulted by dependency markers. Other
machines must match that target and have compatible wheel tags; this is not
a cross-platform resolution. No persistent resolver cache is required.

The Makefile proof covers the real Requests/NumPy flow and the basic failures
above. It does not run the former synthetic fixtures for backtracking,
publisher routing, or redirects. Real Torch/CUDA acquisition and an operator's
existing wheel folder remain separate trials. Publishers without the required
sidecars can be used only when the needed exact wheel is already in the pool.
