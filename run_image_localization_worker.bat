@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv\Scripts\python.exe 1>&2
  exit /b 1
)
".venv\Scripts\python.exe" -m scripts.run_image_localization_worker %*
