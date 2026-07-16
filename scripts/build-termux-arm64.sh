#!/usr/bin/env bash
# Build the esp-prog2-flasher one-file executable for arm64 Android (Termux).
#
# DELIBERATE EXCEPTION to the repo's uv-only rule. On Termux we cannot use uv at
# all:
#   * uv itself rejects Android ("Unknown operating system: android").
#   * the project's build backend is uv_build (Rust); pip cannot build it on
#     Android because rustup has no aarch64-unknown-linux-android target.
#   * Termux's stdlib venv is broken (ensurepip fails).
# So this script does NOT install the project — it installs only the runtime
# dependencies (read from pyproject.toml, so they stay in lockstep) plus
# PyInstaller, straight into Termux's system pip, then builds from src/ (the
# spec's pathex=["src"] makes the package importable without an install).
# Everywhere else, use scripts/build.sh (uv).
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
#       Termux's `rust` package targets Android natively, unlike rustup.
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

# Read [project.dependencies] from pyproject.toml so this list never drifts from
# the real dependency set. We install the deps, NOT the project (its uv_build
# backend won't build on Android — see the header).
mapfile -t DEPS < <(
    python -c 'import pathlib, tomllib; print("\n".join(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["dependencies"]))'
)
if [ "${#DEPS[@]}" -eq 0 ]; then
    echo "Error: parsed no dependencies from pyproject.toml — aborting." >&2
    exit 1
fi

pip install "${DEPS[@]}" pyinstaller

# --noupx overrides the spec's upx=True: UPX is usually absent on Termux and can
# corrupt the aarch64 bootloader. The rest of the build reuses the shared spec,
# which analyses src/esp_prog2_flasher/__main__.py via pathex=["src"].
pyinstaller esp-prog2-flasher.spec --noconfirm --noupx

OUT="dist/esp-prog2-flasher-android-arm64"
mv -f dist/esp-prog2-flasher "$OUT"
chmod +x "$OUT"

echo "Built: $OUT"
