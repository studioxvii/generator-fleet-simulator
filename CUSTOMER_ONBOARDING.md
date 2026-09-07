# Community Edition Quick Start

The MIT-licensed Community Edition package starts from a small terminal
launcher instead of a bare Docker command. The self-service journey is:
download, verify, unzip, run the launcher, read the MIT License and acknowledge the security
guidance, pull the public image, configure the fleet, and open the dashboard.

For website-ready customer instructions, see
[WEBSITE_STARTUP_INSTRUCTIONS.md](WEBSITE_STARTUP_INSTRUCTIONS.md).

## Verify The Delivery

Each delivery includes the launcher ZIP, its `.zip.sha256` checksum, an SPDX
JSON SBOM, and a release receipt. Verify the ZIP before extracting it, then
confirm the receipt names the expected version and immutable Docker image
digest. Use `INSTALL.md` for macOS, Linux, and Windows verification commands.

If the checksum, receipt version, or expected image digest does not match, stop
and contact Studio Seventeen before starting Docker.

## First Run

Docker must be accessible to your account: run `docker info` in the same terminal
and resolve any errors first. On macOS/Linux, the launcher also requires Bash,
Python 3 (`python3`), and `curl`. Windows uses PowerShell and does not require
host Python or curl. See `INSTALL.md` for prerequisites and troubleshooting.

### macOS or Linux

```bash
./start.sh
```

### Windows PowerShell

```powershell
.\start.ps1
```

## Launcher Flow

1. Start guided setup.
2. Install or start Docker if needed.
3. Read the MIT License.
4. Read and acknowledge the security notes.
5. Choose network exposure:
   - Local-only: dashboards and Modbus TCP bind to `127.0.0.1`.
   - Trusted LAN integration mode: dashboards and Modbus TCP bind to all host interfaces on an isolated engineering or test network.
6. Choose the fleet mix:
   - Default: 15 x 500 kW generators.
   - Custom: enter counts for each supported generator size.
   - Configure later: use the browser startup overlay.
7. Start Generator Fleet Simulator.
8. If a terminal fleet mix was selected, configure the simulator through the startup API.
9. Wait for readiness.
10. Open the printed dashboard URL.
11. Connect the SCADA, HMI, EMS, or Modbus client to the printed Modbus endpoint.

After readiness, the simulator runtime and Modbus server are running. Individual
generator units are ready/stopped until the operator issues a start command,
scenario, runbook, or Modbus command.

## Default Access

The launcher defaults to local-only host ports:

| Service | Endpoint |
|---|---|
| Dashboard | `http://localhost:5001` |
| Modbus TCP | `localhost:5021–5028` |

Local-only mode is the safest default for first launch. Choose trusted LAN
integration mode only when another device on an isolated engineering or test
network needs to connect.

## Public Docker Image

The package pulls `studioxvii/generator-fleet-sim:<version>` from the
public Studio Seventeen Docker Hub repository. The exact version is pinned by
`VERSION`; Docker Hub sign-in is not required.

The release receipt identifies the exact digest behind that version. Support
uses the same version, Git commit, image digest, and dependency-lock hash when
diagnosing or approving an upgrade.

If setup reports `pull access denied`:

1. Confirm internet access and that Docker is running.
2. Return to setup and choose `retry`.
3. If the exact tag still fails, report the version and pull error to Studio
   Seventeen. The public image or tag was not published correctly.

The launcher may offer `use` when a cached image is already present. That path
is useful during a temporary outage, but use it only for a previously verified
cached copy of the exact pinned release.

## First Dashboard Checks

After launch, open `http://localhost:5001` and confirm:

- The fleet count matches the selected startup mix.
- `/api/ready` reports `ready: true`.
- The `One-Line SCADA` tab opens and shows the fleet hierarchy.
- `Fleet Status`, `Sub-Fleets`, `Scenario Timelines`, and `Modbus Registers`
  are visible.
- Generator units can be started or transferred from the dashboard or SCADA
  controls.

## What The Launcher Records

The launcher writes small local acknowledgement files under `.studioseventeen/`:

- `security-reviewed`
- `network-mode`

These files are local convenience markers. They are not DRM and they do not
replace the license terms.

## Privacy And Support

Community Edition runs locally. Generator state, sub-fleets, and saved runbooks
remain in the local Docker volume. The application has no telemetry, analytics,
user accounts, or Studio Seventeen-hosted backend. Docker contacts Docker Hub
to retrieve the image.

No support SLA is included with the free edition. Use the feedback/support link
on the download page for reproducible issues and include the release receipt,
platform, version, and relevant logs. Use the private vulnerability-reporting
process in `SECURITY.md` for security issues.

## Direct Docker Usage

Direct Docker and Docker Compose usage is still supported for automation,
CI-style test rigs, and support sessions. When bypassing the launcher, the
operator is responsible for reviewing the license and security notes first.

Example:

```bash
SIM_HOST_BIND=127.0.0.1 docker compose up -d generator
```

Use `SIM_HOST_BIND=0.0.0.0` only when you intentionally want to expose the
published ports on a trusted, customer-controlled host network.

Modbus uses 255 generators per port. The Community Edition launcher publishes
ports 5021–5028. Generator 256 uses port 5022 and wire unit ID 1.
See `MODBUS_REFERENCE.md` for the complete mapping and custom port rules.
