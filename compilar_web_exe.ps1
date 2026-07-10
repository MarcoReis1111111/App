# compilar_web_exe.ps1 — gera executável Windows da Web UI (PyInstaller onedir).
#
# 1) Gera App_web_ui_COMPILADO.py (ficheiro único)
# 2) PyInstaller → dist\AppWebUI_Local\AppWebUI_Local.exe
#
# Cache/config: AppEngenhariaCache\ ao lado do .exe
# Para não pausar ao fim (CI): $env:COMPILAR_NO_PAUSE = "1"
$ErrorActionPreference = "Continue"
$here = $PSScriptRoot
Set-Location -LiteralPath $here

$name = "AppWebUI_Local"
$mainPy = Join-Path $here "App_web_ui_COMPILADO.py"
$logDir = Join-Path $here "build_logs"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$buildLog = Join-Path $logDir "compile_web_exe_$stamp.log"

function Write-BuildLog {
    param([string]$Text)
    try {
        if (-not (Test-Path -LiteralPath $logDir)) {
            New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        }
        Add-Content -LiteralPath $buildLog -Value $Text -Encoding UTF8
    } catch {}
}

$icon = Join-Path $here "app_icon.ico"
$iconArg = @()
if (Test-Path -LiteralPath $icon) {
    $iconArg = @("--icon", $icon)
}

Write-Host "==[ 1/4 ] Gerar App_web_ui_COMPILADO.py ==" -ForegroundColor Cyan
$genOut = & python compilar_web_ui.py 2>&1
$genExit = $LASTEXITCODE
foreach ($line in $genOut) { Write-Host "    $line" }
Write-BuildLog ($genOut -join "`n")
if ($genExit -ne 0) {
    Write-Host "ERRO: compilar_web_ui.py falhou." -ForegroundColor Red
    if (-not $env:COMPILAR_NO_PAUSE) { Read-Host "Prima Enter para fechar" }
    exit $genExit
}

Write-Host ""
Write-Host "==[ 2/4 ] Validar sintaxe ==" -ForegroundColor Cyan
$pyCompileOut = & python -m py_compile $mainPy 2>&1
if ($LASTEXITCODE -ne 0) {
    foreach ($line in $pyCompileOut) { Write-Host "    $line" }
    Write-Host "ERRO: py_compile falhou." -ForegroundColor Red
    if (-not $env:COMPILAR_NO_PAUSE) { Read-Host "Prima Enter para fechar" }
    exit 1
}
Write-Host "  py_compile OK" -ForegroundColor Green

Write-Host ""
Write-Host "==[ 3/4 ] Instalar PyInstaller + pyodbc ==" -ForegroundColor Cyan
$pipOut = & python -m pip install --disable-pip-version-check pyinstaller pyodbc 2>&1
Write-BuildLog ($pipOut -join "`n")
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERRO: pip install falhou." -ForegroundColor Red
    if (-not $env:COMPILAR_NO_PAUSE) { Read-Host "Prima Enter para fechar" }
    exit 1
}

Get-Process -Name $name -ErrorAction SilentlyContinue | ForEach-Object {
    try { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue } catch {}
}
Start-Sleep -Seconds 1

function Remove-Folder-Safe {
    param([string]$Path, [int]$MaxAttempts = 6, [int]$SleepSec = 2)
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    for ($i = 1; $i -le $MaxAttempts; $i++) {
        try {
            attrib -R "$Path\*.*" /S /D 2>$null | Out-Null
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            if (-not (Test-Path -LiteralPath $Path)) { return $true }
        } catch {
            Write-Host ("  Tentativa {0}/{1} falhou ao remover {2}" -f $i, $MaxAttempts, $Path) -ForegroundColor DarkYellow
            Start-Sleep -Seconds $SleepSec
        }
    }
    return -not (Test-Path -LiteralPath $Path)
}

foreach ($p in @((Join-Path $here "dist\$name"))) {
    if (Test-Path -LiteralPath $p) {
        Write-Host "  A remover $p ..."
        if (-not (Remove-Folder-Safe -Path $p)) {
            Write-Host "AVISO: Nao consegui apagar $p (feche o .exe / OneDrive?)." -ForegroundColor DarkYellow
        }
    }
}
$workPath = Join-Path $env:TEMP ("pyi_{0}_{1}" -f $name, $stamp)
$distPath = Join-Path $here "dist"
$specFile = Join-Path $here "$name.spec"
if (Test-Path -LiteralPath $specFile) {
    try { Remove-Item -LiteralPath $specFile -Force -ErrorAction Stop } catch {}
}

Write-Host ""
Write-Host "==[ 4/4 ] PyInstaller onedir (Web UI) ==" -ForegroundColor Cyan
Write-Host "      Pode demorar 2-8 min." -ForegroundColor DarkGray

$hiddenImports = @(
    "pyodbc",
    "uuid", "json", "calendar", "decimal", "contextlib", "shutil", "subprocess",
    "threading", "http.server", "socketserver", "mimetypes", "webbrowser"
)
$hiddenArgs = @()
foreach ($hi in $hiddenImports) {
    $hiddenArgs += @("--hidden-import", $hi)
}

$pyiArgs = @(
    "-u", "-m", "PyInstaller",
    "-y", "--onedir", "--noconsole",
    "--log-level", "INFO",
    "--name", $name,
    "--distpath", $distPath,
    "--workpath", $workPath,
    "--specpath", $workPath
) + $hiddenArgs + @(
    $mainPy
) + $iconArg

$pyiOut = & python @pyiArgs 2>&1
$pyiExit = $LASTEXITCODE
foreach ($line in $pyiOut) {
    if ($line -is [System.Management.Automation.ErrorRecord]) {
        Write-Host "    $($line.Exception.Message)" -ForegroundColor DarkGray
    } else {
        Write-Host "    $line"
    }
}
Write-BuildLog ($pyiOut -join "`n")
if ($pyiExit -ne 0) {
    Write-Host "ERRO: PyInstaller falhou (exit $pyiExit)." -ForegroundColor Red
    Write-Host "Log: $buildLog" -ForegroundColor Yellow
    if (-not $env:COMPILAR_NO_PAUSE) { Read-Host "Prima Enter para fechar" }
    exit $pyiExit
}

$exePath = Join-Path $here "dist\$name\$name.exe"
if (-not (Test-Path -LiteralPath $exePath)) {
    Write-Host "ERRO: $exePath nao existe." -ForegroundColor Red
    if (-not $env:COMPILAR_NO_PAUSE) { Read-Host "Prima Enter para fechar" }
    exit 2
}

$exeInfo = Get-Item -LiteralPath $exePath
Write-Host ""
Write-Host ("Executavel OK ({0:N1} MB): {1}" -f ($exeInfo.Length / 1MB), $exePath) -ForegroundColor Green
Write-Host "Arranque: dist\$name\$name.exe --username Nestar --display-name Nestar --role admin"
Write-Host "Ou use: Iniciar_Web_UI_EXE.bat"
Write-Host "Log build: $buildLog"

if (-not $env:COMPILAR_NO_PAUSE) {
    Write-Host ""
    Read-Host "Concluido. Prima Enter para fechar"
}
