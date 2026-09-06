# Release readiness

This is an MIT source preview for local or isolated-LAN simulation.
The application has no user authentication. Keep its ports off the public internet.

CI checks locked dependencies, installed assets, tests, image vulnerabilities,
and a 2,000-generator runtime smoke. The tagged release workflow adds multi-platform
image checks, signed images, a signed download manifest, and release receipts.
Consult the actual workflow result for each source revision; this document does
not certify that a future build will pass.

Native Windows Docker Desktop and the full clean-machine launcher matrix remain
pending. Website download integration depends on a successful public release.
See releases/v1.1.0-rc.4-acceptance.md and SECURITY.md.
