#!/usr/bin/env bash
# Build the esp-prog2-flasher one-file executable (Linux/macOS).
#
# Syncs the uv environment and runs PyInstaller against esp-prog2-flasher.spec, producing
# dist/esp-prog2-flasher. It is the local counterpart of the CI builds.
#
# Not here:    what gets bundled (esp-prog2-flasher.spec) and release publishing
#              (.github/workflows/build.yml).
# Errors:      aborts on the first failing command (set -euo pipefail).
# Depends on:  uv; run from any directory (it cd's to the repo root).
# See also:    scripts/build.ps1, esp-prog2-flasher.spec
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

uv sync
uv run pyinstaller esp-prog2-flasher.spec --noconfirm

echo "Built: dist/esp-prog2-flasher"
