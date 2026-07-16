@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3.14 -m venv .venv || exit /b 1
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || exit /b 1
)
".venv\Scripts\python.exe" -m alembic upgrade head || exit /b 1
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8020

