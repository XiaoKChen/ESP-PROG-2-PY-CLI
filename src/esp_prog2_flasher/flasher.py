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

from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Callable, List, Optional

# The ESP-Prog-2 enumerates as a CMSIS-DAP device on Espressif's USB vendor ID.
ESP_PROG_VID = 0x303A
ESP_PROG_PID = 0x1002
# Renesas RA4M1 (Arduino UNO R4 Minima MCU) — pyOCD pack target name.
TARGET_TYPE = "r7fa4m1ab"
DEFAULT_FREQUENCY_HZ = 1_000_000

_HEX_NAME = "dfu_minima.hex"
# A blank Cortex-M reads this in PC after reset (vector table = 0xFFFFFFFF).
_BLANK_PC = 0xFFFFFFFE

ProgressCallback = Callable[[float], None]
LogCallback = Callable[[str], None]


def find_default_hex() -> Optional[Path]:
    """Locate the bundled ``hex/dfu_minima.hex`` regardless of how we were launched."""
    seen: set[Path] = set()
    bases = [Path(__file__).resolve(), Path.cwd().resolve()]
    for base in bases:
        for parent in [base, *base.parents]:
            candidate = parent / "hex" / _HEX_NAME
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_file():
                return candidate
    return None


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
    probe: Optional[ProbeInfo] = None

    @property
    def flashable(self) -> bool:
        return self.state in (TargetState.CONNECTED, TargetState.CONNECTED_BLANK)


def _is_esp_prog(probe) -> bool:
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


def _to_info(probe) -> ProbeInfo:
    return ProbeInfo(
        unique_id=probe.unique_id or "",
        product_name=getattr(probe, "product_name", "") or "",
        vendor_name=getattr(probe, "vendor_name", "") or "",
        description=probe.description or "",
        is_esp_prog=_is_esp_prog(probe),
    )


def _raw_probes(unique_id: Optional[str] = None) -> list:
    # Imported lazily so module import never blocks on USB enumeration.
    from pyocd.core.helpers import ConnectHelper

    return ConnectHelper.get_all_connected_probes(
        blocking=False, unique_id=unique_id, print_wait_message=False
    )


def _choose_raw(unique_id: Optional[str] = None):
    probes = _raw_probes(unique_id)
    if not probes:
        return None
    for probe in probes:
        if _is_esp_prog(probe):
            return probe
    # Fall back to the first CMSIS-DAP probe if none self-identify as Espressif.
    return probes[0]


def list_probes() -> List[ProbeInfo]:
    """Return every connected debug probe, flagging which look like an ESP-Prog-2."""
    return [_to_info(p) for p in _raw_probes()]


def find_esp_prog() -> Optional[ProbeInfo]:
    probe = _choose_raw()
    return _to_info(probe) if probe is not None else None


def _open_session(probe, frequency: int):
    from pyocd.core.session import Session

    return Session(
        probe,
        options={"target_override": TARGET_TYPE, "frequency": frequency},
    )


def detect(unique_id: Optional[str] = None, frequency: int = DEFAULT_FREQUENCY_HZ) -> DetectResult:
    """Detect the ESP-Prog-2 and whether an RA4M1 target is reachable through it."""
    probe = _choose_raw(unique_id)
    if probe is None:
        return DetectResult(TargetState.NO_PROBE, "No ESP-Prog-2 / CMSIS-DAP probe connected.")

    info = _to_info(probe)
    session = _open_session(probe, frequency)
    try:
        session.open()  # examines the DP and the Cortex-M core
        target = session.target
        try:
            target.halt()
            pc = target.read_core_register("pc")
            blank = (pc & 0xFFFFFFFE) == (_BLANK_PC & 0xFFFFFFFE)
            if blank:
                return DetectResult(
                    TargetState.CONNECTED_BLANK,
                    f"RA4M1 connected via {info.unique_id} — flash is blank/unprogrammed "
                    f"(pc=0x{pc:08x}). Ready to flash.",
                    info,
                )
            return DetectResult(
                TargetState.CONNECTED,
                f"RA4M1 connected via {info.unique_id} (pc=0x{pc:08x}).",
                info,
            )
        except Exception:
            # Core examined but could not be halted/read — still present and flashable.
            return DetectResult(TargetState.CONNECTED, f"RA4M1 connected via {info.unique_id}.", info)
    except Exception as exc:  # noqa: BLE001 — surface any link/examine failure as "no target"
        return DetectResult(
            TargetState.NO_TARGET,
            f"Probe {info.unique_id} found, but no RA4M1 target responded over SWD "
            f"({type(exc).__name__}). Check wiring and target power.",
            info,
        )
    finally:
        try:
            session.close()
        except Exception:
            pass


def flash(
    hex_path: Path,
    unique_id: Optional[str] = None,
    frequency: int = DEFAULT_FREQUENCY_HZ,
    progress: Optional[ProgressCallback] = None,
    log: Optional[LogCallback] = None,
) -> None:
    """Program ``hex_path`` to the RA4M1 target and reset it. Raises on failure."""
    from pyocd.flash.file_programmer import FileProgrammer

    hex_path = Path(hex_path)
    if not hex_path.is_file():
        raise FileNotFoundError(f"Firmware file not found: {hex_path}")

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    probe = _choose_raw(unique_id)
    if probe is None:
        raise RuntimeError("No ESP-Prog-2 / CMSIS-DAP probe connected.")

    _log(f"Opening {TARGET_TYPE} via {probe.unique_id} @ {frequency // 1000} kHz")
    session = _open_session(probe, frequency)
    session.open()
    try:
        _log(f"Programming {hex_path.name} ({hex_path.stat().st_size} bytes)")
        programmer = FileProgrammer(session, progress=progress)
        programmer.program(str(hex_path))
        _log("Resetting target")
        session.target.reset()
        _log("Done.")
    finally:
        try:
            session.close()
        except Exception:
            pass


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


def find_probe_firmware() -> Optional[Path]:
    """Locate the bundled firmware/esp-prog2.bin regardless of launch directory."""
    seen: set[Path] = set()
    for base in (Path(__file__).resolve(), Path.cwd().resolve()):
        for parent in [base, *base.parents]:
            candidate = parent / "firmware" / _PROBE_BIN_NAME
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_file():
                return candidate
    return None


def find_probe_serial() -> Optional[ProbeSerial]:
    """Find the ESP-Prog-2's serial port; prefer a ROM-download-mode port if present."""
    from serial.tools import list_ports

    esp = [p for p in list_ports.comports() if p.vid == ESP_PROG_VID]
    if not esp:
        return None
    download = [p for p in esp if p.pid not in (None, ESP_PROG_PID)]
    chosen = (download or esp)[0]
    return ProbeSerial(chosen.device, chosen.pid or 0)


def download_probe_firmware(dest: Optional[Path] = None, log: Optional[LogCallback] = None) -> Path:
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
        pass

    if dest is None:
        bundled = find_probe_firmware()
        base = bundled.parent if bundled else (Path.cwd() / "firmware")
        dest = base / _OFFICIAL_BIN_NAME
    else:
        dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if log:
        log(f"Downloading {PROBE_FIRMWARE_URL}")
    with urllib.request.urlopen(PROBE_FIRMWARE_URL, timeout=60) as resp:
        data = resp.read()
    if not data[:1] == b"\xe9":
        raise RuntimeError("Downloaded file is not an ESP image (missing 0xE9 magic).")
    dest.write_bytes(data)
    if log:
        log(f"Saved {len(data)} bytes to {dest}")
    return dest


def flash_probe(
    bin_path: Path,
    port: Optional[str] = None,
    baud: int = 921600,
    progress: Optional[ProgressCallback] = None,
    log: Optional[LogCallback] = None,
) -> None:
    """Flash the ESP-Prog-2's own firmware (ESP32-S3) with esptool. Raises on failure.

    The board must be in ROM download mode (hold BOOT, tap RESET) — the running
    bridge firmware does not expose a self-reflash path on its CDC port.
    """
    import re
    import subprocess
    import sys

    bin_path = Path(bin_path)
    if not bin_path.is_file():
        raise FileNotFoundError(f"Firmware file not found: {bin_path}")

    args = [sys.executable, "-m", "esptool", "--chip", PROBE_CHIP]
    if port:
        args += ["--port", port]
    args += ["--baud", str(baud), "write-flash", PROBE_FLASH_OFFSET, str(bin_path)]

    if log:
        log("esptool " + " ".join(args[3:]))
    percent = re.compile(r"\((\d+)\s*%\)")
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line and log:
            log(line)
        match = percent.search(line)
        if match and progress:
            try:
                progress(float(match.group(1)))
            except ValueError:
                pass
    if proc.wait() != 0:
        raise RuntimeError(
            f"esptool exited with code {proc.returncode}. "
            "Is the ESP-Prog-2 in download mode (hold BOOT, tap RESET)?"
        )
