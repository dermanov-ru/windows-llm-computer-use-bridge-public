@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Install dependencies first: python -m venv .venv
  echo .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)
".venv\Scripts\python.exe" admin_launcher.py %*
if errorlevel 1 pause
