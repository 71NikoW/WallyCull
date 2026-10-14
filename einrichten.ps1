# WallyCull einrichten: Python-Umgebung, Pakete, optional KI, Verknüpfungen auf Desktop und im Startmenü.
# Aufruf aus dem Programmordner (git-Klon): Einrichten.bat   bzw.   Einrichten.bat -MitKI
param([switch]$MitKI, [switch]$OhneStart)
$ErrorActionPreference = "Stop"
$Ordner = $PSScriptRoot
Set-Location $Ordner
Write-Host "WallyCull wird eingerichtet in $Ordner" -ForegroundColor Cyan

function Finde-Python {
    foreach ($v in @("-3.12", "-3.13", "-3.11", "-3")) {
        try { $p = & py $v -c "import sys;print(sys.executable)" 2>$null; if ($LASTEXITCODE -eq 0 -and $p) { return $p.Trim() } } catch {}
    }
    return $null
}

$Python = Finde-Python
if (-not $Python) {
    Write-Host "Python fehlt, installiere Python 3.12 ..." -ForegroundColor Yellow
    winget install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    $Python = Finde-Python
    if (-not $Python) { throw "Python konnte nicht installiert werden. Bitte von python.org installieren und erneut starten." }
}

$VenvPy = Join-Path $Ordner ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Host "Lege Programmumgebung an ..."
    & $Python -m venv .venv
}
& $VenvPy -m pip install --quiet --upgrade pip
Write-Host "Installiere Pakete ..."
& $VenvPy -m pip install --quiet --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Paket-Installation fehlgeschlagen." }

if ($MitKI) {
    & $VenvPy -c "import torch" 2>$null
    if ($LASTEXITCODE -ne 0) { & $VenvPy -m fotosort ki-installieren } else { Write-Host "KI-Pakete sind schon installiert." }
}

$Shell = New-Object -ComObject WScript.Shell
$Ziele = @([Environment]::GetFolderPath("Desktop"), (Join-Path ([Environment]::GetFolderPath("Programs")) ""))
foreach ($Z in $Ziele) {
    $L = $Shell.CreateShortcut((Join-Path $Z "WallyCull.lnk"))
    $L.TargetPath = Join-Path $Ordner ".venv\Scripts\pythonw.exe"
    $L.Arguments = "-m fotosort"
    $L.WorkingDirectory = $Ordner
    $L.IconLocation = (Join-Path $Ordner "fotosort\symbol.ico") + ",0"
    $L.Description = "Fotos nach Qualität und Wert sortieren"
    $L.Save()
}
Write-Host "Fertig. Verknüpfung 'WallyCull' liegt auf dem Desktop und im Startmenü." -ForegroundColor Green
if (-not $OhneStart) { Start-Process -FilePath (Join-Path $Ordner ".venv\Scripts\pythonw.exe") -ArgumentList "-m fotosort" -WorkingDirectory $Ordner }
