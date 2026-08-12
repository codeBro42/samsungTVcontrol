<#
TVHub - remove the Windows scheduled task and the firewall rule.

From an ADMINISTRATOR PowerShell:

    powershell -ExecutionPolicy Bypass -File .\uninstall.ps1

config.json, state\ (including the pairing tokens) and photos\ are deliberately
left alone: uninstalling must never lose the pairings, because re-pairing means
walking to every TV with the remote.
#>

[CmdletBinding()]
param([string]$Python = "")

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "This needs elevation. Run PowerShell as Administrator and try again."
    exit 1
}

if (-not $Python) {
    $found = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $found) { Write-Error "python.exe is not on PATH. Pass -Python 'C:\Path\to\python.exe'."; exit 1 }
    $Python = $found.Source
}

$env:TVHUB_HOME = $here
& $Python -m tvhub uninstall
