param(
    [switch]$Install,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

if ($Clean) {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue build, dist
    exit 0
}

if ($Install) {
    python -m pip install -e ".[mstsc,build]"
}

$BasePrefix = python -c "import sys; print(sys.base_prefix)"
$ExtraBinaries = @()
foreach ($DllName in @("ffi.dll", "libbz2.dll", "liblzma.dll", "libcrypto-3-x64.dll", "libssl-3-x64.dll")) {
    $DllPath = Join-Path $BasePrefix "Library\bin\$DllName"
    if (Test-Path $DllPath) {
        $ExtraBinaries += "--add-binary"
        $ExtraBinaries += "$DllPath;."
    }
}

$ClientArgs = @(
    "--noconfirm", "--onefile", "--name", "rdp2tcp-client",
    "--workpath", "build\client", "--distpath", "dist", "--specpath", "build\spec"
) + $ExtraBinaries + @("scripts/client_entry.py")
python -m PyInstaller @ClientArgs
if ($LASTEXITCODE -ne 0) { throw "rdp2tcp-client build failed" }

$AgentArgs = @(
    "--noconfirm", "--onefile", "--name", "rdp2tcp-agent",
    "--workpath", "build\agent", "--distpath", "dist", "--specpath", "build\spec"
) + $ExtraBinaries + @("scripts/agent_entry.py")
python -m PyInstaller @AgentArgs
if ($LASTEXITCODE -ne 0) { throw "rdp2tcp-agent build failed" }

Write-Host "Built dist\rdp2tcp-client.exe and dist\rdp2tcp-agent.exe"
