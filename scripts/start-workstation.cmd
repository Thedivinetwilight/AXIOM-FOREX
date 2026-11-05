@echo off
rem ===================================================================
rem  AXIOM-FOREX workstation launcher (Windows)
rem
rem  Double-click this file, or run it from cmd.
rem
rem  It starts the local workstation on http://127.0.0.1:8765 and opens your
rem  browser. Paper trading (simulated fills on live prices) is the default;
rem  live execution is off until you pass the Trading mode checks and confirm
rem  once, in the browser. It installs nothing, changes no settings, and
rem  never asks for a password.
rem
rem  THE SUPPORTED ENVIRONMENT IS THE PROJECT VIRTUAL ENVIRONMENT .venv.
rem  This script runs .venv\Scripts\python.exe, the same interpreter
rem  scripts\update-workstation.cmd keeps up to date.  It never falls back to
rem  a different interpreter silently: if .venv is missing it says so, prints
rem  exactly what to run, and only continues on a system Python when you
rem  launch it with --system-python.
rem
rem  Set it up once, in this folder:
rem      py -3 -m venv .venv
rem      .venv\Scripts\python.exe -m pip install -e .
rem      .venv\Scripts\python.exe -m pip install MetaTrader5
rem
rem  (AXIOM's own code is standard library only.  The editable install also
rem   brings tzdata on Windows, which the paper platform needs for time-zone
rem   data -- Windows ships no time-zone database of its own.)
rem
rem  Before the first run, sign in to your account inside MetaTrader 5 and
rem  leave the terminal open. AXIOM reuses that session.
rem ===================================================================
setlocal EnableExtensions
cd /d "%~dp0.."

rem -- which interpreter runs this? ------------------------------------
rem    One rule: the project virtual environment, or nothing.
set "PYTHON=%CD%\.venv\Scripts\python.exe"
set "PYTHON_LABEL=the project virtual environment (.venv)"

if exist "%PYTHON%" goto :have_python

rem -- no .venv: say so, and only continue if the operator asks ---------
set "ALLOW_SYSTEM="
for %%a in (%*) do if /i "%%a"=="--system-python" set "ALLOW_SYSTEM=1"
if defined ALLOW_SYSTEM goto :system_python

echo.
echo   The project virtual environment was not found:
echo.
echo       %CD%\.venv\Scripts\python.exe
echo.
echo   AXIOM runs on that environment, and scripts\update-workstation.cmd
echo   updates it.  Create it once, in this folder:
echo.
echo       py -3 -m venv .venv
echo       .venv\Scripts\python.exe -m pip install -e .
echo       .venv\Scripts\python.exe -m pip install MetaTrader5
echo.
echo   Then run this file again.
echo.
echo   If you deliberately use a system-wide Python instead, run:
echo.
echo       scripts\start-workstation.cmd --system-python
echo.
echo   Nothing was changed, installed, or connected.
echo.
pause
exit /b 1

:system_python
set "PYTHON="
where py >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON (
    where python >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
    echo.
    echo   Python was not found on this machine, and there is no .venv.
    echo.
    echo   1. Install Python 3.11 or newer from https://www.python.org/downloads/windows/
    echo      ^(tick "Add python.exe to PATH" in the installer^).
    echo   2. In this folder:  py -3 -m venv .venv
    echo   3. Then:            .venv\Scripts\python.exe -m pip install -e .
    echo   4. Run this file again.
    echo.
    pause
    exit /b 1
)
set "PYTHON_LABEL=a system Python (NOT the project virtual environment)"
echo.
echo   WARNING: running on a system Python because --system-python was given.
echo   The interpreter printed below is what will run AXIOM.  If it is not the
echo   one you installed AXIOM and MetaTrader5 into, stop now and create .venv.
echo.

:have_python

rem -- everything except our own flag goes to the launcher ------------
set "LAUNCH_ARGS="
for %%a in (%*) do if /i not "%%a"=="--system-python" set "LAUNCH_ARGS=%LAUNCH_ARGS% %%a"

rem -- say which interpreter, out loud, before it runs -----------------
rem    This is the line that tells a venv apart from a global Python 3.15.
echo.
echo   Interpreter: %PYTHON_LABEL%
for /f "delims=" %%v in ('%PYTHON% -c "import sys;print(sys.executable, sys.version.split()[0])" 2^>nul') do echo   %%v
echo.

rem -- prerequisites, checked with the same interpreter -----------------
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
    echo   The usual missing item on Windows is tzdata, from a .venv that was
    echo   created before it became a dependency.  Fix it once:
    echo.
    echo       .venv\Scripts\python.exe -m pip install -e .
    echo.
)

echo   Starting the workstation...
echo   ^(your browser opens once the server is listening^)
echo.
%PYTHON% -m axiom_forex.workstation.launch%LAUNCH_ARGS%

if errorlevel 1 (
    echo.
    echo   The workstation stopped with an error. Nothing was changed on this
    echo   machine. Read the message above; the usual causes are:
    echo     - MetaTrader 5 is not open, or is not signed in
    echo     - the MetaTrader5 package is not installed for this interpreter
    echo       ^(printed above: install it with
    echo        .venv\Scripts\python.exe -m pip install MetaTrader5^)
    echo     - tzdata is missing from this interpreter
    echo       ^(.venv\Scripts\python.exe -m pip install -e .^)
    echo     - port 8765 is already in use ^(try --port 8766^)
    echo.
    pause
    exit /b 1
)

endlocal
