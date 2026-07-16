#!/usr/bin/env bash
# Build the esp-prog2-flasher one-file executable for arm64 Android (Termux).
#
# DELIBERATE EXCEPTION to the repo's uv-only rule: uv does NOT support Android
# (recent Termux Python reports its OS as `android`, which uv's platform
# detection rejects with "Unknown operating system: android"). Termux's stdlib
# `venv` is also broken (ensurepip fails). So on Termux we install straight into
# Termux's own system pip — Termux is already an isolated single-user $PREFIX,
# so a venv buys nothing here. Everywhere else, use scripts/build.sh (uv).
#
# PyInstaller freezes the running interpreter — it does NOT cross-compile — so
# this MUST run inside Termux on an aarch64 Android device. The resulting binary
# is a Termux-native ELF (linked against Termux's libraries under $PREFIX/lib)
# and only runs inside Termux on a compatible device; it is not a
# portable/standalone Android APK.
#
# Prerequisites (one-time, inside Termux):
#   pkg install python rust clang binutils libusb
#     - rust/clang/binutils: build cmsis-pack-manager + capstone from source
#       (no prebuilt Android aarch64 wheels on PyPI); expect a long first build.
#     - libusb: required by pyocd/libusb-package to reach the probe.
# Runtime note: USB access on Android needs Termux's USB API (`pkg install
# termux-api` + the Termux:API app, `termux-usb`); a plain Termux shell cannot
# see USB devices.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

ARCH="$(uname -m)"
if [ "$ARCH" != "aarch64" ] && [ "$ARCH" != "arm64" ]; then
    echo "Error: expected an arm64 host (uname -m = aarch64), got '$ARCH'." >&2
    echo "PyInstaller cannot cross-compile — run this inside Termux on an arm64 device." >&2
    exit 1
fi

if [ -z "${PREFIX:-}" ] || [ ! -d "${PREFIX:-/nonexistent}" ]; then
    echo "Warning: \$PREFIX is not set — this doesn't look like a Termux shell." >&2
    echo "The build may still work on other aarch64 Linux, but the output is only" >&2
    echo "guaranteed to run in the environment it was built in." >&2
fi

if ! command -v pip >/dev/null 2>&1; then
    echo "Error: pip not found. Install Python with: pkg install python" >&2
    exit 1
fi

# Editable install pulls the project's runtime deps from pyproject.toml; add
# pyinstaller (a dev-group tool not in [project.dependencies]) explicitly.
pip install -e . pyinstaller

# --noupx overrides the spec's upx=True: UPX is usually absent on Termux and can
# corrupt the aarch64 bootloader. The rest of the build reuses the shared spec.
pyinstaller esp-prog2-flasher.spec --noconfirm --noupx

OUT="dist/esp-prog2-flasher-android-arm64"
mv -f dist/esp-prog2-flasher "$OUT"
chmod +x "$OUT"

echo "Built: $OUT"
