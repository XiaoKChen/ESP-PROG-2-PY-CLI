"""Command-line entry point. No args launches the Textual TUI."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import flasher
from .dfu_config import (
    DEFAULT_BOOT_CMD_ID,
    DEFAULT_BOOT_REPLY_ID,
    DEFAULT_MANUFACTURER,
    DEVICE_CAN_IDS,
    DEVICE_NAMES,
    MAX_CAN_ID,
    boot_ids_for_device,
)
from .flasher import TargetState

logger = logging.getLogger(__name__)


def _can_id(text: str) -> int:
    """Parse a CLI CAN ID accepting hex (``0x700``) or decimal (``1792``)."""
    try:
        value = int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid CAN ID {text!r}: use hex like 0x700 or decimal"
        ) from None
    if not 0 <= value <= MAX_CAN_ID:
        raise argparse.ArgumentTypeError(f"CAN ID must be 0x000..0x{MAX_CAN_ID:03X}, got {text!r}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="esp-prog2-flasher",
        description="Flash the RA4M1 bootloader (and, once flashed, an application .bin) "
        "through an ESP-Prog-2 (CMSIS-DAP), or reflash the ESP-Prog-2's own firmware. With "
        "no options, launches the interactive TUI.",
    )
    p.add_argument(
        "--list", action="store_true", help="List connected probes and serial ports, then exit."
    )
    p.add_argument(
        "--detect", action="store_true", help="Detect probe + RA4M1 target, print status, exit."
    )
    p.add_argument(
        "--flash", action="store_true", help="Flash the RA4M1 bootloader headlessly, then exit."
    )
    p.add_argument(
        "--psoc6",
        action="store_true",
        help="With --detect: look for a PSoC 6 target (IDU Radar) instead of the RA4M1.",
    )
    p.add_argument(
        "--flash-psoc6",
        action="store_true",
        help="Flash the PSoC 6 (CY8C6245) main flash over SWD, then exit (default: bundled "
        "hex/psoc6_radar_full_image.hex; override with --hex).",
    )
    p.add_argument(
        "--flash-app",
        type=Path,
        default=None,
        metavar="BIN",
        help="Flash an application .bin to the RA4M1 target over SWD, then exit. Programs "
        "the application region only (never the bootloader) — flash the bootloader with "
        "--flash first.",
    )
    p.add_argument(
        "--app-address",
        type=lambda text: int(text, 0),
        default=flasher.APP_BASE_ADDRESS,
        metavar="ADDR",
        help="Base address for --flash-app (hex like 0x4000 or decimal). Default: "
        f"0x{flasher.APP_BASE_ADDRESS:X} (the ArduinoCore-renesas UNO R4 Minima sketch "
        "load address).",
    )
    p.add_argument(
        "--flash-probe",
        action="store_true",
        help="Flash the ESP-Prog-2's own firmware (esp-prog2.bin) via esptool, then exit. "
        "Put the board in download mode first (hold BOOT, tap RESET).",
    )
    p.add_argument(
        "--update-probe-fw",
        action="store_true",
        help="Download Espressif's OFFICIAL (JTAG) esp-prog2.bin for reference, saved as "
        "esp-prog2-official-jtag.bin (does NOT replace the bundled SWD build), then exit.",
    )
    p.add_argument(
        "--hex",
        type=Path,
        default=None,
        help="Path to the .hex for --flash (default: bundled hex/dfu_minima.hex) or "
        "--flash-psoc6 (default: bundled hex/psoc6_radar_full_image.hex).",
    )
    p.add_argument(
        "--probe-bin",
        type=Path,
        default=None,
        help="Path to the ESP-Prog-2 firmware (default: bundled firmware/esp-prog2.bin).",
    )
    p.add_argument(
        "--port", default=None, help="Serial port for --flash-probe (default: auto-detect)."
    )
    p.add_argument(
        "--manufacturer",
        default=DEFAULT_MANUFACTURER,
        help="USB manufacturer name for the DFU config block (max 32 ASCII chars). "
        f"Default: {DEFAULT_MANUFACTURER!r}.",
    )
    p.add_argument(
        "--product",
        default=None,
        metavar="DEVICE",
        help="USB device name for the DFU config block. A KNOWN device — one of: "
        + ", ".join(DEVICE_NAMES)
        + " — has its bootloader CAN IDs fixed from the board table (--boot-cmd-id/"
        "--boot-reply-id are rejected). Any other value is a CUSTOM device whose CAN "
        "IDs you may set. Omit to keep the bootloader's compiled-in default "
        "(interactive runs prompt).",
    )
    p.add_argument(
        "--boot-cmd-id",
        type=_can_id,
        default=None,
        metavar="ID",
        help="Per-device bootloader CAN ID for host->boot frames (hex like 0x700 or decimal, "
        f"0x000..0x{MAX_CAN_ID:03X}). Must be paired with --boot-reply-id. "
        f"Omit both to keep the bootloader default 0x{DEFAULT_BOOT_CMD_ID:03X}.",
    )
    p.add_argument(
        "--boot-reply-id",
        type=_can_id,
        default=None,
        metavar="ID",
        help="Per-device bootloader CAN ID for boot->host frames (hex or decimal, "
        f"0x000..0x{MAX_CAN_ID:03X}). Must be paired with --boot-cmd-id. "
        f"Omit both to keep the bootloader default 0x{DEFAULT_BOOT_REPLY_ID:03X}.",
    )
    p.add_argument(
        "--freq",
        type=int,
        default=flasher.DEFAULT_FREQUENCY_HZ,
        help=f"SWD clock in Hz for target flashing (default: {flasher.DEFAULT_FREQUENCY_HZ}).",
    )
    return p


def _resolve_dfu_config(
    manufacturer: str,
    product: str | None,
    boot_cmd_id: int | None,
    boot_reply_id: int | None,
) -> tuple[str | None, str | None, int | None, int | None]:
    """Resolve the DFU config (USB names + optional CAN ID pair) for a headless flash.

    Writing a config block is gated on the *device* (product): with a device
    selected the block carries ``manufacturer`` (default "Normal Corporation"),
    that device, and its boot CAN ID pair. A KNOWN device (in ``DEVICE_NAMES``)
    takes its pair from the frozen board table and rejects any explicit
    ``--boot-cmd-id``/``--boot-reply-id`` (its IDs are not editable). A CUSTOM name
    may carry a user-supplied pair (all-or-nothing; omitted → bootloader defaults).
    With no device given, an interactive stdin prompts for one; non-interactive
    writes no block (the bootloader keeps its compiled-in defaults).
    """
    if product is not None:
        known_ids = boot_ids_for_device(product)
        if known_ids is not None:
            if boot_cmd_id is not None or boot_reply_id is not None:
                print(
                    f"--boot-cmd-id/--boot-reply-id are not allowed for the known device "
                    f"{product!r}; its bootloader CAN IDs are fixed at "
                    f"0x{known_ids[0]:03X}/0x{known_ids[1]:03X}. Use a custom device name "
                    "to set your own.",
                    file=sys.stderr,
                )
                sys.exit(2)
            return manufacturer, product, known_ids[0], known_ids[1]
        return manufacturer, product, boot_cmd_id, boot_reply_id
    if boot_cmd_id is not None or boot_reply_id is not None:
        print(
            "--boot-cmd-id/--boot-reply-id require a device (--product); no config block "
            "is written without one.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not sys.stdin.isatty():
        return None, None, None, None
    return _prompt_dfu_config(manufacturer)


def _prompt_dfu_config(
    default_mfr: str,
) -> tuple[str | None, str | None, int | None, int | None]:
    """Interactively pick a manufacturer, a device, and its CAN ID pair.

    A KNOWN device's CAN IDs come from the board table and are shown (not
    prompted); the trailing "Custom…" option prompts for a name and an editable
    CAN ID pair.
    """
    print("Custom USB names for DFU mode:")
    entered_mfr = input(f"  Manufacturer [{default_mfr}]: ").strip() or default_mfr
    print("  Device:")
    for i, name in enumerate(DEVICE_NAMES, 1):
        print(f"    {i}) {name}")
    custom_index = len(DEVICE_NAMES) + 1
    print(f"    {custom_index}) Custom…")
    choice = input(f"  Select 1-{custom_index} (blank = keep bootloader default): ").strip()
    if not choice:
        return None, None, None, None
    if not choice.isdigit() or not 1 <= int(choice) <= custom_index:
        print(f"Invalid selection: {choice!r}", file=sys.stderr)
        sys.exit(2)
    index = int(choice)
    if index == custom_index:
        name = input("  Custom device name: ").strip()
        if not name:
            print("Custom device name must not be empty.", file=sys.stderr)
            sys.exit(2)
        cmd_id, reply_id = _prompt_boot_ids()
        return entered_mfr, name, cmd_id, reply_id
    device = DEVICE_NAMES[index - 1]
    cmd_id, reply_id = DEVICE_CAN_IDS[device]
    print(
        f"  Bootloader CAN IDs (fixed for {device}): "
        f"host->boot=0x{cmd_id:03X} boot->host=0x{reply_id:03X}"
    )
    return entered_mfr, device, cmd_id, reply_id


def _prompt_boot_ids() -> tuple[int | None, int | None]:
    """Prompt for the boot CAN ID pair; blank both = keep bootloader defaults."""
    print(
        "  Bootloader CAN IDs (hex like 0x700 or decimal; blank = keep defaults "
        f"0x{DEFAULT_BOOT_CMD_ID:03X}/0x{DEFAULT_BOOT_REPLY_ID:03X}):"
    )
    cmd_raw = input("    host->boot ID: ").strip()
    reply_raw = input("    boot->host ID: ").strip()
    if not cmd_raw and not reply_raw:
        return None, None
    if not cmd_raw or not reply_raw:
        print("Both boot CAN IDs must be set together (or both left blank).", file=sys.stderr)
        sys.exit(2)
    try:
        return _can_id(cmd_raw), _can_id(reply_raw)
    except argparse.ArgumentTypeError as exc:
        print(f"Invalid CAN ID: {exc}", file=sys.stderr)
        sys.exit(2)


def main() -> None:
    # Hidden dispatch for frozen builds: flash_probe() re-execs this bundle with
    # this flag instead of "python -m esptool" (no such script exists when frozen).
    if len(sys.argv) > 1 and sys.argv[1] == flasher.ESPTOOL_SHIM_FLAG:
        import esptool

        esptool.main(sys.argv[2:])
        return

    args = _build_parser().parse_args()

    if args.list:
        probes = flasher.list_probes()
        if probes:
            for pr in probes:
                tag = " [ESP-Prog-2]" if pr.is_esp_prog else ""
                print(f"probe   {pr.unique_id}\t{pr.description}{tag}")
        else:
            print("probe   none connected")
        serial = flasher.find_probe_serial()
        if serial:
            mode = "download mode" if serial.in_download_mode else "bridge running"
            print(f"serial  {serial.device}\tpid 0x{serial.pid:04X} ({mode})")
        else:
            print("serial  none connected")
        return

    if args.detect:
        detect = flasher.detect_psoc6 if args.psoc6 else flasher.detect
        result = detect(frequency=args.freq)
        print(result.detail)
        sys.exit(0 if result.state is not TargetState.NO_PROBE else 2)

    if args.update_probe_fw:
        try:
            dest = flasher.download_probe_firmware(args.probe_bin, log=print)
            print(f"Saved: {dest}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Probe firmware download failed")
            print(f"Download failed: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if args.flash_app:
        try:
            flasher.flash_app(
                args.flash_app,
                frequency=args.freq,
                base_address=args.app_address,
                log=print,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Application flash failed")
            print(f"Application flash failed: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if args.flash_psoc6:
        hex_path = args.hex or flasher.find_default_psoc6_hex()
        if hex_path is None:
            print("No firmware specified and no bundled PSoC 6 hex found.", file=sys.stderr)
            sys.exit(2)
        try:
            flasher.flash_psoc6(hex_path, frequency=args.freq, log=print)
        except Exception as exc:  # noqa: BLE001
            logger.exception("PSoC 6 flash failed")
            print(f"PSoC 6 flash failed: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if args.flash_probe:
        fw = args.probe_bin or flasher.find_probe_firmware()
        if fw is None:
            print("No esp-prog2.bin found. Run --update-probe-fw first.", file=sys.stderr)
            sys.exit(2)
        port = args.port or (
            flasher.find_probe_serial().device if flasher.find_probe_serial() else None
        )
        try:
            flasher.flash_probe(fw, port=port, log=print)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ESP-Prog-2 flash failed")
            print(f"ESP-Prog-2 flash failed: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if args.flash:
        hex_path = args.hex or flasher.find_default_hex()
        if hex_path is None:
            print("No firmware specified and no bundled hex found.", file=sys.stderr)
            sys.exit(2)
        if (args.boot_cmd_id is None) != (args.boot_reply_id is None):
            print(
                "--boot-cmd-id and --boot-reply-id must be given together (or both omitted).",
                file=sys.stderr,
            )
            sys.exit(2)
        manufacturer, product, boot_cmd_id, boot_reply_id = _resolve_dfu_config(
            args.manufacturer, args.product, args.boot_cmd_id, args.boot_reply_id
        )
        try:
            flasher.flash(
                hex_path,
                frequency=args.freq,
                log=print,
                manufacturer=manufacturer,
                product=product,
                boot_cmd_id=boot_cmd_id,
                boot_reply_id=boot_reply_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Target flash failed")
            print(f"Flash failed: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    # Default: interactive TUI.
    from .app import run

    run(hex_path=args.hex or flasher.find_default_hex(), frequency=args.freq)


if __name__ == "__main__":
    main()
