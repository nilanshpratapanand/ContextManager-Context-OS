@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title ContextOS - publish

rem ===============================================================
rem  publish.bat - commit, push and (optionally) publish a release.
rem  Needs:  git   (https://git-scm.com)
rem          gh    (https://cli.github.com, run "gh auth login" once)
rem  Usage:  publish.bat                 asks for everything
rem          publish.bat "commit msg"    commits with that message
rem ===============================================================

where git >nul 2>&1
if errorlevel 1 ( echo git was not found. Install it from https://git-scm.com & pause & exit /b 1 )
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 ( echo This folder is not a git repository. & pause & exit /b 1 )

rem ---------------------------------------------------- safety: never publish secrets
git ls-files --error-unmatch .env >nul 2>&1
if not errorlevel 1 (
  echo STOP: .env is tracked by git, so your API keys would be published.
  echo Run:  git rm --cached .env   then commit, and regenerate any key that was pushed.
  pause & exit /b 1
)

echo.
echo ---- Changes ------------------------------------------------
git status --short
echo -------------------------------------------------------------

rem ------------------------------------------------------- commit
set "MSG=%~1"
git status --porcelain > "%TEMP%\ctxos_status.txt"
for %%A in ("%TEMP%\ctxos_status.txt") do set "SZ=%%~zA"
if "!SZ!"=="0" (
  echo Nothing to commit.
) else (
  if "!MSG!"=="" set /p "MSG=Commit message: "
  if "!MSG!"=="" set "MSG=update"
  git add -A
  git commit -m "!MSG!"
  if errorlevel 1 ( echo Commit failed. & pause & exit /b 1 )
)

rem ------------------------------------------------------- push
for /f %%B in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%B"
echo.
echo Pulling the latest changes first...
git pull --rebase --autostash origin !BRANCH!
if errorlevel 1 ( echo Pull failed - fix conflicts, then run this again. & pause & exit /b 1 )
git push -u origin !BRANCH!
if errorlevel 1 ( echo Push failed. & pause & exit /b 1 )
echo Pushed to !BRANCH!.

rem ------------------------------------------------------- release
echo.
set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY ( where python >nul 2>&1 && set "PY=python" )
if not defined PY ( echo Python not found - skipping the release step. & goto end )
for /f %%V in ('!PY! -c "import re;print(re.search(r'__version__\s*=\s*.([0-9.]+)',open('contextos/__init__.py').read()).group(1))"') do set "VER=%%V"
echo Version in contextos\__init__.py:  v!VER!

where gh >nul 2>&1
if errorlevel 1 ( echo GitHub CLI "gh" not found - install from https://cli.github.com to publish releases. & goto end )

set "ANS="
set /p "ANS=Publish release v!VER! now? [y/N]: "
if /i not "!ANS!"=="y" goto end

gh release view v!VER! >nul 2>&1
if not errorlevel 1 (
  echo Release v!VER! already exists. Change __version__ in contextos\__init__.py, commit, and run again.
  goto end
)

set "NOTES=RELEASE_NOTES.md"
if exist "!NOTES!" (
  gh release create v!VER! install.bat install.sh --target !BRANCH! --title "ContextOS !VER!" --notes-file "!NOTES!"
) else (
  gh release create v!VER! install.bat install.sh --target !BRANCH! --title "ContextOS !VER!" --generate-notes
)
if errorlevel 1 ( echo Release failed. Check "gh auth status". & pause & exit /b 1 )
echo.
echo Release v!VER! published with install.bat and install.sh.

:end
echo.
pause
