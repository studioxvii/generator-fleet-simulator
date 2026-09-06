# Installation Guide — Generator Fleet Simulator Community Edition

This guide covers two deployment modes:

- **Community Edition launcher** — guided startup for the MIT-licensed bundle
- **Direct Docker mode** — run Generator Fleet Simulator manually with Docker

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) 24+ (includes Compose v2)
- Internet access for the first anonymous pull from public Docker Hub
- License: MIT. No startup acceptance variable is required.

The Studio Seventeen delivery includes a launcher ZIP, a `.zip.sha256` file,
an SPDX JSON SBOM, and a release receipt. Keep these files together for support
and upgrade verification.

### Verify the download before unzipping

macOS:

```bash
shasum -a 256 -c generator-fleet-simulator-community-edition-1.1.0-rc.4.zip.sha256
```

Linux:

```bash
sha256sum -c generator-fleet-simulator-community-edition-1.1.0-rc.4.zip.sha256
```

Windows PowerShell:

```powershell
Get-FileHash .\generator-fleet-simulator-community-edition-1.1.0-rc.4.zip -Algorithm SHA256
Get-Content .\generator-fleet-simulator-community-edition-1.1.0-rc.4.zip.sha256
```

The computed hash must match the first value in the checksum file. Stop and
contact Studio Seventeen if it does not.

---

## Community Edition Launcher (recommended)

Use the launcher for the normal self-service journey:

```bash
./start.sh
```

On Windows PowerShell:

```powershell
.\start.ps1
```

The launcher:

1. Verifies `RELEASE_RECEIPT.json` against `VERSION` and the Compose image pin
   when the receipt is present in the extracted bundle.
2. Checks whether Docker is installed and running.
3. Provides Docker installation guidance if Docker is missing.
4. Displays the MIT License; no license acceptance is required.
5. Requires the security notes to be opened and acknowledged.
6. Defaults dashboard and Modbus host ports to `127.0.0.1`.
7. Offers an explicit opt-in for trusted LAN integration mode.
8. Asks whether to start the default fleet, enter custom counts, or configure later.
9. Starts Generator Fleet Simulator.
10. Configures the selected fleet from the terminal and waits for readiness.
11. Prints the dashboard URL and Modbus TCP endpoint.

Readiness means the web process is live, the selected fleet mix is configured,
and the Modbus server is running. Generator units start in a ready/stopped
state until commanded from the dashboard, SCADA tab, Modbus, scenario, or
runbook.

---

## Compose Mode

Runs Generator Fleet Simulator with Docker Compose.

| Service | Port |
|---|---|
| Dashboard | `:5001` |
| Modbus TCP | `:5021–5028` |

### 1. Use the Compose file from the verified package

Do not download `docker-compose.yml` from `main`. Community Edition packages contain the
Compose file that is pinned to the reviewed release version.

### 2. Start the simulator

```bash
SIM_HOST_BIND=127.0.0.1 docker compose up -d generator
```

### 3. Verify the simulator is running

```bash
curl http://localhost:5001/api/live   # Web process is accepting requests
```

The simulator should return JSON with `"ok": true`. If the launcher started a
fleet mix from the terminal, `curl http://localhost:5001/api/ready` should also
return HTTP 200. If you chose browser configuration, `/api/ready` returns 503
until the browser startup overlay is completed.

### Stopping the simulator

```bash
docker compose stop generator
```

State is persisted in the named Docker volume `generator-data`. To reset state,
run `docker compose down -v`.

---

## Direct Docker Mode

### Generator Fleet Simulator

```bash
IMAGE='studioxvii/generator-fleet-sim@sha256:<digest-from-RELEASE_RECEIPT.json>'
docker pull "$IMAGE"

docker run -d \
  --name generator \
  -p 127.0.0.1:5000:5000 \
  -p 127.0.0.1:5020-5027:5020-5027 \
  -v generator-data:/data \
  "$IMAGE"
```

Dashboard: http://localhost:5000
Modbus TCP: `localhost:5020–5027`

## Updating

Pull the pinned public image for the Community Edition package and restart:

```bash
# Compose mode
SIM_HOST_BIND=127.0.0.1 docker compose pull
SIM_HOST_BIND=127.0.0.1 docker compose up -d generator

# Individual mode
IMAGE='studioxvii/generator-fleet-sim@sha256:<digest-from-RELEASE_RECEIPT.json>'
docker pull "$IMAGE"
docker stop <container-name> && docker rm <container-name>
# then re-run the docker run command above
```

To upgrade intentionally, download and verify the next Community Edition package.
Its bundled Compose file pins the new manifest digest. Confirm that digest against
the package's release receipt before restarting.

---

## Troubleshooting

**Container exits immediately**
Read `docker logs generator-sim` and check the configured ports and volume permissions.

**Port already in use**
Change the host-side port mapping. For example, `-p 6001:5000` exposes the REST API on port 6001 instead of 5000.

**Modbus client cannot connect**
Confirm the container is running (`docker ps`) and that no firewall is blocking the Modbus port. The Community Edition launcher binds host ports to `127.0.0.1` by default, so clients on other machines cannot connect unless trusted LAN integration mode is enabled.

**Image pull is denied**
The release image is intended to be publicly and anonymously pullable. Confirm
internet access and Docker status, then retry. If the exact pinned digest still
returns `pull access denied`, report the version and error to Studio Seventeen;
this indicates an image publication or Docker Hub configuration failure. Choose
`use` only for a previously verified cached copy of that exact release.

**Dashboard opens but fleet is not configured**
If you chose `Configure later`, complete the browser startup overlay. If you
selected default or custom counts, check `/api/ready` and container logs.

**Reset simulator state**
Remove the named volume: `docker volume rm <volume-name>`. The simulator recreates default state on next startup.

Modbus uses 255 generators per port. The Community Edition launcher publishes
ports 5021–5028. Generator 256 uses port 5022 and wire unit ID 1.
See `MODBUS_REFERENCE.md` for the complete mapping and custom port rules.
