@echo off
rem ===================================================================
rem  AXIOM-FOREX workstation updater (Windows)
rem
rem  Updates an EXISTING clone in place, on the feature branch:
rem
rem      git fetch origin <branch>
rem      git merge --ff-only origin/<branch>      (fast-forward only)
rem      .venv\Scripts\python.exe -m pip install -e . --no-deps
rem      .venv\Scripts\python.exe -c "import ..." (post-update import check)
rem
rem  What it will never do:
rem    * delete, move, rename, reset, clean, stash, checkout or reclone
rem    * touch mt5_settings.local.json or any other untracked / ignored file
rem    * recreate, delete, or reinstall the .venv directory
rem    * overwrite a file you changed (a tracked change stops it up front)
rem    * connect to MetaTrader 5, read a credential, or trade
rem
rem  It stops BEFORE changing anything when:
rem    * Git is not on PATH, or this folder is not a Git working tree
rem    * the checked-out branch is not the feature branch (or is detached)
rem    * any TRACKED file has local changes (untracked files are fine)
rem    * the branch cannot fast-forward (histories have diverged)
rem
rem  Close the running workstation first: press Ctrl+C in the console
rem  window that is running scripts\start-workstation.cmd.
rem ===================================================================
setlocal EnableExtensions

set "EXPECTED_BRANCH=arena/f67c2859-axiom-forex"
set "REPO_DIR=%~dp0.."

cd /d "%REPO_DIR%"
if errorlevel 1 goto :no_folder

echo.
echo   AXIOM-FOREX workstation updater
echo   ---------------------------------------------------------------
echo   Folder:  %CD%
echo   Branch:  %EXPECTED_BRANCH%
echo.
echo   Close the running workstation before continuing
echo   (Ctrl+C in the console running scripts\start-workstation.cmd).
echo   Nothing is deleted or overwritten by this script.
echo.

rem -- 1. Git must exist ------------------------------------------------
where git >nul 2>nul
if errorlevel 1 goto :no_git

rem -- 2. This must be a Git working tree -------------------------------
git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 goto :no_repo

rem -- 3. It must be the feature branch --------------------------------
git rev-parse --verify --quiet HEAD >nul 2>nul
if errorlevel 1 goto :no_repo

set "CURRENT_BRANCH="
for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set "CURRENT_BRANCH=%%b"
if not defined CURRENT_BRANCH goto :no_repo
if /i "%CURRENT_BRANCH%"=="HEAD" goto :detached
if /i not "%CURRENT_BRANCH%"=="%EXPECTED_BRANCH%" goto :wrong_branch

rem -- 4. No tracked file may have local changes -----------------------
rem    Untracked and ignored files (your settings, databases, exports)
rem    are deliberately not considered: the update leaves them alone.
set "DIRTY="
for /f "delims=" %%f in ('git status --porcelain --untracked-files=no 2^>nul') do if not defined DIRTY set "DIRTY=%%f"
if defined DIRTY goto :dirty

rem -- 5. Fetch, then fast-forward only --------------------------------
set "BEFORE="
for /f "delims=" %%s in ('git rev-parse --short HEAD 2^>nul') do set "BEFORE=%%s"

echo   Fetching %EXPECTED_BRANCH% from origin...
git fetch origin "%EXPECTED_BRANCH%"
if errorlevel 1 goto :fetch_failed

git rev-parse --verify --quiet "origin/%EXPECTED_BRANCH%" >nul 2>nul
if errorlevel 1 goto :no_remote_branch

echo   Fast-forwarding (merge --ff-only)...
git merge --ff-only "origin/%EXPECTED_BRANCH%"
if errorlevel 1 goto :not_fast_forward

set "AFTER="
for /f "delims=" %%s in ('git rev-parse --short HEAD 2^>nul') do set "AFTER=%%s"

echo.
if /i "%BEFORE%"=="%AFTER%" (
    echo   Already up to date at %AFTER%.
) else (
    echo   Updated %BEFORE% -^> %AFTER%
)
echo.

rem -- 6. Refresh the project in the existing .venv --------------------
set "VENV_PY=%CD%\.venv\Scripts\python.exe"
if not exist "%VENV_PY%" goto :no_venv

echo   Refreshing the project in .venv ...
"%VENV_PY%" -m pip install -e . --no-deps --disable-pip-version-check
if errorlevel 1 goto :pip_failed

rem -- 7. Post-update check: the updated packages must import ---------
echo   Checking that the updated code imports ...
"%VENV_PY%" -c "import axiom_forex, axiom_forex.workstation.app, axiom_forex.workstation.session, axiom_forex.workstation.dataflow, axiom_forex.paper_runner.runner, axiom_forex.paper_runner.host, axiom_forex.paper_runner.provider, axiom_forex.paper_runner.live, axiom_forex.brokers.mt5.catalogue, axiom_forex.ingest.mt5_ticks" >nul 2>nul
if errorlevel 1 goto :verify_failed

rem -- 8. MetaTrader 5 package: report it, never install it ------------
rem    That package is installed by YOU and only when you want the live
rem    path. This script never installs anything outside .venv.
set "MT5_NOTE=not installed (paper and research only until you install it)"
"%VENV_PY%" -c "import MetaTrader5" >nul 2>nul
if not errorlevel 1 set "MT5_NOTE=installed"

echo.
echo   Done.
echo     branch  %EXPECTED_BRANCH%
echo     commit  %AFTER%
echo     python  %VENV_PY%
echo     MetaTrader5 package: %MT5_NOTE%
echo.
echo   mt5_settings.local.json, provider_setup.local.json, .venv,
echo   paper_runner.local.sqlite3, imports\,
echo   and your other files were left exactly as they were.
echo.
echo   Start the workstation with:
echo.
echo       scripts\start-workstation.cmd
echo.
echo   Broker ticks are read from imports\ on the Data ^& evaluation page:
echo   drop the MT5 tick export there, press Re-scan, declare the server's
echo   time basis, convert it, then evaluate. Nothing is downloaded for you.
echo.
pause
exit /b 0

rem ====================================================================
rem  Refusals: each one exits non-zero WITHOUT having changed anything.
rem ====================================================================

:no_folder
echo.
echo   Could not open the project folder ^(%~dp0..^).
echo   Nothing was changed.
echo.
pause
exit /b 1

:no_git
echo.
echo   Git was not found on this PC, so the branch cannot be updated.
echo.
echo   Install Git for Windows from https://git-scm.com/download/win, open a
echo   NEW terminal, and run this script again.
echo.
echo   Nothing was changed.
echo.
pause
exit /b 1

:no_repo
echo.
echo   This folder is not a Git working tree, so there is nothing to update in place.
echo.
echo   Run this script from inside your existing clone, or clone it once:
echo.
echo       git clone --branch %EXPECTED_BRANCH% ^<repository-url^>
echo.
echo   Nothing was changed.
echo.
pause
exit /b 1

:detached
echo.
echo   HEAD is detached, so this is not on the feature branch.
echo.
echo   Switch back first:
echo.
echo       git switch %EXPECTED_BRANCH%
echo.
echo   Nothing was changed.
echo.
pause
exit /b 1

:wrong_branch
echo.
echo   This clone is on branch "%CURRENT_BRANCH%", not on the expected
echo   branch "%EXPECTED_BRANCH%".
echo.
echo   Switch to the feature branch first:
echo.
echo       git switch %EXPECTED_BRANCH%
echo.
echo   Nothing was changed.
echo.
pause
exit /b 1

:dirty
echo.
echo   You have local changes to tracked files, so the update was not started.
echo   First reported change:
echo.
echo       %DIRTY%
echo.
echo   Commit or discard those changes yourself, then run this script again.
echo   This script never stashes, resets, or discards your work. Your
echo   mt5_settings.local.json and other untracked files are not affected.
echo.
echo   Nothing was changed.
echo.
pause
exit /b 1

:fetch_failed
echo.
echo   Could not fetch from origin. The network or the remote may be unavailable.
echo.
echo   Nothing was changed; your current commit is still %BEFORE%.
echo.
pause
exit /b 1

:no_remote_branch
echo.
echo   origin does not have a branch named "%EXPECTED_BRANCH%".
echo   This clone may point at a different repository.
echo.
echo       git remote -v
echo.
echo   Nothing was changed; your current commit is still %BEFORE%.
echo.
pause
exit /b 1

:not_fast_forward
echo.
echo   The branch cannot be fast-forwarded, so it was left exactly as it was.
echo.
echo   Git's own message above says why: the remote history either does not
echo   contain your current commit, or the update would overwrite a file you
echo   are keeping. This script never forces, merges, resets, or discards.
echo.
echo   Your current commit is still %BEFORE%.
echo.
pause
exit /b 1

:no_venv
echo.
echo   The update itself succeeded, but there is no .venv in this folder:
echo.
echo       %CD%\.venv\Scripts\python.exe
echo.
echo   Create it once, then run this script again to refresh it:
echo.
echo       py -3 -m venv .venv
echo       .venv\Scripts\python.exe -m pip install -e .
echo.
echo   Nothing was deleted; mt5_settings.local.json is untouched.
echo.
pause
exit /b 1

:pip_failed
echo.
echo   The code update succeeded ^(now at %AFTER%^), but refreshing .venv failed.
echo   Read the pip message above; the usual causes are no network access or a
echo   Python in .venv that has no pip.
echo.
echo   Your settings and files are untouched. You can retry with:
echo.
echo       .venv\Scripts\python.exe -m pip install -e .
echo.
pause
exit /b 1

:verify_failed
echo.
echo   The code update succeeded ^(now at %AFTER%^) and the packages were
echo   refreshed, but the updated code did NOT import cleanly.
echo.
echo   This script changes nothing else and rolls nothing back. Your settings
echo   and data files are untouched. Please report the output of:
echo.
echo       .venv\Scripts\python.exe -c "import axiom_forex.workstation.app"
echo.
pause
exit /b 1
