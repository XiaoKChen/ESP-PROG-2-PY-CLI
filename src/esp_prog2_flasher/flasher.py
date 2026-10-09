"""Probe detection and bootloader flashing for the ESP-Prog-2 / RA4M1 target.

Wraps the pyOCD Python API. The proven, equivalent command-line path is:

    pyocd flash -t r7fa4m1ab dfu_minima.hex

This module reproduces that programmatically and adds ESP-Prog-2 auto-detection
and target-presence detection. It deliberately has no Textual dependency so the
same logic backs both the TUI and the headless ``--detect`` / ``--flash`` CLI.

Note: the installed OpenOCD build lacks the ``renesas_ra`` flash driver, so
flashing the RA4M1 must go through pyOCD, not OpenOCD.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from intelhex import IntelHex
    from pyocd.core.session import Session
    from pyocd.probe.debug_probe import DebugProbe

logger = logging.getLogger(__name__)

# The ESP-Prog-2 enumerates as a CMSIS-DAP device on Espressif's USB vendor ID.
ESP_PROG_VID = 0x303A
ESP_PROG_PID = 0x1002
# Renesas RA4M1 (Arduino UNO R4 Minima MCU) — pyOCD pack target name.
TARGET_TYPE = "r7fa4m1ab"
# Infineon PSoC 6 (CY8C6245 / PSoC 62S3) — built into pyOCD, no CMSIS pack needed.
PSOC6_TARGET_TYPE: Final[str] = "cy8c6xx5"
DEFAULT_FREQUENCY_HZ = 1_000_000
DEFAULT_BAUD_RATE: Final[int] = 921600
# Hidden dispatch flag: when a frozen (PyInstaller) build re-execs itself to run
# esptool, cli.py intercepts this as argv[1] and calls esptool.main() in-process
# instead of parsing it as a normal CLI flag. See flash_probe().
ESPTOOL_SHIM_FLAG: Final[str] = "--esptool-shim"

_HEX_NAME = "dfu_minima.hex"
_PSOC6_HEX_NAME = "psoc6_radar_full_image.hex"
# Slim Renesas.RA_DFP CMSIS pack (pdsc + RA4M1 flash algorithms only) bundled so
# pyOCD can resolve the r7fa4m1ab target offline — without the pack first being
# downloaded into the user's ~/.pyocd cache via `pyocd pack install`. See packs/.
_PACK_NAME = "Renesas.RA_DFP.slim.pack"
# A blank Cortex-M reads this in PC after reset (vector table = 0xFFFFFFFF).
_BLANK_PC = 0xFFFFFFFE
_PC_ADDRESS_MASK: Final[int] = 0xFFFFFFFE
_DOWNLOAD_TIMEOUT_S: Final[int] = 60

# The Arduino UNO R4 Minima / RA4M1 core (ArduinoCore-renesas) reserves the low
# 16 KiB of code flash for the bootloader and links the sketch to start right
# after it: variants/MINIMA/memory_regions.ld defines FLASH_IMAGE_START = 0x4000,
# and fsp.ld uses it as the sketch's FLASH region origin — this is the address a
# PlatformIO/Arduino build for this board is linked at, i.e. "where pio upload
# writes it". Verified against the bootloader bundled in this repo
# (hex/dfu_minima.hex): its code ends at 0x3088 (well under 0x4000) and its ID
# Code block write lands at 0x01010018, matching ID_CODE_START in that same
# memory_regions.ld — confirming this bootloader was built from that exact
# linker layout.
APP_BASE_ADDRESS: Final[int] = 0x4000

# PSoC 62S3 main flash (512 KiB). Only this range is ever programmed — never
# SFLASH (0x16000000) or eFuse.
PSOC6_FLASH_START: Final[int] = 0x10000000
PSOC6_FLASH_END: Final[int] = 0x10080000
# Cypress/Infineon hex files carry programming metadata (checksum, chip
# protection) as pseudo-sections up here; they are not target memory.
PSOC6_METADATA_START: Final[int] = 0x90000000
PSOC6_METADATA_END: Final[int] = 0xA0000000

ProgressCallback = Callable[[float], None]
LogCallback = Callable[[str], None]


class FlasherError(Exception):
    """Base error for this module — callers catch this, not Exception."""


class ProbeNotFoundError(FlasherError):
    """Raised when no ESP-Prog-2 / CMSIS-DAP probe is connected."""


class FlashFailedError(FlasherError):
    """Raised when programming the target or the probe's own firmware fails."""


def _find_bundled(*parts: str) -> Path | None:
    """Locate a data file, checking a PyInstaller onefile bundle before falling
    back to searching upward from the package/CWD (source and installed-package runs).
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and meipass:
        candidate = Path(meipass, *parts)
        if candidate.is_file():
            return candidate

    seen: set[Path] = set()
    for base in (Path(__file__).resolve(), Path.cwd().resolve()):
        for parent in [base, *base.parents]:
            candidate = parent.joinpath(*parts)
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_file():
                return candidate
    return None


def find_default_hex() -> Path | None:
    """Locate the bundled ``hex/dfu_minima.hex`` regardless of how we were launched."""
    return _find_bundled("hex", _HEX_NAME)


def find_default_psoc6_hex() -> Path | None:
    """Locate the bundled ``hex/psoc6_radar_full_image.hex`` (bootloader + App1)."""
    return _find_bundled("hex", _PSOC6_HEX_NAME)


def find_default_pack() -> Path | None:
    """Locate the bundled slim RA_DFP CMSIS pack, which defines the r7fa4m1ab
    target and its flash algorithm so the RA4M1 can be flashed without the pack
    first being installed into the user's cmsis-pack-manager cache."""
    return _find_bundled("packs", _PACK_NAME)


@dataclass(frozen=True)
class ProbeInfo:
    """Display-friendly snapshot of a connected debug probe."""

    unique_id: str
    product_name: str
    vendor_name: str
    description: str
    is_esp_prog: bool

    @property
    def label(self) -> str:
        return f"{self.description or self.product_name} ({self.unique_id})"


class TargetState(Enum):
    NO_PROBE = auto()
    NO_TARGET = auto()
    CONNECTED = auto()
    CONNECTED_BLANK = auto()


@dataclass
class DetectResult:
    state: TargetState
    detail: str
    probe: ProbeInfo | None = None

    @property
    def flashable(self) -> bool:
        return self.state in (TargetState.CONNECTED, TargetState.CONNECTED_BLANK)


def _is_esp_prog(probe: DebugProbe) -> bool:
    text = " ".join(
        filter(
            None,
            [
                getattr(probe, "vendor_name", "") or "",
                getattr(probe, "product_name", "") or "",
                getattr(probe, "description", "") or "",
            ],
        )
    ).lower()
    return "espressif" in text or "esp-prog" in text or "esp prog" in text


def _to_info(probe: DebugProbe) -> ProbeInfo:
    return ProbeInfo(
        unique_id=probe.unique_id or "",
        product_name=getattr(probe, "product_name", "") or "",
        vendor_name=getattr(probe, "vendor_name", "") or "",
        description=probe.description or "",
        is_esp_prog=_is_esp_prog(probe),
    )


def _raw_probes(unique_id: str | None = None) -> list[DebugProbe]:
    # Imported lazily so module import never blocks on USB enumeration.
    from pyocd.core.helpers import ConnectHelper

    return ConnectHelper.get_all_connected_probes(
        blocking=False, unique_id=unique_id, print_wait_message=False
    )


def _choose_raw(unique_id: str | None = None) -> DebugProbe | None:
    probes = _raw_probes(unique_id)
    if not probes:
        return None
    for probe in probes:
        if _is_esp_prog(probe):
            return probe
    # Fall back to the first CMSIS-DAP probe if none self-identify as Espressif.
    return probes[0]


def list_probes() -> list[ProbeInfo]:
    """Return every connected debug probe, flagging which look like an ESP-Prog-2."""
    return [_to_info(p) for p in _raw_probes()]


def find_esp_prog() -> ProbeInfo | None:
    probe = _choose_raw()
    return _to_info(probe) if probe is not None else None


def _open_session(
    probe: DebugProbe,
    frequency: int,
    target_type: str = TARGET_TYPE,
    use_pack: bool = True,
) -> Session:
    from pyocd.core.session import Session

    options: dict[str, object] = {"target_override": target_type, "frequency": frequency}
    # Point pyOCD at the bundled pack when present so r7fa4m1ab resolves offline.
    # Absent (e.g. a plain `uv run` dev checkout), pyOCD falls back to the pack
    # installed in the user's cmsis-pack-manager cache, preserving old behaviour.
    # Built-in targets (PSoC 6) pass use_pack=False.
    pack = find_default_pack() if use_pack else None
    if pack is not None:
        options["pack"] = [str(pack)]
    return Session(probe, options=options)


def detect(unique_id: str | None = None, frequency: int = DEFAULT_FREQUENCY_HZ) -> DetectResult:
    """Detect the ESP-Prog-2 and whether an RA4M1 target is reachable through it."""
    return _detect_target(unique_id, frequency, TARGET_TYPE, "RA4M1", use_pack=True)


def detect_psoc6(
    unique_id: str | None = None, frequency: int = DEFAULT_FREQUENCY_HZ
) -> DetectResult:
    """Detect the ESP-Prog-2 and whether a PSoC 6 target is reachable through it."""
    return _detect_target(unique_id, frequency, PSOC6_TARGET_TYPE, "PSoC 6", use_pack=False)


def _detect_target(
    unique_id: str | None,
    frequency: int,
    target_type: str,
    label: str,
    use_pack: bool,
) -> DetectResult:
    probe = _choose_raw(unique_id)
    if probe is None:
        return DetectResult(TargetState.NO_PROBE, "No ESP-Prog-2 / CMSIS-DAP probe connected.")

    info = _to_info(probe)
    session = _open_session(probe, frequency, target_type, use_pack)
    try:
        session.open()  # examines the DP and the Cortex-M core
        target = session.target
        try:
            target.halt()
            pc = target.read_core_register("pc")
            blank = (pc & _PC_ADDRESS_MASK) == (_BLANK_PC & _PC_ADDRESS_MASK)
            if blank:
                return DetectResult(
                    TargetState.CONNECTED_BLANK,
                    f"{label} connected via {info.unique_id} — flash is blank/unprogrammed "
                    f"(pc=0x{pc:08x}). Ready to flash.",
                    info,
                )
            return DetectResult(
                TargetState.CONNECTED,
                f"{label} connected via {info.unique_id} (pc=0x{pc:08x}).",
                info,
            )
        except Exception:
            # Core examined but could not be halted/read — still present and flashable.
            logger.debug("core halt/read failed for %s", info.unique_id, exc_info=True)
            return DetectResult(
                TargetState.CONNECTED, f"{label} connected via {info.unique_id}.", info
            )
    except Exception as exc:  # noqa: BLE001 — surface any link/examine failure as "no target"
        return DetectResult(
            TargetState.NO_TARGET,
            f"Probe {info.unique_id} found, but no {label} target responded over SWD "
            f"({type(exc).__name__}). Check wiring and target power.",
            info,
        )
    finally:
        try:
            session.close()
        except Exception:
            logger.debug("session close failed for %s", info.unique_id, exc_info=True)


def flash(
    hex_path: Path,
    unique_id: str | None = None,
    frequency: int = DEFAULT_FREQUENCY_HZ,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
    manufacturer: str | None = None,
    product: str | None = None,
    boot_cmd_id: int | None = None,
    boot_reply_id: int | None = None,
) -> None:
    """Program ``hex_path`` to the RA4M1 target and reset it. Raises on failure.

    When both ``manufacturer`` and ``product`` are given, the DFU config block is
    also written to the data flash in the same session (after the bootloader hex,
    before the final reset), optionally carrying a per-device ``boot_cmd_id`` /
    ``boot_reply_id`` CAN ID pair. Omit the names to leave the board on the
    bootloader's compiled-in defaults.
    """
    from pyocd.flash.file_programmer import FileProgrammer

    hex_path = Path(hex_path)
    if not hex_path.is_file():
        raise FileNotFoundError(f"Firmware file not found: {hex_path}")

    # Fail fast on bad names/IDs before touching hardware.
    config_block = _config_block_or_none(manufacturer, product, boot_cmd_id, boot_reply_id)

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    probe = _choose_raw(unique_id)
    if probe is None:
        raise ProbeNotFoundError("No ESP-Prog-2 / CMSIS-DAP probe connected.")

    _log(f"Opening {TARGET_TYPE} via {probe.unique_id} @ {frequency // 1000} kHz")
    session = _open_session(probe, frequency)
    session.open()
    try:
        # Reset-and-halt into a known-clean state BEFORE programming. Connecting
        # to a *running* target — the RA4M1 executing its bootloader/app, with
        # the clock configured and USB/CAN/flash-LP peripherals live (a parked
        # bootloader leaves R_FLASH_LP open) — leaves pyOCD's flash algorithm
        # unable to initialize the RA4M1 flash controller: the first program()
        # dies with "flash init failure" and only a second attempt (core already
        # halted from the first) succeeds. Resetting to the halted reset vector
        # first makes every attempt start from the same clean state.
        _log("Reset/halt target")
        session.target.reset_and_halt()
        _log(f"Programming {hex_path.name} ({hex_path.stat().st_size} bytes)")
        programmer = FileProgrammer(session, progress=progress)
        programmer.program(str(hex_path))
        if config_block is not None:
            _program_config_block(session, config_block, _log)
        _log("Resetting target")
        session.target.reset()
        _log("Done.")
    finally:
        try:
            session.close()
        except Exception:
            logger.debug("session close failed for %s", probe.unique_id, exc_info=True)


def flash_app(
    bin_path: Path,
    unique_id: str | None = None,
    frequency: int = DEFAULT_FREQUENCY_HZ,
    base_address: int = APP_BASE_ADDRESS,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
) -> None:
    """Program an application ``.bin`` at ``base_address`` and reset. Raises on failure.

    Flash the bootloader with ``flash()`` first — this programs the application
    region ONLY (sector erase of just the written pages, never a chip erase), so
    the already-flashed bootloader is left untouched. A ``.bin`` carries no
    embedded load address, so ``base_address`` (default ``APP_BASE_ADDRESS``) is
    passed explicitly to pyOCD's ``FileProgrammer``.
    """
    from pyocd.flash.file_programmer import FileProgrammer

    bin_path = Path(bin_path)
    if not bin_path.is_file():
        raise FileNotFoundError(f"Application file not found: {bin_path}")

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    probe = _choose_raw(unique_id)
    if probe is None:
        raise ProbeNotFoundError("No ESP-Prog-2 / CMSIS-DAP probe connected.")

    _log(f"Opening {TARGET_TYPE} via {probe.unique_id} @ {frequency // 1000} kHz")
    session = _open_session(probe, frequency)
    session.open()
    try:
        # See flash(): reset-and-halt first so pyOCD's flash algorithm always
        # starts from the same clean state.
        _log("Reset/halt target")
        session.target.reset_and_halt()
        _log(
            f"Programming {bin_path.name} ({bin_path.stat().st_size} bytes) @ 0x{base_address:08X}"
        )
        # chip_erase="sector" erases only the pages this .bin touches — the
        # bootloader region below base_address is never erased or written.
        programmer = FileProgrammer(session, progress=progress, chip_erase="sector")
        programmer.program(str(bin_path), file_format="bin", base_address=base_address)
        _log("Resetting target")
        session.target.reset()
        _log("Done.")
    finally:
        try:
            session.close()
        except Exception:
            logger.debug("session close failed for %s", probe.unique_id, exc_info=True)


def psoc6_flash_segments(image: IntelHex) -> list[tuple[int, bytes]]:
    """Return the ``(address, data)`` segments of ``image`` that belong in PSoC 6 main flash.

    Cypress programming-metadata pseudo-sections are dropped; any other data
    outside main flash (SFLASH, eFuse, ...) is refused rather than written.
    """
    segments: list[tuple[int, bytes]] = []
    for start, end in image.segments():
        if PSOC6_METADATA_START <= start < PSOC6_METADATA_END:
            logger.info("Dropping Cypress metadata section 0x%08X-0x%08X", start, end)
            continue
        if not (start >= PSOC6_FLASH_START and end <= PSOC6_FLASH_END):
            raise FlasherError(
                f"Hex data at 0x{start:08X}-0x{end:08X} is outside PSoC 6 main flash "
                f"(0x{PSOC6_FLASH_START:08X}-0x{PSOC6_FLASH_END:08X}); refusing to flash."
            )
        segments.append((start, bytes(image.tobinarray(start=start, end=end - 1))))
    if not segments:
        raise FlasherError("Hex file contains no data in PSoC 6 main flash.")
    return segments


def flash_psoc6(
    hex_path: Path,
    unique_id: str | None = None,
    frequency: int = DEFAULT_FREQUENCY_HZ,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
) -> None:
    """Program ``hex_path`` to a PSoC 6 target's main flash and reset. Raises on failure."""
    from intelhex import IntelHex
    from pyocd.flash.loader import FlashLoader

    hex_path = Path(hex_path)
    if not hex_path.is_file():
        raise FileNotFoundError(f"Firmware file not found: {hex_path}")

    # Fail fast on out-of-range data before touching hardware.
    segments = psoc6_flash_segments(IntelHex(str(hex_path)))

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    probe = _choose_raw(unique_id)
    if probe is None:
        raise ProbeNotFoundError("No ESP-Prog-2 / CMSIS-DAP probe connected.")

    _log(f"Opening {PSOC6_TARGET_TYPE} via {probe.unique_id} @ {frequency // 1000} kHz")
    session = _open_session(probe, frequency, PSOC6_TARGET_TYPE, use_pack=False)
    session.open()
    try:
        _log("Reset/halt target")
        session.target.reset_and_halt()
        total = sum(len(data) for _, data in segments)
        _log(f"Programming {hex_path.name} ({total} bytes in {len(segments)} segments)")
        loader = FlashLoader(session, progress=progress)
        for address, data in segments:
            loader.add_data(address, data)
        loader.commit()
        _log("Resetting target")
        session.target.reset()
        _log("Done.")
    finally:
        try:
            session.close()
        except Exception:
            logger.debug("session close failed for %s", probe.unique_id, exc_info=True)


def _config_block_or_none(
    manufacturer: str | None,
    product: str | None,
    boot_cmd_id: int | None = None,
    boot_reply_id: int | None = None,
) -> bytes | None:
    """Build the DFU config block, or ``None`` when no names are given.

    Raises:
        ValueError: (as ``InvalidNameError``/``InvalidCanIdError``) if only one
            name is supplied, a name violates the frozen contract, or the boot
            CAN ID pair is half-specified or out of range — surfaced at the entry
            point, not mid-flash.
    """
    from .dfu_config import InvalidNameError, build_config_block

    if manufacturer is None and product is None:
        return None
    if manufacturer is None or product is None:
        raise InvalidNameError(
            "manufacturer and product must be provided together (or both omitted)."
        )
    return build_config_block(manufacturer, product, boot_cmd_id, boot_reply_id)


def _program_config_block(session: Session, block: bytes, log: LogCallback) -> None:
    """Program ``block`` to the DFU config address via the flash algorithm and
    read-back-verify. Assumes the target is already halted in this ``session``.
    """
    import time

    from pyocd.flash.loader import FlashLoader

    from .dfu_config import CONFIG_ADDRESS, DATA_FLASH_ENABLE_ADDR

    log(f"Writing DFU config block ({len(block)} bytes) @ 0x{CONFIG_ADDRESS:08X}")
    loader = FlashLoader(session, no_reset=True)
    loader.add_data(CONFIG_ADDRESS, block)
    loader.commit()

    # The RA4M1 data-flash region (0x40100000) is unreadable over SWD until data-flash
    # read access is enabled: R_FACI_LP->DFLCTL (0x407EC090) must be 1, followed by a
    # tDSTOP settling wait (~6 us), or every read returns garbage. pyOCD's flash algo
    # enables this internally while programming, but our manual read-back runs with
    # DFLEN=0, so we must set it ourselves before verifying (FSP r_flash_lp.c ~L1101).
    session.target.write_memory(DATA_FLASH_ENABLE_ADDR, 0x01, transfer_size=8)
    time.sleep(0.001)  # generously covers the 6 us tDSTOP requirement.

    readback = bytes(session.target.read_memory_block8(CONFIG_ADDRESS, len(block)))
    if readback != block:
        raise FlashFailedError(
            f"DFU config read-back mismatch at 0x{CONFIG_ADDRESS:08X}: "
            f"wrote {block.hex()}, read {readback.hex()}"
        )
    log("DFU config verified.")


def write_dfu_config(
    manufacturer: str,
    product: str,
    unique_id: str | None = None,
    frequency: int = DEFAULT_FREQUENCY_HZ,
    log: LogCallback | None = None,
) -> None:
    """Write only the DFU name config block to the RA4M1 data flash (standalone).

    Opens its own session, reset-and-halts, programs the 76-byte block through
    the flash algorithm, and read-back-verifies. Raises on failure.
    """
    from .dfu_config import build_config_block

    block = build_config_block(manufacturer, product)

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    probe = _choose_raw(unique_id)
    if probe is None:
        raise ProbeNotFoundError("No ESP-Prog-2 / CMSIS-DAP probe connected.")

    _log(f"Opening {TARGET_TYPE} via {probe.unique_id} @ {frequency // 1000} kHz")
    session = _open_session(probe, frequency)
    session.open()
    try:
        _log("Reset/halt target")
        session.target.reset_and_halt()
        _program_config_block(session, block, _log)
        _log("Resetting target")
        session.target.reset()
        _log("Done.")
    finally:
        try:
            session.close()
        except Exception:
            logger.debug("session close failed for %s", probe.unique_id, exc_info=True)


# ---------------------------------------------------------------------------
# ESP-Prog-2 OWN firmware (esp-usb-bridge) — prebuilt image flashed with esptool.
# ---------------------------------------------------------------------------

# Espressif's prebuilt image. NOTE: the launchpad image is the JTAG-interface
# build WITH mass storage — NOT what this project wants. The bundled
# firmware/esp-prog2.bin is instead a locally-built SWD/CMSIS-DAP + MSC-disabled
# variant (see firmware/README.md). So the official download is saved under a
# separate name and never overwrites the bundled SWD build.
PROBE_FIRMWARE_URL = "https://espressif.github.io/esp-usb-bridge/esp-prog2.bin"
PROBE_CHIP = "esp32s3"
PROBE_FLASH_OFFSET = "0x0"  # merged image: bootloader + partition table + app
_PROBE_BIN_NAME = "esp-prog2.bin"
_OFFICIAL_BIN_NAME = "esp-prog2-official-jtag.bin"


@dataclass(frozen=True)
class ProbeSerial:
    device: str
    pid: int

    @property
    def in_download_mode(self) -> bool:
        # The running bridge firmware exposes PID 0x1002 (its CDC bridge to the
        # target). ROM download mode re-enumerates with a different PID.
        return self.pid != ESP_PROG_PID


def find_probe_firmware() -> Path | None:
    """Locate the bundled firmware/esp-prog2.bin regardless of launch directory."""
    return _find_bundled("firmware", _PROBE_BIN_NAME)


def find_probe_serial() -> ProbeSerial | None:
    """Find the ESP-Prog-2's serial port; prefer a ROM-download-mode port if present."""
    from serial.tools import list_ports

    esp = [p for p in list_ports.comports() if p.vid == ESP_PROG_VID]
    if not esp:
        return None
    download = [p for p in esp if p.pid not in (None, ESP_PROG_PID)]
    chosen = (download or esp)[0]
    return ProbeSerial(chosen.device, chosen.pid or 0)


def download_probe_firmware(dest: Path | None = None, log: LogCallback | None = None) -> Path:
    """Download Espressif's OFFICIAL esp-prog2.bin (JTAG build) for reference.

    Saved as ``esp-prog2-official-jtag.bin`` so it never overwrites the bundled
    SWD/CMSIS-DAP build that the flasher uses by default. This image is JTAG-mode
    with mass storage — flashing it will disable SWD/CMSIS-DAP and re-add the MSC
    drive, so only use it deliberately (via --probe-bin).
    """
    import urllib.request

    # Use the OS trust store so corporate TLS interception doesn't break the
    # download (Python's bundled certifi often lacks the local root CA).
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        logger.debug("truststore injection failed, using default SSL context", exc_info=True)

    if dest is None:
        bundled = find_probe_firmware()
        base = bundled.parent if bundled else (Path.cwd() / "firmware")
        dest = base / _OFFICIAL_BIN_NAME
    else:
        dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if log:
        log(f"Downloading {PROBE_FIRMWARE_URL}")
    with urllib.request.urlopen(PROBE_FIRMWARE_URL, timeout=_DOWNLOAD_TIMEOUT_S) as resp:
        data = resp.read()
    if not data[:1] == b"\xe9":
        raise FlashFailedError("Downloaded file is not an ESP image (missing 0xE9 magic).")
    dest.write_bytes(data)
    if log:
        log(f"Saved {len(data)} bytes to {dest}")
    return dest


def flash_probe(
    bin_path: Path,
    port: str | None = None,
    baud: int = DEFAULT_BAUD_RATE,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
) -> None:
    """Flash the ESP-Prog-2's own firmware (ESP32-S3) with esptool. Raises on failure.

    The board must be in ROM download mode (hold BOOT, tap RESET) — the running
    bridge firmware does not expose a self-reflash path on its CDC port.
    """
    import re
    import subprocess

    bin_path = Path(bin_path)
    if not bin_path.is_file():
        raise FileNotFoundError(f"Firmware file not found: {bin_path}")

    if getattr(sys, "frozen", False):
        # No "esptool" script on PATH and sys.executable is this bundle itself —
        # re-exec it with a hidden flag that cli.py dispatches to esptool.main().
        args = [sys.executable, ESPTOOL_SHIM_FLAG, "--chip", PROBE_CHIP]
    else:
        args = [sys.executable, "-m", "esptool", "--chip", PROBE_CHIP]
    if port:
        args += ["--port", port]
    args += ["--baud", str(baud), "write-flash", PROBE_FLASH_OFFSET, str(bin_path)]

    if log:
        log("esptool " + " ".join(args[3:]))
    percent = re.compile(r"\((\d+)\s*%\)")
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line and log:
            log(line)
        match = percent.search(line)
        if match and progress:
            with contextlib.suppress(ValueError):
                progress(float(match.group(1)))
    if proc.wait() != 0:
        raise FlashFailedError(
            f"esptool exited with code {proc.returncode}. "
            "Is the ESP-Prog-2 in download mode (hold BOOT, tap RESET)?"
        )
