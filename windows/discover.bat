@echo off
REM Scan the LAN for Samsung TVs and report any not yet in config.json.
REM     C:\tvbridge\discover.bat
setlocal
set PYEXE=C:\Program Files\Python312\python.exe
if not exist "%PYEXE%" set PYEXE=py
"%PYEXE%" -u "%~dp0discover.py" %1
endlocal
