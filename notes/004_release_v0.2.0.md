# Wheelhouse v0.2.0

Exact package sources can bind an HTTPS wheel or a private local wheel with SHA-256. `acquire --project PROJECT --package NAME` prepares the exact declaration without resolving dependencies or requiring a publisher metadata sidecar. Local candidates remain outside the shared pool and cannot be replaced by same-version released wheels.

`lock --offline` resolves from explicit local bindings and compatible wheels in the selected pool without contacting publishers. It records local provenance. Ordinary locking remains metadata-only. Wheels containing vendored distributions, including setuptools, use their single root metadata record.

`check --requirements OUTPUT` emits complete hashed file-URL requirements for offline pip or uv installation. Structured results include exact artifact paths and hashes. An optional complete destination map lets consumers render container installation paths while retaining the original source identity. Failed verification or invalid mappings leave existing output untouched. Lock output cannot replace its project or declared local wheels; installation output cannot replace selected wheels, its lock or its destination map.

The shared consumer design is recorded in `notes/003_shared_wheelhouse_design.md`. SDK and scientific consumer migrations are separate work; this release does not establish their full qualification.
