@echo off
cd /d "%~dp0"
setlocal
title Trainer Control
set "PYEXE="

where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
  python -c "import sys" >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  echo.
  echo   [!] No usable Python 3 found.
  echo       Install from https://www.python.org/downloads/
  echo       and tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

echo Using: %PYEXE%
%PYEXE% server.py

if errorlevel 1 (
  echo.
  echo Server exited with an error. See crash.log
  pause
)
endlocal