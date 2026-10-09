# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the esp-prog2-flasher one-file executable.

Bundles the hex/ and firmware/ data files plus the plugin data/entry-points that
pyocd, esptool, and textual need (target packs, stub-flasher JSONs, CSS/tree-sitter
data, and pyocd's ``pyocd.probe`` / ``pyocd.rtos`` entry-point plugins).
"""

from PyInstaller.utils.hooks import collect_all, collect_entry_point

datas = [
    ("hex/dfu_minima.hex", "hex"),
    ("hex/psoc6_radar_full_image.hex", "hex"),
    ("firmware/esp-prog2.bin", "firmware"),
    # Slim RA_DFP CMSIS pack so pyOCD resolves the r7fa4m1ab target offline,
    # without a prior `pyocd pack install` on the target machine. See packs/.
    ("packs/Renesas.RA_DFP.slim.pack", "packs"),
]
binaries = []
hiddenimports = []

for pkg in (
    "pyocd",
    "esptool",
    "textual",
    "cmsis_pack_manager",
    "capstone",
    "libusb_package",
):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

# pyocd loads its built-in probe/RTOS backends via importlib.metadata entry
# points at runtime (pyocd/core/plugin.py) — static analysis can't see these.
for group in ("pyocd.probe", "pyocd.rtos"):
    ep_datas, ep_hiddenimports = collect_entry_point(group)
    datas += ep_datas
    hiddenimports += ep_hiddenimports

a = Analysis(
    ["src/esp_prog2_flasher/__main__.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="esp-prog2-flasher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    onefile=True,
)
