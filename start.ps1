# Studio Seventeen guided launcher for Generator Fleet Simulator Community Edition.

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ComposeFile = Join-Path $ScriptDir "docker-compose.yml"
$StateDir = Join-Path $ScriptDir ".studioseventeen"
$SecurityReviewFile = Join-Path $StateDir "security-reviewed"
$NetworkModeFile = Join-Path $StateDir "network-mode"
$VersionFile = Join-Path $ScriptDir "VERSION"
$ImageRepository = "studioxvii/generator-fleet-sim"
$ImageTag = if (Test-Path $VersionFile) { (Get-Content $VersionFile -First 1).Trim() } else { "1.1.0-rc.5" }
if (-not $ImageTag) {
    $ImageTag = "1.1.0-rc.5"
}
$FleetStartupPayload = $null
$FleetTotal = 0
$FleetConfigDescription = "Configure later in browser"
$script:DockerCheckError = ""

function Confirm-ReleaseIntegrity {
    $ReceiptPath = Join-Path $ScriptDir "RELEASE_RECEIPT.json"
    if (-not (Test-Path $ReceiptPath)) {
        return
    }

    $Receipt = Get-Content $ReceiptPath -Raw | ConvertFrom-Json
    $Version = if (Test-Path $VersionFile) { (Get-Content $VersionFile -First 1).Trim() } else { "" }
    if ($Receipt.version -ne $Version) {
        throw "Release receipt version '$($Receipt.version)' does not match VERSION '$Version'."
    }

    $Digest = $Receipt.image.digest
    $Compose = Get-Content $ComposeFile -Raw
    if ($Digest -and ($Compose -notlike "*@$Digest*") -and ($Compose -notmatch "GENSIM_IMAGE_TAG")) {
        throw "docker-compose.yml is not pinned to the receipt image digest."
    }
    Write-Host "Release receipt verified."
}

function Show-Header {
    Write-Host ""
    Write-Host "Studio Seventeen - Generator Fleet Simulator Community Edition"
    Write-Host "Guided startup"
    Write-Host ""
}

function Pause-ForUser {
    Read-Host "Press Enter to continue" | Out-Null
}

function Test-IsWindows {
    return [System.Environment]::OSVersion.Platform -eq "Win32NT"
}

function Show-TextFile {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        throw "Missing required file: $Path"
    }

    Write-Host "-------------------------------------------------------------------------------"
    Get-Content $Path | ForEach-Object { Write-Host $_ }
    Write-Host "-------------------------------------------------------------------------------"
}

function Save-Acknowledgement {
    param([string]$Path)

    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    @(
        "accepted_at=$((Get-Date).ToUniversalTime().ToString('o'))"
        "launcher=start.ps1"
    ) | Set-Content -Path $Path -Encoding UTF8
}

function Get-NetworkMode {
    if ((Test-Path $NetworkModeFile) -and ((Get-Content $NetworkModeFile -Raw).Trim() -eq "lan")) {
        return "lan"
    }
    return "local"
}

function Get-HostBind {
    if ((Get-NetworkMode) -eq "lan") {
        return "0.0.0.0"
    }
    return "127.0.0.1"
}

function Get-NetworkLabel {
    if ((Get-NetworkMode) -eq "lan") {
        return "trusted LAN integration mode enabled"
    }
    return "local machine only"
}

function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & docker compose --file $ComposeFile @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed with exit code $LASTEXITCODE"
    }
}

function Get-GeneratorImageReference {
    $Images = @(& docker compose --file $ComposeFile config --images 2> $null)
    if (($LASTEXITCODE -ne 0) -or ($Images.Count -eq 0)) {
        return $null
    }
    return $Images[0]
}

function Test-GeneratorImageAvailable {
    param([string]$ImageReference)

    if (-not $ImageReference) {
        return $false
    }
    & docker image inspect $ImageReference *> $null
    return $LASTEXITCODE -eq 0
}

function Test-DockerEngineReady {
    $script:DockerCheckError = ""
    $Job = Start-Job -ScriptBlock {
        $ErrorActionPreference = "Continue"
        $Detail = & docker info 2>&1 | Out-String
        [PSCustomObject]@{ ExitCode = $LASTEXITCODE; Detail = $Detail.Trim() }
    }
    $Completed = Wait-Job $Job -Timeout 20
    if (-not $Completed) {
        Stop-Job $Job -ErrorAction SilentlyContinue | Out-Null
        Remove-Job $Job -Force -ErrorAction SilentlyContinue
        $script:DockerCheckError = "Docker did not respond within 20 seconds."
        return $false
    }

    $Result = Receive-Job $Job -ErrorAction SilentlyContinue
    Remove-Job $Job -Force -ErrorAction SilentlyContinue
    if ($null -eq $Result) {
        $script:DockerCheckError = "Docker check failed without a response. Run docker info in this terminal."
        return $false
    }
    $script:DockerCheckError = $Result.Detail
    return $Result.ExitCode -eq 0
}

function Show-DockerInstallHelp {
    Write-Host ""
    Write-Host "Docker Desktop is required before the simulator can start."
    Write-Host ""
    Write-Host "Install Docker Desktop:"
    Write-Host "  https://docs.docker.com/get-docker/"
    Write-Host ""

    if (Test-IsWindows) {
        Write-Host "Windows option with winget:"
        Write-Host "  winget install Docker.DockerDesktop"
        $Response = Read-Host "Type install to run winget now, quit to exit, or press Enter after installing manually"
        if ($Response -eq "quit") {
            exit 0
        }
        if ($Response -eq "install") {
            winget install Docker.DockerDesktop
        }
    } else {
        $Response = Read-Host "Type quit to exit, or press Enter after installing manually"
        if ($Response -eq "quit") {
            exit 0
        }
    }
}

function Ensure-DockerInstalled {
    while (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Show-DockerInstallHelp
    }
}

function Ensure-DockerRunning {
    while (-not (Test-DockerEngineReady)) {
        Write-Host ""
        Write-Host "Cannot access the Docker engine."
        Write-Host $script:DockerCheckError
        if ($script:DockerCheckError -match "permission denied|access is denied") {
            Write-Host "This account does not have permission to access Docker."
            Write-Host "See INSTALL.md, Docker access denied. Resolve access for this account, then retry."
        } else {
            Write-Host "Start Docker Desktop or the Docker daemon if stopped. If it is already running,"
            Write-Host "check docker context show and docker info in this terminal."
        }
        $Response = Read-Host "Type quit to exit, or press Enter to check again"
        if ($Response -eq "quit") {
            exit 0
        }
    }
}

function Ensure-ComposeAvailable {
    while ($true) {
        & docker compose version *> $null
        if ($LASTEXITCODE -eq 0) {
            return
        }

        Write-Host ""
        Write-Host "Docker is available, but Docker Compose v2 was not found."
        Write-Host "Install or update Docker Desktop, then return here."
        $Response = Read-Host "Type quit to exit, or press Enter after updating Docker Desktop"
        if ($Response -eq "quit") {
            exit 0
        }
    }
}

function Step-Docker {
    Write-Host ""
    Write-Host "Step 1 of 6 - Docker check"
    Ensure-DockerInstalled
    Ensure-DockerRunning
    Ensure-ComposeAvailable
    Write-Host "Docker is ready."
}

function Step-License {
    Write-Host ""
    Write-Host "Step 2 of 6 - MIT License"
    Show-TextFile (Join-Path $ScriptDir "LICENSE")
}

function Step-Security {
    Write-Host ""
    Write-Host "Step 3 of 6 - Security notes"
    Write-Host "Read the security notes below. The simulator cannot launch until you acknowledge them."
    Write-Host ""
    Show-TextFile (Join-Path $ScriptDir "SECURITY.md")

    $Response = ""
    while ($Response -ne "understood") {
        $Response = Read-Host "Type understood to acknowledge the security notes"
    }
    Save-Acknowledgement $SecurityReviewFile
    Write-Host "Security review recorded."
}

function Step-Network {
    Write-Host ""
    Write-Host "Step 4 of 6 - Network exposure"
    Write-Host "Modbus TCP is unauthenticated control traffic."
    Write-Host "The dashboard APIs can also issue simulator commands."
    Write-Host "Choose how this computer should publish the simulator ports."
    Write-Host ""
    Write-Host "1) Local-only, bound to 127.0.0.1"
    Write-Host "2) Trusted LAN integration mode, bound to all host interfaces"
    Write-Host ""

    $Choice = ""
    while (($Choice -ne "1") -and ($Choice -ne "2")) {
        $Choice = Read-Host "Choose 1 or 2"
    }

    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    if ($Choice -eq "2") {
        $Confirm = ""
        Write-Host ""
        Write-Host "LAN integration mode is for isolated engineering or test networks only."
        Write-Host "Do not use it on public, guest, or general corporate networks without firewall/VPN controls."
        while ($Confirm -ne "trusted-lan") {
            $Confirm = Read-Host "Type trusted-lan to confirm unauthenticated LAN exposure"
        }
        Set-Content -Path $NetworkModeFile -Value "lan" -Encoding UTF8
    } else {
        Set-Content -Path $NetworkModeFile -Value "local" -Encoding UTF8
    }

    Write-Host "Network mode set to: $(Get-NetworkLabel)"
}

function Read-GeneratorCount {
    param([string]$Label)

    while ($true) {
        $RawValue = Read-Host $Label
        if ([string]::IsNullOrWhiteSpace($RawValue)) {
            return 0
        }
        $Count = 0
        if ([int]::TryParse($RawValue, [ref]$Count) -and $Count -ge 0) {
            return $Count
        }
        Write-Host "Enter a whole number 0 or greater."
    }
}

function Set-FleetPayload {
    param(
        [int]$Count500,
        [int]$Count1000,
        [int]$Count1500,
        [int]$Count2000,
        [int]$Count2500
    )

    $script:FleetTotal = $Count500 + $Count1000 + $Count1500 + $Count2000 + $Count2500
    $Payload = @{
        size_counts = @{
            "500" = $Count500
            "1000" = $Count1000
            "1500" = $Count1500
            "2000" = $Count2000
            "2500" = $Count2500
        }
    }
    $script:FleetStartupPayload = $Payload | ConvertTo-Json -Compress
    $script:FleetConfigDescription = "$($script:FleetTotal) generators ($Count500 x 500 kW, $Count1000 x 1000 kW, $Count1500 x 1500 kW, $Count2000 x 2000 kW, $Count2500 x 2500 kW)"
}

function Step-FleetConfig {
    Write-Host ""
    Write-Host "Step 5 of 6 - Fleet configuration"
    Write-Host "Choose the fleet mix the launcher should start now."
    Write-Host ""
    Write-Host "1) Start default fleet: 15 x 500 kW generators"
    Write-Host "2) Enter custom generator counts by size"
    Write-Host "3) Configure later in the browser dashboard"
    Write-Host ""

    $Choice = ""
    while (($Choice -ne "1") -and ($Choice -ne "2") -and ($Choice -ne "3")) {
        $Choice = Read-Host "Choose 1, 2, or 3"
    }

    if ($Choice -eq "1") {
        Set-FleetPayload -Count500 15 -Count1000 0 -Count1500 0 -Count2000 0 -Count2500 0
    } elseif ($Choice -eq "2") {
        while ($true) {
            $Count500 = Read-GeneratorCount "500 kW generators"
            $Count1000 = Read-GeneratorCount "1000 kW generators"
            $Count1500 = Read-GeneratorCount "1500 kW generators"
            $Count2000 = Read-GeneratorCount "2000 kW generators"
            $Count2500 = Read-GeneratorCount "2500 kW generators"
            $Total = $Count500 + $Count1000 + $Count1500 + $Count2000 + $Count2500
            if (($Total -ge 1) -and ($Total -le 2000)) {
                Set-FleetPayload -Count500 $Count500 -Count1000 $Count1000 -Count1500 $Count1500 -Count2000 $Count2000 -Count2500 $Count2500
                break
            }
            Write-Host "Total generators must be between 1 and 2000. Current total: $Total"
        }
    } else {
        $script:FleetStartupPayload = $null
        $script:FleetTotal = 0
        $script:FleetConfigDescription = "Configure later in browser"
    }

    Write-Host "Fleet configuration: $($script:FleetConfigDescription)"
}

function Wait-ForUrl {
    param(
        [string]$Name,
        [string]$Url
    )

    Write-Host -NoNewline "Waiting for $Name"
    for ($Attempt = 1; $Attempt -le 30; $Attempt++) {
        try {
            Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3 | Out-Null
            Write-Host " ready"
            return $true
        } catch {
            Write-Host -NoNewline "."
            Start-Sleep -Seconds 2
        }
    }
    Write-Host " timed out"
    return $false
}

function Show-AccessInfo {
    Write-Host ""
    Write-Host "Generator Fleet Simulator is ready."
    Write-Host ""
    Write-Host "Dashboard:"
    Write-Host "  http://localhost:5001"
    Write-Host ""
    Write-Host "Modbus TCP ports (255 generators per port):"
    Write-Host "  localhost:5021-5028"
    Write-Host ""
    Write-Host "Network mode:"
    Write-Host "  $(Get-NetworkLabel)"
    Write-Host ""
    Write-Host "Fleet configuration:"
    Write-Host "  $($script:FleetConfigDescription)"
    Write-Host ""

    if ((Get-NetworkMode) -eq "lan") {
        Write-Host "Trusted LAN integration mode is enabled. Other devices can connect to this host's IP address."
        Write-Host "Keep this host on an isolated engineering network or behind customer-managed firewall/VPN controls."
    } else {
        Write-Host "Local-only mode is enabled. Other devices cannot connect through the host ports."
    }
}

function Start-FleetFromLauncher {
    if (-not $script:FleetStartupPayload) {
        Write-Host ""
        Write-Host "Fleet startup skipped. Configure the simulator from the browser dashboard."
        return $true
    }

    Write-Host -NoNewline "Starting configured fleet from terminal"
    for ($Attempt = 1; $Attempt -le 15; $Attempt++) {
        try {
            Invoke-RestMethod `
                -Uri "http://localhost:5001/api/startup" `
                -Method Post `
                -Body $script:FleetStartupPayload `
                -ContentType "application/json" `
                -TimeoutSec 5 | Out-Null
            Write-Host " started"
            return $true
        } catch {
            Write-Host -NoNewline "."
            Start-Sleep -Seconds 2
        }
    }

    Write-Host " failed"
    Write-Host "Could not configure the fleet from the terminal."
    return $false
}

function Ensure-GeneratorImage {
    $ImageReference = Get-GeneratorImageReference

    while ($true) {
        try {
            Invoke-Compose "pull" "generator"
            return $true
        } catch {
            if (Test-GeneratorImageAvailable $ImageReference) {
                Write-Host ""
                Write-Host "Could not refresh the public Generator Fleet image, but the exact pinned image is cached locally."
                Write-Host "A refresh failure can indicate a network outage or a Studio Seventeen publication problem."
                $Response = Read-Host "Type use to launch the cached image, retry to pull again, or quit to exit setup"
                switch ($Response) {
                    "use" {
                        Write-Host "Using cached Generator Fleet image."
                        return $true
                    }
                    "retry" {}
                    "" {}
                    "quit" {
                        return $false
                    }
                    default {
                        Write-Host "Unknown option."
                    }
                }
            } else {
                if (-not $ImageReference) {
                    $ImageReference = "$($ImageRepository):$($ImageTag)"
                }
                Write-Host ""
                Write-Host "Could not pull the public Generator Fleet image, and no cached image is available."
                Write-Host "$ImageReference is intended to be anonymously pullable; Docker Hub sign-in is not required."
                Write-Host "Check internet access and Docker status. If the denial continues, it is likely a Studio Seventeen publication or tag configuration failure."
                Write-Host "Contact Studio Seventeen with version $ImageTag and the pull error."
                $Response = Read-Host "Type retry to pull again or quit to exit setup"
                switch ($Response) {
                    "retry" {}
                    "" {}
                    "quit" {
                        return $false
                    }
                    default {
                        Write-Host "Unknown option."
                    }
                }
            }
        }
    }
}

function Step-Launch {
    Write-Host ""
    Write-Host "Step 6 of 6 - Launch simulator"
    $env:SIM_HOST_BIND = Get-HostBind
    $env:GENSIM_IMAGE_TAG = $ImageTag

    if (-not (Ensure-GeneratorImage)) {
        return
    }

    Invoke-Compose "up" "-d" "generator"
    if (Wait-ForUrl "Generator Fleet web service" "http://localhost:5001/api/live") {
        if (-not (Start-FleetFromLauncher)) {
            return
        }
        if ($script:FleetStartupPayload) {
            if (-not (Wait-ForUrl "Generator Fleet readiness" "http://localhost:5001/api/ready")) {
                return
            }
        }
        Show-AccessInfo
    } else {
        Write-Host ""
        Write-Host "The container started, but the dashboard did not become ready."
        Write-Host "Check the container logs, then run setup again after resolving the issue:"
        Write-Host "  docker compose --file `"$ComposeFile`" logs --tail=100 generator"
        return
    }
}

function Start-GuidedSetup {
    Show-Header
    Confirm-ReleaseIntegrity
    Step-Docker
    Step-License
    Step-Security
    Step-Network
    Step-FleetConfig
    Step-Launch
}

function Stop-Simulator {
    Ensure-DockerInstalled
    Ensure-DockerRunning
    Ensure-ComposeAvailable
    $env:SIM_HOST_BIND = Get-HostBind
    $env:GENSIM_IMAGE_TAG = $ImageTag
    Invoke-Compose "stop" "generator"
}

function Show-Logs {
    Ensure-DockerInstalled
    Ensure-DockerRunning
    Ensure-ComposeAvailable
    & docker compose --file $ComposeFile logs -f --tail=100 generator
}

function Show-MainMenu {
    while ($true) {
        Show-Header
        Write-Host "This setup starts Generator Fleet Simulator Community Edition only."
        Write-Host "The setup will walk through Docker, license, security, network, fleet configuration, and launch."
        Write-Host ""
        Write-Host "1) Start guided setup"
        Write-Host "2) Exit"
        Write-Host ""
        $Choice = Read-Host "Choose an option"

        switch ($Choice) {
            "1" { Start-GuidedSetup; Pause-ForUser }
            "2" { return }
            default { Write-Host "Unknown option."; Pause-ForUser }
        }
    }
}

Show-MainMenu
