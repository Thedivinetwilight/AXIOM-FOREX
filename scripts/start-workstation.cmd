@echo off
rem ===================================================================
rem  AXIOM-FOREX workstation launcher (Windows)
rem
rem  Double-click this file, or run it from cmd.
rem
rem  It starts the local, observe-only workstation on http://127.0.0.1:8765
rem  and opens your browser. It installs nothing, changes no settings, and
rem  never asks for a password.
rem
rem  Prerequisites (once, in the same Python):
rem      py -m pip install -e .            (AXIOM itself: standard library only)
rem      py -m pip install MetaTrader5     (talks to your installed terminal)
rem
rem  Before the first run, sign in to your account inside MetaTrader 5 and
rem  leave the terminal open. AXIOM reuses that session.
rem ===================================================================
setlocal
cd /d "%~dp0.."

set "PYTHON="
where py >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON (
    where python >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
    echo.
    echo   Python was not found on this machine.
    echo.
    echo   1. Install Python 3.11 or newer from https://www.python.org/downloads/windows/
    echo      ^(tick "Add python.exe to PATH" in the installer^).
    echo   2. Open a new terminal and run:  py -m pip install -e .
    echo   3. Then:                         py -m pip install MetaTrader5
    echo   4. Run this file again.
    echo.
    pause
    exit /b 1
)

echo.
echo   Checking prerequisites...
echo.
%PYTHON% -m axiom_forex.workstation.launch --print-prerequisites
if errorlevel 1 (
    echo.
    echo   Some prerequisites are missing ^(details above^). The workstation
    echo   can still open in preview mode with sample data:
    echo.
    echo       %PYTHON% -m axiom_forex.workstation.launch --simulate
    echo.
)

echo   Starting the workstation...
echo   ^(your browser opens once the server is listening^)
echo.
%PYTHON% -m axiom_forex.workstation.launch

if errorlevel 1 (
    echo.
    echo   The workstation stopped with an error. Nothing was changed on this
    echo   machine. Read the message above; the usual causes are:
    echo     - MetaTrader 5 is not open, or is not signed in
    echo     - the MetaTrader5 package is not installed for this Python
    echo     - port 8765 is already in use ^(try --port 8766^)
    echo.
    pause
    exit /b 1
)

endlocal
