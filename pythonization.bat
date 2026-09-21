@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

:: Pythonization launcher — creates .venv on first run, then starts the app.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ and add to PATH.
    pause
    exit /b 1
)

:: Create venv if missing
if not exist ".venv\Scripts\activate.bat" (
    echo [*] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created.
)

:: Activate venv
call ".venv\Scripts\activate.bat"

:: Required packages live in requirements.txt (single source of truth).
:: Optional ones -- zhinst for the MFLI module, scipy for the Savitzky-Golay
:: derivative filter -- are in requirements-optional.txt and are NOT installed
:: automatically. The app runs fine without them.
echo [*] Installing required packages...
pip install --upgrade --quiet -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Package installation failed. Check your internet connection.
    pause
    exit /b 1
)
echo [OK] Packages ready.

:: Launch
echo [*] Starting Pythonization...
python main.py

if errorlevel 1 (
    echo.
    echo [ERROR] Program exited with an error.
    echo         See the log at: %%USERPROFILE%%\Documents\pythonization\settings\logs\app.log
    pause
)

endlocal
