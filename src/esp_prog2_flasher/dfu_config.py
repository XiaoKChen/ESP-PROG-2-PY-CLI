"""Encode the DFU config block written to the RA4M1 data flash.

FROZEN CONTRACT — these values are shared byte-for-byte with the bootloader that
reads this block. Do not change any of them without changing the bootloader too.

Block layout v2 (little-endian, 80 bytes, every unused/tail byte padded with 0xFF):

    0x00  u32       magic         = 0xDF0C0FDF
    0x04  u8        version       = 2
    0x05  u8        mfr_len       (0..32)
    0x06  u8        prod_len      (0..32)
    0x07  u8        reserved      = 0xFF
    0x08  char[32]  manufacturer  (ASCII; bytes past mfr_len = 0xFF)
    0x28  char[32]  product       (ASCII; bytes past prod_len = 0xFF)
    0x48  u16       boot_cmd_id   host→boot CAN ID (11-bit 0x000..0x7FF);
                                  0xFFFF = unset → bootloader default 0x79E
    0x4A  u16       boot_reply_id boot→host CAN ID (11-bit 0x000..0x7FF);
                                  0xFFFF = unset → bootloader default 0x79F
    0x4C  u32       crc32         = zlib.crc32 over the first 76 bytes (0x00..0x4B)
"""

from __future__ import annotations

import struct
import zlib
from typing import Final

# FROZEN CONTRACT — mirrored in the bootloader; never change without changing both.
CONFIG_ADDRESS: Final[int] = 0x40101C00  # RA4M1 data flash, last 1 KB block.
# R_FACI_LP->DFLCTL (R_FACI_LP_BASE 0x407EC000 + 0x90). Writing 1 enables reads of
# the 0x40100000 data-flash region; reads return garbage while it is 0. Not part of
# the frozen contract — a hardware quirk of the read-back path (see flasher.py).
DATA_FLASH_ENABLE_ADDR: Final[int] = 0x407EC090
CONFIG_MAGIC: Final[int] = 0xDF0C0FDF
CONFIG_VERSION: Final[int] = 2
MAX_STRING_LEN: Final[int] = 32
BLOCK_SIZE: Final[int] = 80
_PAD_BYTE: Final[int] = 0xFF
_RESERVED_BYTE: Final[int] = 0xFF
# CRC covers everything before the trailing u32 crc field.
_CRC_COVERAGE: Final[int] = BLOCK_SIZE - 4  # 76
_MIN_ASCII: Final[int] = 0x20
_MAX_ASCII: Final[int] = 0x7E

# --- Per-device bootloader CAN ID pair (block offsets 0x48/0x4A) -------------
# Sentinel written when the provisioner leaves an ID unset; the bootloader then
# falls back to its compiled-in defaults below. 11-bit classic-CAN IDs only.
UNSET_CAN_ID: Final[int] = 0xFFFF
MAX_CAN_ID: Final[int] = 0x7FF  # 11-bit standard CAN identifier ceiling.
# Bootloader fallback IDs when the pair is left unset — documented here for the
# CLI/TUI help text; the *bootloader* owns these values, not this block.
DEFAULT_BOOT_CMD_ID: Final[int] = 0x79E  # host→boot
DEFAULT_BOOT_REPLY_ID: Final[int] = 0x79F  # boot→host

# ---------------------------------------------------------------------------
# Provisioning defaults — the values the flasher OFFERS in its CLI/TUI. NOT part
# of the frozen block contract; safe to edit here without touching the bootloader
# (they are just what gets encoded into the block above). Every entry must satisfy
# _validate_name (printable ASCII, <= MAX_STRING_LEN) — guarded by a host test.
# ---------------------------------------------------------------------------
DEFAULT_MANUFACTURER: Final[str] = "Normal Corporation"

# Single source of truth for the KNOWN devices and their fixed bootloader CAN ID
# pairs. Mirrors airform-server-dev's BOARDS table: boot_cmd_id = board.rx
# (host->boot), boot_reply_id = board.tx (boot->host). ODU and IDU sit on separate
# buses, so both reuse the low IDs — each named device still maps to exactly one
# pair. A KNOWN device's pair is not user-editable (fixed here); a CUSTOM name has
# no entry and the provisioner supplies its own pair. Every key must satisfy
# _validate_name and every value must be a valid pair — guarded by host tests.
DEVICE_CAN_IDS: Final[dict[str, tuple[int, int]]] = {
    "ODU Controller": (0x05, 0x06),
    "ODU Superheat": (0x03, 0x04),
    "ODU Air Sensor": (0x01, 0x02),
    "ODU Power Board": (0x07, 0x08),
    "IDU Controller": (0x07, 0x08),
    "IDU Power Board": (0x09, 0x0A),
    "IDU Radar": (0x01, 0x02),
    "IDU Articulation": (0x05, 0x06),
    "IDU Air Sensor": (0x03, 0x04),
}
# Derived, never stored separately: the ordered device list is just these keys.
DEVICE_NAMES: Final[tuple[str, ...]] = tuple(DEVICE_CAN_IDS)


class DfuConfigError(Exception):
    """Base error for this module — callers catch this, not Exception."""


class InvalidNameError(DfuConfigError):
    """Raised when a manufacturer/product name fails validation at the boundary."""


class InvalidCanIdError(DfuConfigError):
    """Raised when a boot CAN ID is out of range or the pair is half-specified."""


def boot_ids_for_device(name: str) -> tuple[int, int] | None:
    """Return the fixed ``(boot_cmd_id, boot_reply_id)`` for a KNOWN device.

    A device is KNOWN when it appears in ``DEVICE_CAN_IDS`` (equivalently
    ``DEVICE_NAMES``); its pair is fixed and not user-editable. Any other name is
    a CUSTOM device and returns ``None`` — the caller then supplies its own pair.
    """
    return DEVICE_CAN_IDS.get(name)


def _validate_name(field: str, value: str) -> bytes:
    """Return the ASCII bytes of ``value`` after checking it against the contract.

    Raises:
        InvalidNameError: if the name exceeds ``MAX_STRING_LEN`` or contains a
            byte outside printable ASCII (0x20..0x7E).
    """
    if len(value) > MAX_STRING_LEN:
        raise InvalidNameError(
            f"{field} must be at most {MAX_STRING_LEN} characters, got {len(value)}"
        )
    for char in value:
        code = ord(char)
        if code < _MIN_ASCII or code > _MAX_ASCII:
            raise InvalidNameError(
                f"{field} must be printable ASCII (0x20..0x7E); found U+{code:04X} ({char!r})"
            )
    return value.encode("ascii")


def _pad(data: bytes) -> bytes:
    return data + bytes([_PAD_BYTE]) * (MAX_STRING_LEN - len(data))


def _validate_can_id(field: str, value: int) -> int:
    """Return ``value`` after checking it is an 11-bit CAN identifier.

    Raises:
        InvalidCanIdError: if ``value`` is not an int (bools are rejected) or
            lies outside 0x000..0x7FF.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidCanIdError(f"{field} must be an integer, got {value!r}")
    if not 0 <= value <= MAX_CAN_ID:
        raise InvalidCanIdError(
            f"{field} must be an 11-bit CAN ID (0x000..0x{MAX_CAN_ID:03X}), got 0x{value:X}"
        )
    return value


def _resolve_can_id_pair(boot_cmd_id: int | None, boot_reply_id: int | None) -> tuple[int, int]:
    """Resolve the CAN ID pair to the two u16 words written into the block.

    Rule: the pair is all-or-nothing. Both ``None`` → both words are
    ``UNSET_CAN_ID`` (bootloader keeps its defaults). Supplying exactly one is an
    error — a half-configured device would silently use one custom ID and one
    default, which is never what the provisioner meant.

    Raises:
        InvalidCanIdError: if exactly one of the pair is given, or either ID is
            out of range.
    """
    if (boot_cmd_id is None) != (boot_reply_id is None):
        raise InvalidCanIdError(
            "boot_cmd_id and boot_reply_id must be set together (or both omitted)."
        )
    if boot_cmd_id is None or boot_reply_id is None:
        return UNSET_CAN_ID, UNSET_CAN_ID
    return (
        _validate_can_id("boot_cmd_id", boot_cmd_id),
        _validate_can_id("boot_reply_id", boot_reply_id),
    )


def build_config_block(
    manufacturer: str,
    product: str,
    boot_cmd_id: int | None = None,
    boot_reply_id: int | None = None,
) -> bytes:
    """Encode the 80-byte v2 DFU config block.

    Carries the USB ``manufacturer``/``product`` names plus an optional per-device
    bootloader CAN ID pair. Both names (printable ASCII, ≤ 32 chars) and both IDs
    (11-bit, all-or-nothing) are validated before any bytes are produced, so bad
    input fails loudly instead of writing a corrupt block. Leaving the pair unset
    writes ``UNSET_CAN_ID`` (0xFFFF) so the bootloader falls back to 0x79E/0x79F.

    Raises:
        InvalidNameError: if either name violates the contract.
        InvalidCanIdError: if the ID pair is half-specified or out of range.
    """
    mfr = _validate_name("manufacturer", manufacturer)
    prod = _validate_name("product", product)
    cmd_id, reply_id = _resolve_can_id_pair(boot_cmd_id, boot_reply_id)

    header = struct.pack(
        "<IBBBB",
        CONFIG_MAGIC,
        CONFIG_VERSION,
        len(mfr),
        len(prod),
        _RESERVED_BYTE,
    )
    body = header + _pad(mfr) + _pad(prod) + struct.pack("<HH", cmd_id, reply_id)
    assert len(body) == _CRC_COVERAGE  # noqa: S101 — guards the frozen layout.

    crc = zlib.crc32(body) & 0xFFFFFFFF
    block = body + struct.pack("<I", crc)
    assert len(block) == BLOCK_SIZE  # noqa: S101 — guards the frozen layout.
    return block
