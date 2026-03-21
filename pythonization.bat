@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

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

:: Install / upgrade packages
echo [*] Installing packages...
pip install --upgrade --quiet PySide6 pyvisa numpy pyqtgraph pydantic PyYAML
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
    pause
)

endlocal
