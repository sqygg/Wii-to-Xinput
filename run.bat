@echo off
REM Convenience launcher: creates a virtual environment on first run.
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv || goto :error
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt || goto :error
)
start "" .venv\Scripts\pythonw.exe -m wiimote_xinput %*
exit /b 0
:error
echo Setup failed. Make sure Python 3.10+ is installed from python.org.
pause
exit /b 1
