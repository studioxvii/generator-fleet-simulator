# Website Startup Instructions

Use this copy for the free Community Edition download page.

## Start Generator Fleet Simulator Community Edition

**Free download. MIT-licensed project code.**

Generator Fleet Simulator Community Edition runs locally from a guided terminal
startup. The setup checks Docker, opens the license and security notes, pulls
the pinned public image, asks how the simulator should be published on your
network, configures the fleet, then opens the dashboard.

### Before You Start

Have these ready:

- Your Studio Seventeen download package
- The matching `.zip.sha256` checksum and release receipt
- Docker Desktop
- A terminal on macOS/Linux or PowerShell on Windows
- On macOS/Linux: Bash, Python 3 (`python3`), and `curl`
- Docker access for your account: `docker info` must succeed in that terminal

If Docker Desktop is not installed yet, the startup script will show the install
options and pause until Docker is ready.

### 1. Verify And Open The Download Folder

Before unzipping, verify the SHA-256 checksum using the command for your
operating system in `INSTALL.md`. The computed hash must match the checksum
file supplied beside the download. Stop and contact Studio Seventeen if it does
not match.

After verification, unzip the package and open a terminal in the folder.

macOS or Linux:

```bash
cd /path/to/generator-fleet-simulator
```

Windows PowerShell:

```powershell
cd C:\path\to\generator-fleet-simulator
```

### 2. Start The Guided Setup

macOS or Linux:

```bash
./start.sh
```

Windows PowerShell:

```powershell
.\start.ps1
```

### 3. Complete Each Startup Step

The setup walks through six required steps:

1. Docker check
2. License agreement
3. Security notes
4. Network exposure
5. Fleet configuration
6. Simulator launch

The security step must be acknowledged before launch. The setup
does not provide a skip option.

### 4. Choose Network Exposure

Choose local-only unless another machine on your trusted engineering network
needs to connect.

| Option | Use When |
|---|---|
| Local-only | You are using the dashboard and Modbus client on this computer |
| Trusted LAN integration mode | A trusted HMI, SCADA workstation, or Modbus client must connect from another computer on an isolated engineering or test network |

Local-only mode binds dashboard and Modbus ports to `127.0.0.1`.
Trusted LAN integration mode binds the ports to all host interfaces and should
not be used on public, guest, or general corporate networks without
customer-managed firewall/VPN controls.

### 5. Choose Fleet Configuration

The setup can start the simulator directly from the terminal.

| Option | Use When |
|---|---|
| Default fleet | You want to start immediately with 15 x 500 kW generators |
| Custom counts | You know the number of generators needed at each supported kW size |
| Configure later | You want to choose the fleet mix from the browser dashboard |

When default or custom counts are selected, setup waits until the simulator is
ready and Modbus is running before it prints the final access details. The
selected generator units are configured and ready, but they remain stopped until
you issue a start command from the dashboard, SCADA tab, scenario timeline,
runbook, or Modbus command.

### 6. Pull The Public Image

The launcher pulls the exact public image pinned by the package. A Docker Hub
account and `docker login` are not required.

If a pull fails, check internet access and Docker status, then choose `retry`.
If the exact tag returns `pull access denied`, report the package version and
error to Studio Seventeen. That indicates a publication or Docker Hub
configuration failure, not a missing customer entitlement.

If support has preloaded the image on your machine, setup may also offer `use`.
Choose `use` only when you know the cached image came from Studio Seventeen or
from a local support build.

The release receipt supplied with the download names the approved image digest.
Keep the receipt, checksum, and SPDX SBOM with the package for support and
future upgrades.

### 7. Open The Dashboard

When startup completes, the terminal prints the access details:

| Service | Default Endpoint |
|---|---|
| Dashboard | `http://localhost:5001` |
| Modbus TCP | `localhost:5021–5028` |

Open the dashboard URL in your browser. If you selected default or custom
counts, the simulator runtime and Modbus server are already running. If you
selected configure later, choose the fleet mix in the startup screen, then start
the simulator.

After the dashboard opens, check:

- Fleet count matches the selected fleet mix.
- Running count is `0` until you start generators intentionally.
- The `One-Line SCADA` tab opens and shows the fleet hierarchy.
- The `Fleet Status` table, `Sub-Fleets`, `Scenario Timelines`, and `Modbus Registers` tabs are available.
- Generator units can be started from fleet, group, SCADA, or Modbus controls.

### 8. Stop The Simulator

To stop the simulator while keeping saved state:

```bash
docker compose --file docker-compose.yml stop generator
```

To remove the container and network while keeping the image:

```bash
docker compose --file docker-compose.yml down
```

To reset all saved simulator state:

```bash
docker compose --file docker-compose.yml down --volumes
```

### Troubleshooting

Docker Desktop is spinning:
Wait a few minutes on first launch. If Docker Desktop stays on startup for more
than 5 to 10 minutes, quit and reopen Docker Desktop, then run the startup
script again.

Image pull is denied:
The image should be anonymously pullable. Check internet access and Docker
status, then choose `retry`. If denial continues, report the package version and
error to Studio Seventeen because the release was not published correctly. Use
a cached image only when it is the previously verified exact pinned release.

Setup says the simulator is ready but generators are stopped:
This is normal. Readiness means the simulator runtime and Modbus server are
available. Start generator units from the dashboard, SCADA tab, scenario
timeline, runbook, or Modbus command when you want them to run.

Dashboard does not open:
Run `docker compose --file docker-compose.yml logs --tail=100 generator` and
send the output to support.

Another computer cannot connect:
Run setup again and choose trusted LAN integration mode, or keep local-only mode
and use the dashboard and Modbus client on the same computer.

### Privacy And Support

The simulator runs locally and stores state in a local Docker volume. It does
not include telemetry, analytics, accounts, or a Studio Seventeen-hosted
backend. Docker contacts Docker Hub to download the image. No support SLA is
included with Community Edition; use the feedback/support link beside the
download and include the release receipt, version, platform, and relevant logs.

Modbus uses 255 generators per port. The Community Edition launcher publishes
ports 5021–5028. Generator 256 uses port 5022 and wire unit ID 1.
See `MODBUS_REFERENCE.md` for the complete mapping and custom port rules.
