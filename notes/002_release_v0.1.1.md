# Wheelhouse v0.1.1

Package sources can now point directly to an HTTPS HTML listing, including a raw GitHub file pinned to a commit. Declare the URL as `page` in `[tool.wheelhouse.sources.PACKAGE]`; string source values continue to select index roots.

Explicit pages use the existing wheel and metadata hash checks, cached-wheel preference, and metadata-only locking. The lock records the configured page URL as the package source. No wheel bytes are fetched during resolution.

The first live raw-page qualification uses the published provider-core 0.7.8 SDK: its API extra resolves to 17 packages from a commit-pinned listing and PyPI, locking downloads no wheels, repeated acquisition reuses all files, and installation plus imports succeeds with networking disabled on Linux CPython 3.13.18 and uv 0.10.0. The README includes that listing and the new bundle checksum.
