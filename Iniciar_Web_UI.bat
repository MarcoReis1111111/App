@echo off
title App Web UI - Engenharia
cd /d "%~dp0"

echo A parar servidores antigos na porta 8765...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 2 /nobreak >nul

echo.
echo A arrancar App_web_ui_moderno.py ...
python -c "import App_web_ui_moderno as m; print('Versao UI:', m.APP_VERSION)" 2>nul
if errorlevel 1 (
    echo AVISO: nao foi possivel ler a versao. Verifica App_web_ui_moderno.py e _app_web_ui_base.cpython-313.pyc
)
echo.
echo IMPORTANTE: no browser usa Ctrl+Shift+R apos arrancar (limpa cache da UI).
echo.
python App_web_ui_moderno.py --username Nestar --display-name "Nestar" --role admin

pause
