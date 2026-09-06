# Security Policy

## Supported Use

Generator Fleet Simulator Community Edition is designed to run locally for
simulation, demonstrations, and integration testing. Project code uses the
MIT License in `LICENSE`. The default configuration binds the web dashboard and Modbus TCP
server to `127.0.0.1`.

If you expose the simulator to a shared network, you are responsible for adding the appropriate network controls. The simulator does not implement authentication for dashboard, Socket.IO, REST command APIs, or Modbus commands.

## Community Edition Launcher

Use `./start.sh` on macOS/Linux or `.\start.ps1` on Windows for self-service
startup. The launcher requires this security file to be opened and
acknowledged before it starts containers.

The launcher defaults to local-only host port bindings:

- Dashboard ports bind to `127.0.0.1`
- Modbus TCP ports bind to `127.0.0.1`

Trusted LAN integration mode is opt-in from the launcher menu. Enable it only
on an isolated engineering or test network where other devices must connect to
the simulator. The launcher requires a second confirmation before binding ports
to all host interfaces.

## Modbus TCP Exposure

Modbus TCP is unauthenticated by design. Any client that can reach the published
Modbus port can issue supported read and write commands. Treat the simulator as
test equipment, not as an internet-facing service.

Recommended controls:

- Keep the default local-only binding for single-machine testing.
- Use host firewall rules when exposing the simulator to a LAN.
- Prefer a customer VPN, jump host, or reverse proxy with authentication when
  browser access must cross network boundaries.
- Do not publish Modbus TCP ports directly to the public internet.
- Reset simulator state between customer demos or shared training sessions.

## Privacy And Data Handling

The application runs locally and stores generator state, sub-fleet assignments,
and saved runbooks in the local Docker volume. It does not include application
telemetry, analytics, user accounts, or a Studio Seventeen-hosted backend.
Docker contacts Docker Hub to retrieve the public image; Docker, DNS, proxy,
firewall, and host-platform logs remain subject to the operator's own tools and
provider policies.

The simulator does not collect or transmit operator-entered fleet configuration
to Studio Seventeen. Do not place confidential production data in saved names,
runbooks, or exported files unless the local host and its backups are approved
for that data.

## Reporting a Vulnerability

Please do not open public issues for security-sensitive problems.

Report vulnerabilities privately to the maintainer with:

- A description of the issue
- Affected version or commit
- Reproduction steps
- Any suggested mitigation

Send private reports to `legal@studioseventeen.io` with the subject prefix
`SECURITY`. I will acknowledge reports as quickly as practical and coordinate a
fix before public disclosure.


## Browser request controls

REST writes reject cross-site Origin, Referer, and Fetch Metadata headers.
Socket.IO uses same-origin checks by default. Host checks cover both HTTP and
Socket.IO. Literal IP addresses, localhost, and configured web host names are
allowed. Add other exact host names with `GENSIM_TRUSTED_HOSTS` (comma-separated).
These checks reduce browser attacks. They do not authenticate a network client.
Keep proxy access on the same origin and apply authentication at the proxy.
Do not set wildcard Socket.IO origins on a shared network.
