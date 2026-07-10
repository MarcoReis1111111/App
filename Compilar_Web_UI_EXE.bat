@echo off
title Compilar Web UI (executavel .exe)
cd /d "%~dp0"

echo == Gerar AppWebUI_Local.exe com PyInstaller ==
echo Isto pode demorar alguns minutos...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0compilar_web_exe.ps1"
exit /b %ERRORLEVEL%
