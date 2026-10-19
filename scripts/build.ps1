# Build the esp-prog2-flasher one-file executable (Windows).
#
# Syncs the uv environment and runs PyInstaller against esp-prog2-flasher.spec, producing
# dist/esp-prog2-flasher.exe. It is the local counterpart of the CI builds.
#
# Not here:    what gets bundled (esp-prog2-flasher.spec) and release publishing
#              (.github/workflows/build.yml).
# Errors:      $ErrorActionPreference = "Stop" aborts on the first failing cmdlet.
# Depends on:  uv; run from any directory (it moves to the repo root).
# See also:    scripts/build.sh, esp-prog2-flasher.spec
$ErrorActionPreference = "Stop"

Set-Location (Join-Path $PSScriptRoot "..")

uv sync
uv run pyinstaller esp-prog2-flasher.spec --noconfirm

Write-Host "Built: dist/esp-prog2-flasher.exe"
