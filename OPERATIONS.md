# Operations Guide — Generator Fleet Simulator

This guide covers operating Generator Fleet Simulator in a production-like test
environment.

---

## Contents

1. [Prerequisites](#prerequisites)
2. [Community Edition Launcher](#community-edition-launcher)
3. [License](#license)
4. [Docker Deployment](#docker-deployment)
   - [Individual Product](#individual-product)
   - [Compose](#compose)
5. [Configuration Reference](#configuration-reference)
   - [Generator Fleet](#generator-fleet-configuration)
6. [Health Checks](#health-checks)
7. [Persistent State](#persistent-state)
8. [Modbus TCP Connectivity](#modbus-tcp-connectivity)
9. [Scenarios and Runbooks](#scenarios-and-runbooks)
10. [Data Export](#data-export)
11. [Logs](#logs)
12. [Release Integrity](#release-integrity)
13. [Upgrading](#upgrading)
14. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- Docker Engine 24+ or Docker Desktop 4.28+
- Docker Compose v2 (comes bundled with Docker Desktop)
- macOS/Linux launcher: Bash, Python 3 (`python3`), and `curl`; Windows: PowerShell
- Docker access for the current account (`docker info` must succeed)
- Internet access for the first anonymous pull from public Docker Hub
- Ports available on the host: see [Configuration Reference](#configuration-reference)

---

## Community Edition Launcher

For the MIT-licensed Community Edition bundle, use the launcher as the
normal entrypoint:

```bash
./start.sh
```

On Windows PowerShell:

```powershell
.\start.ps1
```

The launcher requires license display, security review, and an explicit
network exposure choice before starting containers. It defaults all published
dashboard and Modbus ports to `127.0.0.1`; trusted LAN integration mode is
reserved for isolated engineering or test networks. The launcher can also start
the selected fleet mix directly from the terminal and wait for simulator
readiness. Readiness means the runtime is configured and Modbus is live;
generator units remain ready/stopped until commanded.

The public release image must pull without Docker Hub authentication. If the
launcher cannot pull it, check connectivity and Docker status, then retry. A
persistent denial means the image visibility or tag publication is wrong. Use
the cached-image `use` path only when the exact pinned image was verified
previously.

---

## License

Project code uses the MIT License in `LICENSE`. Startup does not require license
acceptance or a license environment variable. The launcher displays the license
and requires a separate acknowledgement of the security notes.

---

## Docker Deployment

### Individual Product

Pull and run the simulator:

```bash
# Generator Fleet — REST :5000, Modbus :5020
IMAGE='studioxvii/generator-fleet-sim@sha256:<digest-from-RELEASE_RECEIPT.json>'
docker run -d \
  --name generator-sim \
  -p 127.0.0.1:5000:5000 \
  -p 127.0.0.1:5020-5027:5020-5027 \
  -v generator-data:/data \
  "$IMAGE"
```

Open the browser dashboard at `http://localhost:5000` after the container starts.

### Compose

Run Generator Fleet Simulator using Docker Compose:

| Product         | REST (Dashboard) | Modbus TCP |
|-----------------|:----------------:|:----------:|
| Generator Fleet | :5001            | :5021–5028 |

```bash
# Start the simulator
SIM_HOST_BIND=127.0.0.1 docker compose up -d generator

# Tail logs
docker compose logs -f generator

# Stop the simulator (data volume is preserved)
docker compose stop generator
```

The `docker-compose.yml` at the root of the Generator Fleet repository includes
the Generator Fleet service. The Community Edition launcher wraps this file and sets
`SIM_HOST_BIND` for local-only or trusted LAN integration mode. The downloadable
package's generated Compose file pins the exact multi-architecture manifest
digest recorded in `RELEASE_RECEIPT.json`.

---

## Configuration Reference

All configuration is done via environment variables. Bind and state-path
defaults differ by runtime layer:

- **Local Python / `main.py`:** `GENSIM_WEB_HOST` and `GENSIM_MODBUS_HOST`
  default to `127.0.0.1`. State files default to `generator_state.json` and
  `generator_runbooks.json` beside the process working directory.
- **Docker image:** the Dockerfile sets in-container binds to `0.0.0.0` and
  state paths to `/data/...` so Compose can publish ports and persist a volume.
- **Host publish:** `SIM_HOST_BIND` defaults to `127.0.0.1`. Trusted LAN
  integration is an explicit launcher confirmation, not a process default.

### Generator Fleet Configuration

| Variable                    | Default       | Description                               |
|-----------------------------|---------------|-------------------------------------------|
| `GENSIM_NUM_GENERATORS`     | `15`          | Initial generator count on startup        |
| `GENSIM_MAX_GENERATORS`     | `2000`        | Hard upper limit for the fleet            |
| `GENSIM_WEB_HOST`           | `127.0.0.1` local Python; `0.0.0.0` in Docker | Process bind for the Flask/Gunicorn server |
| `GENSIM_WEB_PORT`           | `5000`        | Port for the REST API and dashboard       |
| `GENSIM_MODBUS_HOST`        | `127.0.0.1` local Python; `0.0.0.0` in Docker | Process bind for Modbus TCP |
| `GENSIM_MODBUS_PORT`        | `5020`        | Port for Modbus TCP connections           |
| `GENSIM_PUBLIC_WEB_HOST`    | request host  | Hostname shown in dashboard connection hints |
| `GENSIM_PUBLIC_WEB_PORT`    | request port  | Web port shown in dashboard connection hints |
| `GENSIM_PUBLIC_MODBUS_HOST` | request host  | Modbus host shown in dashboard connection hints |
| `GENSIM_PUBLIC_MODBUS_PORT` | `GENSIM_MODBUS_PORT` | Modbus port shown in dashboard connection hints |
| `GENSIM_STATE_FILE`         | `generator_state.json` local Python; `/data/generator_state.json` in Docker | Path for persisted state |
| `GENSIM_RUNBOOKS_FILE`      | `generator_runbooks.json` local Python; `/data/generator_runbooks.json` in Docker | Path for saved runbooks |
| `GENSIM_SAVE_INTERVAL`      | `60`          | Seconds between automatic state saves     |
| `GENSIM_TICK_SECONDS`       | `1.0`         | Simulation tick interval in seconds       |
| `GENSIM_SECRET_KEY`         | *(random)*    | Flask session secret; set for stable sessions |
| `GENSIM_CORS_ALLOWED_ORIGINS` | same-origin | Comma-separated CORS origins, or `*`     |
| `GENSIM_HTTP_RATE_LIMIT`    | `120`         | Max mutating HTTP/Socket.IO commands per client per window; `0` disables |
| `GENSIM_HTTP_RATE_WINDOW`   | `60`          | Rate-limit window in seconds              |

---

## Health Checks

Generator Fleet Simulator exposes separate health endpoints:

- `GET /api/live`: web process liveness. Docker `HEALTHCHECK` polls this.
- `GET /api/ready`: simulator readiness. Returns HTTP 200 only after the
  fleet setup is complete and the Modbus server is running.
- `GET /api/health`: backward-compatible status payload for support and API
  clients.
- `GET /api/scada/topology`: current SCADA hierarchy and kW rollups.
- `GET /api/scada/alarms`: scoped actionable alarm list for SCADA selections.

```bash
# Manual health check
curl http://localhost:5000/api/live

# Simulator ready check, after terminal or dashboard setup
curl http://localhost:5000/api/ready
```

Example `/api/ready` response after setup:
```json
{
  "ok": true,
  "ready": true,
  "configured": true,
  "web_host": "127.0.0.1",
  "web_port": 5000,
  "modbus_host": "127.0.0.1",
  "modbus_port": 5020,
  "modbus_running": true,
  "num_generators": 15,
  "active_scenario": null
}
```

The `configured` field indicates whether the simulator has been started through
the terminal launcher or browser UI. Before configuration, `/api/live` returns
200, while `/api/ready` returns 503 with `configured: false` and
`modbus_running: false`.

Check Docker health status:
```bash
docker inspect --format='{{.State.Health.Status}}' generator-sim
# healthy | unhealthy | starting
```

For release or staging verification, run the automated smoke test against the
running dashboard. The smoke configures a small fleet, waits for readiness,
checks fleet and register APIs, verifies invalid sub-fleet member payloads are
rejected, and performs a real Modbus holding-register read:

```bash
python tools/release_smoke.py \
  --base-url http://localhost:5000 \
  --modbus-host 127.0.0.1 \
  --modbus-port 5020
```

Use the published host and port values for the target deployment. The smoke
test reconfigures the simulator to a two-generator fleet, so run it only against
staging, release candidates, or a customer instance where that reset is
expected.

For the Community Edition Compose package, the published host Modbus port is `5021` but
the app still binds `5020` inside the container. In that case, pass both ports:

```bash
python tools/release_smoke.py \
  --base-url http://localhost:5001 \
  --modbus-host 127.0.0.1 \
  --modbus-port 5021 \
  --startup-modbus-port 5020
```

---

## Persistent State

Generator Fleet Simulator persists its state to a JSON file so data survives container
restarts. The volume mount (`-v generator-data:/data`) ensures the file is
stored outside the container layer.

| Product         | Default state file path       |
|-----------------|-------------------------------|
| Generator Fleet | `/data/generator_state.json`  |

**Persisted data includes:**
- Generator/unit run hours
- Subfleet/site assignments
- Active scenario state (if a scenario was running at shutdown)

At startup, invalid JSON, non-object JSON, unreadable files, and state files
larger than 4 MiB are ignored so the simulator can recover to a clean runtime
instead of blocking startup on a damaged persistence volume.

**To reset state**, stop the container, delete or rename the state file inside
the volume, and restart:

```bash
docker exec generator-sim rm /data/generator_state.json
docker restart generator-sim
```

Or remove the volume entirely for a clean slate:

```bash
docker compose down -v   # WARNING: deletes all state volumes
```

---

## Modbus TCP Connectivity

Generator Fleet Simulator runs a Modbus TCP server that exposes one Modbus unit per
simulated device. Unit IDs start at 1.

**Connection parameters:**

| Product         | Default Host | Default Port |
|-----------------|:------------:|:------------:|
| Generator Fleet | `localhost`  | `5020`       |

When running through the Community Edition Compose file, the published host port is
`5021`.

**Supported function codes:**
- FC 3 — Read Holding Registers
- FC 6 — Write Single Register
- FC 16 — Write Multiple Registers

**Polling guidance:** poll each unit every 1 second to match the simulator's
tick rate. Faster polling is supported but does not yield higher resolution data.

Download the Modbus register map from `GET /api/export/config.csv` for a full
register reference including alarm bits and command codes.

---

## Scenarios and Runbooks

The Generator Fleet exposes a built-in scenario catalog at `GET /api/scenarios`.
Scenarios are scripted timelines that automatically issue fleet or unit commands
at predefined intervals, useful for demonstrations and integration testing.

**Start a scenario:**
```bash
curl -X POST http://localhost:5000/api/scenarios/run \
  -H "Content-Type: application/json" \
  -d '{"scenario_id": "utility-fail-recovery"}'
```

**Stop a running scenario:**
```bash
curl -X POST http://localhost:5000/api/scenarios/stop
```

**Available scenarios:**

| ID                      | Name                         | Duration |
|-------------------------|------------------------------|----------|
| `utility-fail-recovery` | Utility Fail and Recovery    | 45 s     |
| `fault-and-reset`       | Fault Injection and Reset    | 35 s     |
| `parallel-mode-demo`    | Parallel Mode Demo           | 40 s     |
| `e-stop-drill`          | Emergency Stop Drill         | 30 s     |

---

## Data Export

Download a full CSV export of the simulator configuration, register map, and
fleet state:

```bash
curl -O http://localhost:5000/api/export/config.csv
```

The CSV includes:
- Simulator configuration (host, port, fleet size)
- All configured units with names and ratings
- Full Modbus register map with scaling and engineering ranges
- Alarm bit definitions with severity levels
- Command register values and descriptions

---

## One-Line SCADA

The dashboard includes a `One-Line SCADA` tab for large-fleet navigation. It
renders a hierarchy from fleet to sub-fleet, generator size, unit range, and
individual unit. Each level rolls up:

- configured units
- running units
- generated kW and running rated kW
- utility load kW
- fault and actionable alarm counts
- generator and utility breaker status

Closed generator breaker paths are shown as energized red conductors. Open
breakers use the open breaker symbol and do not energize the downstream
generator line. The alarm/info control opens a scoped alarm popout so operators
can inspect alarms without losing their place in the hierarchy.

For launch demos with 2,000 generators, create sub-fleets from the dashboard or
use a support seed/runbook when a pre-grouped 100-block demo is required. The
terminal launcher currently configures fleet counts and does not automatically
create named sub-fleets.

---

## Logs

The simulator writes structured logs to stdout. Use `docker logs` or Docker
Compose log aggregation to collect them.

```bash
# Stream logs
docker logs -f generator-sim

# Last 100 lines
docker logs --tail=100 generator-sim

# With Compose
docker compose logs -f generator
```

Log level is `INFO` by default. The `%(name)s` field identifies the subsystem
(e.g., `modbus_server`, `root`).

---

## Release Integrity

The tag workflow installs only hashed dependencies, runs the full verification
suite and Docker release smoke, then publishes a multi-architecture image with
BuildKit SBOM and max-level provenance attestations. It signs the immutable
image digest with GitHub Actions OIDC and verifies that signature before it
builds Community Edition release assets. It pushes only an unadvertised
candidate tag until exact amd64 and arm64 digest smokes, anonymous pull, asset
validation, and the asset-manifest signature have passed.

Each release delivery contains:

- `generator-fleet-simulator-community-edition-<version>.zip`
- the matching `.zip.sha256` checksum
- `generator-fleet-simulator-community-edition-<version>-release-receipt.json`
- `generator-fleet-simulator-community-edition-v<version>-sbom.spdx.json`
- `generator-fleet-simulator-community-edition-v<version>-SHA256SUMS`
- the matching `.sigstore.json` verification bundle

The receipt is the operational join key. It records the version, source commit,
image repository/digest, runtime-lock hash, workflow run, and required release
gates. Preserve it with release and support records.

Verify the image signature by digest, never by a mutable tag:

```bash
cosign verify \
  --certificate-identity-regexp '^https://github.com/studioxvii/generator-fleet-simulator/.github/workflows/docker-publish.yml@refs/tags/v' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  studioxvii/generator-fleet-sim@sha256:<digest-from-receipt>
```

Verify the downloadable asset set before running either launcher:

```bash
cosign verify-blob \
  --bundle generator-fleet-simulator-community-edition-v<version>-SHA256SUMS.sigstore.json \
  --certificate-identity-regexp '^https://github.com/studioxvii/generator-fleet-simulator/.github/workflows/docker-publish.yml@refs/tags/v' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  generator-fleet-simulator-community-edition-v<version>-SHA256SUMS
sha256sum --check generator-fleet-simulator-community-edition-v<version>-SHA256SUMS
```

If the GitHub repository is private, GitHub-native artifact attestations may
depend on organization plan entitlements. The Docker image's BuildKit
attestations and cosign signature are the release's portable verification path.

---

## Upgrading

Images are tagged by release version, with `latest` reserved for stable releases.
Community Edition packages pin the immutable manifest digest, not a mutable tag.

```bash
# Pull the pinned image from the new package receipt
IMAGE='studioxvii/generator-fleet-sim@sha256:<digest-from-RELEASE_RECEIPT.json>'
docker pull "$IMAGE"

# Restart with the new image (Compose)
SIM_HOST_BIND=127.0.0.1 docker compose pull
SIM_HOST_BIND=127.0.0.1 docker compose up -d generator

# Or restart a single service
docker stop generator-sim
docker rm generator-sim
docker run -d --name generator-sim ... "$IMAGE"
```

State volumes are preserved across upgrades. The state file format is
backward-compatible within a major version. Upgrade only after receiving a new
Community Edition package or explicit support instruction with the replacement digest.
Verify its ZIP checksum, receipt, image digest, and signature before replacing
the existing container. Keep the prior digest and receipt available for rollback.

---

## Troubleshooting

### Container exits immediately

Run `docker logs generator-sim` to see the error message.

### Dashboard shows "Simulator not configured"

This is expected only if the launcher was told to configure later in the
browser, or if the terminal startup request failed. Click **Configure** (or
**Start Simulator**) in the browser UI to initialize the fleet and start the
Modbus server.

### Simulator is ready but all units are stopped

This is normal after terminal startup. The launcher configures the fleet and
starts Modbus; it does not start every generator. Start generators from the
dashboard, SCADA controls, scenarios, runbooks, or Modbus command registers.

### Image pull is denied

- The exact pinned image is intended to be public and anonymously pullable.
- Confirm network access and Docker status, then retry without signing in.
- If denial persists, verify Docker Hub repository visibility is **Public** and
  that the exact immutable digest exists; treat failure as a publisher-side release
  configuration incident.
- Use the cached-image option only for a previously verified copy of the exact
  pinned image.

### Modbus connection refused

- Verify the simulator has been configured via the terminal launcher or UI
  (Modbus only starts after configuration).
- Check that the Modbus port is published: local-only mode should show `127.0.0.1:5021->5020/tcp`.
- Confirm no firewall rule is blocking the port.

### Readiness check returns 503

- `/api/live` should return HTTP 200 once the web process is running.
- `/api/ready` returns HTTP 503 until the terminal launcher or dashboard startup
  flow configures the fleet and starts Modbus.
- If `/api/live` fails or the container is stuck in `starting`, check container
  logs and resource limits.

### State not persisting across restarts

Ensure the volume mount is present in your `docker run` or `docker-compose.yml`
command. Without a volume, state is written inside the container layer and lost
on removal.

### Port conflicts

If another process is already using the default ports, choose different host
ports in `docker-compose.yml` or run direct Docker with alternate mappings:

```bash
-p 127.0.0.1:6001:5000
-p 127.0.0.1:6021:5020
```

Host validation accepts literal IP addresses and configured web host names.
Set `GENSIM_TRUSTED_HOSTS` to a comma-separated list of other exact DNS names
needed for an isolated LAN or same-origin proxy. This does not add authentication.

Modbus uses 255 generators per port. The Community Edition launcher publishes
ports 5021–5028. Generator 256 uses port 5022 and wire unit ID 1.
See `MODBUS_REFERENCE.md` for the complete mapping and custom port rules.
