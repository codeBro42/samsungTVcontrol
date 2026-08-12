# Print any slideshow-fetch log lines for a given client IP, newest last.
# Usage: powershell -File check_fetch.ps1 192.168.1.203
param([string]$Ip = "")

$log = "C:\Windows\System32\config\systemprofile\AppData\Local\SamsungTVControl\tvbridge.log"
if (-not (Test-Path $log)) { Write-Output "NOLOG"; exit 0 }

$lines = Select-String -Path $log -Pattern "slideshow being fetched" -ErrorAction SilentlyContinue
if ($Ip) { $lines = $lines | Where-Object { $_.Line -like "*$Ip*" } }

if ($lines) { $lines | ForEach-Object { $_.Line.Trim() } } else { Write-Output "NONE" }
