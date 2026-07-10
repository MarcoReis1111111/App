@echo off
title App Web UI (executavel) - Engenharia
cd /d "%~dp0"

set "EXE=dist\AppWebUI_Local\AppWebUI_Local.exe"
set "SRC_CFG=AppEngenhariaCache\config.json"
set "DST_CACHE=dist\AppWebUI_Local\AppEngenhariaCache"
set "DST_CFG=%DST_CACHE%\config.json"

if not exist "%EXE%" (
    echo Executavel em falta: %EXE%
    echo Corre primeiro: Compilar_Web_UI_EXE.bat
    pause
    exit /b 1
)

if exist "%SRC_CFG%" (
    if not exist "%DST_CACHE%" mkdir "%DST_CACHE%"
    copy /Y "%SRC_CFG%" "%DST_CFG%" >nul
    echo Config SQL copiada para o executavel.
) else (
    echo AVISO: %SRC_CFG% em falta — o .exe pode usar Windows auth sem permissoes SQL.
)

echo A parar servidores antigos na porta 8765...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 2 /nobreak >nul

echo A arrancar Web UI em segundo plano...
start "" "%EXE%" --username Nestar --display-name "Nestar" --role admin
timeout /t 3 /nobreak >nul
echo.
echo Web UI a correr (sem janela preta).
echo Browser: http://127.0.0.1:8765  (Ctrl+Shift+R apos abrir)
echo Para parar: Parar_Web_UI.bat
echo.
pause
