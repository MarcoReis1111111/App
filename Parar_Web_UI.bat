@echo off
title Parar Web UI - Engenharia
cd /d "%~dp0"

echo A parar Web UI...
taskkill /F /IM AppWebUI_Local.exe >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 2 /nobreak >nul
echo Web UI parada.
pause
