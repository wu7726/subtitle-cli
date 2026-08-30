@echo off
rem Launch the local web UI (double-click friendly). ASCII-only on purpose:
rem cmd parses batch files in the ANSI codepage and UTF-8 Chinese text garbles.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run inside the project folder:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -e .
    pause
    exit /b 1
)
echo Starting subtitle tool at http://127.0.0.1:8765 ...
echo (A browser window will open automatically. Close this window to stop.)
.venv\Scripts\python.exe web\server.py
echo.
echo Server stopped.
pause
