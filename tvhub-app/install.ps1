<#
TVHub - install as a Windows scheduled task that runs at boot as SYSTEM.

Run from an ADMINISTRATOR PowerShell, in the folder this script lives in:

    powershell -ExecutionPolicy Bypass -File .\install.ps1

This is a thin wrapper. It installs the two dependencies for ALL USERS and then
hands over to `python -m tvhub install`, which registers the task, opens the
firewall port, kills any stray process squatting on it, and prints what it did.

Why all-users and not --user: the task runs as SYSTEM. Packages installed with
`pip install --user` under your login are invisible to SYSTEM, and the service
then starts and dies immediately. That failure mode once cost a day; `python -m
tvhub doctor` names it directly if it happens.
#>

[CmdletBinding()]
param(
    # Interpreter to register. Defaults to the pythonw.exe beside the python.exe
    # on PATH, because pythonw runs with no console window.
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Error "This needs elevation. Right-click PowerShell and choose 'Run as Administrator', then run it again."
        exit 1
    }
}

Assert-Admin

if (-not $Python) {
    $found = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $found) {
        Write-Error "python.exe is not on PATH. Install Python 3.9 or newer (tick 'Add python.exe to PATH'), or pass -Python 'C:\Path\to\python.exe'."
        exit 1
    }
    $Python = $found.Source
}

# The floor the whole codebase is written against (contract 0.1).
& $Python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Python 3.9 or newer is required. Found: $(& $Python -V 2>&1)"
    exit 1
}

Write-Host "== installing dependencies for all users"
& $Python -m pip install --upgrade -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Error "pip install failed. Run this from an Administrator prompt so the packages land system-wide, not in your user profile."
    exit 1
}

Write-Host ""
Write-Host "== registering the service"
# TVHUB_HOME pins the machine-wide state folder for this process and every child,
# so the SYSTEM service and a CLI you run later resolve the same pairing tokens
# (contract 1 / I14). This is the exact mistake that once hid 14 valid tokens.
$env:TVHUB_HOME = $here
& $Python -m tvhub install

Write-Host ""
Write-Host "== diagnosis"
& $Python -m tvhub doctor

Write-Host ""
Write-Host "Next steps"
Write-Host "  1. Open the wizard:  http://localhost:8899/ui/setup  (or the port in config.json)"
Write-Host "  2. Set server.base_url to this host's RESERVED address. That string is typed"
Write-Host "     into every TV's browser homepage by hand, so it must never change."
Write-Host "  3. This host must sit on the TVs' own subnet: Wake-on-LAN, pairing and the"
Write-Host "     UPnP volume read-back do not route between subnets."
Write-Host ""
Write-Host "  schtasks /Query /TN TVHub        is it registered?"
Write-Host "  schtasks /End   /TN TVHub        stop it now"
Write-Host "  schtasks /Run   /TN TVHub        start it now"
Write-Host "  .\uninstall.ps1                  remove it, keeping config/state/photos"
