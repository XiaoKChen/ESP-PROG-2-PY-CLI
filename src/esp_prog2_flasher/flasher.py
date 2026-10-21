"""UI-agnostic flashing engine: probe/target detection and every flash path of the tool.

Wraps pyOCD to detect the ESP-Prog-2 CMSIS-DAP probe and to flash the RA4M1 (bootloader
hex, application .bin, DFU config block) and the PSoC 6 (main-flash hex) over SWD, and wraps
esptool to reflash the ESP-Prog-2's own ESP32-S3 firmware and to provision (write, verify,
read back) the DFU config block in an ESP32-S3 CAN node's ``cancfg`` partition over the
node's own serial/USB download port. It also flashes a SOLO board (ESP32-S3) from the
Embedded-CAN-Bootloaders release bundle: bootloader, partition table, otadata, CAN updater and
the board's cancfg image, optionally plus an app in ota_0. It has no Textual dependency, so the
same logic backs both the TUI and the headless CLI.

Owns:        probe VID/PID and target-type names, the serial-port classification (running
             bridge = unambiguous, 303A:1001 = candidate only), APP_BASE_ADDRESS, the PSoC 6
             flash and metadata address ranges, bundled-asset lookup (hex/, packs/, firmware/),
             ESPTOOL_SHIM_FLAG (the frozen-build esptool re-exec flag dispatched by cli.py),
             and the node's partition-table offset/entry format, the cancfg partition identity
             and its fallback offset; the SOLO release bundle (repo, default tag, asset pattern,
             manifest format and its validation), the SOLO flash sequence, and the otadata image
             that selects ota_0.
Not here:    DFU config-block encoding/decoding and the device table (dfu_config.py), the
             board-profile record, cancfg image and SOLO presets (board_profile.py), argument
             parsing (cli.py), the TUI (app.py).
Invariants:  a node's config block is only written to the cancfg partition its partition table
             names; the fixed DEFAULT_CANCFG_OFFSET is used only when no valid table is found.
             A SOLO flash validates the manifest, the app image and the cancfg image before its
             first esptool call; every esptool step but the last keeps the chip in download mode
             (--after no-reset); a separate final chip-id call reboots the board with
             --after watchdog-reset, because the RTS hard reset does not reboot an ESP32-S3
             over USB-Serial-JTAG. That call's exit code is ignored: the port re-enumerates
             as the chip reboots, which esptool reports as a serial error.
Concurrency: calls block until hardware I/O finishes; progress and log callbacks fire on the
             calling thread, so the TUI runs these functions in worker threads.
Errors:      FlasherError hierarchy (ProbeNotFoundError, FlashFailedError, BundleError for a
             bad, missing or unfetchable SOLO bundle); FileNotFoundError for a missing image;
             InvalidNameError/InvalidCanIdError from dfu_config and InvalidProfileError from
             board_profile are raised before any hardware is touched.
Depends on:  dfu_config.py, board_profile.py; pyocd, esptool, pyserial, intelhex and
             truststore, all imported lazily so importing this module never blocks on USB; the
             GitHub CLI (gh) on PATH, only for fetch_solo_bundle.
See also:    tests/test_psoc6.py, tests/test_dfu_config.py, tests/test_flasher.py, cli.py,
             app.py

The proven, equivalent pyOCD command-line path for the RA4M1 is:

    pyocd flash -t r7fa4m1ab dfu_minima.hex

Note: the installed OpenOCD build lacks the ``renesas_ra`` flash driver, so
flashing the RA4M1 must go through pyOCD, not OpenOCD.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from .dfu_config import DEFAULT_MANUFACTURER

if TYPE_CHECKING:
    from intelhex import IntelHex
    from pyocd.core.session import Session
    from pyocd.probe.debug_probe import DebugProbe

    from .board_profile import BoardProfile
    from .dfu_config import DfuConfig

logger = logging.getLogger(__name__)

# The ESP-Prog-2 enumerates as a CMSIS-DAP device on Espressif's USB vendor ID.
ESP_PROG_VID: Final[int] = 0x303A
ESP_PROG_PID: Final[int] = 0x1002
# Every ESP32-S3 on USB-Serial-JTAG (ESP-Prog-2 in ROM download mode, SOLO boards) uses this.
ESP32S3_USB_JTAG_PID: Final[int] = 0x1001
# Renesas RA4M1 (Arduino UNO R4 Minima MCU) — pyOCD pack target name.
TARGET_TYPE: Final[str] = "r7fa4m1ab"
# Infineon PSoC 6 (CY8C6245 / PSoC 62S3) — built into pyOCD, no CMSIS pack needed.
PSOC6_TARGET_TYPE: Final[str] = "cy8c6xx5"
DEFAULT_FREQUENCY_HZ: Final[int] = 1_000_000
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
_BLANK_PC: Final[int] = 0xFFFFFFFE
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

# ESP32-S3 CAN node (node_updater): the DFU config block lives in a raw data
# partition named "cancfg" with custom subtype 0x40 (node_updater/partitions.csv).
NODE_CHIP: Final[str] = "esp32s3"
PARTITION_TABLE_OFFSET: Final[int] = 0x8000  # CONFIG_PARTITION_TABLE_OFFSET
PARTITION_TABLE_SIZE: Final[int] = 0xC00  # ESP-IDF maximum table length
# Used only when the node has no valid partition table (e.g. blank flash).
DEFAULT_CANCFG_OFFSET: Final[int] = 0x11000
CANCFG_PARTITION_NAME: Final[str] = "cancfg"
CANCFG_PARTITION_SUBTYPE: Final[int] = 0x40
PARTITION_TYPE_DATA: Final[int] = 0x01
# ESP-IDF partition entry: magic, type, subtype, offset, size, name[16], flags.
_PARTITION_ENTRY_FORMAT: Final[str] = "<2sBBII16sI"
_PARTITION_ENTRY_SIZE: Final[int] = struct.calcsize(_PARTITION_ENTRY_FORMAT)  # 32
_PARTITION_ENTRY_MAGIC: Final[bytes] = b"\xaa\x50"  # u16 0x50AA little-endian
_PARTITION_MD5_MAGIC: Final[bytes] = b"\xeb\xeb"  # optional MD5 entry ends the table
_PARTITION_END_MAGIC: Final[bytes] = b"\xff\xff"  # erased flash ends the table

ProgressCallback = Callable[[float], None]
LogCallback = Callable[[str], None]
# Runs one esptool argv and returns its exit code. Injected so tests can capture the
# exact command plan without hardware; _run_esptool is the real one.
EsptoolRunner = Callable[[list[str], ProgressCallback | None, LogCallback | None], int]
# esptool --after modes: no-reset for every write/verify step; the board is rebooted by a
# separate final call (_reboot_node_board).
ResetMode = Literal["no-reset", "watchdog-reset"]


class FlasherError(Exception):
    """Base error for this module — callers catch this, not Exception."""


class ProbeNotFoundError(FlasherError):
    """Raised when no ESP-Prog-2 / CMSIS-DAP probe is connected."""


class FlashFailedError(FlasherError):
    """Raised when programming the target or the probe's own firmware fails."""


class BundleError(FlasherError):
    """Raised when a SOLO release bundle is missing, cannot be fetched, or fails validation."""


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


@dataclass(frozen=True, slots=True)
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


@dataclass(slots=True)
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
        logger.debug("session open failed for %s", info.unique_id, exc_info=True)
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
        InvalidNameError: if only one name is supplied or a name violates the
            frozen contract.
        InvalidCanIdError: if the boot CAN ID pair is half-specified or out of
            range. Both are surfaced at the entry point, not mid-flash.
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

    Opens its own session, reset-and-halts, programs the 80-byte block through
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
PROBE_FIRMWARE_URL: Final[str] = "https://espressif.github.io/esp-usb-bridge/esp-prog2.bin"
PROBE_CHIP: Final[str] = "esp32s3"
PROBE_FLASH_OFFSET: Final[str] = "0x0"  # merged image: bootloader + partition table + app
_PROBE_BIN_NAME = "esp-prog2.bin"
_OFFICIAL_BIN_NAME = "esp-prog2-official-jtag.bin"


@dataclass(frozen=True)
class ProbeSerial:
    """The running ESP-Prog-2 bridge's serial port (the only unambiguous identification)."""

    device: str
    pid: int


def find_probe_firmware() -> Path | None:
    """Locate the bundled firmware/esp-prog2.bin regardless of launch directory."""
    return _find_bundled("firmware", _PROBE_BIN_NAME)


@dataclass(frozen=True, slots=True)
class SerialPortInfo:
    """One serial port as the OS lists it."""

    device: str
    description: str
    vid: int | None
    pid: int | None

    @property
    def is_esp_prog_bridge(self) -> bool:
        # Only the running bridge is unambiguous: in ROM download mode the ESP-Prog-2
        # enumerates as 303A:1001, the same as any ESP32-S3 board on USB-Serial-JTAG.
        return self.vid == ESP_PROG_VID and self.pid == ESP_PROG_PID

    @property
    def is_download_mode_candidate(self) -> bool:
        """An ESP32-S3 on USB-Serial-JTAG: the ESP-Prog-2 in download mode OR a SOLO board."""
        return self.vid == ESP_PROG_VID and self.pid == ESP32S3_USB_JTAG_PID


def list_serial_ports() -> list[SerialPortInfo]:
    """List every serial port, sorted by device name, for picking a SOLO board's port."""
    from serial.tools import list_ports

    ports = (SerialPortInfo(p.device, p.description, p.vid, p.pid) for p in list_ports.comports())
    return sorted(ports, key=lambda port: port.device)


def find_probe_serial() -> ProbeSerial | None:
    """Find the running ESP-Prog-2 bridge's port; never guesses from a 303A:1001 port."""
    for port in list_serial_ports():
        if port.is_esp_prog_bridge:
            return ProbeSerial(port.device, ESP_PROG_PID)
    return None


def find_download_mode_candidates() -> list[SerialPortInfo]:
    """List the 303A:1001 ports: each is the ESP-Prog-2 in download mode or a SOLO board."""
    return [port for port in list_serial_ports() if port.is_download_mode_candidate]


def download_probe_firmware(dest: Path | None = None, log: LogCallback | None = None) -> Path:
    """Download Espressif's OFFICIAL esp-prog2.bin (JTAG build) for reference.

    Saved as ``esp-prog2-official-jtag.bin`` so it never overwrites the bundled
    SWD/CMSIS-DAP build that the flasher uses by default. This image is JTAG-mode
    with mass storage — flashing it will disable SWD/CMSIS-DAP and re-add the MSC
    drive, so only use it deliberately (via --probe-bin).
    """
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
    bin_path = Path(bin_path)
    if not bin_path.is_file():
        raise FileNotFoundError(f"Firmware file not found: {bin_path}")

    args = _esptool_command(PROBE_CHIP)
    if port:
        args += ["--port", port]
    args += ["--baud", str(baud), "write-flash", PROBE_FLASH_OFFSET, str(bin_path)]

    if log:
        log("esptool " + " ".join(args[3:]))
    returncode = _run_esptool(args, progress, log)
    if returncode != 0:
        raise FlashFailedError(
            f"esptool exited with code {returncode}. "
            "Is the ESP-Prog-2 in download mode (hold BOOT, tap RESET)?"
        )


def _esptool_command(chip: str) -> list[str]:
    """Return the argv prefix that runs esptool for ``chip``, from source or a frozen build."""
    if getattr(sys, "frozen", False):
        # No "esptool" script on PATH and sys.executable is this bundle itself —
        # re-exec it with a hidden flag that cli.py dispatches to esptool.main().
        return [sys.executable, ESPTOOL_SHIM_FLAG, "--chip", chip]
    return [sys.executable, "-m", "esptool", "--chip", chip]


def _run_esptool(
    args: list[str], progress: ProgressCallback | None, log: LogCallback | None
) -> int:
    """Run esptool, streaming its output to ``log`` and its percentages to ``progress``.

    Returns esptool's exit code; callers turn a non-zero code into their own error.
    """
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
    return proc.wait()


# ---------------------------------------------------------------------------
# ESP32-S3 CAN node — DFU config block in the node's `cancfg` data partition,
# written over the NODE's own serial/USB download port (not the ESP-Prog-2's).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PartitionEntry:
    """One entry of an ESP-IDF partition table."""

    name: str
    partition_type: int
    subtype: int
    offset: int
    size: int


def parse_partition_table(table: bytes) -> list[PartitionEntry]:
    """Decode a raw ESP-IDF partition table, or return ``[]`` if ``table`` holds none.

    Entries are read until the erased-flash (0xFFFF) or MD5 (0xEBEB) marker. An
    entry with any other unknown magic means this is not a partition table (blank
    or foreign flash), so nothing is returned rather than an untrustworthy prefix.
    """
    entries: list[PartitionEntry] = []
    for start in range(0, len(table) - _PARTITION_ENTRY_SIZE + 1, _PARTITION_ENTRY_SIZE):
        magic, partition_type, subtype, offset, size, raw_name, _flags = struct.unpack_from(
            _PARTITION_ENTRY_FORMAT, table, start
        )
        if magic in (_PARTITION_END_MAGIC, _PARTITION_MD5_MAGIC):
            break
        if magic != _PARTITION_ENTRY_MAGIC:
            return []
        name = raw_name.split(b"\x00", 1)[0].decode("ascii", "replace")
        entries.append(PartitionEntry(name, partition_type, subtype, offset, size))
    return entries


def cancfg_offset_from_table(table: bytes) -> int | None:
    """Return the ``cancfg`` partition offset named by a raw partition table.

    Returns ``None`` when ``table`` holds no valid partition table (e.g. blank
    flash) so the caller can fall back to ``DEFAULT_CANCFG_OFFSET``.

    Raises:
        FlasherError: if the table is valid but has no cancfg data partition —
            the node firmware would never read a block written anywhere else.
    """
    entries = parse_partition_table(table)
    if not entries:
        return None
    for entry in entries:
        if (
            entry.partition_type == PARTITION_TYPE_DATA
            and entry.subtype == CANCFG_PARTITION_SUBTYPE
            and entry.name == CANCFG_PARTITION_NAME
        ):
            return entry.offset
    raise FlasherError(
        f"The node's partition table has no {CANCFG_PARTITION_NAME!r} data partition "
        f"(subtype 0x{CANCFG_PARTITION_SUBTYPE:02X}); is this a CAN node running node_updater?"
    )


def _run_node_esptool(
    port: str,
    baud: int,
    command: list[str],
    log: LogCallback | None,
    progress: ProgressCallback | None = None,
    runner: EsptoolRunner = _run_esptool,
    after: ResetMode | None = None,
) -> None:
    reset_args = ["--after", after] if after is not None else []
    esptool_args = ["--port", port, "--baud", str(baud), *reset_args, *command]
    if log:
        log(f"esptool --chip {NODE_CHIP} " + " ".join(esptool_args))
    returncode = runner(_esptool_command(NODE_CHIP) + esptool_args, progress, log)
    if returncode != 0:
        raise FlashFailedError(
            f"esptool exited with code {returncode}. Is the node's ESP32-S3 connected on "
            f"{port} and able to enter download mode?"
        )


def _reboot_node_board(port: str, log: LogCallback | None, runner: EsptoolRunner) -> None:
    """Reboot the board with a watchdog reset, tolerating the port vanishing mid-call.

    Over USB-Serial-JTAG the chip resets and the port disappears while esptool still
    holds it, so esptool exits non-zero after a successful reset; that is not a failure.
    """
    esptool_args = ["--port", port, "--before", "no-reset", "--after", "watchdog-reset", "chip-id"]
    if log:
        log(f"esptool --chip {NODE_CHIP} " + " ".join(esptool_args))
    returncode = runner(_esptool_command(NODE_CHIP) + esptool_args, None, log)
    if returncode != 0 and log:
        log("board rebooted (the USB port re-enumerates)")


def _read_node_flash(
    port: str,
    baud: int,
    offset: int,
    size: int,
    log: LogCallback | None,
    runner: EsptoolRunner = _run_esptool,
    after: ResetMode | None = None,
) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "read.bin"
        _run_node_esptool(
            port,
            baud,
            ["read-flash", f"0x{offset:X}", str(size), str(out_path)],
            log,
            runner=runner,
            after=after,
        )
        return out_path.read_bytes()


def _node_cancfg_offset(
    port: str, baud: int, log: LogCallback | None, runner: EsptoolRunner = _run_esptool
) -> int:
    table = _read_node_flash(port, baud, PARTITION_TABLE_OFFSET, PARTITION_TABLE_SIZE, log, runner)
    offset = cancfg_offset_from_table(table)
    if offset is None:
        offset = DEFAULT_CANCFG_OFFSET
        logger.info("No partition table at 0x%X; using 0x%X", PARTITION_TABLE_OFFSET, offset)
        if log:
            log(
                f"No valid partition table at 0x{PARTITION_TABLE_OFFSET:X}; "
                f"using the default cancfg offset 0x{offset:X}"
            )
    elif log:
        log(f"Found {CANCFG_PARTITION_NAME} partition @ 0x{offset:X}")
    return offset


def provision_node(
    port: str,
    manufacturer: str,
    product: str,
    boot_cmd_id: int | None = None,
    boot_reply_id: int | None = None,
    baud: int = DEFAULT_BAUD_RATE,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
    profile: BoardProfile | None = None,
    runner: EsptoolRunner = _run_esptool,
) -> None:
    """Write the DFU config block to an ESP32-S3 CAN node's cancfg partition and verify it.

    The offset comes from the node's partition table (``DEFAULT_CANCFG_OFFSET`` if
    it has none). With a ``profile``, its board-profile record follows the block as
    the full 4 KB cancfg image (board_profile.build_cancfg_image); the block's names
    and IDs still come from the arguments. After writing, everything written is read
    back and compared byte-for-byte.

    Raises:
        InvalidNameError, InvalidCanIdError, InvalidProfileError: bad names/IDs or a
            bad profile record, before any hardware I/O.
        FlasherError: a valid partition table without a cancfg partition.
        FlashFailedError: esptool failed, or the read-back differs from the block.
    """
    from .board_profile import build_cancfg_image
    from .dfu_config import build_config_block

    # Fail fast on bad names/IDs before touching hardware.
    block = build_config_block(manufacturer, product, boot_cmd_id, boot_reply_id)
    if profile is not None:
        block = build_cancfg_image(block, profile.record)
    offset = _node_cancfg_offset(port, baud, log, runner)

    if log:
        log(f"Writing DFU config block ({len(block)} bytes) @ 0x{offset:X}")
    with tempfile.TemporaryDirectory() as tmp:
        block_path = Path(tmp) / "cancfg.bin"
        block_path.write_bytes(block)
        _run_node_esptool(
            port,
            baud,
            ["write-flash", f"0x{offset:X}", str(block_path)],
            log,
            progress,
            runner,
        )

    readback = _read_node_flash(port, baud, offset, len(block), log, runner)
    if readback != block:
        raise FlashFailedError(
            f"Node config read-back mismatch at 0x{offset:X}: "
            f"wrote {block.hex()}, read {readback.hex()}"
        )
    if log:
        log("Node config verified.")


def read_node_config(
    port: str, baud: int = DEFAULT_BAUD_RATE, log: LogCallback | None = None
) -> DfuConfig | None:
    """Read and decode the DFU config block from an ESP32-S3 CAN node's cancfg partition.

    Returns ``None`` when the block is unprovisioned or invalid — the node then
    uses its defaults (0x79E/0x79F, empty name).

    Raises:
        FlasherError: a valid partition table without a cancfg partition.
        FlashFailedError: esptool failed.
    """
    from .dfu_config import BLOCK_SIZE, parse_config_block

    offset = _node_cancfg_offset(port, baud, log)
    return parse_config_block(_read_node_flash(port, baud, offset, BLOCK_SIZE, log))


# ---------------------------------------------------------------------------
# SOLO boards (ESP32-S3) — the Embedded-CAN-Bootloaders "one updater" release
# bundle plus a per-board cancfg image, written over the board's own USB port.
# ---------------------------------------------------------------------------

SOLO_BUNDLE_REPO: Final[str] = "XiaoKChen/Embedded-CAN-Bootloaders"
DEFAULT_SOLO_BUNDLE_TAG: Final[str] = "c-cpp-esp32-v1.2.1"
SOLO_BUNDLE_ASSET_PATTERN: Final[str] = "esp32s3-can-updater-*.zip"
SOLO_MANIFEST_NAME: Final[str] = "manifest.json"
SOLO_MANIFEST_FORMAT: Final[int] = 1
SOLO_FLASH_MODE: Final[str] = "dio"
SOLO_FLASH_FREQ: Final[str] = "80m"
# A stock Arduino-ESP32 layout keeps its app at 0x10000; "migrate from stock" erases
# what it left where the new phy_init, cancfg and bootcnt partitions now live.
STOCK_MIGRATION_ERASE_OFFSET: Final[int] = 0x10000
STOCK_MIGRATION_ERASE_SIZE: Final[int] = 0x10000
OTADATA_SIZE: Final[int] = 0x2000  # two 4 KB otadata sectors
# esp_ota_select_entry_t (esp_flash_partitions.h): ota_seq, seq_label[20], ota_state,
# crc. The bootloader boots slot (ota_seq - 1) % <slot count>, so sequence 1 is ota_0.
_OTA_SEQ_OTA_0: Final[int] = 1
_OTA_SEQ_LABEL_SIZE: Final[int] = 20
# UNDEFINED, not PENDING_VERIFY: a USB install is not a rollback candidate.
_OTA_IMG_UNDEFINED: Final[int] = 0xFFFFFFFF
_ESP_IMAGE_MAGIC: Final[bytes] = b"\xe9"
_SOLO_FILE_KEYS: Final[tuple[str, ...]] = ("bootloader", "updater", "otadata")
_HEX_PREFIX: Final[str] = "0x"

GhRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class SoloLayout:
    """One flash layout of a bundle's manifest: its partition-table image, size and offsets."""

    table: Path
    flash_size: str
    bootloader: int
    table_offset: int
    otadata: int
    cancfg: int
    factory: int
    ota_0: int


@dataclass(frozen=True, slots=True)
class SoloBundle:
    """A validated release bundle: its version, the shared images and its flash layouts."""

    version: str
    bootloader: Path
    updater: Path
    otadata: Path
    layouts: dict[str, SoloLayout]


def _manifest_name(parent: str, key: str) -> str:
    return f"{parent}.{key}" if parent else key


def _manifest_field(table: dict[str, object], key: str, parent: str) -> object:
    if key not in table:
        raise BundleError(f"manifest.json has no {_manifest_name(parent, key)!r}")
    return table[key]


def _manifest_table(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BundleError(f"manifest.json {name!r} must be an object")
    return value


def _manifest_str(table: dict[str, object], key: str, parent: str) -> str:
    value = _manifest_field(table, key, parent)
    if not isinstance(value, str):
        raise BundleError(f"manifest.json {_manifest_name(parent, key)!r} must be a string")
    return value


def _manifest_offset(table: dict[str, object], key: str, parent: str) -> int:
    text = _manifest_str(table, key, parent)
    try:
        if not text.lower().startswith(_HEX_PREFIX):
            raise ValueError(text)
        return int(text, 16)
    except ValueError:
        raise BundleError(
            f"manifest.json {_manifest_name(parent, key)!r} must be a hex offset like "
            f"'0x8000', got {text!r}"
        ) from None


def _bundle_file(bundle_dir: Path, table: dict[str, object], key: str, parent: str) -> Path:
    name = _manifest_str(table, key, parent)
    # Bare file names only: a manifest must not point outside its own bundle.
    if not name or Path(name).name != name or name in (".", ".."):
        raise BundleError(
            f"manifest.json {_manifest_name(parent, key)!r} must be a file name in the bundle, "
            f"got {name!r}"
        )
    path = bundle_dir / name
    if not path.is_file():
        raise BundleError(f"Bundle file {name!r} ({_manifest_name(parent, key)}) is missing")
    return path


def _parse_solo_layout(bundle_dir: Path, name: str, value: object) -> SoloLayout:
    parent = f"layouts.{name}"
    table = _manifest_table(value, parent)
    layout = SoloLayout(
        table=_bundle_file(bundle_dir, table, "table", parent),
        flash_size=_manifest_str(table, "flash_size", parent),
        bootloader=_manifest_offset(table, "bootloader", parent),
        table_offset=_manifest_offset(table, "table_offset", parent),
        otadata=_manifest_offset(table, "otadata", parent),
        cancfg=_manifest_offset(table, "cancfg", parent),
        factory=_manifest_offset(table, "factory", parent),
        ota_0=_manifest_offset(table, "ota_0", parent),
    )
    # The firmware reads cancfg at this fixed offset; a bundle moving it is incompatible.
    if layout.cancfg != DEFAULT_CANCFG_OFFSET:
        raise BundleError(
            f"manifest.json {parent}.cancfg is 0x{layout.cancfg:X}; the SOLO firmware reads "
            f"cancfg at 0x{DEFAULT_CANCFG_OFFSET:X}"
        )
    return layout


def load_solo_bundle(bundle_dir: Path) -> SoloBundle:
    """Read and validate an unzipped SOLO release bundle's ``manifest.json``.

    The manifest must be format 1 for the esp32s3 with dio/80m flash, name existing
    bare files for the bootloader, updater and otadata, and give every layout an
    existing partition-table image, a flash size and hex offsets, with cancfg at
    ``DEFAULT_CANCFG_OFFSET``.

    Raises:
        BundleError: the manifest is missing, not JSON, or breaks any rule above.
    """
    bundle_dir = Path(bundle_dir)
    manifest_path = bundle_dir / SOLO_MANIFEST_NAME
    if not manifest_path.is_file():
        raise BundleError(
            f"No {SOLO_MANIFEST_NAME} in {bundle_dir}; is this an unzipped "
            f"{SOLO_BUNDLE_ASSET_PATTERN} bundle?"
        )
    try:
        raw: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
        raise BundleError(f"{manifest_path} is not valid JSON: {exc}") from exc
    manifest = _manifest_table(raw, "manifest")

    supported: dict[str, object] = {
        "format": SOLO_MANIFEST_FORMAT,
        "chip": NODE_CHIP,
        "flash_mode": SOLO_FLASH_MODE,
        "flash_freq": SOLO_FLASH_FREQ,
    }
    for key, expected in supported.items():
        value = _manifest_field(manifest, key, "")
        # type() check too: JSON true must not pass for format 1.
        if type(value) is not type(expected) or value != expected:
            raise BundleError(
                f"manifest.json {key!r} is {value!r}; this tool supports {expected!r}"
            )

    files = _manifest_table(_manifest_field(manifest, "files", ""), "files")
    bootloader, updater, otadata = (
        _bundle_file(bundle_dir, files, key, "files") for key in _SOLO_FILE_KEYS
    )
    layouts = _manifest_table(_manifest_field(manifest, "layouts", ""), "layouts")
    return SoloBundle(
        version=_manifest_str(manifest, "version", ""),
        bootloader=bootloader,
        updater=updater,
        otadata=otadata,
        layouts={
            name: _parse_solo_layout(bundle_dir, name, value) for name, value in layouts.items()
        },
    )


def otadata_selecting_ota0() -> bytes:
    """Return an 8 KB otadata image whose first entry selects ota_0; the rest is erased.

    A port of Airform-SOLO-ODU-Controller/scripts/ota_slot_upload.py: ota_seq 1,
    seq_label 0xFF, ota_state UNDEFINED and crc32(ota_seq, 0xFFFFFFFF).
    """
    ota_seq = struct.pack("<I", _OTA_SEQ_OTA_0)
    # bootloader_common_ota_select_crc() is esp_rom_crc32_le(UINT32_MAX, &ota_seq, 4). The
    # ROM function inverts on entry and exit as zlib does, so the same seed gives the same
    # value (IDF's otatool.py: binascii.crc32(seq, 0xFFFFFFFF)).
    crc = zlib.crc32(ota_seq, 0xFFFFFFFF) & 0xFFFFFFFF
    entry = ota_seq + b"\xff" * _OTA_SEQ_LABEL_SIZE + struct.pack("<II", _OTA_IMG_UNDEFINED, crc)
    return entry.ljust(OTADATA_SIZE, b"\xff")


def _checked_app_image(app: Path) -> Path:
    app = Path(app)
    if not app.is_file():
        raise FileNotFoundError(f"Application file not found: {app}")
    with app.open("rb") as image:
        magic = image.read(len(_ESP_IMAGE_MAGIC))
    if magic != _ESP_IMAGE_MAGIC:
        raise FlasherError(f"{app} is not an ESP app image (first byte must be 0xE9)")
    return app


def _first_difference(expected: bytes, actual: bytes) -> int:
    for index, (want, got) in enumerate(zip(expected, actual, strict=False)):
        if want != got:
            return index
    return min(len(expected), len(actual))


def flash_solo(
    profile: BoardProfile,
    port: str,
    bundle_dir: Path,
    app: Path | None = None,
    migrate_stock: bool = False,
    manufacturer: str = DEFAULT_MANUFACTURER,
    baud: int = DEFAULT_BAUD_RATE,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
    runner: EsptoolRunner = _run_esptool,
) -> None:
    """Flash a SOLO board from an unzipped release bundle, verify cancfg, then reset it.

    Steps, over the board's own USB port:

    1. With ``migrate_stock``, erase 0x10000..0x1FFFF (a stock Arduino app's leftovers).
    2. One ``write-flash --flash-mode dio --flash-freq 80m --flash-size <layout>`` of the
       bootloader, the layout's partition table, the blank otadata, the CAN updater at
       the layout's ``factory`` offset and the board's cancfg image (v2 block + profile
       record, 4 KB) at 0x11000.
    3. Read cancfg back and compare it byte-for-byte.
    4. With ``app``, write it at ``ota_0`` plus an otadata image selecting ota_0;
       without it the board boots the CAN updater.

    Every step runs with ``--after no-reset``; a final ``chip-id`` call then reboots the
    board with ``--after watchdog-reset`` and its exit code is ignored. Everything is
    validated before the first esptool call.

    Raises:
        BundleError: the bundle is invalid or lacks the profile's layout.
        FileNotFoundError, FlasherError: ``app`` is missing or not an ESP image.
        InvalidNameError: ``manufacturer`` violates the block contract.
        FlashFailedError: esptool failed, or the cancfg read-back differs.
    """
    from .board_profile import build_cancfg_image

    bundle = load_solo_bundle(bundle_dir)
    layout = bundle.layouts.get(profile.layout)
    if layout is None:
        raise BundleError(
            f"Bundle {bundle.version} has no {profile.layout!r} layout "
            f"(it has: {', '.join(bundle.layouts) or 'none'})"
        )
    if layout.flash_size != profile.flash_size:
        raise BundleError(
            f"Bundle layout {profile.layout!r} is {layout.flash_size}; "
            f"{profile.product} needs {profile.flash_size}"
        )
    app_path = _checked_app_image(app) if app is not None else None
    cancfg = build_cancfg_image(profile.config_block(manufacturer), profile.record)

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    _log(f"Flashing {profile.product} ({profile.layout}, bundle {bundle.version}) on {port}")
    with tempfile.TemporaryDirectory() as tmp:
        cancfg_path = Path(tmp) / "cancfg.bin"
        cancfg_path.write_bytes(cancfg)
        if migrate_stock:
            _log("Erasing the stock layout's leftovers")
            _run_node_esptool(
                port,
                baud,
                [
                    "erase-region",
                    f"0x{STOCK_MIGRATION_ERASE_OFFSET:X}",
                    f"0x{STOCK_MIGRATION_ERASE_SIZE:X}",
                ],
                log,
                runner=runner,
                after="no-reset",
            )
        _log("Writing bootloader, partition table, otadata, CAN updater and cancfg")
        _run_node_esptool(
            port,
            baud,
            [
                "write-flash",
                "--flash-mode",
                SOLO_FLASH_MODE,
                "--flash-freq",
                SOLO_FLASH_FREQ,
                "--flash-size",
                layout.flash_size,
                f"0x{layout.bootloader:X}",
                str(bundle.bootloader),
                f"0x{layout.table_offset:X}",
                str(layout.table),
                f"0x{layout.otadata:X}",
                str(bundle.otadata),
                f"0x{layout.factory:X}",
                str(bundle.updater),
                f"0x{layout.cancfg:X}",
                str(cancfg_path),
            ],
            log,
            progress,
            runner,
            after="no-reset",
        )
        readback = _read_node_flash(
            port,
            baud,
            layout.cancfg,
            len(cancfg),
            log,
            runner,
            after="no-reset",
        )
        if readback != cancfg:
            raise FlashFailedError(
                f"cancfg read-back mismatch at 0x{layout.cancfg:X}: first difference at "
                f"+0x{_first_difference(cancfg, readback):X} ({len(readback)} bytes read)"
            )
        _log("cancfg verified.")
        if app_path is not None:
            otadata_path = Path(tmp) / "otadata_ota0.bin"
            otadata_path.write_bytes(otadata_selecting_ota0())
            _log(f"Writing {app_path.name} @ ota_0 and otadata selecting ota_0")
            _run_node_esptool(
                port,
                baud,
                [
                    "write-flash",
                    f"0x{layout.ota_0:X}",
                    str(app_path),
                    f"0x{layout.otadata:X}",
                    str(otadata_path),
                ],
                log,
                progress,
                runner,
                after="no-reset",
            )
        _reboot_node_board(port, log, runner)
    _log("Done.")


def _run_gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)


def _bundle_root(extract_dir: Path) -> Path:
    """Return the directory holding manifest.json: the zip's root or its one top folder."""
    if (extract_dir / SOLO_MANIFEST_NAME).is_file():
        return extract_dir
    nested = [d for d in extract_dir.iterdir() if (d / SOLO_MANIFEST_NAME).is_file()]
    if len(nested) != 1:
        raise BundleError(f"No {SOLO_MANIFEST_NAME} at the top of the bundle in {extract_dir}")
    return nested[0]


def fetch_solo_bundle(
    tag: str,
    dest: Path,
    log: LogCallback | None = None,
    run_gh: GhRunner = _run_gh,
) -> Path:
    """Download release ``tag``'s SOLO bundle with the GitHub CLI and unzip it under ``dest``.

    Runs ``gh release download -R XiaoKChen/Embedded-CAN-Bootloaders <tag> -p
    "esp32s3-can-updater-*.zip"`` into ``dest/download`` and extracts the one zip next
    to it. Returns the directory holding ``manifest.json``, ready for flash_solo.

    Raises:
        BundleError: gh is not installed, the release or asset does not exist (e.g. a
            tag not yet published), the download fails, or the zip is unusable.
    """
    dest = Path(dest)
    download_dir = dest / "download"
    download_dir.mkdir(parents=True, exist_ok=True)
    args = [
        "release",
        "download",
        "-R",
        SOLO_BUNDLE_REPO,
        tag,
        "-p",
        SOLO_BUNDLE_ASSET_PATTERN,
        "-D",
        str(download_dir),
    ]
    if log:
        log("gh " + " ".join(args))
    try:
        result = run_gh(args)
    except FileNotFoundError:
        raise BundleError(
            "The GitHub CLI (gh) is not on PATH. Install it and run `gh auth login`, or pass "
            "a local bundle directory."
        ) from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if "release not found" in detail.lower():
            raise BundleError(
                f"Release {tag!r} not found in {SOLO_BUNDLE_REPO}; it may not be published yet. "
                "Pass a local bundle directory instead."
            )
        raise BundleError(
            f"gh release download of {tag!r} failed (exit {result.returncode}): {detail}"
        )
    zips = sorted(download_dir.glob("*.zip"))
    if len(zips) != 1:
        raise BundleError(
            f"Expected one {SOLO_BUNDLE_ASSET_PATTERN} asset in release {tag!r}, got {len(zips)}"
        )
    extract_dir = dest / zips[0].stem
    try:
        with zipfile.ZipFile(zips[0]) as archive:
            # extractall drops absolute paths and ".." components, so nothing lands
            # outside extract_dir.
            archive.extractall(extract_dir)
    except zipfile.BadZipFile as exc:
        raise BundleError(f"{zips[0].name} is not a valid zip: {exc}") from exc
    if log:
        log(f"Unzipped {zips[0].name} to {extract_dir}")
    return _bundle_root(extract_dir)
