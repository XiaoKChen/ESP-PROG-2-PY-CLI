#!/usr/bin/env bash
# Run esp-prog2-flasher from source on Termux (Android).
#
# WHY NOT A FROZEN BINARY: PyInstaller does not support Android — its bootloader
# has no Bionic/Android target — and a Termux-built binary would only run inside
# Termux anyway, so freezing buys nothing here. We run straight from source
# instead. uv is also unusable on Termux (it rejects Android's OS tag, and the
# project's uv_build backend can't compile there), so runtime deps go into
# Termux's own system pip. Everywhere else, use the packaged binary or
# `uv run esp-prog2-flasher`.
#
# Prerequisites (one-time, inside Termux):
#   pkg install python libusb
#     - libusb: required by pyocd/libusb-package to reach the probe.
# Runtime note: USB access on Android needs Termux's USB API (`pkg install
# termux-api` + the Termux:API app, `termux-usb`); a plain Termux shell cannot
# see USB devices.
#
# Usage: scripts/run-termux.sh [flasher args...]      # e.g. --list, --detect
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v pip >/dev/null 2>&1; then
    echo "Error: pip not found. Install Python with: pkg install python" >&2
    exit 1
fi

# Install runtime deps only when they aren't already importable, so repeat runs
# stay fast and offline. The project itself is not installed (its uv_build
# backend won't build on Android) — src/ goes on PYTHONPATH below instead. The
# dep list is read from pyproject.toml so it can't drift from the real set.
if ! python -c 'import pyocd, esptool, serial, textual, truststore' 2>/dev/null; then
    echo "Installing runtime dependencies (first run)…"
    mapfile -t DEPS < <(
        python -c 'import pathlib, tomllib; print("\n".join(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["dependencies"]))'
    )
    if [ "${#DEPS[@]}" -eq 0 ]; then
        echo "Error: parsed no dependencies from pyproject.toml — aborting." >&2
        exit 1
    fi
    pip install "${DEPS[@]}"
fi

exec env PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}" python -m esp_prog2_flasher "$@"
