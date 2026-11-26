# Wheelhouse

This repo helps projects **reuse downloaded Python packages and install them
reliably without internet access**.

A *wheel* is a ready-to-install Python package file. A *wheelhouse* is a shared
folder containing those files, so several projects can reuse them.

It works in three steps:

1. **Choose packages — `lock`.** Read a project’s requirements and ask uv to
   find compatible versions, preferring suitable wheels already in the shared
   folder. Fetch dependency information as needed, but no wheel files. Save the
   exact selection, download locations, and checksums in `wheelhouse.lock`.

2. **Fill the folder — `acquire`.** Follow that lock, verify existing wheels,
   and download only missing ones. Check both the package bytes and the
   dependency information before accepting a file. Conflicting files cause an
   error rather than being overwritten.

3. **Prepare installation — `check`.** Without network access, verify that every
   required wheel is present and produce a temporary lock pointing to local
   files. uv then installs from those files.

For example, two projects needing the same NumPy wheel can share one downloaded
copy. Once their locked dependencies are present, their installations can run
offline.

Each lock applies to a matching Python version and platform. The current tests
demonstrate Requests and NumPy acquisition, reuse, and offline installation.
Source builds are outside its scope; Torch/CUDA has not been qualified.

## Build and test the tool

Run `make` to list workflows. Pass a local Docker context on the command line,
or omit it to choose at the terminal; environment values never select it.
The Make entrypoint and input selection are copied and adapted from ChefNMR2.
From this checkout, run:

```sh
make test DOCKER_CONTEXT=science
```

This builds the pinned tools image, runs the repository checks, creates
`dist/wheelhouse.pyz`, and tests that bundle. The artifact tests run without
Python site packages; the integration tests acquire Requests and NumPy online,
then install them in a fresh container with networking disabled. Temporary test
containers and the shared test volume are removed after the run.

The individual targets are:

```sh
make tests/repo-checks DOCKER_CONTEXT=science
make tests/unit DOCKER_CONTEXT=science
make tests/integration DOCKER_CONTEXT=science
make package DOCKER_CONTEXT=science
```

`package` produces `dist/wheelhouse.pyz` and `dist/SHA256SUMS`. The bundle includes
its Python dependencies and their installed license and metadata files. It
requires Python and, for lock creation, the uv executable. The tested versions
are pinned in [Dockerfile](Dockerfile). No host Python installation is needed.
Image preparation can use the network; bundle creation and repository checks
run without network access. Rebuilding replaces the outputs in `dist/`.

## Declare your project's dependencies

Add static dependencies and a publisher index to your project's `pyproject.toml`:

```toml
[project]
dependencies = ["requests==2.32.3", "numpy==2.2.2"]

[tool.wheelhouse]
index = "https://pypi.org/simple"
```

An optional `[tool.wheelhouse.sources]` table maps individual package names to
other HTTPS indexes. Source builds, dynamic dependencies, and direct dependency
URLs are unsupported. Publishers must supply separate dependency metadata when
a verified wheel is unavailable locally. Missing required metadata fails lock
creation without downloading wheel bytes or replacing the existing lock.

## Create the lock and acquire wheels

After building the tool, set these paths in one shell. Replace the project and
pool paths with your own absolute paths; keep the pool outside your project.

```sh
context=science
image=wheelhouse:proof
bundle="$PWD/dist/wheelhouse.pyz"
project=/absolute/path/to/your-project
pool=/absolute/path/to/shared-wheels
mkdir -p "$pool"
```

Create a lock for the Python version and platform inside the tools container:

```sh
docker --context "$context" run --rm --pull never \
  --mount "type=bind,src=$bundle,dst=/tool.pyz,readonly" \
  --mount "type=bind,src=$project,dst=/project" \
  --mount "type=bind,src=$pool,dst=/wheels,readonly" \
  --entrypoint python "$image" -S /tool.pyz lock \
  --project /project/pyproject.toml --pool /wheels \
  --lock /project/wheelhouse.lock
```

Commit `wheelhouse.lock` with the project. Lock creation contacts publisher
indexes even when the pool already contains all needed wheels. Run it again
when you intend to reconsider the dependency selection.

Acquire the exact locked files, preserving that selection:

```sh
docker --context "$context" run --rm --pull never \
  --mount "type=bind,src=$bundle,dst=/tool.pyz,readonly" \
  --mount "type=bind,src=$project,dst=/project,readonly" \
  --mount "type=bind,src=$pool,dst=/wheels" \
  --entrypoint python "$image" -S /tool.pyz acquire \
  --lock /project/wheelhouse.lock --pool /wheels
```

Repeating acquisition verifies and reuses existing files. A conflicting file
causes a failure and remains in place for inspection.

## Install offline

The following command demonstrates installation in a disposable container.
It uses the paths set above, with both inputs mounted read-only:

```sh
docker --context "$context" run --rm --pull never --network none \
  --mount "type=bind,src=$bundle,dst=/tool.pyz,readonly" \
  --mount "type=bind,src=$project,dst=/project,readonly" \
  --mount "type=bind,src=$pool,dst=/wheels,readonly" \
  --entrypoint sh "$image" -ec '
    python -S /tool.pyz check --lock /project/wheelhouse.lock \
      --pool /wheels --local-lock /tmp/pylock.toml
    uv --no-config --no-cache venv /tmp/venv \
      --python /usr/local/bin/python --no-python-downloads
    uv --no-config --no-cache pip sync --offline --no-index \
      --no-python-downloads --only-binary=:all: --require-hashes \
      --python /tmp/venv/bin/python /tmp/pylock.toml
    /tmp/venv/bin/python -I -c "import numpy, requests"
  '
```

The final import matches the example dependencies; change it for your project.
The container and installed environment disappear on exit. For a deployable
application, perform these steps in its image build and retain the installed
environment in the resulting image. Use the application's target interpreter
for both lock creation and consumption. A lock for another Python version,
ABI, OS, or architecture is rejected. Additional targets need qualification;
the current proof exercises Linux CPython 3.13.

The temporary `pylock.toml` contains local paths. Do not commit or distribute it;
share the authoritative `wheelhouse.lock` instead.

## Publish and consume a release

Publish the zipapp and checksum as assets of a versioned GitHub release. This
follows the provider-tools artifact model without coupling releases to its SDK.
Publishing requires Git and an authenticated GitHub CLI (`gh`) on the host.
Write the release notes as `notes/NNN_release_vX.Y.Z.md`, keeping each paragraph
or list item on one source line. Commit the source and notes, then run:

```sh
make publish/release DOCKER_CONTEXT=science
```

The highest version in the release-note filenames determines the tag; for
example, `001_release_v0.1.0.md` declares `v0.1.0`. Versions are ordered
numerically, independently of note numbering. Each version must have one note.
If the newest version is already tagged, write a note for the next version;
the lane never falls back to an older untagged note.

This publishes publicly to `numpde/wheelhouse`. The lane exports the clean
commit to a temporary directory, builds and tests the bundle there, pushes the
release tag, and uploads `wheelhouse.pyz` and `SHA256SUMS` to a draft release.
It downloads those assets and compares them with the tested files before making
the release public. The tools image must match the target used by the tests.

The lane does not rebuild between testing and uploading. Existing tags are
rejected, and assets are never replaced. On failure it reports the retained
source and test log. Inspect any remote release and tag before retrying;
a lost response to the final publish request can leave publication uncertain.
Successful runs remove their temporary directory.
Ordinary builds and CI never publish releases.

Consumers should pin the release URL and expected SHA-256 in their preparation
configuration. Download and verify the bundle during explicit preparation,
then copy or mount it into the target container. Run it with
`python -S /path/to/wheelhouse.pyz`; it does not install packages into the
application environment. Supply the tested uv version for `lock` and offline
installation. The native uv executable is separate from the Python bundle.

For an offline destination, prepare its target image, the verified tool bundle,
the project's `wheelhouse.lock`, and every wheel required by that lock before
transfer. An online destination can acquire the wheels from their locked URLs.
