# Website Product And Download Copy

Use this as the ready-to-paste content handoff for the Studio Seventeen website.
Replace only the bracketed URLs after the versioned assets exist.

## Product Hero

### Generator Fleet Simulator Community Edition

Run realistic standby-generator fleet demonstrations before hardware is in the
room. Configure and operate up to 2,000 simulated generators through a live
dashboard, one-line SCADA view, scenarios, reusable runbooks, REST APIs, and
native Modbus TCP for all 2,000 generators across up to eight consecutive ports.

**Free download. MIT-licensed project code.**

Primary CTA: **Download Community Edition 1.1.0-rc.4**

CTA target: `[VERSIONED_ZIP_URL]`

Secondary links:

- **Release notes:** `[RELEASE_NOTES_URL]`
- **SHA-256 checksum:** `[CHECKSUM_URL]`
- **Release receipt:** `[RELEASE_RECEIPT_URL]`
- **SPDX SBOM:** `[SBOM_URL]`
- **Signed asset manifest:** `[ASSET_MANIFEST_URL]`
- **Sigstore verification bundle:** `[ASSET_SIGNATURE_BUNDLE_URL]`
- **Send feedback / get community support:** `[FEEDBACK_URL]`
- **Report a security issue privately:** `[SECURITY_CONTACT_URL]`

## What You Download

The download is a versioned launcher bundle for macOS, Windows, and Linux. It
contains guided launchers, Docker Compose configuration, the MIT
License, security guidance, quick-start documentation, version information,
and a release receipt. The launcher ZIP does not contain application source. Link this repository
for MIT-licensed project code and third-party notices.

The launcher automatically pulls the exact public multi-architecture Docker
image pinned to this release. A Docker Hub account or sign-in is not required.
The signed asset manifest authenticates the ZIP, checksum, receipt, and SBOM;
publish the identity-constrained `cosign verify-blob` command beside the links.

## System Requirements

- Docker Desktop or Docker Engine 24+ with Docker Compose v2
- A 64-bit `amd64` or `arm64` machine supported by Docker
- Internet access for the first image pull
- Available local ports `5001` and `5021–5028`, or equivalent alternate mappings
- A current desktop browser

## Five-Minute Start

1. Download the ZIP and matching SHA-256 checksum.
2. Verify the checksum, then unzip the package.
3. Run `./start.sh` on macOS/Linux or `.\start.ps1` in Windows PowerShell.
4. Read the MIT License and acknowledge the security guidance.
5. Keep local-only networking unless an isolated trusted lab requires LAN
   access.
6. Choose a fleet mix and open `http://localhost:5001`.

## Security Warning

The dashboard, REST APIs, Socket.IO commands, and Modbus TCP do not include
application authentication. The supported boundary is the default local-only
binding or an explicitly isolated trusted LAN protected by customer-managed
firewall/VPN controls. Never expose the simulator or Modbus TCP to the public
internet.

## Privacy

Community Edition runs locally in Docker. Generator state, sub-fleets, and saved
runbooks stay in the local Docker volume. The application does not include
telemetry, analytics, user accounts, or a Studio Seventeen-hosted backend.
Docker contacts Docker Hub to download the public image; Docker and the user's
network/host tooling may retain their own operational logs.

## Support Boundary

Community Edition is provided without an included support SLA. Use the feedback
link for reproducible issues and include the release receipt, version, operating
system, Docker version, and relevant container logs. Use the private security
contact for vulnerabilities.

## Legal Review Notice

The final website must not add promises about permitted educational/commercial
use, perpetual free use, future pricing, jurisdiction, warranties, or support
until the decisions in `docs/legal-review-checklist.md` are approved and the
binding license is updated if necessary.
