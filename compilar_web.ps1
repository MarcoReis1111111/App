# compilar_web.ps1 — gera App_web_ui_COMPILADO.py (ficheiro unico)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "== Compilar App_web_ui_COMPILADO.py ==" -ForegroundColor Cyan
python compilar_web_ui.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERRO na geracao do ficheiro unico." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "== Validar sintaxe ==" -ForegroundColor Cyan
python -m py_compile App_web_ui_COMPILADO.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERRO: py_compile falhou." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "OK: App_web_ui_COMPILADO.py pronto." -ForegroundColor Green
Write-Host "Arranque: python App_web_ui_COMPILADO.py --username Nestar --display-name Nestar --role admin"
