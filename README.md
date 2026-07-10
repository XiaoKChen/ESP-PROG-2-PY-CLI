# ESP-Prog-2 Flasher

A `uv`-run Python **TUI/CLI** for the **ESP-Prog-2** debug probe. It does two jobs:

1. **Flash a bootloader to a target board** through the probe (auto-detects the
   ESP-Prog-2 on Espressif's USB VID `0x303A`/PID `0x1002`, detects whether the
   target MCU is connected, and programs it with **pyOCD**).
2. **Reflash the ESP-Prog-2's own firmware** — writes the bundled
   `firmware/esp-prog2.bin` to the ESP32-S3 with **esptool**.

Default target: **Renesas RA4M1 / R7FA4M1AB** (the MCU on an Arduino UNO R4 Minima).
Default target firmware: bundled `hex/dfu_minima.hex` (the stock R4 Minima DFU bootloader).
Default probe firmware: bundled `firmware/esp-prog2.bin` — a **custom esp-usb-bridge
build** (SWD/CMSIS-DAP interface + mass storage disabled). The stock launchpad image is
JTAG-mode with an MSC drive and does **not** work with pyOCD; see
[`firmware/README.md`](firmware/README.md) for the why and the rebuild recipe.

## Install (prebuilt binary)

Standalone binaries for Windows/macOS/Linux are attached to every
[GitHub release](https://github.com/XiaoKChen/ESP-PROG-2-PY-CLI/releases).
The repo is currently **private**, so the installers download through the
[GitHub CLI](https://cli.github.com/) — run `gh auth login` once first.
From a clone of this repo:

```sh
scripts/install.sh        # macOS / Linux → ~/.local/bin/esp-prog2-flasher
```

```powershell
.\scripts\install.ps1     # Windows → %LOCALAPPDATA%\Programs\esp-prog2-flasher (added to user PATH)
```

Both take an optional version (e.g. `scripts/install.sh v0.2.0`,
`.\scripts\install.ps1 -Version v0.2.0`); the default is the latest release.
Or skip the scripts and grab an asset directly:

```sh
gh release download --repo XiaoKChen/ESP-PROG-2-PY-CLI --pattern esp-prog2-flasher-linux   # or -macos / -windows.exe
```

> If the repo goes public, the scripts fall back to plain
> `curl`/`Invoke-WebRequest` against the release URLs — no `gh` needed.

## Setup

Requires [uv](https://docs.astral.sh/uv/). Everything runs through it — no
manual venv or pip.

```sh
uv sync          # create .venv from uv.lock
```

## Run it

```sh
uv run esp-prog2-flasher          # launch the interactive TUI
```

TUI keys: **d** detect, **f** flash target (RA4M1), **p** flash the ESP-Prog-2
firmware, **u** fetch Espressif's official (JTAG) image for reference, **q** quit.
"Flash target" stays disabled until an RA4M1 is detected.

### Headless / scripting

```sh
uv run esp-prog2-flasher --list             # list connected probes + serial ports
uv run esp-prog2-flasher --detect           # print probe+target status, exit
uv run esp-prog2-flasher --flash            # flash the RA4M1 bootloader (bundled hex)
uv run esp-prog2-flasher --flash --hex path/to/other.hex

uv run esp-prog2-flasher --flash-probe       # flash the bundled SWD firmware (needs download mode)
uv run esp-prog2-flasher --flash-probe --port COM110
uv run esp-prog2-flasher --flash-probe --probe-bin path/to/other.bin
uv run esp-prog2-flasher --update-probe-fw   # fetch official JTAG image as esp-prog2-official-jtag.bin (reference)
```

`--freq <hz>` overrides the SWD clock for target flashing (default 1 MHz).
`--detect` exit codes: `0` target/probe OK, `2` no probe found.

## What it does

1. **Auto-detect the ESP-Prog-2** — enumerates CMSIS-DAP probes and picks the one
   identifying as Espressif/ESP-Prog (falls back to the first CMSIS-DAP probe).
2. **Detect the target** — opens an SWD session against `r7fa4m1ab` and examines
   the Cortex-M core. Reports one of: *no probe* / *probe but no target* /
   *RA4M1 connected* / *RA4M1 connected (blank flash)*. A blank chip reads
   `pc=0xfffffffe` after reset — still flashable.
3. **Flash target** — programs the selected `.hex` via pyOCD's `FileProgrammer`
   (equivalent to `pyocd flash -t r7fa4m1ab <hex>`), shows progress, then resets
   the target.
4. **Flash ESP-Prog-2** — writes `firmware/esp-prog2.bin` to the ESP32-S3 with
   esptool (`write-flash 0x0`). Requires the board in ROM download mode (hold
   BOOT, tap RESET) — see [`firmware/README.md`](firmware/README.md).

## Layout

```
ESP-PROG-2-PY-CLI/
├── src/esp_prog2_flasher/
│   ├── flasher.py   # pyOCD (target) + esptool (probe) logic — no UI deps
│   ├── app.py       # Textual TUI
│   ├── cli.py       # argparse entry point (TUI by default)
│   └── __main__.py  # `python -m esp_prog2_flasher`
├── hex/             # bundled dfu_minima.hex (RA4M1 bootloader); override with --hex
├── firmware/        # bundled esp-prog2.bin (ESP-Prog-2's own firmware) + README
├── tools/           # pyOCD config + notes on flashing prerequisites
├── scripts/         # build.ps1 / build.sh — PyInstaller one-file build
├── esp-prog2-flasher.spec   # PyInstaller spec (bundles hex/ + firmware/)
└── pyproject.toml
```

## Notes

- The target **must be flashed with pyOCD, not OpenOCD** — the installed OpenOCD
  build lacks the `renesas_ra` flash driver. See [`tools/README.md`](tools/README.md).
- Reflashing the **probe** needs it in ROM download mode (hold BOOT, tap RESET)
  and uses esptool — see [`firmware/README.md`](firmware/README.md).
- pyOCD only accepts a CMSIS-DAPv2 interface whose USB **subclass is 0**. The
  stock esp-usb-bridge advertises subclass `0xFF` (its esp_usb_jtag id), so the
  bundled firmware is built with the interface subclass forced to `0` — without
  that, the probe enumerates but pyOCD reports "no probe."
- If `--detect` says the target type is unknown, install the device pack once:
  `uv run pyocd pack install r7fa4m1ab`.

## Development

```sh
uv run ruff check src/      # lint
uv run ruff format src/     # format
uv run pytest               # tests (tests/)
uv run mypy src/            # type check (strict)
uv add <pkg>                # add a dependency (--dev for tooling)
```

Python version: pinned in `.python-version` / `pyproject.toml`.

### Standalone binary

A one-file executable (no Python install required to run it) can be built with
PyInstaller:

```sh
uv run pyinstaller esp-prog2-flasher.spec --noconfirm   # or scripts/build.ps1 / scripts/build.sh
```

Produces `dist/esp-prog2-flasher.exe` (Windows) or `dist/esp-prog2-flasher`
(macOS/Linux). The bundled `hex/` and `firmware/` files are packed into the
binary. GitHub Actions (`.github/workflows/build.yml`) builds all three
platforms on every push and attaches them to the GitHub release on `v*` tags.

Note: flashing the RA4M1 target for the first time still requires pyOCD's
`r7fa4m1ab` device pack, which pyOCD installs into the user's home directory
(`~/.pyocd`) at runtime, not into the bundle. Run `uv run pyocd pack install
r7fa4m1ab` once (from any pyOCD install, not necessarily this project's venv)
before the first target flash on a machine.

## Dependencies

All declared in `pyproject.toml`, resolved by uv:

- **pyocd** — SWD/CMSIS-DAP flashing of the RA4M1 target through the probe.
- **esptool** — reflashing the ESP-Prog-2's own ESP32-S3 firmware.
- **pyserial** — serial-port enumeration for probe auto-detection.
- **textual** — the interactive TUI.
- **truststore** — system CA trust for the official-firmware download.
