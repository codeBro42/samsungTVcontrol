@echo off
REM Samsung TV Power Control launcher - keeps the window open on exit or error.
cd /d "%~dp0"
where py >nul 2>nul && (py tv_control.py) || (python tv_control.py)
echo.
echo ----------------------------------
pause
