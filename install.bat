@echo off
rem ===========================================================================
rem  ContextOS installer for Windows 10/11
rem
rem  Download just this file and double-click it. It will:
rem    1. find Python 3.9+ (and offer to install it with winget if missing)
rem    2. download ContextOS (git if you have it, otherwise a zip)
rem    3. create a private Python environment and install requirements.txt
rem    4. create .env from .env.example, run the self-test
rem    5. offer a desktop shortcut and to start ContextOS
rem
rem  Options:  install.bat /y            accept every default, never prompt
rem            install.bat /dir "D:\X"   install somewhere other than %%USERPROFILE%%\ContextOS
rem            install.bat /nolaunch     don't offer to start at the end
rem  Env:      CONTEXTOS_DIR, CONTEXTOS_REPO (git URL or local path, for forks)
rem
rem  Re-running it updates an existing install. Your .env keys and chats are kept.
rem ===========================================================================
setlocal enabledelayedexpansion
title ContextOS installer

set "REPO_URL=https://github.com/nilanshpratapanand/ContextManager-Context-OS"
if defined CONTEXTOS_REPO set "REPO_URL=%CONTEXTOS_REPO%"
set "ZIP_URL=https://github.com/nilanshpratapanand/ContextManager-Context-OS/archive/refs/heads/main.zip"
set "DIR=%USERPROFILE%\ContextOS"
if defined CONTEXTOS_DIR set "DIR=%CONTEXTOS_DIR%"
set "YES=0"
set "LAUNCH=1"

:args
if "%~1"=="" goto args_done
if /i "%~1"=="/y"        set "YES=1"
if /i "%~1"=="-y"        set "YES=1"
if /i "%~1"=="/nolaunch" set "LAUNCH=0"
if /i "%~1"=="/dir" (
  set "DIR=%~2"
  shift
)
shift
goto args
:args_done

echo.
echo  ContextOS installer   -  context stays, models are disposable
echo  Installing to: %DIR%

rem ------------------------------------------------------------------ python
echo.
echo ==^> Checking for Python 3.9 or newer
call :find_python
if defined PY goto python_ok

echo     ! Python 3.9+ is not installed.
where winget >nul 2>&1
if errorlevel 1 goto python_manual
call :ask "Install Python 3.12 now with winget?" Y
if /i not "!ANSWER!"=="Y" goto python_manual
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
rem winget does not refresh this window's PATH, so look where it installs.
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY call :find_python
if defined PY goto python_ok

:python_manual
echo.
echo     Python 3.9 or newer is required.
echo     1. Download it from https://www.python.org/downloads/
echo     2. On the first installer screen, tick "Add python.exe to PATH"
echo     3. Run this installer again.
goto fail

:python_ok
for /f "delims=" %%v in ('"%PY%" --version 2^>^&1') do echo     OK %%v  [%PY%]

rem ---------------------------------------------------------------- download
echo.
echo ==^> Getting ContextOS
if not exist "%DIR%" mkdir "%DIR%" || goto fail
where git >nul 2>&1
if errorlevel 1 goto zip_download

if exist "%DIR%\.git" (
  git -C "%DIR%" pull --ff-only --quiet || goto fail
  echo     OK Updated the existing copy - git pull
  goto downloaded
)
dir /b /a "%DIR%" 2>nul | findstr "." >nul
if errorlevel 1 (
  git clone --depth 1 --quiet "%REPO_URL%" "%DIR%" || goto fail
  echo     OK Downloaded with git
  goto downloaded
)
rem Folder already has files but isn't a git checkout: copy over it, which
rem leaves .env and chat_data alone because the repo contains neither.
set "TMPSRC=%TEMP%\contextos_src_%RANDOM%"
git clone --depth 1 --quiet "%REPO_URL%" "%TMPSRC%" || goto fail
rd /s /q "%TMPSRC%\.git" 2>nul
xcopy "%TMPSRC%\*" "%DIR%\" /E /Y /I /Q >nul || goto fail
rd /s /q "%TMPSRC%" 2>nul
echo     OK Updated the files in %DIR% - your .env and chats are untouched
goto downloaded

:zip_download
set "TMPZIP=%TEMP%\contextos_%RANDOM%.zip"
set "TMPSRC=%TEMP%\contextos_src_%RANDOM%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing '%ZIP_URL%' -OutFile '%TMPZIP%'; Expand-Archive -Force '%TMPZIP%' '%TMPSRC%'" || goto fail
for /d %%d in ("%TMPSRC%\*") do xcopy "%%d\*" "%DIR%\" /E /Y /I /Q >nul
rd /s /q "%TMPSRC%" 2>nul
del /q "%TMPZIP%" 2>nul
echo     OK Downloaded the source zip

:downloaded
if not exist "%DIR%\contextos\server.py" (
  echo     The download looks incomplete: %DIR%\contextos\server.py is missing.
  goto fail
)
cd /d "%DIR%"

rem ------------------------------------------------------------- environment
echo.
echo ==^> Setting up a private Python environment - .venv
if not exist ".venv\Scripts\python.exe" (
  "%PY%" -m venv .venv || goto fail
)
set "VPY=%DIR%\.venv\Scripts\python.exe"
echo     OK Environment ready: %DIR%\.venv

echo.
echo ==^> Installing requirements
"%VPY%" -m pip install --upgrade --quiet pip >nul 2>&1
"%VPY%" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
  echo     ! Optional packages didn't install. ContextOS still works - token counts are estimated.
) else (
  echo     OK Installed requirements.txt
)

rem ---------------------------------------------------------------- settings
echo.
echo ==^> Settings
if exist ".env" (
  echo     OK .env already exists - your API keys were kept
) else (
  copy /y ".env.example" ".env" >nul
  echo     OK Created .env from .env.example - add at least one free API key to it
)

rem --------------------------------------------------------------- self-test
echo.
echo ==^> Running the self-test
"%VPY%" tests\test_contextos.py > "%TEMP%\contextos-test.log" 2>&1
if errorlevel 1 (
  echo     ! Some tests failed - see %TEMP%\contextos-test.log. The app may still run.
) else (
  for /f "delims=" %%l in ('powershell -NoProfile -Command "(Get-Content '%TEMP%\contextos-test.log')[-1]"') do echo     OK %%l
)

rem ---------------------------------------------------------------- shortcut
if "%YES%"=="1" goto shortcut_done
echo.
call :ask "Put a ContextOS shortcut on your desktop?" Y
if /i not "!ANSWER!"=="Y" goto shortcut_done
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\ContextOS.lnk'); $s.TargetPath='%DIR%\RUN.bat'; $s.WorkingDirectory='%DIR%'; $s.Description='ContextOS chat'; $s.Save()" && echo     OK Desktop shortcut created
:shortcut_done

rem -------------------------------------------------------------------- done
echo.
echo ===============================================================
echo   ContextOS is installed.
echo.
echo   Folder:     %DIR%
echo   Start it:   double-click RUN.bat - or the desktop shortcut
echo   API keys:   in the app, click the key icon at the bottom left
echo   Test keys:  RUN.bat, then press T
echo ===============================================================

if "%LAUNCH%"=="0" goto end
if "%YES%"=="1" goto end
findstr /r /b "[A-Z_]*_API_KEY=." ".env" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   No API keys yet. When the app opens, click "Set up free models" -
  echo   it links to each free provider and saves your keys for you.
)
echo.
call :ask "Start ContextOS now?" Y
if /i "!ANSWER!"=="Y" start "ContextOS" "%DIR%\RUN.bat"
goto end

rem =================================================================== helpers
:find_python
rem Each probe is its own statement: chaining "if ... && set" binds && to the IF.
set "PY="
for %%c in (py python python3) do (
  if not defined PY (
    where %%c >nul 2>&1
    if not errorlevel 1 (
      %%c -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
      if not errorlevel 1 set "PY=%%c"
    )
  )
)
goto :eof

:ask
rem  call :ask "Question?" Y|N   ->  sets ANSWER to Y or N
if "%YES%"=="1" (
  set "ANSWER=%~2"
  goto :eof
)
set "ANSWER="
if /i "%~2"=="Y" (set /p "ANSWER=    %~1 [Y/n] ") else (set /p "ANSWER=    %~1 [y/N] ")
if not defined ANSWER set "ANSWER=%~2"
set "ANSWER=!ANSWER:~0,1!"
if /i "!ANSWER!"=="y" (set "ANSWER=Y") else (set "ANSWER=N")
goto :eof

:fail
echo.
echo  Installation did not finish. Scroll up for the reason.
if "%YES%"=="0" pause
endlocal
exit /b 1

:end
echo.
if "%YES%"=="0" pause
endlocal
exit /b 0
