$ErrorActionPreference = "Stop"

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Test-VCRedistInstalled {
    $keys = @(
        "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
    )
    foreach ($k in $keys) {
        try {
            $p = Get-ItemProperty -Path $k -ErrorAction Stop
            if ($p.Installed -eq 1) { return $true }
        } catch {}
    }
    return $false
}

function Test-Odbc18Installed {
    $odbcDrivers = @()
    try {
        $odbcDrivers += (Get-ItemProperty "HKLM:\SOFTWARE\ODBC\ODBCINST.INI\ODBC Drivers" -ErrorAction Stop).PSObject.Properties.Name
    } catch {}
    try {
        $odbcDrivers += (Get-ItemProperty "HKLM:\SOFTWARE\WOW6432Node\ODBC\ODBCINST.INI\ODBC Drivers" -ErrorAction Stop).PSObject.Properties.Name
    } catch {}
    return ($odbcDrivers -contains "ODBC Driver 18 for SQL Server")
}

function Install-WithWinget($id, $displayName) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "winget nao encontrado. Vou usar download direto para: $displayName"
        return $false
    }
    Write-Host "A instalar via winget: $displayName"
    winget install --id $id --exact --accept-package-agreements --accept-source-agreements --silent
    return ($LASTEXITCODE -eq 0)
}

function Install-VCRedist {
    if (Test-VCRedistInstalled) {
        Write-Host "VC++ Redistributable ja instalado."
        return
    }

    Write-Step "Instalar Microsoft Visual C++ Redistributable 2015-2022 (x64)"
    $ok = Install-WithWinget "Microsoft.VCRedist.2015+.x64" "Microsoft VC++ Redistributable x64"
    if ($ok) { return }

    $url = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    $tmp = Join-Path $env:TEMP "vc_redist.x64.exe"
    Write-Host "Download: $url"
    Invoke-WebRequest -Uri $url -OutFile $tmp
    Start-Process -FilePath $tmp -ArgumentList "/install /quiet /norestart" -Wait
}

function Install-Odbc18 {
    if (Test-Odbc18Installed) {
        Write-Host "ODBC Driver 18 ja instalado."
        return
    }

    Write-Step "Instalar ODBC Driver 18 for SQL Server (x64)"
    $ok = Install-WithWinget "Microsoft.ODBCDriver18forSQLServer" "ODBC Driver 18 for SQL Server"
    if ($ok) { return }

    # URL oficial atual da Microsoft para MSI x64
    $url = "https://download.microsoft.com/download/f/6/6/f66d8675-7015-4aeb-a731-2f6e40d18f15/msodbcsql18.msi"
    $tmp = Join-Path $env:TEMP "msodbcsql18.msi"
    Write-Host "Download: $url"
    Invoke-WebRequest -Uri $url -OutFile $tmp
    Start-Process -FilePath "msiexec.exe" -ArgumentList "/i `"$tmp`" /qn /norestart IACCEPTMSODBCSQLLICENSETERMS=YES" -Wait
}

if (-not (Test-IsAdmin)) {
    Write-Error "Execute este script como Administrador."
}

Write-Host "Instalacao de prerequisitos para AppEngenhariaElectronics" -ForegroundColor Green
Write-Host "PC: $env:COMPUTERNAME | User: $env:USERNAME"

Install-VCRedist
Install-Odbc18

Write-Step "Validacao final"
$vc = Test-VCRedistInstalled
$odbc = Test-Odbc18Installed
Write-Host "VC++ instalado: $vc"
Write-Host "ODBC 18 instalado: $odbc"

if ($vc -and $odbc) {
    Write-Host ""
    Write-Host "OK: prerequisitos instalados com sucesso." -ForegroundColor Green
    Write-Host "Ja pode executar AppEngenhariaElectronics.exe"
    exit 0
}

Write-Host ""
Write-Host "Atencao: alguns prerequisitos ainda nao estao detetados." -ForegroundColor Yellow
Write-Host "Reinicie o PC e execute novamente este script."
exit 1
