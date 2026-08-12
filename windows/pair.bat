@echo off
REM Pair every unpaired TV at once. Run from anywhere:
REM     C:\tvbridge\pair.bat            all TVs in the 'home' group
REM     C:\tvbridge\pair.bat frames     one group
REM     C:\tvbridge\pair.bat frame-1    one TV
REM Absolute interpreter path so it does not depend on PATH or shell syntax.

setlocal
set PYEXE=C:\Program Files\Python312\python.exe
if not exist "%PYEXE%" set PYEXE=py

set TARGET=%1
if "%TARGET%"=="" set TARGET=home

"%PYEXE%" -u "%~dp0pair_all.py" %TARGET%
endlocal
