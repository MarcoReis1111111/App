# Cria [0]App_Engenharia_Web — apenas ficheiros necessarios para App_web_ui_moderno.py
$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if ((Split-Path -Leaf $scriptDir) -eq "[0]App_Engenharia_Web") {
    $root = Split-Path -Parent $scriptDir
    $dstRoot = $scriptDir
} else {
    $root = $scriptDir
    $dstRoot = Join-Path $root "[0]App_Engenharia_Web"
}
$srcUi = Join-Path $root "02_WEB_UI"
$dstUi = Join-Path $dstRoot "02_WEB_UI"

$pyModules = @(
    "App_web_ui_moderno.py",
    "tasks_common.py",
    "scheduled_tasks_engine.py",
    "excel_filters.py",
    "files_service.py",
    "gantt_service.py",
    "planning_service.py",
    "actions_service.py",
    "attachments_service.py",
    "archive_service.py",
    "tasks_service.py",
    "board_service.py",
    "dashboard_service.py",
    "project_service.py",
    "scheduled_service.py",
    "catalog_service.py",
    "system_service.py",
    "notes_service.py",
    "diagnostic_service.py"
)

$compileFiles = @(
    "compilar_web_ui.py",
    "compilar_web.ps1",
    "compilar_web_exe.ps1",
    "compilar_web_ui_pacote.ps1",
    "Compilar_Web_UI.bat",
    "Compilar_Web_UI_EXE.bat"
)

$launchFiles = @(
    "Iniciar_Web_UI.bat",
    "Parar_Web_UI.bat",
    "Iniciar_Web_UI_EXE.bat"
)

function Ensure-Dir([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Copy-FileSafe([string]$Src, [string]$Dst) {
    if (-not (Test-Path -LiteralPath $Src)) {
        throw "Ficheiro em falta: $Src"
    }
    $dir = Split-Path -Parent $Dst
    Ensure-Dir $dir
    Copy-Item -LiteralPath $Src -Destination $Dst -Force
}

Write-Host "== Criar [0]App_Engenharia_Web ==" -ForegroundColor Cyan
foreach ($sub in @("02_WEB_UI", "web", "[0]Programas_prontos")) {
    $p = Join-Path $dstRoot $sub
    if (Test-Path -LiteralPath $p) {
        Write-Host "  A limpar $sub..."
        Remove-Item -LiteralPath $p -Recurse -Force
    }
}
foreach ($f in @("Iniciar_Web_UI.bat", "Parar_Web_UI.bat", "Compilar_Web_UI.bat", "Compilar_Web_UI_EXE.bat", "LEIA-ME.txt", "requirements-web.txt", "app_icon.ico", "instalar_prerequisitos.ps1")) {
    $p = Join-Path $dstRoot $f
    if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force }
}
Ensure-Dir $dstRoot

Ensure-Dir $dstUi
Ensure-Dir (Join-Path $dstUi "AppEngenhariaCache")
Ensure-Dir (Join-Path $dstRoot "web\vendor\frappe-gantt")

Write-Host "  Copiar modulos Python..."
foreach ($f in $pyModules) {
    Copy-FileSafe (Join-Path $srcUi $f) (Join-Path $dstUi $f)
}

Write-Host "  Copiar bytecode base..."
Copy-FileSafe (Join-Path $srcUi "_app_web_ui_base.cpython-313.pyc") (Join-Path $dstUi "_app_web_ui_base.cpython-313.pyc")
$fallbackPyc = Join-Path $srcUi "__pycache__\_recovered\App_web_ui_moderno.cpython-313.pyc"
if (Test-Path -LiteralPath $fallbackPyc) {
    $fbDst = Join-Path $dstUi "__pycache__\_recovered\App_web_ui_moderno.cpython-313.pyc"
    Copy-FileSafe $fallbackPyc $fbDst
}

Write-Host "  Copiar config..."
Copy-FileSafe (Join-Path $srcUi "AppEngenhariaCache\config.json") (Join-Path $dstUi "AppEngenhariaCache\config.json")

$webUsersSrc = Join-Path $srcUi "AppEngenhariaCache\web_users"
if (Test-Path -LiteralPath $webUsersSrc) {
    $webUsersDst = Join-Path $dstUi "AppEngenhariaCache\web_users"
    Ensure-Dir $webUsersDst
    Get-ChildItem -LiteralPath $webUsersSrc -Filter "*.json" -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $webUsersDst $_.Name) -Force
    }
}

Write-Host "  Copiar assets Gantt (web/vendor)..."
$ganttFiles = @("frappe-gantt.css", "frappe-gantt.umd.js", "LICENSE.txt")
foreach ($gf in $ganttFiles) {
    $gs = Join-Path $root "web\vendor\frappe-gantt\$gf"
    $gd = Join-Path $dstRoot "web\vendor\frappe-gantt\$gf"
    Copy-FileSafe $gs $gd
}

Write-Host "  Copiar scripts de compilacao e arranque..."
foreach ($f in ($compileFiles + $launchFiles)) {
    Copy-FileSafe (Join-Path $srcUi $f) (Join-Path $dstUi $f)
}

$iconSrc = Join-Path $root "app_icon.ico"
if (Test-Path -LiteralPath $iconSrc) {
    Copy-FileSafe $iconSrc (Join-Path $dstRoot "app_icon.ico")
    Copy-FileSafe $iconSrc (Join-Path $dstUi "app_icon.ico")
}

$prereq = Join-Path $root "instalar_prerequisitos.ps1"
if (Test-Path -LiteralPath $prereq) {
    Copy-FileSafe $prereq (Join-Path $dstRoot "instalar_prerequisitos.ps1")
}

# Ajustar compilar_web_ui_pacote.ps1 para output local
$pacoteSrc = Join-Path $dstUi "compilar_web_ui_pacote.ps1"
if (Test-Path -LiteralPath $pacoteSrc) {
    $pacoteText = Get-Content -LiteralPath $pacoteSrc -Raw -Encoding UTF8
    $pacoteText = $pacoteText -replace '\$projectRoot = Split-Path -Parent \$here', '$projectRoot = $here'
    $pacoteText = $pacoteText -replace '\$prontosRoot = Join-Path \$projectRoot "\[0\]Programas_prontos"', '$prontosRoot = Join-Path (Split-Path -Parent $here) "[0]Programas_prontos"'
    Set-Content -LiteralPath $pacoteSrc -Value $pacoteText -Encoding UTF8
}

# Launchers na raiz do pacote
$launcherBat = @'
@echo off
title App Engenharia Web
cd /d "%~dp002_WEB_UI"
call "%~dp002_WEB_UI\Iniciar_Web_UI.bat"
'@
Set-Content -LiteralPath (Join-Path $dstRoot "Iniciar_Web_UI.bat") -Value $launcherBat -Encoding ASCII

$stopBat = @'
@echo off
title Parar App Engenharia Web
cd /d "%~dp002_WEB_UI"
call "%~dp002_WEB_UI\Parar_Web_UI.bat"
'@
Set-Content -LiteralPath (Join-Path $dstRoot "Parar_Web_UI.bat") -Value $stopBat -Encoding ASCII

$compileBat = @'
@echo off
title Compilar Web UI
cd /d "%~dp002_WEB_UI"
call "%~dp002_WEB_UI\Compilar_Web_UI.bat"
'@
Set-Content -LiteralPath (Join-Path $dstRoot "Compilar_Web_UI.bat") -Value $compileBat -Encoding ASCII

$compileExeBat = @'
@echo off
title Compilar Web UI EXE
cd /d "%~dp002_WEB_UI"
call "%~dp002_WEB_UI\Compilar_Web_UI_EXE.bat"
'@
Set-Content -LiteralPath (Join-Path $dstRoot "Compilar_Web_UI_EXE.bat") -Value $compileExeBat -Encoding ASCII

$readme = @'
App Engenharia — Web UI (pacote minimo)
========================================

Este pacote contem apenas o necessario para App_web_ui_moderno.py.

ESTRUTURA
  02_WEB_UI\          Codigo Python + servicos + cache config
  web\vendor\         Assets Gantt (frappe-gantt)
  Iniciar_Web_UI.bat  Arranque rapido (Python)
  Compilar_Web_UI.bat Gerar App_web_ui_COMPILADO.py
  Compilar_Web_UI_EXE.bat  Gerar executavel Windows

PRE-REQUISITOS
  - Python 3.13
  - pip install pyodbc pandas openpyxl
  - ODBC Driver 18 for SQL Server (instalar_prerequisitos.ps1)

ARRANQUE (desenvolvimento)
  1. Editar 02_WEB_UI\AppEngenhariaCache\config.json (SQL, caminhos)
  2. Executar Iniciar_Web_UI.bat
  3. Browser: http://127.0.0.1:8765  (Ctrl+Shift+R apos abrir)

COMPILAR
  - Ficheiro unico: Compilar_Web_UI.bat
  - Executavel: Compilar_Web_UI_EXE.bat -> 02_WEB_UI\dist\AppWebUI_Local\

NOTAS
  - A pasta original 02_WEB_UI na raiz do projeto mantem-se intacta.
  - Logs e cache de sessao sao criados em 02_WEB_UI\AppEngenhariaCache\ em runtime.
  - Para atualizar este pacote a partir de 02_WEB_UI: regenerar_pacote.ps1
'@
Set-Content -LiteralPath (Join-Path $dstRoot "LEIA-ME.txt") -Value $readme -Encoding UTF8

$req = @'
pyodbc
pandas
openpyxl
pyinstaller
'@
Set-Content -LiteralPath (Join-Path $dstRoot "requirements-web.txt") -Value $req -Encoding UTF8

Write-Host ""
Write-Host "Pacote criado: $dstRoot" -ForegroundColor Green
$fileCount = (Get-ChildItem -LiteralPath $dstRoot -Recurse -File).Count
Write-Host "  Ficheiros: $fileCount"
