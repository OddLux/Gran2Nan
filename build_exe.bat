@echo off
rem Builds dist\Gran2Nan.exe - a single file that runs on any 64-bit Windows 10/11 PC
rem without Python installed. Copy it anywhere; it keeps config.json next to itself.
cd /d "%~dp0"
if not exist .venv (
    python -m venv .venv || goto :error
)
.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt pyinstaller || goto :error
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name Gran2Nan ^
    --collect-submodules bleak --collect-submodules winrt ^
    Gran2Nan.pyw || goto :error
echo.
echo Built: %~dp0dist\Gran2Nan.exe
exit /b 0
:error
echo Build failed.
exit /b 1
