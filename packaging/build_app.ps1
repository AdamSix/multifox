# packaging/build_app.ps1 — build the multifox desktop app with PyInstaller (Windows)
#
#   .\packaging\build_app.ps1    build dist\multifox\ (+ a zip next to it)
#
# Mirrors build_app.sh. Run on Windows — PyInstaller does not cross-compile.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Py = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Py)) { Write-Error ".venv missing - run 'py install.py' first" }
& $Py -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) { & $Py -m pip install --quiet pyinstaller }

# stage the installed Camoufox browser + GeoIP DB + addons as a zip inside the
# bundle so the app installs offline (the launcher falls back to downloading
# if the payload is absent)
$PayloadZip = Join-Path $Root "build\bundle_payload.zip"
$Stage = Join-Path $Root "build\payload_stage"
Remove-Item $PayloadZip -Force -ErrorAction SilentlyContinue
Remove-Item $Stage -Recurse -Force -ErrorAction SilentlyContinue
$CamouCache = (& $Py -c "from camoufox.pkgman import INSTALL_DIR; print(INSTALL_DIR)").Trim()
if (-not (Test-Path (Join-Path $CamouCache "browsers"))) {
  Write-Error "no camoufox browser in $CamouCache - run '$Py -m camoufox fetch' first"
}
New-Item -ItemType Directory -Path $Stage | Out-Null
foreach ($part in @("browsers", "geoip", "addons")) {
  $src = Join-Path $CamouCache $part
  if (Test-Path $src) { Copy-Item $src (Join-Path $Stage $part) -Recurse }
}
Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $PayloadZip -Force
Remove-Item $Stage -Recurse -Force
Write-Host "staged browser payload zip into the bundle"

Set-Location $Root
& $Py -m PyInstaller --noconfirm --clean --distpath (Join-Path $Root "dist") --workpath (Join-Path $Root "build") (Join-Path $Root "packaging\multifox-windows.spec")
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller failed" }

$AppDir = Join-Path $Root "dist\multifox"
if (-not (Test-Path (Join-Path $AppDir "multifox.exe"))) { Write-Error "$AppDir was not produced" }

Compress-Archive -Path $AppDir -DestinationPath (Join-Path $Root "dist\multifox.zip") -Force

Write-Host ""
Write-Host "built: $AppDir"
Write-Host "zipped: $(Join-Path $Root 'dist\multifox.zip')"
