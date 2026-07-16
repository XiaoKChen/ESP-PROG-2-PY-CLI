# ESP-Prog-2 Flasher

A `uv`-run Python **TUI/CLI** for the **ESP-Prog-2** debug probe. It does two jobs:

1. **Flash a bootloader to a target board** through the probe (auto-detects the
   ESP-Prog-2 on Espressif's USB VID `0x303A`/PID `0x1002`, detects whether the
   target MCU is connected, and programs it with **pyOCD**). It can optionally
   write a **device config block** into the target's data flash (`0x40101C00`) —
   **custom USB DFU manufacturer/product names** the bootloader advertises in DFU
   mode, plus the **per-device bootloader CAN ID pair** (host→boot / boot→host).
   Once the bootloader is in place it can also **flash an application `.bin`**
   (e.g. a PlatformIO/Arduino build) to the target's application region
   (`0x4000`) over SWD — the same image `pio run -t upload` sends over DFU, but
   pushed through the probe. It programs the application region only, leaving the
   bootloader untouched.
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

Both take an optional version (e.g. `scripts/install.sh v1.0.0`,
`.\scripts\install.ps1 -Version v1.0.0`); the default is the latest release.
If `esp-prog2-flasher` is already on your PATH, the scripts report the existing
install and prompt `Replace it? [y/N]` (default: keep). In a non-interactive /
non-TTY shell they skip the prompt and keep the existing install.
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

TUI keys: **d** detect, **f** flash target (RA4M1), **a** flash application
`.bin`, **p** flash the ESP-Prog-2 firmware, **u** fetch Espressif's official
(JTAG) image for reference, **q** quit. "Flash target" and "Flash app" stay
disabled until an RA4M1 is detected. The **Application (RA4M1 over SWD)** panel
(pick a `.bin`, then Flash app) writes to the app region above the bootloader.

The target panel also has a **USB manufacturer** field (defaults to
`Normal Corporation`) and a **USB device** dropdown. Pick a known device to write
its config block (DFU names + the device's fixed boot CAN IDs) into the target's
data flash along with the bootloader; the CAN ID fields fill in and lock. Pick
**Custom…** to type your own device name and (optionally) enter a boot CAN ID
pair — leave both ID fields blank to keep the bootloader defaults. Leave the
dropdown on "keep bootloader default" to skip the config write entirely.

### Headless / scripting

```sh
uv run esp-prog2-flasher --list             # list connected probes + serial ports
uv run esp-prog2-flasher --detect           # print probe+target status, exit
uv run esp-prog2-flasher --flash            # flash the RA4M1 bootloader (bundled hex)
uv run esp-prog2-flasher --flash --hex path/to/other.hex
uv run esp-prog2-flasher --flash --product "IDU Controller"                 # also write the device config block
uv run esp-prog2-flasher --flash --product "IDU Controller" --manufacturer "Acme"
uv run esp-prog2-flasher --flash --product "My Custom Board" --boot-cmd-id 0x700 --boot-reply-id 0x701

uv run esp-prog2-flasher --flash-app path/to/firmware.bin        # flash an app .bin to the RA4M1 app region (0x4000)
uv run esp-prog2-flasher --flash-app path/to/firmware.bin --app-address 0x4000   # override the base address

uv run esp-prog2-flasher --flash-probe       # flash the bundled SWD firmware (needs download mode)
uv run esp-prog2-flasher --flash-probe --port COM110
uv run esp-prog2-flasher --flash-probe --probe-bin path/to/other.bin
uv run esp-prog2-flasher --update-probe-fw   # fetch official JTAG image as esp-prog2-official-jtag.bin (reference)
```

`--freq <hz>` overrides the SWD clock for target flashing (default 1 MHz).
`--detect` exit codes: `0` target/probe OK, `2` no probe found.

`--product DEVICE` writes the device config block alongside the bootloader flash.
`--manufacturer NAME` sets the manufacturer string (max 32 ASCII chars, default
`Normal Corporation`) and only takes effect when a `--product` is given. Omit
`--product` and, on an interactive terminal, `--flash` prompts you to pick a
device (or skip); non-interactively it writes no block and the bootloader keeps
its compiled-in defaults.

A **known device** (from the list below) has its boot CAN ID pair fixed from the
board table — passing `--boot-cmd-id`/`--boot-reply-id` for one is rejected. Any
other `--product` value is a **custom device** whose CAN IDs you may set with
`--boot-cmd-id ID` and `--boot-reply-id ID` (given together; hex like `0x700` or
decimal, `0x000..0x7FF`). Omit both to keep the bootloader defaults
(`0x79E`/`0x79F`). Known `--product` values:

- `ODU Controller`, `ODU Superheat`, `ODU Air Sensor`, `ODU Power Board`
- `IDU Controller`, `IDU Power Board`, `IDU Radar`, `IDU Articulation`, `IDU Air Sensor`

## What it does

1. **Auto-detect the ESP-Prog-2** — enumerates CMSIS-DAP probes and picks the one
   identifying as Espressif/ESP-Prog (falls back to the first CMSIS-DAP probe).
2. **Detect the target** — opens an SWD session against `r7fa4m1ab` and examines
   the Cortex-M core. Reports one of: *no probe* / *probe but no target* /
   *RA4M1 connected* / *RA4M1 connected (blank flash)*. A blank chip reads
   `pc=0xfffffffe` after reset — still flashable.
3. **Flash target** — programs the selected `.hex` via pyOCD's `FileProgrammer`
   (equivalent to `pyocd flash -t r7fa4m1ab <hex>`), shows progress, then resets
   the target. If a device was given, it also writes the encoded config block
   (DFU names + boot CAN ID pair) to the target's data flash (`0x40101C00`)
   before the reset.
4. **Flash application** — programs an app `.bin` at the application base address
   (`0x4000` by default, overridable) via pyOCD's `FileProgrammer`, then resets.
   A `.bin` carries no load address, so the base address is supplied explicitly;
   only the pages the image touches are sector-erased, so the bootloader region
   below it is left intact.
5. **Flash ESP-Prog-2** — writes `firmware/esp-prog2.bin` to the ESP32-S3 with
   esptool (`write-flash 0x0`). Requires the board in ROM download mode (hold
   BOOT, tap RESET) — see [`firmware/README.md`](firmware/README.md).

## Layout

```
ESP-PROG-2-PY-CLI/
├── src/esp_prog2_flasher/
│   ├── flasher.py     # pyOCD (target) + esptool (probe) logic — no UI deps
│   ├── dfu_config.py  # encode the device config block (DFU names + boot CAN IDs)
│   ├── app.py         # Textual TUI
│   ├── cli.py         # argparse entry point (TUI by default)
│   └── __main__.py    # `python -m esp_prog2_flasher`
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
