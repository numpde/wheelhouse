# Wheelhouse

Prefer wheels already in a shared folder, resolve missing dependencies from
metadata, then acquire and install the locked files. See the
[desiderata](notes/000_desiderata.txt).

uv handles resolution and installation; `packaging` handles lock validation and
wheel selection. This repo supplies pool management and metadata checks.
Its Python dependencies are pinned in [requirements.txt](requirements.txt)
and installed inside the tools image.

| Command | Project | Shared wheel folder | Network |
| --- | --- | --- | --- |
| `lock` | Read declarations; write `wheelhouse.lock` | Read-only | Publisher indexes and separate metadata |
| `acquire` | Read the lock | Reuse verified files; download missing wheels | Locked artifact URLs |
| `check` | Read the lock; optionally write a local pylock | Read-only | None |

## Test

Choose an existing Docker context explicitly:

```sh
cd /home/ra/repos/wheelhouse &&
make test ENGINE_CONTEXT=science
```

Make builds the tools image and runs the [artifact tests](tests/test_artifacts.py)
and the [Requests/NumPy integration test](tests/test_wheelhouse.py), using the
[checked-in project](tests/fixtures/pyproject.toml). The integration test acquires
wheels online, then installs them in a fresh container with networking disabled
and inputs mounted read-only. Its temporary Docker volume is mounted at `/trial`
and removed on success or failure; test containers and scratch files are also
removed. [CI](https://github.com/numpde/wheelhouse/actions/workflows/proof.yml)
runs the same target. No host Python is needed.

## Use a shared wheel folder

Declare dependencies and publishers in the consuming project's `pyproject.toml`:

```toml
[project]
dependencies = ["requests>=2.32,<3"]

[tool.wheelhouse]
index = "https://pypi.org/simple"

# Optional publisher for a particular package:
# [tool.wheelhouse.sources]
# torch = "https://download.pytorch.org/whl/cpu"
```

Set `project` and `pool` to existing absolute paths. The tools image defines the
Python version and platform for the lock and its consumers.

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

Commit `wheelhouse.lock`. Downloads go into the host folder mounted at `/wheels`.
Each wheel must match both its artifact hash and the metadata used during
resolution before publication or reuse. Conflicting pool files are retained
and reported. Acquisition follows the lock without resolving again.

## Install offline

Use the prepared tools image with networking disabled and the project and wheel
folder mounted read-only at `/project` and `/wheels`. Inside the container:

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

`/tmp/pylock.toml` is a disposable projection containing local paths. Keep
`/opt/venv` and the application in the resulting image; the wheel folder is a
build input.

## Lock and scope

The committed lock contains the target, a standard pylock resolution, and the
expected SHA-256 of each wheel's complete embedded `METADATA`. This evidence is
required for every permitted wheel. The lock is portable to machines matching
its Python version, ABI, OS and architecture; other targets need their own lock.

The prototype accepts static named dependencies and wheels from HTTPS Simple
API publishers. Resolution needs separately published metadata or a verified
local wheel; it never downloads wheel contents or byte ranges. Missing metadata
causes a failure and preserves the existing lock. Source builds, dynamic
dependencies and direct dependency URLs are outside the current scope.
Requests/NumPy is exercised by the integration test; Torch/CUDA remains a
separate trial.
