@echo off
REM Record a remote-key macro for a TV. Run from anywhere:
REM     C:\tvbridge\learn.bat frame-75
REM Uses an absolute interpreter path so it does not depend on PATH or on the
REM shell supporting && (Windows PowerShell 5.1 does not).

setlocal
set PYEXE=C:\Program Files\Python312\python.exe
if not exist "%PYEXE%" set PYEXE=py

set ALIAS=%1
if "%ALIAS%"=="" set ALIAS=frame-75

"%PYEXE%" -u "%~dp0tvbridge.py" learn %ALIAS%
endlocal
