@echo off
rem Run from source (needs Python 3.10+). First run creates the virtual environment.
cd /d "%~dp0"
if not exist .venv (
    python -m venv .venv || exit /b 1
    .venv\Scripts\python.exe -m pip install -r requirements.txt || exit /b 1
)
start "" .venv\Scripts\pythonw.exe Gran2Nan.pyw %*
