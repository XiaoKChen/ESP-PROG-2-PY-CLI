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

## Windows: probe not detected but COM port present

**Symptom:** `esp-prog2-flasher --detect` reports *"No ESP-Prog-2 / CMSIS-DAP
probe connected"* even though the probe is plugged in, and `--list` still shows
its serial port (PID 0x1002, "bridge running"). Replugging does not help. The
CDC serial interface works, but pyOCD sees no probe.

**Root cause:** The probe's CMSIS-DAP USB interface (interface 2,
`USB\VID_303A&PID_1002&MI_02`) is bound to the WinUSB driver — sometimes via a
Zadig/libwdi-generated "WinUSB Generic Device" INF — but the interface's
registry `Device Parameters` key has **no `DeviceInterfaceGUIDs` value**.
Without that GUID, winusb.sys never registers a device interface (no symbolic
link), so libusb's `libusb_open()` fails with *"Entity not found"* and pyOCD's
CMSIS-DAPv2 backend silently drops the device. (The HID/CMSIS-DAPv1 path is
also unavailable because the interface is not bound to the HID class driver.)

To confirm: `Get-PnpDeviceProperty -InstanceId '<MI_02 instance id>' -KeyName
'DEVPKEY_Device_Service'` shows `WinUSB`, but `Get-ItemProperty
'HKLM:\SYSTEM\CurrentControlSet\Enum\<MI_02 instance id>\Device Parameters'`
has no `DeviceInterfaceGUIDs`.

**Fix** — add the missing GUID and restart the interface:

```powershell
# Run in an elevated (Administrator) PowerShell
$dap = Get-PnpDevice | Where-Object { $_.InstanceId -match 'VID_303A&PID_1002&MI_02' -and $_.Status -eq 'OK' }
$params = "HKLM:\SYSTEM\CurrentControlSet\Enum\$($dap.InstanceId)\Device Parameters"
New-ItemProperty -Path $params -Name DeviceInterfaceGUIDs -PropertyType MultiString `
  -Value '{CDB3B5AD-293B-4663-AA36-1AAE46463776}' -Force
pnputil /restart-device $dap.InstanceId
```

Any well-formed GUID works — libusb enumerates whatever GUID is listed. The fix
persists across replugs of the same probe on the same machine; a different
probe unit or a driver reinstall may need it re-applied.

**Verify:** `uv run esp-prog2-flasher --detect` now finds the probe (prints its
serial). A follow-on *"no RA4M1 target responded over SWD"* message just means
no target board is wired/powered — probe detection itself is fixed.
