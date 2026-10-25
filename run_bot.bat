@echo off
rem Runs the bot once. Extra options are passed through, e.g.  run_bot.bat --dry-run
cd /d "%~dp0"
".venv\Scripts\python.exe" -m bot.run_daily %*
