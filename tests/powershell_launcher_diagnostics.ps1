$ErrorActionPreference = "Stop"

# Exercise the real launcher functions without entering its interactive menu.
$Tokens = $null
$Errors = $null
$Ast = [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PSScriptRoot "../start.ps1"), [ref]$Tokens, [ref]$Errors)
if ($Errors.Count) { throw ($Errors | Out-String) }
foreach ($Name in @("Test-DockerEngineReady", "Ensure-DockerRunning")) {
    $Function = $Ast.Find({
        param($Node)
        $Node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $Node.Name -eq $Name
    }, $true)
    . ([scriptblock]::Create($Function.Extent.Text))
}

$TestBin = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
$OriginalPath = $env:PATH
New-Item -ItemType Directory $TestBin | Out-Null
try {
    @'
@echo off
if "%GENSIM_TEST_DOCKER_RESULT%"=="denied" (
  echo permission denied while connecting to the Docker engine 1>&2
  exit /b 1
)
if "%GENSIM_TEST_DOCKER_RESULT%"=="unavailable" (
  echo Cannot connect to the Docker daemon 1>&2
  exit /b 1
)
echo Docker engine is ready
exit /b 0
'@ | Set-Content (Join-Path $TestBin "docker.cmd") -Encoding ASCII
    $env:PATH = "$TestBin;$OriginalPath"
    function Read-Host { throw "end-test-prompt" }

    foreach ($Mode in @("denied", "unavailable", "ready")) {
        $env:GENSIM_TEST_DOCKER_RESULT = $Mode
        $Ready = Test-DockerEngineReady
        if ($Ready -ne ($Mode -eq "ready")) { throw "Wrong readiness result: $Mode" }
        if ($Mode -eq "ready") { continue }
        $Output = & {
            try { Ensure-DockerRunning }
            catch { if ($_.Exception.Message -ne "end-test-prompt") { throw } }
        } 6>&1 | Out-String
        if ($Output -notmatch "Cannot access the Docker engine") { throw "Missing diagnostic: $Output" }
        if ($Output -match "daemon is not running") { throw "Misleading diagnostic: $Output" }
        if ($Mode -eq "denied" -and $Output -notmatch "This account does not have permission") {
            throw "Missing permission guidance: $Output"
        }
        if ($Mode -eq "unavailable" -and $Output -notmatch "Cannot connect to the Docker daemon") {
            throw "Docker error was lost: $Output"
        }
    }
    Write-Host "PowerShell Docker diagnostics passed."
} finally {
    $env:PATH = $OriginalPath
    Remove-Item Env:GENSIM_TEST_DOCKER_RESULT -ErrorAction SilentlyContinue
    Remove-Item Function:Read-Host -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $TestBin
}
