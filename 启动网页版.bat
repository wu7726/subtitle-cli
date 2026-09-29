@echo off
rem Smart launcher for the local web UI (double-click friendly).
rem ASCII-only on purpose: cmd parses batch files in the ANSI codepage
rem and UTF-8 Chinese text garbles.
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
title subtitle-cli web UI
cd /d "%~dp0"

set "URL=http://127.0.0.1:8765"

rem 1) Tool already serving? Then do not start a second instance: just open the browser.
call :health
if not errorlevel 1 (
    echo subtitle tool is already running - opening %URL%
    start "" "%URL%"
    exit /b 0
)

rem 2) venv sanity check
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run inside the project folder:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -e .
    pause
    exit /b 1
)

rem 3) Start the server in its own minimized window (kept in the taskbar).
rem    Closing that window stops the tool. --no-open: this script opens the
rem    browser only after the health endpoint answers, so the tab never
rem    hits a half-started server.
echo Starting subtitle tool server (minimized to taskbar)...
start "subtitle-cli server" /min cmd /c "title subtitle-cli server && .venv\Scripts\python.exe web\server.py --no-open"

rem 4) Wait until /api/health answers (max ~20 s).
set /a tries=0
:waitloop
ping -n 2 127.0.0.1 >nul
call :health
if not errorlevel 1 goto started
set /a tries+=1
if %tries% lss 20 goto waitloop

echo [ERROR] server did not come up on %URL% within 20 seconds.
echo Likely port 8765 is held by another program, or the venv is broken.
echo Check the minimized "subtitle-cli server" window for its error output.
pause
exit /b 1

:started
echo Ready. Opening %URL% ...
start "" "%URL%"
echo.
echo Tip: to stop the tool, close the minimized "subtitle-cli server" window,
echo or run the stop script sitting next to this launcher.
ping -n 4 127.0.0.1 >nul
exit /b 0

:health
powershell -NoProfile -Command "try{ $r=Invoke-WebRequest -UseBasicParsing -Uri '%URL%/api/health' -TimeoutSec 2; if($r.Content -match 'subtitle-cli'){ exit 0 } }catch{ }; exit 1"
exit /b %errorlevel%
