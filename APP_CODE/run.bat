@echo off
REM ESP32 Logic Analyzer - Windows Launcher

cd /d "%~dp0"

REM Check if virtual environment exists
if exist venv\Scripts\activate.bat (
    echo Activating virtual environment...
    call venv\Scripts\activate.bat
)

REM Check dependencies
python -c "import pyqtgraph, PyQt6" 2>nul || (
    echo Dependencies not found. Installing...
    pip install -r requirements.txt
)

REM Run the application
echo Starting ESP32 Logic Analyzer...
python main.py %*