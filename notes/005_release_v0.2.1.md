# Wheelhouse v0.2.1

Metadata-only locking no longer creates a waiting Python thread for every resolver request. The metadata adapter now uses one server thread, preserving the serialized publisher access and metadata evidence updates while avoiding thread exhaustion and the secondary shutdown error it could cause.

Wheelhouse sets uv's concurrent downloads to four and native installation workers to two after removing ambient uv configuration. Consumers retain their existing process limits. The offline unit-test lane now runs with a 64-task limit, including a 97-package metadata-only regression that verifies bounded Python thread use and zero wheel downloads.

Qualification includes the artifact suite with uv 0.10.0 and 0.13.6. A real MolDeTr CUDA dependency declaration resolved 50 packages with zero wheel downloads and a measured peak of 13 tasks under its unchanged 64-task limit. This is dependency-lock qualification; CUDA image builds and GPU execution are outside this release's evidence.
