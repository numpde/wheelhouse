# Local-first wheelhouse demo

One target-specific PEP 751 lock with publisher URLs and hashes, a shared flat directory of raw wheels, and an explicit offline installation. No provider code, no new dependency resolver, no new SDK release. The lock is portable between machines with the same target; it is not a cross-platform resolution. See [desiderata](notes/000_desiderata.txt) for the implementation-independent requirements.

## Run the proof

Use a local Docker context. All Python and uv invocations below run inside containers. The first command explicitly prepares the pinned tools, including the released wheelhouse tool 0.7.7. Subsequent container runs do not pull images. The proof has no external network access: both its fixture publisher and cache view run on loopback inside the container.

```bash
CONTEXT=science &&
docker --context "$CONTEXT" build --platform linux/amd64 -t wheel-demo:tools . &&
docker --context "$CONTEXT" run --rm --pull=never --platform linux/amd64 \
    --network none -v "$PWD:/work:ro" wheel-demo:tools \
    python -B prove.py /wheelhouse.pyz
```

The proof counts publisher wheel transfers separately from metadata requests. It exercises a complete mixed pool (0 transfers), a partially populated pool (1 transfer), incompatible ABI/backend/architecture (1 transfer of the correct wheel), an explicit package source, multiple eligible cached versions, and resolver backtracking to a remote version using metadata only. It checks the lock's artifact identities, acquires into a separate empty pool with the released SDK, repeats acquisition with no transfers, checks that only the missing selected wheel is acquired after backtracking, rejects an insecure publisher redirect, rejects missing and corrupt locked files offline, and installs with the publisher stopped. The synthetic artifacts are disposable. `--no-cache` gives every uv invocation a fresh tool cache.

`proof.log` records a **partial resolver-only run**, not the Docker/SDK proof. The explicit `--resolver-only` option is for adapter development and skips released-tool acquisition and negative offline checks. Use the command above for the actual trial.

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

The last command is an offline installation proof rather than a production image build. Commit only the authored files and generated publisher-URL `pylock.toml` if you want to keep the example. Never commit a projected local lock or the pool. To install elsewhere on the same target, reuse the committed lock and run acquisition, not lock generation.

After successful acquisition, repeat the lock-generation command with `pylock.warm.toml` as the output. Expect `Lock-generation wheel downloads: 0` with a complete eligible pool. Use the original `pylock.toml` for installation; the warm resolve is a trial observation, not a second authoritative lock.

## Why an adapter is needed

An arbitrary flat wheel pool does not contain publisher URLs. Also, simply adding `--find-links` to an online resolve is not a contractual cache-first policy. `lock.py` presents two ordered Simple API views of **the same declared source per package**. The first contains only hash-matching, target-compatible wheels present at the beginning of the run. The second contains all hashed wheel links. Standard `packaging` checks tags; uv chooses dependency versions and solves the graph. The `unsafe-first-match` strategy is scoped to two views of the same source, not competing publishers. Published PEP 658/714 metadata sidecars are forwarded separately and checked against the advertised hash when present. Every wheel GET is served from the pool when possible. On a miss, the adapter retrieves and verifies the publisher bytes and publishes them without overwriting another file. Finally it replaces the loopback aliases in uv's native lock with the original publisher URLs and literal wheel names. No guessing URLs from filenames, no second resolution, no embedded requirements-text lock. The existing SDK handles ordinary acquisition and the local lock projection.

## Scope and validation

This wheel-only proof of concept targets Linux x86-64 CPython 3.13. The fixture contains no native machine code; its wheel tags exercise real resolver compatibility. The adapter expects ordinary named requirements and declared HTTPS publisher indexes with hashed wheel links (HTML Simple API); loopback HTTP is allowed for the fixture. It checks redirect targets and reports publisher or artifact failures at the lock command boundary. It is not a general network sandbox, and the released SDK's own transport policy is unchanged.

The cache-first policy is a per-package preference, not a global minimum-download optimizer. Index and sidecar metadata may be requested even on a complete hit. Where sidecars are absent, uv may require an entire wheel to inspect its metadata: a rejected candidate may therefore be downloaded. The adapter's fallback acquisition code is temporary and duplicates some released-tool responsibilities; longer-term ownership is intentionally undecided. There is no support for sdists, direct URL requirements, private indexes, concurrent pool writers, or general proxy policy.

The resolver-only proof passed with uv 0.10.0, Python 3.13.5, packaging 25.0 and the existing environment's vendored TOML writer. No dependencies were installed. The pinned Docker image, released SDK integration and actual Torch/NumPy run remain unqualified until the trial commands above are executed.
