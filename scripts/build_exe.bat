@echo off
REM Build a standalone WiimoteXInput.exe (windowed, single file) with PyInstaller.
REM Run from the repository root inside an activated virtual environment:
REM     pip install -r requirements-dev.txt
REM     scripts\build_exe.bat
setlocal
pyinstaller --noconfirm --clean --onefile --windowed ^
  --name WiimoteXInput ^
  --collect-all vgamepad ^
  --hidden-import hid ^
  scripts\launcher.py
if errorlevel 1 exit /b 1
echo.
echo Built dist\WiimoteXInput.exe
endlocal
