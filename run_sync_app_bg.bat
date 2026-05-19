@echo off
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "sync_app.py" --tray --autostart
