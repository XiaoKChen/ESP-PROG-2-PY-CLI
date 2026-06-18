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
uv run esp-prog2-flasher --update-probe-fw   # fetch official JTAG image as esp-prog2-official-jtag.bin (reference)
```

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
