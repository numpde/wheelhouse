# Local-first wheelhouse demo

One portable PEP 751 lock, a shared flat directory of raw wheels, and an explicit offline installation. No provider code, no new dependency resolver, no new SDK release. The fixture is deliberately small: it tests CPython ABI tags, CPU/CUDA version tags, a transitive dependency, and a newer remote alternative without downloading Torch.

## Run the proof

Use a local Docker context. All Python and uv invocations below run inside containers. The first command explicitly prepares the pinned tools, including the released wheelhouse tool 0.7.7. Subsequent container runs do not pull images. The proof has no external network access: both its fixture publisher and cache view run on loopback inside the container.

```bash
CONTEXT=science &&
docker --context "$CONTEXT" build --platform linux/amd64 -t wheel-demo:tools . &&
docker --context "$CONTEXT" run --rm --pull=never --platform linux/amd64 \
    --network none -v "$PWD:/work:ro" wheel-demo:tools \
    python -B prove.py /wheelhouse.pyz
```

The proof checks a complete mixed pool (0 wheel transfers), a partially populated pool (1 transfer, keeping the older eligible cached helper), and a pool containing only an incompatible ABI/backend for the required CPU artifact (1 transfer of the correct wheel). It checks the portable lock's URLs and hashes, acquires into a separate empty pool with the released SDK, repeats acquisition with no transfers, and installs with the publisher stopped. The generated synthetic wheels and locks are disposable. `--no-cache` gives every uv invocation a fresh tool cache.

## Try actual packages

The provided requirements use Torch CPU and NumPy. Use a disposable pool first; for an existing shared pool, ensure no concurrent writer is modifying it during this demo. The following commands require the tools image built above. Specify your own absolute pool path.

```bash
CONTEXT=science &&
POOL="$HOME/.cache/wheelhouse-demo" &&
mkdir -p "$POOL" &&

# Explicit lock generation; may fetch missing wheel bytes to inspect metadata.
docker --context "$CONTEXT" run --rm --pull=never --platform linux/amd64 \
    -v "$PWD:/work" -v "$POOL:/wheels" wheel-demo:tools \
    python -B lock.py requirements.in /wheels pylock.toml sources.toml &&

# Explicit acquisition of the exact locked artifacts; reuse verified cache hits.
docker --context "$CONTEXT" run --rm --pull=never --platform linux/amd64 \
    -v "$PWD:/work:ro" -v "$POOL:/wheels" wheel-demo:tools \
    python -B /wheelhouse.pyz acquire --lock pylock.toml --pool /wheels &&

# Fresh container, no network, no tool cache, pool mounted read-only.
docker --context "$CONTEXT" run --rm --pull=never --platform linux/amd64 \
    --network none -v "$PWD:/work:ro" -v "$POOL:/wheels:ro" wheel-demo:tools sh -ec '
    python -B /wheelhouse.pyz check --lock pylock.toml --pool /wheels \
        --local-lock /tmp/pylock.local.toml --local-root /wheels &&
    uv --no-config --no-cache pip sync --system --offline --require-hashes /tmp/pylock.local.toml &&
    python -c "import torch, numpy; print(torch.__version__, numpy.__version__); print(torch.arange(4).square())"
'
```

The last command is an offline installation proof rather than a production image build. Commit only the authored files and generated publisher-URL `pylock.toml` if you want to keep the example. Never commit a projected local lock or the pool. To install elsewhere, reuse the committed lock and run acquisition, not lock generation.

## Why an adapter is needed

An arbitrary flat wheel pool does not contain publisher URLs. Also, simply adding `--find-links` to an online resolve is not a contractual cache-first policy. `lock.py` presents two ordered Simple API views of **the same declared source per package**. The first contains only hash-matching, target-compatible cached wheels. The second contains all hashed wheel links. Standard `packaging` checks tags; uv chooses dependency versions and solves the graph. The `unsafe-first-match` strategy is scoped to two views of the same source, not competing publishers. Every wheel GET is served from the pool when possible. On a miss, the adapter retrieves and verifies the publisher bytes and publishes them without overwriting another file. Finally it replaces the loopback aliases in uv's native lock with the original publisher URLs and literal wheel names. No guessing URLs from filenames, no second resolution, no embedded requirements-text lock. The existing SDK handles ordinary acquisition and the local lock projection.

## Scope and validation

This is a wheel-only demonstration for Linux x86-64 CPython 3.13, not a replacement for the production acquisition owner. The fixture contains no native machine code; its wheel tags exercise real resolver compatibility. Only explicit HTTPS publisher indexes with hashed wheel links (HTML Simple API), plus the local fixture, are supported. Index metadata can be requested even on a complete hit. The cache-first policy is a per-package preference, not a mathematical optimization of all possible dependency closures. The prototype does not preserve PEP 658 metadata sidecars, so lock generation can stream a missing wheel to read metadata, and backtracking can acquire a candidate that is ultimately not selected. It does not handle sdists, private indexes, concurrent pool writers, or production proxy policy. Actual public-index behavior, Docker execution and released SDK integration require running the commands above; the native resolver and fixture installation were exercised locally with uv 0.10.0, Python 3.13.5, packaging 25.0 and a TOML writer from the existing environment. No host dependencies were installed.
