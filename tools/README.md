# tools/ — what's needed for flashing to work

Flashing the **target board** (the RA4M1 / UNO R4 Minima) is done with **pyOCD**
over the ESP-Prog-2's CMSIS-DAP interface. Everything pyOCD needs is a Python
dependency of this project (`uv add pyocd` already pinned it), plus the Renesas
device support pack.

## Requirements

1. **pyOCD** — installed as a project dependency. Run via `uv run`.
2. **The `r7fa4m1ab` CMSIS device pack** — pyOCD uses it to know the RA4M1's
   flash layout. It is usually already present. If `--detect` reports the target
   type is unknown, install it once:

   ```sh
   uv run pyocd pack install r7fa4m1ab
   ```

3. **The ESP-Prog-2 probe**, enumerated as a CMSIS-DAP device
   (USB `VID 0x303A` / `PID 0x1002`, Espressif), wired to the target's SWD pins
   (SWDIO/SWCLK/GND/RESET).

## `pyocd.yaml`

Drop-in pyOCD config (target + clock) so you can run bare `pyocd` commands from
the project root without repeating `-t r7fa4m1ab`. The TUI/CLI does not require
it — it sets the same options in code — but it is handy for manual debugging.

## Why not OpenOCD?

The repo ships OpenOCD configs at `../../tools/openocd/` (`ra4m1.cfg`,
`R7FA4M1AB.cfg`). They connect and read the core IDCODE fine, **but the installed
xPack OpenOCD 0.12 build does not include the `renesas_ra` flash driver** —
`program <hex>` fails with *"flash driver 'renesas_ra' not found"*. Only Arduino's
OpenOCD fork ships that driver. So this project flashes the RA4M1 with pyOCD.
Use OpenOCD only for connection tests.
