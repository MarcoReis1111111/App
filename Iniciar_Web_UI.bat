@echo off
title App Web UI - Engenharia
cd /d "%~dp0"

echo ========================================
echo  App Web UI - teste rapido de arranque
echo ========================================
echo.

REM Preferir Python 3.13 (bytecode .cpython-313.pyc)
set "PYEXE="
where py >nul 2>&1 && (
  py -3.13 -c "import sys; raise SystemExit(0 if sys.version_info[:2]==(3,13) else 1)" >nul 2>&1 && set "PYEXE=py -3.13"
)
if not defined PYEXE (
  where python >nul 2>&1 && (
    python -c "import sys; raise SystemExit(0 if sys.version_info[:2]==(3,13) else 1)" >nul 2>&1 && set "PYEXE=python"
  )
)
if not defined PYEXE (
  echo ERRO: Python 3.13 nao encontrado.
  echo O ficheiro _app_web_ui_base.cpython-313.pyc so corre com Python 3.13.
  echo Instala Python 3.13 e/ou usa: py -3.13
  echo.
  pause
  exit /b 1
)

echo A usar: %PYEXE%
%PYEXE% -c "import sys; print('Python', sys.version)"
echo.

if not exist "_app_web_ui_base.cpython-313.pyc" (
  echo ERRO: falta _app_web_ui_base.cpython-313.pyc nesta pasta.
  pause
  exit /b 1
)

echo A correr test_init_web_ui.py ...
%PYEXE% test_init_web_ui.py
if errorlevel 1 (
  echo.
  echo Inicializacao FALHOU — corrige os [FAIL] acima antes de abrir a app.
  pause
  exit /b 1
)
echo.

echo A parar servidores antigos na porta 8765...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 2 /nobreak >nul

echo.
echo A arrancar App_web_ui_moderno.py ...
%PYEXE% -c "import App_web_ui_moderno as m; print('Versao UI:', m.APP_VERSION)"
if errorlevel 1 (
    echo ERRO: nao foi possivel importar App_web_ui_moderno.py
    pause
    exit /b 1
)
echo.
echo IMPORTANTE: no browser usa Ctrl+Shift+R apos arrancar (limpa cache da UI).
echo.
%PYEXE% App_web_ui_moderno.py --username Nestar --display-name "Nestar" --role admin

pause
