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

## Prepare the tool

The examples below run in Docker. You need its CLI and a running local engine
whose context uses a Unix socket. In one shell, list your contexts and select
one you already use; replace `default` if needed:

```sh
docker context ls
context=default
```

Choose **one** preparation route. Both set `image` and `bundle` for the same
[lock, acquire](#create-the-lock-and-acquire-wheels), and
[offline install](#install-offline) commands below. Preparation and locking need
network access; acquisition needs it for missing wheels. Offline installation
starts after those inputs are ready.

### Use a released bundle with your target image

Use this route if you already have a local Linux target image providing `sh`,
Python, and uv on `PATH`. The tested versions are Python 3.13.18 and uv 0.10.0,
pinned in [Dockerfile](Dockerfile). The bundle includes its Python dependencies,
licenses, and metadata; it does not include Python or uv. No host Python is
needed for these Docker examples.

With `curl` and `sha256sum` on the host, download the bundle from the pinned
[v0.2.0 release](https://github.com/numpde/wheelhouse/releases/tag/v0.2.0) and
verify it against the recorded checksum below. Use a dedicated folder; these
commands replace `wheelhouse.pyz` there:

```sh
version=v0.2.0
release="https://github.com/numpde/wheelhouse/releases/download/$version"
expected_sha256=b9730fd1dd9668e98f55a952e64328f193b5d2ca6d203fc1c150332fc672e90a
mkdir -p vendor/wheelhouse
curl --fail --location "$release/wheelhouse.pyz" \
  --output vendor/wheelhouse/wheelhouse.pyz
(cd vendor/wheelhouse && \
  printf '%s  wheelhouse.pyz\n' "$expected_sha256" | sha256sum --check -)
bundle="$PWD/vendor/wheelhouse/wheelhouse.pyz"
image=your-project:prepared
```

Replace `your-project:prepared` with that existing image. Expect
`wheelhouse.pyz: OK`; stop if verification fails. Record the release URL and
expected SHA-256 in your project's preparation configuration so future runs
verify the same artifact. When upgrading, update the version and expected
checksum together using the new release's checksum.

### Or build the tools image and bundle from source

Use this route for the complete example when you do not have a target image.
You also need Git, GNU Make, and Bash on the host. From an existing checkout,
skip the clone and `cd` commands:

```sh
git clone https://github.com/numpde/wheelhouse.git
cd wheelhouse
make package DOCKER_CONTEXT="$context"
image=wheelhouse:proof
bundle="$PWD/dist/wheelhouse.pyz"
```

This builds the pinned tools image and writes `dist/wheelhouse.pyz` and
`dist/SHA256SUMS`. Rebuilding replaces those outputs. The image provides Python
and uv for the demonstration; it does not build or deploy your application.
For application use, prepare your own target image with Python and uv as
described above, then set `image` to its name before running `lock` below.
Keep `image=wheelhouse:proof` to follow this demonstration.

## Declare your project's dependencies

Add static dependencies and a publisher index to your project's `pyproject.toml`:

```toml
[project]
dependencies = ["requests==2.32.3", "numpy==2.2.2"]

[tool.wheelhouse]
index = "https://pypi.org/simple"
```

An optional `[tool.wheelhouse.sources]` table maps individual package names to
other HTTPS indexes. To use a raw HTML file instead of an index directory, give
that package an explicit page URL:

```toml
[tool.wheelhouse.sources.provider-core]
page = "https://raw.githubusercontent.com/numpde/nmr-provider-tools/4721f8ba64011fd231cab7ff230decafc7252022/simple/provider-core.html"
```

This example pins the published provider-core 0.7.8 listing. Add
`provider-core[api]==0.7.8` to your project dependencies to use it. When upgrading,
update the dependency and listing commit together.

The page must link to wheels with SHA-256 hashes. For wheels missing
from the pool, it must also advertise a metadata sidecar at the wheel URL plus
`.metadata`, preferably with its own hash. Wheelhouse requests the exact page
URL and resolves relative wheel links against its final URL after redirects.
Pinning the listing to a commit keeps its wheel selection and hashes stable.

Source builds, dynamic dependencies, and direct dependency
URLs are unsupported. Publishers must supply separate dependency metadata when
a verified wheel is unavailable locally. Missing required metadata fails lock
creation without downloading wheel bytes or replacing the existing lock.

### Use a prepared wheel or a private candidate

The following capabilities are included in the pinned v0.2.0 release.

Bind a package to one wheel by declaring `wheel` (an HTTPS URL) and `sha256`,
or `path` (a local wheel file) and `sha256`. These bindings are exclusive:
Wheelhouse never substitutes a same-version wheel from another source.
Local paths are relative to the project file and must be visible inside the
target container. Keep candidate wheels in a private directory outside the pool.

For example, after building a candidate SDK wheel, declare its actual checksum:

```toml
[tool.wheelhouse.sources.provider-core]
path = "candidates/provider_core-0.7.8-py3-none-any.whl"
sha256 = "REPLACE_WITH_THE_CANDIDATE_SHA256"
```

Replace the placeholder with the wheel's 64-character lowercase SHA-256.
A lock made from this declaration contains an absolute local path; it is a
temporary candidate lock, not a portable release lock. A missing or changed
candidate fails even if the pool contains a released wheel with the same name.

An exact remote wheel can be prepared before locking, even when its publisher
does not provide a metadata sidecar. In the target container, with the project,
bundle and writable pool mounted as above, run:

```sh
python -S /tool.pyz acquire --project /project/pyproject.toml \
  --package provider-core --pool /wheels
```

Preparation reads the same declaration used by `lock`, verifies the wheel's
checksum and embedded metadata, and downloads no dependencies. Locking then
uses that verified metadata. Local preparation verifies the candidate in place.
Index and page declarations require ordinary resolution instead.

To resolve entirely from local wheels, add `--offline` to `lock` and disable the
container's network. Explicit bindings must then be local; packages without a
binding use compatible wheels in the chosen pool. The resulting lock records
local paths. Offline resolution never falls back to a publisher.

## Create the lock and acquire wheels

Continue in the same shell used to prepare the tool. Set your existing project
folder and a shared wheel folder to absolute paths; keep the pool outside your
project. The project folder must contain the `pyproject.toml` from above.

```sh
project=/absolute/path/to/your-project
pool=/absolute/path/to/shared-wheels
mkdir -p "$pool"
```

Use the same target image for locking, acquisition, and installation. The lock
records the running interpreter and platform, including the Python patch
version and ABI (the binary interface used by compiled packages). A different
target is rejected; regenerate the lock in the intended target image.

Create the lock online:

```sh
docker --context "$context" run --rm --pull never \
  --mount "type=bind,src=$bundle,dst=/tool.pyz,readonly" \
  --mount "type=bind,src=$project,dst=/project" \
  --mount "type=bind,src=$pool,dst=/wheels,readonly" \
  --entrypoint python "$image" -S /tool.pyz lock \
  --project /project/pyproject.toml --pool /wheels \
  --lock /project/wheelhouse.lock
```

Commit a remote-origin `wheelhouse.lock` with the project. Keep candidate and
offline locks temporary because they contain local paths. Ordinary index
resolution contacts publishers even when the pool contains all needed wheels.
Run it again when you intend to reconsider the dependency selection.

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
causes a failure and remains in place for inspection. If a later download
fails, earlier verified wheels remain in the pool; retrying acquisition reuses
them. Successful acquisition prints JSON listing the selected `artifacts` and
newly `downloaded` filenames; a fully populated pool has `"downloaded": []`.

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
      --python "$(command -v python)" --no-python-downloads
    uv --no-config --no-cache pip sync --offline --no-index \
      --no-python-downloads --only-binary=:all: --require-hashes \
      --python /tmp/venv/bin/python /tmp/pylock.toml
    /tmp/venv/bin/python -I -c "import numpy, requests"
  '
```

A zero exit status from the final import confirms the example packages installed
without network access; it prints nothing on success. Change the imports for
your project. The container and installed environment disappear on exit. For a
deployable application, perform these steps in its image build and retain the
installed environment in the resulting image. Additional targets need qualification;
the current proof exercises Linux CPython 3.13.

The temporary `pylock.toml` contains local paths. Do not commit or distribute it;
share the remote-origin `wheelhouse.lock` instead. Candidate and offline locks
also contain local paths and must be regenerated where those inputs are available.

### Install with pip or supply container destinations

Wheelhouse can also produce hashed requirements from the complete checked
selection. In a prepared target container, use:

```sh
python -S /tool.pyz check --lock /project/wheelhouse.lock \
  --pool /wheels --requirements /tmp/install.txt
python -m pip install --no-index --no-deps --require-hashes -r /tmp/install.txt
```

The output uses escaped `file:` URLs and SHA-256 hashes, supported by pip and uv.
Keep the container offline. The installer verifies the bytes again when it uses
them. Failed verification leaves any existing output unchanged.

Each successful check also returns `artifact_entries`: the selected package,
version, filename, SHA-256 and absolute `source_path`. A build-context producer
can use these entries to transfer just the selected wheels, including private
candidates outside the pool.

When installation paths differ from source paths, pass
`--destination-map /project/destinations.json` together with `--requirements`.
The map contains an `artifacts` array with one entry per selected wheel:
`source_path`, `sha256`, and absolute `destination`. Copy source identities and
hashes from the check result, retain each wheel's filename, and cover the full
selection. Incomplete, extra, conflicting or duplicate mappings fail.
Wheelhouse renders installation paths; the consumer owns file transfer and
must verify that its exporter and image builder both finish successfully.

## Reuse the pool or prepare another machine

For a second project, use the same pool path and that project's own
`pyproject.toml` and `wheelhouse.lock`. Run `lock` and `acquire` again; compatible
cached wheels are preferred and reused. Ordinary index resolution still contacts publishers.
After changing dependencies, regenerate the lock, review and commit it, then
acquire any newly selected wheels.

Keep the shared wheel pool and commit each project's remote-origin
`wheelhouse.lock`. Regenerate the temporary `pylock.toml` wherever you install,
because it contains local paths. Do not commit or transfer that temporary file.

Before using an offline destination, prepare its matching target image, the
verified tool bundle, its project lock, and every wheel required by that lock.
Run `check` there and generate a new local projection before installation. An
online destination can instead acquire missing wheels from their locked URLs.

## Build and test changes

From a checkout, run `make` to list workflows. Pass `DOCKER_CONTEXT` explicitly,
or omit it to choose at the terminal; environment values never select it.
The Make entrypoint and input selection are copied and adapted from ChefNMR2.

```sh
make test DOCKER_CONTEXT="$context"
```

This builds the tools image and bundle, runs repository and artifact checks,
acquires Requests and NumPy online, and installs them in a fresh container with
networking disabled. Temporary containers and the test volume are removed.
Image preparation may use the network; packaging and repository checks run
offline. The individual test targets are:

```sh
make tests/repo-checks DOCKER_CONTEXT="$context"
make tests/unit DOCKER_CONTEXT="$context"
make tests/integration DOCKER_CONTEXT="$context"
```

## Publish a release

Maintainers publish the bundle and checksum as assets of a versioned GitHub
release. Work from a clean checkout with Git, Make, Bash, a local Docker engine,
and an authenticated GitHub CLI (`gh`) with release access to this repository.
Write the release notes as `notes/NNN_release_vX.Y.Z.md`, keeping each paragraph
or list item on one source line. Commit the source and notes, then run:

```sh
make publish/release DOCKER_CONTEXT="$context"
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
