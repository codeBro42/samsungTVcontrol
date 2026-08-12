@echo off
REM Remove the boot task and firewall rules. Leaves the folder, config, and tokens alone.
REM Run from an Administrator command prompt.

set TASKNAME=SamsungTVBridge

echo Stopping and removing scheduled task "%TASKNAME%"...
schtasks /End /TN "%TASKNAME%" >nul 2>nul
schtasks /Delete /TN "%TASKNAME%" /F

echo Removing firewall rules...
netsh advfirewall firewall delete rule name="tvbridge HTTP" >nul 2>nul
netsh advfirewall firewall delete rule name="tvbridge TCP" >nul 2>nul
netsh advfirewall firewall delete rule name="tvbridge UDP" >nul 2>nul

echo.
echo Done. Tokens and logs are still in %LOCALAPPDATA%\SamsungTVControl\
echo Delete that folder too if you want a completely clean slate.
