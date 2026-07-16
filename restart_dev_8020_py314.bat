@echo off
setlocal
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8020 .*LISTENING"') do taskkill /PID %%P /F >nul 2>nul
call "%~dp0run_dev_8020_py314.bat"

