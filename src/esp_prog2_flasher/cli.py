"""Command-line entry point. No args launches the Textual TUI."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import flasher
from .flasher import TargetState

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="esp-prog2-flasher",
        description="Flash the RA4M1 bootloader through an ESP-Prog-2 (CMSIS-DAP), or reflash "
        "the ESP-Prog-2's own firmware. With no options, launches the interactive TUI.",
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
        help="Path to the RA4M1 .hex (default: bundled hex/dfu_minima.hex).",
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
        default=None,
        help="USB manufacturer name to write into the DFU config block (max 32 ASCII chars). "
        "Omit to keep the bootloader's compiled-in default.",
    )
    p.add_argument(
        "--product",
        default=None,
        help="USB product/device name to write into the DFU config block (max 32 ASCII chars). "
        "Omit to keep the bootloader's compiled-in default.",
    )
    p.add_argument(
        "--freq",
        type=int,
        default=flasher.DEFAULT_FREQUENCY_HZ,
        help=f"SWD clock in Hz for target flashing (default: {flasher.DEFAULT_FREQUENCY_HZ}).",
    )
    return p


def _resolve_dfu_names(
    manufacturer: str | None, product: str | None
) -> tuple[str | None, str | None]:
    """Resolve the DFU USB names for a headless flash.

    If neither is given and stdin is interactive, prompt for both (blank keeps
    the bootloader default). Non-interactive with neither given writes no config
    block. Supplying exactly one on the command line is an error.
    """
    if manufacturer is not None or product is not None:
        if manufacturer is None or product is None:
            print(
                "--manufacturer and --product must be given together (or both omitted).",
                file=sys.stderr,
            )
            sys.exit(2)
        return manufacturer, product

    if not sys.stdin.isatty():
        return None, None

    print("Custom USB names (press Enter to keep the bootloader's defaults):")
    entered_mfr = input("  Manufacturer: ").strip()
    entered_prod = input("  Product/device: ").strip()
    if not entered_mfr and not entered_prod:
        return None, None
    if not entered_mfr or not entered_prod:
        print("Enter both a manufacturer and a product, or leave both blank.", file=sys.stderr)
        sys.exit(2)
    return entered_mfr, entered_prod


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
        result = flasher.detect(frequency=args.freq)
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
        manufacturer, product = _resolve_dfu_names(args.manufacturer, args.product)
        try:
            flasher.flash(
                hex_path,
                frequency=args.freq,
                log=print,
                manufacturer=manufacturer,
                product=product,
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
