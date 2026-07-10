# Build the esp-prog2-flasher one-file executable (Windows).
$ErrorActionPreference = "Stop"

Set-Location (Join-Path $PSScriptRoot "..")

uv sync
uv run pyinstaller esp-prog2-flasher.spec --noconfirm

Write-Host "Built: dist/esp-prog2-flasher.exe"
