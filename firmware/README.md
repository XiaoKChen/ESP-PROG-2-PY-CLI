# firmware/ — ESP-Prog-2 firmware

`esp-prog2.bin` here is a **custom build** of the
[esp-usb-bridge](https://github.com/espressif/esp-usb-bridge) firmware for the
ESP-Prog-2's ESP32-S3, with two changes from Espressif's stock image:

1. **SWD / CMSIS-DAP** debug interface (`CONFIG_DEBUG_PROBE_IFACE_SWD=y`), instead
   of the default **JTAG** (`esp_usb_jtag`). pyOCD and the RA4M1/SWD flashing in
   this repo need CMSIS-DAP — the stock launchpad image is JTAG and does **not**
   work for that.
2. **Mass storage (MSC) removed** (`CFG_TUD_MSC=0`, MSC interface dropped from the
   USB descriptor). No `ESPPROG_MSC` drive appears, so Windows AutoPlay never pops
   a folder open.

- **Format:** merged image (bootloader + partition table + app), flashed at `0x0`.
- **Flashed with:** `esptool` (a Python dependency of this project).

## Flashing the ESP-Prog-2

Use the TUI ("Flash ESP-Prog-2", key `p`) or the CLI:

```sh
uv run esp-prog2-flasher --flash-probe        # flashes the bundled esp-prog2.bin
```

**Put the board in ROM download mode first** (the running bridge firmware can't
reflash itself over its CDC port):

1. Hold **BOOT**, tap **RESET** (or hold BOOT while plugging in USB).
2. It re-enumerates as an ESP32-S3 ROM device (new COM port / PID); the TUI's
   "Serial" line flips to "download mode".
3. Run the flash; esptool writes `esp-prog2.bin` at `0x0` and resets.

After flashing it should enumerate as **CMSIS-DAP** (pyOCD sees it) with **no
drive**.

## Rebuilding it

The source lives on the **`esp-prog2-swd-no-msc`** branch of the fork
[XiaoKChen/esp-usb-bridge](https://github.com/XiaoKChen/esp-usb-bridge) — upstream
esp-usb-bridge plus the MSC removal and a CMSIS-DAPv2 interface-subclass fix
(pyOCD only accepts `bInterfaceSubClass == 0`, so SWD builds drop the Espressif
USB-JTAG identifiers in `eub_vendord.h`).

Clone the branch, then build in Docker (toolchain bundled — sidesteps the native
toolchain-download TLS failure and the spaces in a repo path):

```sh
git clone -b esp-prog2-swd-no-msc https://github.com/XiaoKChen/esp-usb-bridge.git C:\esp\esp-usb-bridge
docker run --rm -v C:\esp\esp-usb-bridge:/project -w /project espressif/idf:release-v5.3 \
  bash -c "git config --global --add safe.directory /project; \
    SDKCONFIG_DEFAULTS='sdkconfig.defaults;sdkconfig.defaults.esp32s3;sdkconfig.defaults.esp_prog2;sdkconfig.swd.defaults' \
    idf.py set-target esp32s3 build merge-bin"
# result: build/merged-binary.bin  ->  copied here as esp-prog2.bin
```

## The official image

`Get official (JTAG)` / `--update-probe-fw` downloads Espressif's stock
`esp-prog2.bin` as **`esp-prog2-official-jtag.bin`** (kept separate so it never
overwrites the SWD build). That image is **JTAG + MSC** — flashing it
(`--flash-probe --probe-bin firmware/esp-prog2-official-jtag.bin`) will disable
SWD/CMSIS-DAP and re-add the drive. Only use it deliberately.
