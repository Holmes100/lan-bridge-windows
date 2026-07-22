@echo off
REM Build lan-gateway-win.py into lan-gateway.exe (bundled as a Tauri resource).
REM Run on Windows from the project root (lan-bridge-windows), in a Developer/Powershell
REM prompt that has Python on PATH. Produces lan-gateway.exe next to this script.
setlocal
cd /d "%~dp0"

echo [1/3] Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 ( echo pip install FAILED & exit /b 1 )

echo [2/3] Running PyInstaller...
python -m PyInstaller --onefile --clean --noconfirm --name lan-gateway lan-gateway-win.py
if errorlevel 1 ( echo PyInstaller FAILED & exit /b 1 )

echo [3/3] Copying to project root for tauri bundle...
copy /Y "dist\lan-gateway.exe" "lan-gateway.exe" >nul
if errorlevel 1 ( echo copy FAILED & exit /b 1 )

echo.
echo Done: lan-gateway.exe built. Next:  npm install  ^&  npm run tauri build
echo (If runtime later raises ImportError on cryptography, rebuild with:
echo   python -m PyInstaller --onefile --collect-all cryptography --name lan-gateway lan-gateway-win.py)
endlocal
