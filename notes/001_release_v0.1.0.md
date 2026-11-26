# Wheelhouse v0.1.0

Wheelhouse creates target-specific dependency locks, prefers eligible wheels already in a shared folder, and acquires only missing locked files. Lock creation uses separately published metadata or verified local wheels without downloading wheel contents.

- Distribute the tool as `wheelhouse.pyz`, with its Python dependencies included and a `SHA256SUMS` file for verification.
- Verify both wheel bytes and embedded dependency metadata before accepting downloaded or cached files.
- Produce a local lock for offline installation with uv.
- Run the copied formatting and release-note checks alongside bundle, artifact, online acquisition, and offline installation tests.

The qualified target is Linux CPython 3.13 with the uv version pinned in Dockerfile. The bundle does not contain Python or uv. Other targets and Torch/CUDA remain unqualified; source builds, dynamic dependencies, and direct dependency URLs are unsupported.
