@echo off
REM Install tvbridge on this Windows PC: deps, firewall rules, boot task.
REM Run this from an Administrator command prompt (right-click > Run as administrator).

setlocal
set HERE=%~dp0
set HERE=%HERE:~0,-1%
set TASKNAME=SamsungTVBridge

echo.
echo === tvbridge install ===
echo Folder: %HERE%
echo.

where py >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python launcher "py" not found.
  echo Install Python 3.9+ from https://www.python.org/downloads/windows/
  echo and tick "Add python.exe to PATH".
  goto :fail
)

echo [1/4] Installing Python packages...
py -m pip install --upgrade pip
py -m pip install -r "%HERE%\requirements.txt"
if errorlevel 1 goto :fail

echo.
echo [2/4] Opening firewall ports 8899/TCP, 8900/TCP, 8900/UDP...
netsh advfirewall firewall delete rule name="tvbridge HTTP" >nul 2>nul
netsh advfirewall firewall delete rule name="tvbridge TCP" >nul 2>nul
netsh advfirewall firewall delete rule name="tvbridge UDP" >nul 2>nul
netsh advfirewall firewall add rule name="tvbridge HTTP" dir=in action=allow protocol=TCP localport=8899 profile=any
netsh advfirewall firewall add rule name="tvbridge TCP"  dir=in action=allow protocol=TCP localport=8900 profile=any
netsh advfirewall firewall add rule name="tvbridge UDP"  dir=in action=allow protocol=UDP localport=8900 profile=any

echo.
echo [3/4] Registering scheduled task "%TASKNAME%" to start at boot...
REM pythonw.exe runs it with no console window. /RU SYSTEM so it starts before login.
for /f "delims=" %%P in ('py -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set PYW=%%P
if not exist "%PYW%" set PYW=pythonw.exe
schtasks /Delete /TN "%TASKNAME%" /F >nul 2>nul
schtasks /Create /TN "%TASKNAME%" /SC ONSTART /RU SYSTEM /RL HIGHEST /F ^
  /TR "\"%PYW%\" \"%HERE%\tvbridge.py\" run"
if errorlevel 1 goto :fail

echo.
echo [4/4] Starting it now...
schtasks /Run /TN "%TASKNAME%"

echo.
echo === Done ===
echo.
echo Next steps:
echo   1. Pair each TV (TV must be ON, press ALLOW on the screen):
echo        py "%HERE%\tvbridge.py" pair business
echo   2. Drop photos into %HERE%\photos\default\
echo   3. Check everything:
echo        py "%HERE%\tvbridge.py" doctor
echo        curl http://localhost:8899/help
echo.
echo Log file: %LOCALAPPDATA%\SamsungTVControl\tvbridge.log
echo.
goto :eof

:fail
echo.
echo INSTALL FAILED - see the error above.
exit /b 1
