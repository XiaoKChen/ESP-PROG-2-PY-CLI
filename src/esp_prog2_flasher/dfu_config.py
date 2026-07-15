"""Encode the DFU name config block written to the RA4M1 data flash.

FROZEN CONTRACT — these values are shared byte-for-byte with the bootloader that
reads this block. Do not change any of them without changing the bootloader too.

Block layout (little-endian, 76 bytes, every unused/tail byte padded with 0xFF):

    0x00  u32       magic    = 0xDF0C0FDF
    0x04  u8        version  = 1
    0x05  u8        mfr_len  (0..32)
    0x06  u8        prod_len (0..32)
    0x07  u8        reserved = 0xFF
    0x08  char[32]  manufacturer (ASCII; bytes past mfr_len = 0xFF)
    0x28  char[32]  product      (ASCII; bytes past prod_len = 0xFF)
    0x48  u32       crc32    = zlib.crc32 over the first 72 bytes (0x00..0x47)
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
CONFIG_VERSION: Final[int] = 1
MAX_STRING_LEN: Final[int] = 32
BLOCK_SIZE: Final[int] = 76
_PAD_BYTE: Final[int] = 0xFF
_RESERVED_BYTE: Final[int] = 0xFF
# CRC covers everything before the trailing u32 crc field.
_CRC_COVERAGE: Final[int] = BLOCK_SIZE - 4  # 72
_MIN_ASCII: Final[int] = 0x20
_MAX_ASCII: Final[int] = 0x7E


class DfuConfigError(Exception):
    """Base error for this module — callers catch this, not Exception."""


class InvalidNameError(DfuConfigError):
    """Raised when a manufacturer/product name fails validation at the boundary."""


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
                f"{field} must be printable ASCII (0x20..0x7E); "
                f"found U+{code:04X} ({char!r})"
            )
    return value.encode("ascii")


def _pad(data: bytes) -> bytes:
    return data + bytes([_PAD_BYTE]) * (MAX_STRING_LEN - len(data))


def build_config_block(manufacturer: str, product: str) -> bytes:
    """Encode the 76-byte DFU name config block for ``manufacturer``/``product``.

    Both names are validated (printable ASCII, ≤ 32 chars) before any bytes are
    produced, so a bad name fails loudly instead of writing a corrupt block.

    Raises:
        InvalidNameError: if either name violates the contract.
    """
    mfr = _validate_name("manufacturer", manufacturer)
    prod = _validate_name("product", product)

    header = struct.pack(
        "<IBBBB",
        CONFIG_MAGIC,
        CONFIG_VERSION,
        len(mfr),
        len(prod),
        _RESERVED_BYTE,
    )
    body = header + _pad(mfr) + _pad(prod)
    assert len(body) == _CRC_COVERAGE  # noqa: S101 — guards the frozen layout.

    crc = zlib.crc32(body) & 0xFFFFFFFF
    block = body + struct.pack("<I", crc)
    assert len(block) == BLOCK_SIZE  # noqa: S101 — guards the frozen layout.
    return block
