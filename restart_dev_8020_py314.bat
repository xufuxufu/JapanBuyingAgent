@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restart_8020.ps1"
exit /b %ERRORLEVEL%

