# Wheelhouse v0.1.1

Package sources can now point directly to an HTTPS HTML listing, including a raw GitHub file pinned to a commit. Declare the URL as `page` in `[tool.wheelhouse.sources.PACKAGE]`; string source values continue to select index roots.

Explicit pages use the existing wheel and metadata hash checks, cached-wheel preference, and metadata-only locking. The lock records the configured page URL as the package source. No wheel bytes are fetched during resolution.
