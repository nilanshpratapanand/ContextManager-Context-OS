@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title ContextOS

rem ---------------------------------------------------------------- find python
rem Each probe is its own statement: chaining "if ... && set" on one line binds
rem the && to the IF, not to WHERE, and silently picks the wrong interpreter.
set "PY="
rem The installer's private environment wins over any system Python.
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
  where py >nul 2>&1
  if not errorlevel 1 set "PY=py"
)
if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  where python3 >nul 2>&1
  if not errorlevel 1 set "PY=python3"
)
if not defined PY goto nopython

rem confirm it actually runs (a stub python.exe from the Store does not)
%PY% -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
if errorlevel 1 goto badversion

rem ------------------------------------------------- update to the latest release
rem Checks GitHub for a newer release (5 s timeout, silent when offline) and
rem installs it over this folder. .env, chats and keys are never touched.
rem Skip with:  set CONTEXTOS_NO_UPDATE=1   The block below is parsed as a whole,
rem so it is safe for the update to replace this very file while it runs.
if defined CONTEXTOS_NO_UPDATE goto noupdate
if /i "%~1"=="/noupdate" goto noupdate
echo Checking for a newer ContextOS release...
%PY% -m contextos.update
if errorlevel 10 (
  echo.
  echo Restarting on the new version...
  timeout /t 2 /nobreak >nul
  call "%~f0" /noupdate
  exit /b
)
:noupdate

rem Drop any stale bytecode so an updated .py can never be shadowed by an old .pyc.
if exist "contextos\__pycache__" rd /s /q "contextos\__pycache__" 2>nul
if exist "tests\__pycache__" rd /s /q "tests\__pycache__" 2>nul

:menu
cls
echo ===============================================================
echo   ContextOS
echo   The state layer that makes trajectory-drop handoffs work
echo ===============================================================
echo.
echo   Using: %PY%
echo   Folder: %cd%
echo.
echo   ---- DASHBOARD (start here) --------------------------------
echo   1.  Chat - REAL models            uses the keys in .env
echo   T.  Test my providers             one real call to each model
echo   M.  List usable models            ask providers what your keys allow
echo   2.  Chat - no keys                simulated, only if 1 fails
echo.
echo   ---- see the research ----------------------------------
echo   3.  Demo              the whole story in the terminal
echo   4.  Benchmark         offline sufficiency table
echo   5.  Tests             64 tests, proves it works
echo.
echo   ---- live models -------------------------------------------
echo   6.  Check API keys    which keys .env actually has
echo   7.  Live dry run      verify harness, no API calls
echo   8.  Live evaluation   real models, uses your API keys
echo.
echo   ---- play with it ------------------------------------------
echo   9.  Sample project    seed a store and try a handoff
echo   C.  Command prompt    run contextos commands yourself
echo   U.  Update            get the latest release now
echo   R.  Reset             delete ALL chats and local databases
echo.
echo   0.  Exit
echo.
set "choice="
set /p "choice=Select: "

if /i "%choice%"=="1" goto dashlive
if /i "%choice%"=="2" goto dashoff
if /i "%choice%"=="T" goto check
if /i "%choice%"=="M" goto models
if /i "%choice%"=="3" goto demo
if /i "%choice%"=="4" goto bench
if /i "%choice%"=="5" goto tests
if /i "%choice%"=="6" goto keys
if /i "%choice%"=="7" goto dryrun
if /i "%choice%"=="8" goto livereal
if /i "%choice%"=="9" goto sample
if /i "%choice%"=="C" goto shell
if /i "%choice%"=="U" goto update
if /i "%choice%"=="R" goto reset
if /i "%choice%"=="0" exit /b 0
goto menu

:check
cls
if not exist ".env" (
  echo No .env file found. Put it in:  %cd%
  goto done
)
echo Calling every provider in your .env once. This uses your real keys.
echo Running from: %cd%
echo.
%PY% -m contextos.live --check
echo.
echo Anything marked FAIL usually means the model name moved. Tell me which
echo one failed and what it said, and the name can be corrected in .env.
goto done

:models
cls
if not exist ".env" (
  echo No .env file found. Put it in:  %cd%
  goto done
)
echo Asking each provider which models your key can actually use.
echo A * marks the one currently configured.
echo.
%PY% -m contextos.live --models
echo.
echo To change one, add a line to .env, for example:
echo    GROQ_MODEL=llama-3.3-70b-versatile
goto done

:dashoff
cls
echo Starting the chat with simulated replies - no API keys used.
echo Your browser will open at http://127.0.0.1:8000
echo.
echo Try this once it loads:
echo   1. Type a task, for example "build OAuth2 login, no new dependencies"
echo   2. Open "Models" at the bottom left and click a model to make it fail
echo   3. Send another message - watch it switch models and carry on
echo.
echo Press Ctrl+C in this window to stop the server.
echo.
%PY% -m contextos.server --offline
goto done

:dashlive
cls
if not exist ".env" (
  echo No .env file found. Put it in:  %cd%
  echo Then pick option 1 again. Or use option 2, which needs no keys.
  goto done
)
echo Starting the chat with your real providers from .env.
echo Your browser will open at http://127.0.0.1:8000
echo.
echo This makes real API calls. Press Ctrl+C in this window to stop.
echo.
%PY% -m contextos.server
goto done

:demo
cls
%PY% -m contextos.demo
goto done

:bench
cls
echo Running all three handoff directions...
echo.
for %%d in (escalate downshift lateral) do (
  %PY% -m contextos.bench --direction %%d
  echo.
)
goto done

:tests
cls
%PY% tests\test_contextos.py
goto done

:keys
cls
if not exist ".env" (
  echo No .env file found in this folder.
  echo.
  echo Put your .env here:  %cd%
  echo It needs at least one line such as:
  echo    GROQ_API_KEY=your_key_here
  echo.
  goto done
)
%PY% -m contextos.live --list-providers
goto done

:dryrun
cls
echo Harness self-test. No API calls, no keys needed.
echo.
%PY% -m contextos.live --dry-run
goto done

:livereal
cls
if not exist ".env" (
  echo No .env file found. Put it in:  %cd%
  goto done
)
%PY% -m contextos.live --list-providers
echo.
echo Pick two providers from the list above that show "yes".
echo.
set "pa=groq"
set "pb=gemini"
set /p "pa=Provider that STARTS the task [groq]: "
set /p "pb=Provider that FINISHES it [gemini]: "
if "!pa!"=="" set "pa=groq"
if "!pb!"=="" set "pb=gemini"
set "reps=1"
set /p "reps=Repeats per task [1]: "
if "!reps!"=="" set "reps=1"
echo.
echo Running: !pa! -^> !pb!, !reps! repeat(s). This makes real API calls.
echo.
%PY% -m contextos.live --a !pa! --b !pb! --repeats !reps!
goto done

:sample
cls
if exist "contextos.db" del /q "contextos.db"
echo Seeding a sample project...
echo.
%PY% -m contextos.cli put /task/goal "Replace cookie sessions with OAuth2 in the billing API" --kind goal --importance 1.0 --pinned
%PY% -m contextos.cli put /project/constraints/no-new-deps "No new runtime dependencies without architecture review" --kind constraint --importance 0.95
%PY% -m contextos.cli put /project/decisions/oauth-lib "Use authlib, not oauthlib - authlib is already vendored" --kind decision --source architect --importance 0.9
%PY% -m contextos.cli put /task/blockers/redis-acl "Redis in staging has no ACL; revocation list cannot be written" --kind blocker --importance 0.8
%PY% -m contextos.cli put /tool/search/rfc "RFC 6749 section 4.1 authorization code flow notes" --kind tool_result --importance 0.1
echo Adding the tool chatter a real session accumulates...
for /L %%i in (1,1,15) do @%PY% -m contextos.cli put /tool/search/hit%%i "Search result %%i: retry timeout handler cursor socket index schema worker cache token grant scope nonce claim refresh revoke introspect bearer opaque jwks discovery" --kind tool_result --importance 0.1 >nul
echo.
echo --- Asking: what is blocking us ------------------------------
%PY% -m contextos.cli search "what is blocking us right now"
echo.
echo --- Handoff packet, weak model to strong model ---------------
%PY% -m contextos.cli handoff --direction escalate --budget 2000 --from-model haiku --to-model opus --difficulty 0.75
echo.
echo Store saved as contextos.db - option 8 lets you keep exploring it.
goto done

:shell
cls
echo ContextOS command prompt. Examples:
echo.
echo   %PY% -m contextos.cli ls
echo   %PY% -m contextos.cli put /project/decisions/db "PostgreSQL 16" --kind decision
echo   %PY% -m contextos.cli search "what database did we pick"
echo   %PY% -m contextos.cli handoff --direction escalate --budget 2000
echo   %PY% -m contextos.cli stats
echo   %PY% -m contextos.cli conflicts
echo.
echo Type "exit" to return to the menu.
echo.
cmd /k prompt contextos$G$S
goto menu

:update
cls
%PY% -m contextos.update --check
%PY% -m contextos.update
if errorlevel 10 (
  echo.
  echo Updated. Close this window and run RUN.bat again to start the new version.
  pause
  exit /b 0
)
goto done

:reset
cls
set "gone=0"
for %%f in (contextos.db dashboard.db) do (
  if exist "%%f" (
    del /q "%%f" "%%f-wal" "%%f-shm" 2>nul
    echo Deleted %%f
    set "gone=1"
  )
)
if exist "chat_data" (
  rmdir /s /q "chat_data"
  echo Deleted chat_data - all chats and their memory
  set "gone=1"
)
if "!gone!"=="0" echo No databases to delete.
goto done

:nopython
cls
echo ===============================================================
echo   Python was not found on this computer.
echo ===============================================================
echo.
echo ContextOS needs Python 3.9 or newer. It has no other
echo dependencies - nothing else to install.
echo.
echo   1. Get it from https://www.python.org/downloads/
echo   2. On the first screen of the installer, tick
echo      "Add python.exe to PATH" - this is the step people miss.
echo   3. Close this window, then run RUN.bat again.
echo.
pause
exit /b 1

:badversion
cls
echo Python was found, but it is older than 3.9 or is the
echo Microsoft Store placeholder.
echo.
%PY% --version
echo.
echo Install Python 3.9+ from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH".
echo.
pause
exit /b 1

:done
echo.
echo ---------------------------------------------------------------
pause
goto menu
