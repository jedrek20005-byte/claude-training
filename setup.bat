@echo off
rem One-time setup: creates a Python environment and installs the bot's packages.
cd /d "%~dp0"
py -3 -m venv .venv 2>nul || python -m venv .venv
if not exist ".venv\Scripts\python.exe" (
    echo Could not create the Python environment. Is Python installed and on PATH?
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if not exist .env copy .env.example .env
echo.
echo Setup done. Next: open .env and fill in your keys:   notepad .env
pause
