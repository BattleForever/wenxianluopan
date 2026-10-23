@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" launcher.py
  exit /b
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 launcher.py
  exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
  python launcher.py
  exit /b
)
echo Python 3.11 or newer is required. Install Python, then run start.bat again.
pause
exit /b 1
