# compilar_web_ui_pacote.ps1 — compila Web UI e cria pacote em [0]Programas_prontos
#
# Saida: ..\[0]Programas_prontos\2026_07_10__14_16_00 - Versao_14.2\
# Para nao pausar ao fim (CI): $env:COMPILAR_NO_PAUSE = "1"
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
Set-Location -LiteralPath $here

$exeName = "AppWebUI_Local"
$distSrc = Join-Path $here "dist\$exeName"
$projectRoot = $here
$prontosRoot = Join-Path (Split-Path -Parent $here) "[0]Programas_prontos"

function Get-AppVersion {
    $runtime = Join-Path $here "App_web_ui_moderno.py"
    $text = Get-Content -LiteralPath $runtime -Raw -Encoding UTF8
    if ($text -match 'APP_VERSION\s*=\s*"([^"]+)"') {
        return $Matches[1]
    }
    return "0.0.0"
}

function Get-VersionLabel {
    param([string]$Version)
    $v = $Version -replace '^0\.', ''
    return "Versão_$v"
}

function Copy-Tree {
    param([string]$Source, [string]$Dest)
    if (-not (Test-Path -LiteralPath $Source)) {
        throw "Origem em falta: $Source"
    }
    if (-not (Test-Path -LiteralPath $Dest)) {
        New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    }
    $robocopy = Get-Command robocopy -ErrorAction SilentlyContinue
    if ($robocopy) {
        & robocopy $Source $Dest /E /COPY:DAT /R:2 /W:2 /NFL /NDL /NJH /NJS /NC /NS | Out-Null
        if ($LASTEXITCODE -ge 8) {
            throw "robocopy falhou (exit $LASTEXITCODE) ao copiar $Source -> $Dest"
        }
        return
    }
    Copy-Item -LiteralPath $Source -Destination $Dest -Recurse -Force
}

Write-Host "==[ 1/3 ] Compilar executavel Web UI ==" -ForegroundColor Cyan
$env:COMPILAR_NO_PAUSE = "1"
& (Join-Path $here "compilar_web_exe.ps1")
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERRO: compilacao do executavel falhou." -ForegroundColor Red
    if (-not $env:COMPILAR_NO_PAUSE) { Read-Host "Prima Enter para fechar" }
    exit $LASTEXITCODE
}

if (-not (Test-Path -LiteralPath (Join-Path $distSrc "$exeName.exe"))) {
    Write-Host "ERRO: executavel nao encontrado em $distSrc" -ForegroundColor Red
    exit 2
}

Write-Host ""
Write-Host "==[ 2/3 ] Preparar pasta [0]Programas_prontos ==" -ForegroundColor Cyan
$appVersion = Get-AppVersion
$versionLabel = Get-VersionLabel -Version $appVersion
$stamp = Get-Date -Format "yyyy_MM_dd__HH_mm_ss"
$releaseName = "$stamp - $versionLabel"
$releaseDir = Join-Path $prontosRoot $releaseName

if (-not (Test-Path -LiteralPath $prontosRoot)) {
    New-Item -ItemType Directory -Path $prontosRoot -Force | Out-Null
    Write-Host "  Criada pasta: $prontosRoot" -ForegroundColor Green
}

if (Test-Path -LiteralPath $releaseDir) {
    Write-Host "  A remover pasta anterior: $releaseName"
    Remove-Item -LiteralPath $releaseDir -Recurse -Force
}
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null

Write-Host ""
Write-Host "==[ 3/3 ] Copiar pacote para utilizadores ==" -ForegroundColor Cyan
Copy-Tree -Source $distSrc -Dest $releaseDir

$releaseExe = Join-Path $releaseDir "$exeName.exe"
if (-not (Test-Path -LiteralPath $releaseExe)) {
    throw "Pacote incompleto: $releaseExe em falta apos copia de $distSrc"
}

$cacheDir = Join-Path $releaseDir "AppEngenhariaCache"
$srcCfg = Join-Path $here "AppEngenhariaCache\config.json"
$dstCfg = Join-Path $cacheDir "config.json"
if (-not (Test-Path -LiteralPath $cacheDir)) {
    New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null
}
if (Test-Path -LiteralPath $srcCfg) {
    Copy-Item -LiteralPath $srcCfg -Destination $dstCfg -Force
    Write-Host "  config.json copiado para AppEngenhariaCache\" -ForegroundColor Green
} else {
    Write-Host "  AVISO: config.json em falta — utilizadores devem criar AppEngenhariaCache\config.json" -ForegroundColor DarkYellow
}

$launcherBat = @"
@echo off
title App Engenharia - Web UI v$appVersion
cd /d "%~dp0"

set "EXE=AppWebUI_Local.exe"
if not exist "%EXE%" (
    echo Executavel em falta: %EXE%
    pause
    exit /b 1
)

echo A parar servidores antigos na porta 8765...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 2 /nobreak >nul

echo A arrancar Web UI v$appVersion em segundo plano...
start "" "%EXE%" --username %USERNAME% --display-name %USERNAME% --role edit
timeout /t 3 /nobreak >nul
echo.
echo Web UI a correr (sem janela preta).
echo Browser: http://127.0.0.1:8765
echo Para parar: execute Parar_Web_UI.bat
echo.
pause
"@
Set-Content -LiteralPath (Join-Path $releaseDir "Iniciar_Web_UI.bat") -Value $launcherBat -Encoding ASCII

$stopBat = @"
@echo off
title Parar Web UI v$appVersion
cd /d "%~dp0"

echo A parar Web UI...
taskkill /F /IM AppWebUI_Local.exe >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 2 /nobreak >nul
echo Web UI parada.
pause
"@
Set-Content -LiteralPath (Join-Path $releaseDir "Parar_Web_UI.bat") -Value $stopBat -Encoding ASCII

$readme = @"
App Engenharia — Web UI v$appVersion
Pacote gerado: $stamp

COMO USAR
1. Copie esta pasta inteira para o PC do utilizador.
2. Edite AppEngenhariaCache\config.json com as ligacoes SQL/caminhos corretos.
3. Execute Iniciar_Web_UI.bat — a app corre em segundo plano (sem janela preta).
4. Abra o browser em http://127.0.0.1:8765
5. Para terminar: execute Parar_Web_UI.bat

NOTAS
- Nao apague a pasta _internal\ (necessaria para o executavel).
- A porta predefinida e 8765.
- Logs: AppEngenhariaCache\web_ui_local.log
- Versao compilada: $appVersion
"@
Set-Content -LiteralPath (Join-Path $releaseDir "LEIA-ME.txt") -Value $readme -Encoding UTF8

$exeInfo = Get-Item -LiteralPath (Join-Path $releaseDir "$exeName.exe") -ErrorAction Stop
Write-Host ""
Write-Host "Pacote pronto para utilizadores:" -ForegroundColor Green
Write-Host "  $releaseDir"
Write-Host ("  Executavel: {0:N1} MB" -f ($exeInfo.Length / 1MB))
Write-Host "  Versao: $appVersion ($versionLabel)"
Write-Host ""
Write-Host "Os utilizadores podem copiar a pasta completa para o PC deles."

if (-not $env:COMPILAR_NO_PAUSE) {
    Write-Host ""
    Read-Host "Concluido. Prima Enter para fechar"
}

