# Repository check port

Donor: https://github.com/numpde/nmr-provider-tools/tree/5f6b121a4b121fc8d42657c8e5b549f706ddca3b/tests/repository/quality

The formatting, expression, diagnostic-field, assertion and release-note checks retain
their detector tests and file fixtures. Local adaptations remove the donor's
container guard (Make owns execution here), relocate fixture/report imports,
and replace its source inventory. Detector files are maintained ported code;
source scans cover this repository's application, build tools and product
tests, rather than treating detector examples as application code.

Run `make tests/repo-checks DOCKER_CONTEXT=<local-context>`; no sibling checkout
is needed. Paths are resolved from this checkout, including fixture locations.
The local inventory and diagnostic helpers are checked along with authored code.

Release notes use `notes/NNN_release_vX.Y.Z.md`. Each paragraph or list item
stays on one source line; fenced code may span lines. README prose remains
wrapped for source readability.
