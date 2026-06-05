@echo off
title HireLab Screener
color 0A

echo.
echo  ====================================================
echo    HireLab Screener - Starting
echo  ====================================================
echo.

:: Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found!
    echo.
    echo  Please install Python from: https://python.org
    echo  Make sure to check "Add Python to PATH" during install
    echo.
    pause
    exit /b 1
)

echo  Python found. Installing packages...
python -m pip install flask flask-cors requests pdfplumber python-docx --quiet --no-warn-script-location

echo.
echo  ====================================================
echo   YOUR DATA IS SAFE AT:
echo   %USERPROFILE%\HireLab\
echo  ====================================================
echo.
echo  Starting server...
echo  Open browser at: http://localhost:5000
echo.
echo  Press Ctrl+C to stop the server.
echo  ====================================================
echo.

:: Change to the folder where this bat file is
cd /d "%~dp0"

:: Kill anything on port 5000 first (clean start)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5000" 2^>nul') do (
    taskkill /f /pid %%a >nul 2>&1
)

:: Start the server
python server.py

echo.
echo  Server stopped.
pause
