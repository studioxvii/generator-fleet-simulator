Generator Fleet Simulator Community Edition {{VERSION}} runs locally with Docker.

Download the [Community Edition ZIP](https://github.com/studioxvii/generator-fleet-simulator/releases/download/v{{VERSION}}/generator-fleet-simulator-community-edition-{{VERSION}}.zip)
and its [checksum file](https://github.com/studioxvii/generator-fleet-simulator/releases/download/v{{VERSION}}/generator-fleet-simulator-community-edition-{{VERSION}}.zip.sha256)
into the same folder. Choose this launcher ZIP from Assets; GitHub's separate
“Source code” archives are for developers. Keep the release receipt, SBOM, and
signed SHA256SUMS files from Assets with your download for verification and support.
GitHub and Docker Hub accounts are not required.

You need Docker with Compose v2 and permission to use it (`docker info` must
succeed). The macOS/Linux launcher also needs Bash, Python 3 (`python3`), and
curl installed on the host. Windows uses PowerShell without host Python or curl.

Verify the checksum **before extracting**. From the download folder:

Linux:

```bash
sha256sum -c generator-fleet-simulator-community-edition-{{VERSION}}.zip.sha256
```

macOS:

```bash
shasum -a 256 -c generator-fleet-simulator-community-edition-{{VERSION}}.zip.sha256
```

Continue only if verification reports `OK`. Extract the ZIP, then:

```bash
cd generator-fleet-simulator-community-edition-{{VERSION}}
./start.sh
```

Windows PowerShell:

```powershell
$Expected = ((Get-Content .\generator-fleet-simulator-community-edition-{{VERSION}}.zip.sha256 -Raw).Trim() -split '\s+')[0]
$Actual = (Get-FileHash .\generator-fleet-simulator-community-edition-{{VERSION}}.zip -Algorithm SHA256).Hash
if ($Actual -ne $Expected) { throw "Checksum mismatch. Do not extract or run this download." }
Expand-Archive .\generator-fleet-simulator-community-edition-{{VERSION}}.zip -DestinationPath .
Set-Location .\generator-fleet-simulator-community-edition-{{VERSION}}
.\start.ps1
```

Follow guided setup and keep local-only mode unless you need an isolated test LAN.
The default dashboard is http://localhost:5001; Modbus uses ports 5021–5028.
The bundled `INSTALL.md` covers troubleshooting, and `MODBUS_REFERENCE.md`
contains generator addressing and client examples.

This preview has no application authentication. Use it locally or on an isolated
LAN. Native Windows Docker Desktop and full clean-machine acceptance remain
pending. Project code is MIT; third-party licenses are included.
