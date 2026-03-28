# Build a single-file autoprimenet.exe with PyInstaller.
# Default: EXE -> C:\Users\jeffr\source\repos ; temp build -> %TEMP%\AutoPrimeNet-PyInstaller (nothing left in repo).

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$DistPath = if ($env:AUTOPRIMENET_DIST) { $env:AUTOPRIMENET_DIST } else { "C:\Users\jeffr\source\repos" }
$WorkPath = if ($env:AUTOPRIMENET_WORK) { $env:AUTOPRIMENET_WORK } else { Join-Path $env:TEMP "AutoPrimeNet-PyInstaller" }

New-Item -ItemType Directory -Force -Path $WorkPath | Out-Null
$AssetsDir = Join-Path $RepoRoot "assets"
New-Item -ItemType Directory -Force -Path $AssetsDir | Out-Null
$icon = Join-Path $AssetsDir "favicon.ico"
if (-not (Test-Path $icon)) {
	Invoke-WebRequest -Uri "https://www.mersenne.org/favicon.ico" -OutFile $icon -UseBasicParsing
}
New-Item -ItemType Directory -Force -Path $DistPath | Out-Null
$WorkPathGui = Join-Path $WorkPath "gui-build"
New-Item -ItemType Directory -Force -Path $WorkPathGui | Out-Null

Push-Location $RepoRoot
try {
	python -OO -m PyInstaller `
		-F `
		-n autoprimenet `
		-i $icon `
		--distpath $DistPath `
		--workpath $WorkPath `
		--specpath $WorkPath `
		--clean `
		--noconfirm `
		wrapper.py

	python -OO -m PyInstaller `
		-F `
		-n autoprimenet_gui `
		-i $icon `
		--add-data "$($icon);assets" `
		--distpath $DistPath `
		--workpath $WorkPathGui `
		--specpath $WorkPathGui `
		--collect-all tkinter `
		--collect-all ttkbootstrap `
		--clean `
		--noconfirm `
		autoprimenet_gui.py
}
finally {
	Pop-Location
}

Write-Host "Output: $(Join-Path $DistPath 'autoprimenet.exe')"
Write-Host "GUI:    $(Join-Path $DistPath 'autoprimenet_gui.exe') (keep next to autoprimenet.exe)"
