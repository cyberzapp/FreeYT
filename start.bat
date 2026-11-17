@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 (
  echo Python 3 is required. Install Python from python.org, then run this file again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Setting up the local environment...
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Could not create the local environment. Confirm Python 3 is installed.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -c "import yt_dlp, yt_dlp_ejs" >nul 2>&1
if errorlevel 1 (
  echo Installing yt-dlp. An internet connection is needed for first-time setup.
  ".venv\Scripts\python.exe" -m pip install --upgrade pip "yt-dlp[default]"
  if errorlevel 1 (
    echo Setup failed. Check your internet connection and run start.bat again.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" app.py
pause
