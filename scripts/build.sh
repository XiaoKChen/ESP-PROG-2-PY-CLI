#!/usr/bin/env bash
# Build the esp-prog2-flasher one-file executable (Linux/macOS).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

uv sync
uv run pyinstaller esp-prog2-flasher.spec --noconfirm

echo "Built: dist/esp-prog2-flasher"
