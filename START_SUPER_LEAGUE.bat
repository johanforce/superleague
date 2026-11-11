@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Super League Control Center

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [1/3] Creating Python virtual environment...
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv "%~dp0.venv"
    ) else (
        where python >nul 2>nul
        if errorlevel 1 (
            echo Python 3 was not found. Please install Python 3.10 or newer.
            pause
            exit /b 1
        )
        python -m venv "%~dp0.venv"
    )
    if errorlevel 1 (
        echo Failed to create .venv.
        pause
        exit /b 1
    )
)

if not exist "%VENV_PY%" (
    echo Virtual environment Python was not created.
    pause
    exit /b 1
)

echo [2/3] Checking core Python requirements...
"%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo Failed to install requirements. Check your internet connection and Python installation.
    pause
    exit /b 1
)

echo [3/3] Starting Super League Control Center...
"%VENV_PY%" -u "%~dp0src\super_league_hub.py"
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" (
    echo.
    echo Super League exited with code %APP_EXIT%.
    echo Check memory\gui_startup.log if it exists.
    pause
)
exit /b %APP_EXIT%
