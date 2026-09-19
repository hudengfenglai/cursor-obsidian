# Build cursor-sidecar.exe (Windows x64, PyInstaller onefile) and release zip.
# Usage (from repo root):
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Sidecar = Join-Path $RepoRoot "cursor-sidecar"
$Plugin = Join-Path $RepoRoot "obsidian-plugin"
$Dist = Join-Path $RepoRoot "dist"
$BuildDir = Join-Path $RepoRoot "build"
$ReleaseRoot = Join-Path $RepoRoot "release\cursor-sidecar"
$Venv = Join-Path $RepoRoot ".packaging-venv"
$Spec = Join-Path $PSScriptRoot "cursor-sidecar.spec"
$Version = "0.7.2-dev"
$ZipName = "cursor-sidecar-v$Version-windows-x64.zip"
$ZipPath = Join-Path $RepoRoot "release\$ZipName"

Write-Host "== Cursor Sidecar packaging v$Version =="

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python not found on PATH"
}

Write-Host "[1/5] Clean build env"
if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }
if (Test-Path $BuildDir) { Remove-Item -Recurse -Force $BuildDir }
if (Test-Path $ReleaseRoot) { Remove-Item -Recurse -Force $ReleaseRoot }

Write-Host "[2/5] Create venv + install deps"
python -m venv $Venv
$Py = Join-Path $Venv "Scripts\python.exe"
& $Py -m pip install --upgrade pip
& $Py -m pip install -r (Join-Path $Sidecar "requirements-dev.txt")

Write-Host "[3/5] PyInstaller onefile"
& $Py -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $BuildDir $Spec

$Exe = Join-Path $Dist "cursor-sidecar.exe"
if (-not (Test-Path $Exe)) {
    throw "Build failed: $Exe missing"
}

Write-Host "[4/5] Assemble release layout"
New-Item -ItemType Directory -Force -Path (Join-Path $ReleaseRoot "bin") | Out-Null
Copy-Item (Join-Path $Plugin "main.js") $ReleaseRoot
Copy-Item (Join-Path $Plugin "manifest.json") $ReleaseRoot
if (Test-Path (Join-Path $Plugin "styles.css")) {
    Copy-Item (Join-Path $Plugin "styles.css") $ReleaseRoot
} else {
    Set-Content -Path (Join-Path $ReleaseRoot "styles.css") -Value "" -Encoding utf8
}
Copy-Item $Exe (Join-Path $ReleaseRoot "bin\cursor-sidecar.exe")

if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
Compress-Archive -Path (Join-Path $ReleaseRoot "*") -DestinationPath $ZipPath -Force

Write-Host "[5/5] Fingerprints"
$Hash = (Get-FileHash -Algorithm SHA256 $Exe).Hash.ToLower()
$Size = (Get-Item $Exe).Length
Write-Host "helper_path=$Exe"
Write-Host "helper_size=$Size"
Write-Host "helper_sha256=$Hash"
Write-Host "release_dir=$ReleaseRoot"
Write-Host "release_zip=$ZipPath"
Write-Host "DONE"
