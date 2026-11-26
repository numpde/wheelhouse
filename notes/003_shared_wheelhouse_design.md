# Shared Wheelhouse design and implementation

Status: ChefNMR2 is migrated and qualified as the first consumer of the published standalone tool. SDK adoption is deferred for reconsideration; other consumer work remains paused. Wheelhouse v0.2.0 and Chef’s prepared ANTLR prerequisite are published. Local integration and qualification are recorded below.

## Decision

Wheelhouse owns wheel resolution, acquisition, verification and installation projections. Consumers own dependency intent, target environments, source builds, container lifecycle and release composition. Repositories may change their inputs and workflows; the old SDK command names and lock formats do not define the new interface.

Keep `lock`, `acquire` and `check`. Keep metadata-only locking and suitable cached-wheel preference as explicit product commitments. Use one complete generated wheel lock per installation environment or role. Remove permanent requirements-lock/projection/embedded-resolution/select chains as consumers migrate. Review changes to existing transitive and runtime pins explicitly. SDK installation locks remain release-qualification evidence; SDK dependency metadata and consumer declarations govern future resolution without a second hidden constraints authority.

## Wheel sources and preparation

Package declarations may bind an index, an exact HTML listing, an exact HTTPS wheel with SHA-256, or an exact local wheel file with SHA-256. Preparation and resolution read the same declaration.

`acquire --lock LOCK` follows the locked selection. `acquire --project PROJECT --package NAME` prepares an already exact wheel declaration without resolving dependencies. Index-only and unpinned declarations are refused by this mode. Both modes use the same streaming verification and publication implementation. Exact remote preparation verifies the wheel hash, identity and embedded METADATA; it does not require a sidecar. A later lock reads that metadata from the verified cached wheel. Metadata-only lock never silently downloads wheel bytes.

A package's explicit source is exclusive. Missing or corrupted private candidate bytes fail even when a valid released wheel of the same name and version exists in the pool. Conflicting bindings fail. No default-index fallback substitutes another source.

Local candidates remain in private build directories and are verified in place. They are not forcibly admitted into the shared flat pool. This permits different SDK candidates with the same version and filename without overwriting released bytes. Candidate locks are ephemeral and may contain absolute local paths. Published runtime locks use retrievable remote origins.

`lock --offline` explicitly selects supplied local wheel bindings and the chosen pool as the available local source universe. It reads actual wheel metadata and makes no publisher requests. Explicit bindings exclude pool alternatives for that package. Otherwise unbound dependencies may use pool candidates; output records local origin rather than invented remote provenance. Missing dependencies fail with their requirements and selected locations. Offline operation is never an automatic fallback from failed networking.

## Source-built prerequisites

Runtime resolution remains wheel-only. Source compilation happens first in consumer-owned preparation lanes with pinned source bytes, complete wheel-only build dependency locks, and pinned base/toolchain inputs. Builds run without networking. Their outputs supply real wheel metadata; no handwritten Requires-Dist substitutions.

`numpde/chefnmr2-repro` owns ANTLR preparation and a separately versioned immutable dependency release. SECS consumes the same wheel and does not copy the recipe. `numpde/nmrnet-repro` owns Uni-Core preparation. SECS owns preparation of its own project wheels. Shared publications retain the wheel, its metadata at `wheelURL.metadata`, and source/build-input/output receipts. These publications are planned, not yet verified to exist.

Retain published bytes instead of assuming identical rebuilds. Changed outputs require distinct valid wheel build tags or versions. Ordinary SDK or scientific dependency updates do not rebuild unchanged prepared prerequisites. Preparation starts from available pinned base/toolchain and wheel-only dependencies; missing prerequisites fail explicitly. Source archives use existing consumer source acquisition machinery. Wheelhouse does not execute source backends or become a generic file downloader.

## Offline installation and pool behavior

`check` verifies a complete selection offline and can atomically emit pip/uv-compatible requirements containing absolute wheel paths and SHA-256 hashes. Installation uses `--no-index --no-deps --require-hashes`, verifying bytes again at use. Existing local Pylock output may remain for current users; both formats derive from one selection.

Structured check output exposes exact package, filename, digest and source path. This is disposable derived evidence, not another maintained lock. A consumer-supplied destination map keyed by exact source identity and digest can map both private candidates and pooled wheels to final container paths. Wheelhouse alone renders mapped installation input and rejects incomplete, extra or conflicting mappings. Consumers own layout; they do not filter package subsets after resolution. Use separate complete role/environment locks instead.

Capture lock, target and destination map once per operation. Publish installation output only after all verification succeeds. Complete pools work read-only. Acquisition never re-resolves. Corrupt or conflicting existing bytes remain untouched and cause failure. An interrupted operation retains previously verified additions and publishes no partial wheel. Process large wheels with bounded memory.

## Shared build-context assembly

The existing SDK acquisition bootstrap gains one standard-library-only context component. It consumes caller entries `{path, destination, sha256, executable}` and knows no Python package or lock semantics. Source entries come from consumer-owned captured source; artifact entries and installation files derive from the same checked selection and destination map.

SDK delivery, NMRNet, NMRPeak and SECS use this shared component. ChefNMR2 may retain Bake. Reuse applicable file-admission/streaming behavior from the deleted tool without restoring package-selection code. Preserve safe regular-file opening, canonical relative destinations, duplicate and parent-path collision rejection, modes, bounded streaming and hashes of bytes actually emitted. Do not require full-pool transfer or copying all large wheels.

The build owner reports success only when both exporter and builder complete successfully. Late corruption, truncated export, broken pipe or builder failure prevents success receipts and image publication, even if temporary image bytes were produced. The context component streams already rendered installation files; it does not render pip input.

## Independent distribution and SDK migration

Each consumer pins Wheelhouse independently by release URL and digest. The self-contained bundle supports the actual target interpreter fleet. A minimal standard-library fetch and hash check can acquire it before installing the SDK. Wheelhouse has no SDK dependency.

Remove SDK ownership of the deleted tool's build imports, mounts, source inventories, build dependency lock, release asset, implementation tests, image ownership and publication qualification. SDK tests qualify integration with the released external tool. The acquisition archive records its independent tool dependency. Existing released bootstrap code can prepare the new SDK/owner, avoiding a cycle. No implicit fallback to the old tool.

## Consumer acceptance

- SDK: cold bootstrap through wheel build, release qualification and delivery build; independent SDK and tool upgrades; repeated same-name/version private candidates, including changed candidate dependencies.
- ChefNMR2: provider, CPU worker, CUDA worker and sanitizer; empty-pool ANTLR acquisition; reviewed baseline pins; all four offline image builds.
- NMRNet: Uni-Core preparation followed by runtime wheel closure and selected context export.
- NMRPeak: explicit local SDK candidates, native/scientific/API and CUDA benchmark roles; no package-specific URL rewriting.
- SECS: prepared local project and shared ANTLR wheels, CPU/CUDA environments and selected context export.
- MolDetr: declared Python 3.13 CPU/provider and 3.12 CUDA intent; its incomplete checkout is not evidence of working consumer qualification.

Cross-cutting proofs cover Torch publisher metadata, cold exact-wheel preparation, CPU/CUDA pool coexistence, wrong target, conflicting bytes, missing metadata, sidecar/embedded metadata disagreement, no source fallback, interrupted acquisition, read-only warm pool, atomic projection, and mutation during streaming. Export a private candidate outside the pool together with pooled dependencies into one role and install offline without leaking host paths.

## Review record

Five independent GPT-6.1 Sol reviewers at high reasoning represented Wheelhouse, SDK, ChefNMR2, other providers and an independent architecture critic. Criteria were consumer coverage, ownership/bootstrap, upgrade/cold-start behavior, offline/failure behavior and minimum maintained mechanisms.

D1 ratings: Wheelhouse complexity 7/10 and ownership 8/10; SDK ownership/failure 7/10; Chef coverage 7/10 and upgrades 6/10; other providers coverage 7/10 and failure 8/10; critic ownership/complexity 8/10. Findings required an exact-wheel cold preparation path, explicit candidate handling, a concrete pip output contract and shared context ownership.

D2 resolved those structural gaps. Remaining findings named ANTLR's producer, eliminated duplicate preparation pins, required exclusive candidate bindings and covered two-origin path mappings. D3 resolved these; Wheelhouse's reviewer identified one remaining duplicate installer renderer. D4 assigned rendering solely to Wheelhouse and byte transfer solely to context assembly. All final assigned criteria received 10/10. Scores assess the design only and grant no implementation or qualification credit.

## Implementation ledger

Work is supervised in bounded patches with independent audits and local commits. Update this ledger with integrated slices, tests and remaining consumer obligations. No completed slice alone establishes the cross-repository migration.

- Wheelhouse core is integrated in local commit `eb6d39f`; documentation and the v0.2.0 release note are in `6c94bd2`. Exact preparation, exclusive private sources, offline resolution and mapped hashed requirements passed 35 unit tests and 143 repository checks, plus real acquisition/reuse and offline installation proofs. The self-contained bundle is published as v0.2.0, with SHA-256 `b9730fd1dd9668e98f55a952e64328f193b5d2ca6d203fc1c150332fc672e90a`. The maintained release lane passed repository, unit and online/offline integration checks and verified uploaded bytes before publication.
- SDK context assembly is integrated in `1a6f989e`, with twelve focused streaming and failure tests. SDK acquisition/release/delivery migration is integrated in `63366af8`. The maintained release suite, acquisition refresh, delivery and real offline checks passed. Full `publish/prepare TAG=v0.7.9` completed on the science engine with `PRIVACY_TESTS=0`; its nine artifacts and build/test logs are retained in `../nmr-provider-tools/release-artifacts/v0.7.9-wheelhouse/`. This is a locally qualified candidate, not a published release. The broad SDK quality suite still has pre-existing failures. Review legacy SDK installation constraints against the agreed single-authority end state before future dependency refreshes.
- ChefNMR2 preparation is integrated in `2ab8a1d`; cleanup is in `88a0ee1`. Artifact acquisition uses the existing fetch owner. ANTLR preparation retains one verified result per source, backend, recipe, base-image and tool identity; seven focused tests cover reuse, changed tool, corruption and failed/interrupted scratch retirement. A real offline build and repeat succeeded before the identity-only audit correction. Runtime migration is committed and pushed as `bd40471`. The prepared ANTLR release `dependencies-antlr4-4.9.3-1` is public; its assets were downloaded and byte-compared before publication. The maintained exact-preparation and lock lanes regenerated all four role locks with declaration fingerprints. Their complete 17/33/51/11-package selections preserve every previous provider/CPU/CUDA/sanitizer package version and qualified wheel hash, with HTTPS origins and no private paths. Thirty-three operator tests pass, including stale declaration and source-pin rejection. All four maintained acquisition and offline image builds passed on the science engine. The 18 provider and 9 CPU binding/checkpoint tests also passed, for 60 Chef tests total. CUDA imports of Torch, OmegaConf, ANTLR, RDKit and NumPy passed without GPU grants. No deployment or new scientific inference run was performed. Temporary integration proposals are retired after this record; the published dependency release retains preparation bytes and receipts.
- NMRNet preparation is integrated in `be9c95a`; cleanup is in `88bb096`. The host owner now retains one input set and retires operation scratch and exact workers, including handled SIGTERM. Uncertain retirement preserves recovery inputs and both failure causes. Thirty-five preparation and image tests passed as nonroot, and a real offline build left only the intended artifact root at `/tmp/nmrnet-migration/cleanup-result`; its log is `cleanup-success.log`. Runtime integration remains unfinished. Keep `/tmp/nmrnet-migration` prepared wheels and qualification receipts: earlier runtime and preparation proofs refer to different retained wheel bytes.
- NMRPeak remains a provider-only proposal, `/tmp/nmrpeak-migration/provider.patch`, which applies cleanly to the unchanged checkout. Eighteen tool tests, an offline provider image and a mixed private-SDK/pool installation passed. Those proofs used an explicitly assembled context; the maintained entrypoint still needs the authenticated SDK acquisition snapshot, private-candidate image integration and a role-lock refresh lane. Native/scientific/CUDA roles are not migrated. Keep the patch, private wheel/contract and proof evidence until integration or abandonment.
- SECS remains unintegrated. `/tmp/secs-wheelhouse-proposal` is an incomplete, untested consolidation into existing owners; do not apply it wholesale. Earlier provider/CPU installation and style proofs apply to the preceding prototype. `/tmp/secs-wheelhouse-proof` retains prepared wheels, candidate contracts and receipts. CUDA installation, caller completion and tool/bootstrap pins remain open. MolDetr has design coverage only.

## Cleanup and pause

Retired settled patch workspaces, formatter copies, duplicate SDK candidate artifacts, private qualification pools and task-owned proof images/cache records after their users finished. Moved the fully qualified SDK output into its repository’s existing release-artifacts owner. Removed generated Wheelhouse bytecode and Ruff caches. Preserved the shared wheel pool, reusable toolchains, user configuration, unpublished release outputs and pending consumer proposals; no host-wide prune was used.

Temporary consumer proposals and their associated evidence are recovery inputs for resuming this task, not permanent project documentation. Remove them after integration or an explicit decision to abandon them. Keep prepared dependency bytes and build receipts through their immutable publication. Keep the SDK release candidate and Wheelhouse bundle through release review/publication. No release, push or deployment was performed during the cleanup phase. On resumption, Wheelhouse v0.2.0 and Chef’s `dependencies-antlr4-4.9.3-1` were published; their uploaded bytes were verified before publication. Deployment remains outside this migration.

## ChefNMR2 qualification

Source: `numpde/chefnmr2-repro` commit `bd40471`. Commands used the explicit science Docker context, `/home/ra/Downloads/wheelhouse`, and `PROXY_URL=none` for online lanes. `make wheelhouse/acquire` downloaded the published bundle into the initially missing cache entry. For each of `provider`, `worker-cpu`, `worker-cuda`, and `weights-sanitize`, `make prepare`, `make lock`, `make acquire`, and `make build` succeeded; builds installed hashed selections with networking disabled and passed pip dependency checks. `make test/operator`, `make test/provider DOCKER_CONTEXT=science`, and `make test/unit DOCKER_CONTEXT=science` passed.

| Role | Final local image ID |
| --- | --- |
| Provider | `sha256:367eec8b9516706fc772a14b1458a7ea736423d138f6a8827a80aec8fb354e81` |
| CPU worker | `sha256:512192899660a35af2032347ad70bf06c5c3870af36288afcf9e08b4791b844c` |
| CUDA worker | `sha256:3c0a52219ac270f643c873351caea953aa330dae439a05a4944395686f2ea2d7` |
| Weights sanitizer | `sha256:a98d8b157508d0cb896c2f43f4ac1f53696e1d14dfa100a3f6f490d728e9aac9` |

Published inputs are [Wheelhouse v0.2.0](https://github.com/numpde/wheelhouse/releases/tag/v0.2.0) and [prepared ANTLR 4.9.3](https://github.com/numpde/chefnmr2-repro/releases/tag/dependencies-antlr4-4.9.3-1). Chef continues to use SDK 0.7.7; no SDK release was required. Local images are retained as intended build outputs; shared pool inputs and BuildKit caches remain under their normal owners.
