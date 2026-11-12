# Install the filament manager on a QIDI printer from Windows.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 user@printer
#
# Uses the ssh and scp that ship with Windows 10 and 11. The files are
# copied with LF line endings, then install.sh runs on the printer.
# SSH asks for the password once per connection, then sudo asks again.
# No password is stored.

$ErrorActionPreference = "Stop"

$Source = $PSScriptRoot
$Files = @(
    "catalog.py",
    "server.py",
    "index.html",
    "install.sh",
    "uninstall.sh",
    "README.md",
    "filament-manager.service"
)

function Show-Usage {
    Write-Host "Usage: powershell -ExecutionPolicy Bypass -File install.ps1 user@printer"
    Write-Host "  user@printer   copy this directory to that machine and install it"
    Write-Host "To install on the printer itself, run bash install.sh there."
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

$Staging = Join-Path ([System.IO.Path]::GetTempPath()) ("filament-manager-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Staging | Out-Null
try {
    $utf8 = New-Object System.Text.UTF8Encoding $false
    foreach ($file in $Files) {
        $path = Join-Path $Source $file
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            Write-Host "Missing $path" -ForegroundColor Red
            exit 1
        }
        $text = [System.IO.File]::ReadAllText($path, $utf8).Replace("`r`n", "`n")
        [System.IO.File]::WriteAllText((Join-Path $Staging $file), $text, $utf8)
    }

    Write-Host "Connecting to $Target."
    Write-Host "The password is asked more than once. Each prompt is the next step, not a rejected password."
    Write-Host "A wrong password is reported as incorrect."
    Write-Host ""
    Write-Host "Creating a temporary directory on the printer. Enter the SSH password."
    ssh $Target "rm -rf /tmp/filament-manager-install && mkdir -p /tmp/filament-manager-install"
    Assert-Success "Creating the temporary directory"
    Write-Host ""
    Write-Host "Copying the app. Enter the same password again."
    # Relative names keep scp from reading a drive letter such as C: as a host.
    Push-Location $Staging
    try {
        scp @Files "${Target}:/tmp/filament-manager-install/"
        Assert-Success "Copying the app"
    } finally {
        Pop-Location
    }
    Write-Host ""
    Write-Host "Running the installer on the printer. Enter the same password again."
    Write-Host "After you are logged in, sudo asks for it once more."
    ssh -t $Target 'bash /tmp/filament-manager-install/install.sh --local; status=$?; rm -rf /tmp/filament-manager-install; exit $status'
    Assert-Success "The installer on the printer"
} finally {
    Remove-Item -LiteralPath $Staging -Recurse -Force -ErrorAction SilentlyContinue
}
