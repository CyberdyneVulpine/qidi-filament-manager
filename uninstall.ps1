# Remove the filament manager service and its sudoers rule from Windows.
#
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 user@printer
#
# Uses the ssh and scp that ship with Windows 10 and 11. The app directory
# and the filament catalog are left in place.

$ErrorActionPreference = "Stop"

function Show-Usage {
    Write-Host "Usage: powershell -ExecutionPolicy Bypass -File uninstall.ps1 user@printer"
    Write-Host "  user@printer   remove the service on that machine"
    Write-Host "To uninstall on the printer itself, run bash uninstall.sh there."
}

function Assert-Success($Step) {
    if ($LASTEXITCODE -ne 0) {
        Write-Host "$Step failed (exit code $LASTEXITCODE)." -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

if ($args.Count -eq 1 -and @("-h", "--help", "/?") -contains $args[0]) {
    Show-Usage
    exit 0
}
if ($args.Count -ne 1) {
    Show-Usage
    exit 1
}
$Target = [string]$args[0]
if ($Target -notmatch '^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$') {
    Write-Host "Expected user@printer, for example qidi@printer.local" -ForegroundColor Red
    exit 1
}
foreach ($tool in @("ssh", "scp")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Host "$tool was not found. Turn on OpenSSH Client under Settings > Apps > Optional features." -ForegroundColor Red
        exit 1
    }
}

$Script = Join-Path $PSScriptRoot "uninstall.sh"
if (-not (Test-Path -LiteralPath $Script -PathType Leaf)) {
    Write-Host "Missing $Script" -ForegroundColor Red
    exit 1
}

$Staging = Join-Path ([System.IO.Path]::GetTempPath()) ("filament-manager-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Staging | Out-Null
try {
    $utf8 = New-Object System.Text.UTF8Encoding $false
    $text = [System.IO.File]::ReadAllText($Script, $utf8).Replace("`r`n", "`n")
    [System.IO.File]::WriteAllText((Join-Path $Staging "uninstall.sh"), $text, $utf8)

    Write-Host "Connecting to $Target."
    Write-Host "The password is asked more than once. Each prompt is the next step, not a rejected password."
    Write-Host ""
    Write-Host "Copying the uninstaller. Enter the SSH password."
    # A relative name keeps scp from reading a drive letter such as C: as a host.
    Push-Location $Staging
    try {
        scp "uninstall.sh" "${Target}:/tmp/filament-manager-uninstall.sh"
        Assert-Success "Copying the uninstaller"
    } finally {
        Pop-Location
    }
    Write-Host ""
    Write-Host "Removing the service on the printer. Enter the same password again."
    Write-Host "After you are logged in, sudo asks for it once more."
    ssh -t $Target 'bash /tmp/filament-manager-uninstall.sh --local; status=$?; rm -f /tmp/filament-manager-uninstall.sh; exit $status'
    Assert-Success "The uninstaller on the printer"
} finally {
    Remove-Item -LiteralPath $Staging -Recurse -Force -ErrorAction SilentlyContinue
}
