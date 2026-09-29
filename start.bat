@echo off
cd /d "%~dp0"

echo Checking Python...
python --version 2>nul
if errorlevel 1 (
    echo ERROR: Python not found. Install from https://python.org
    pause
    exit /b 1
)

echo Installing dependencies...
pip install -r requirements.txt -q

echo.
echo Starting Search Demand Explorer on http://127.0.0.1:5000
echo Press Ctrl+C to stop.
echo.

start "" "http://127.0.0.1:5000"
python app.py
pause
