@echo off
REM TVHub - install as a Windows scheduled task (boot, SYSTEM).
REM
REM Right-click this file and choose "Run as administrator", or from an
REM Administrator command prompt:  install.bat
REM
REM It only forwards to install.ps1, which does the real work. Batch is here
REM because double-clicking a .ps1 opens Notepad instead of running it.

setlocal
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo ERROR this needs elevation.
    echo       Right-click install.bat and choose "Run as administrator".
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
