@echo off
title Compilar Web UI - Pacote para utilizadores
cd /d "%~dp0"

echo == Compilar Web UI e criar pacote em [0]Programas_prontos ==
echo Isto pode demorar alguns minutos (PyInstaller)...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0compilar_web_ui_pacote.ps1"
exit /b %ERRORLEVEL%
