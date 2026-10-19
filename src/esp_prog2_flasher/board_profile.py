"""Encode and decode the ESP32-S3 board-profile record and build a SOLO board's cancfg image.

The SOLO boards share one CAN bootloader and updater, so each board's TWAI pins, CAN bitrate
and early hold-LOW GPIOs come from a 24-byte board-profile record stored right after the v2
DFU config block in the cancfg partition. This module encodes and validates that record,
pads v2 block + record into the 4 KB cancfg image flasher.py writes, decodes a record read
back exactly as the firmware would, and owns the SOLO ODU/IDU presets that cli.py offers.

Owns:        the record layout, magic, version, refused GPIOs and bitrate codes; the
             firmware's fallback record (DEFAULT_PROFILE_RECORD, mirrored here, owned by the
             updater); the cancfg image layout (v2 block at 0x00, record at 0x50, 0xFF to
             4 KB); and SOLO_PROFILES, the SOLO presets (flash layout, TWAI pins, holds).
Not here:    the v2 block and the known devices' CAN ID pairs (dfu_config.py, which a preset's
             product name keys into); the release bundle, its manifest and writing the image
             to flash (flasher.py); the flags (cli.py).
Invariants:  FROZEN CONTRACT — the record layout, magic, version, length, CRC, refused GPIOs,
             bitrate codes and the record's 0x50 offset are shared byte-for-byte with the
             Embedded-CAN-Bootloaders ESP32 bootloader and updater that read them. v1/v2
             DFU-config readers only read the first 80 bytes, so the record never disturbs
             them. Every SOLO_PROFILES product is a key of dfu_config.DEVICE_CAN_IDS.
Errors:      raises BoardProfileError subclasses: InvalidProfileError for a record field that
             breaks a rule. parse_profile_record never raises: an invalid or absent record
             decodes to None and the firmware then uses DEFAULT_PROFILE_RECORD.
Depends on:  dfu_config.py and the standard library only.
See also:    tests/test_board_profile.py, dfu_config.py, flasher.py

Board-profile record v1 (little-endian, 24 bytes, at cancfg offset 0x50):

    0x00  u32  magic          = 0xDFB0A7D1
    0x04  u8   version        = 1
    0x05  u8   length         = 24
    0x06  u16  reserved       = 0xFFFF (ignored on read)
    0x08  u8   twai_tx_gpio   0..48, not refused
    0x09  u8   twai_rx_gpio   0..48, not refused, != tx
    0x0A  u8   bitrate code   0 = 125k, 1 = 250k, 2 = 500k, 3 = 1M
    0x0B  u8   reserved       = 0xFF (ignored on read)
    0x0C  u64  hold_low_mask  bit n = drive GPIOn LOW early; no refused or TWAI pins
    0x14  u32  crc32          = zlib.crc32 over bytes 0x00..0x13
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum
from typing import Final, Literal

from .dfu_config import BLOCK_SIZE, DEVICE_CAN_IDS, build_config_block

# FROZEN CONTRACT — mirrored in the ESP32 bootloader and updater; never change one side alone.
PROFILE_MAGIC: Final[int] = 0xDFB0A7D1
PROFILE_VERSION: Final[int] = 1
# magic, version, length, reserved u16, tx, rx, bitrate, reserved u8, hold_low_mask.
_BODY_FORMAT: Final[str] = "<IBBHBBBBQ"
_BODY_SIZE: Final[int] = struct.calcsize(_BODY_FORMAT)  # 20 — the CRC's coverage
PROFILE_RECORD_SIZE: Final[int] = _BODY_SIZE + 4  # 24
PROFILE_RECORD_OFFSET: Final[int] = BLOCK_SIZE  # 0x50, right after the v2 block
CANCFG_IMAGE_SIZE: Final[int] = 0x1000  # the whole cancfg partition
MAX_GPIO: Final[int] = 48  # ESP32-S3 GPIO0..GPIO48
# 0 is the BOOT strap, 19/20 are USB D-/D+, 26..32 are flash/PSRAM on the
# WROOM-1U-N16R8 and the S3FH4R2: never TWAI pins, never driven LOW.
REFUSED_GPIOS: Final[frozenset[int]] = frozenset({0, 19, 20, *range(26, 33)})
_U16_RESERVED: Final[int] = 0xFFFF
_U8_RESERVED: Final[int] = 0xFF
_PAD_BYTE: Final[bytes] = b"\xff"


class Bitrate(IntEnum):
    """CAN bitrate codes stored in the record."""

    KBPS_125 = 0
    KBPS_250 = 1
    KBPS_500 = 2
    MBPS_1 = 3


# Flash layouts the release bundle carries; each name maps to the chip's flash size.
LayoutName = Literal["node-4MB", "idu-16MB"]
FlashSize = Literal["4MB", "16MB"]
LAYOUT_FLASH_SIZES: Final[dict[LayoutName, FlashSize]] = {"node-4MB": "4MB", "idu-16MB": "16MB"}


class BoardProfileError(Exception):
    """Base error for this module — callers catch this, not Exception."""


class InvalidProfileError(BoardProfileError):
    """Raised when a record field breaks one of the record's rules."""


@dataclass(frozen=True, slots=True)
class ProfileRecord:
    """The board-profile record's fields: TWAI pins, bitrate and hold-LOW GPIO mask."""

    twai_tx_gpio: int
    twai_rx_gpio: int
    bitrate: Bitrate
    hold_low_mask: int


# The updater's fallback for an invalid or absent record (today's ODU behaviour).
DEFAULT_PROFILE_RECORD: Final[ProfileRecord] = ProfileRecord(4, 5, Bitrate.KBPS_250, 0)


@dataclass(frozen=True, slots=True)
class BoardProfile:
    """A SOLO preset: which device it is, its flash layout, and its profile record.

    ``product`` names a KNOWN device in ``dfu_config.DEVICE_CAN_IDS``, which supplies
    the fixed CAN ID pair of the v2 block; the flash size follows from ``layout``.
    """

    product: str
    layout: LayoutName
    record: ProfileRecord

    def __post_init__(self) -> None:
        if self.product not in DEVICE_CAN_IDS:
            raise BoardProfileError(
                f"{self.product!r} is not a known device (dfu_config.DEVICE_CAN_IDS)"
            )

    @property
    def flash_size(self) -> FlashSize:
        return LAYOUT_FLASH_SIZES[self.layout]

    @property
    def boot_ids(self) -> tuple[int, int]:
        """The fixed ``(boot_cmd_id, boot_reply_id)`` pair from the device table."""
        return DEVICE_CAN_IDS[self.product]

    def config_block(self, manufacturer: str) -> bytes:
        """Encode this preset's 80-byte v2 DFU config block.

        Raises:
            InvalidNameError: if ``manufacturer`` violates the block contract.
        """
        boot_cmd_id, boot_reply_id = self.boot_ids
        return build_config_block(manufacturer, self.product, boot_cmd_id, boot_reply_id)


SOLO_PROFILES: Final[dict[str, BoardProfile]] = {
    profile.product: profile
    for profile in (
        BoardProfile("SOLO ODU Controller", "node-4MB", ProfileRecord(4, 5, Bitrate.KBPS_250, 0)),
        BoardProfile(
            "SOLO IDU Controller", "idu-16MB", ProfileRecord(6, 7, Bitrate.KBPS_250, 1 << 3)
        ),
    )
}


def _gpio_mask(gpios: frozenset[int]) -> int:
    mask = 0
    for gpio in gpios:
        mask |= 1 << gpio
    return mask


_REFUSED_MASK: Final[int] = _gpio_mask(REFUSED_GPIOS)


def _rule_violation(
    twai_tx_gpio: int, twai_rx_gpio: int, bitrate_code: int, mask: int
) -> str | None:
    """Return why the fields break the record's rules, or ``None`` when they are valid."""
    for field, gpio in (("twai_tx_gpio", twai_tx_gpio), ("twai_rx_gpio", twai_rx_gpio)):
        if not 0 <= gpio <= MAX_GPIO:
            return f"{field} must be GPIO 0..{MAX_GPIO}, got {gpio}"
        if gpio in REFUSED_GPIOS:
            return f"{field} GPIO{gpio} is refused (strap, USB or flash/PSRAM pin)"
    if twai_tx_gpio == twai_rx_gpio:
        return f"twai_tx_gpio and twai_rx_gpio must differ, both are GPIO{twai_tx_gpio}"
    if bitrate_code not in tuple(Bitrate):
        return f"bitrate code must be 0..{max(Bitrate)}, got {bitrate_code}"
    if mask & _REFUSED_MASK:
        return f"hold_low_mask 0x{mask:X} holds a refused GPIO"
    if mask & ((1 << twai_tx_gpio) | (1 << twai_rx_gpio)):
        return f"hold_low_mask 0x{mask:X} holds a TWAI pin"
    return None


def encode_profile_record(record: ProfileRecord) -> bytes:
    """Encode the 24-byte board-profile record.

    Raises:
        InvalidProfileError: if a field breaks the record's rules, or (stricter than
            the reader) the hold mask names a GPIO above 48, so the tool never writes
            a hold the chip cannot carry out.
    """
    fields = (record.twai_tx_gpio, record.twai_rx_gpio, record.hold_low_mask)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in fields):
        raise InvalidProfileError(f"record fields must be integers, got {record!r}")
    mask = record.hold_low_mask
    if not 0 <= mask < (1 << (MAX_GPIO + 1)):
        raise InvalidProfileError(f"hold_low_mask 0x{mask:X} names a GPIO above {MAX_GPIO}")
    violation = _rule_violation(record.twai_tx_gpio, record.twai_rx_gpio, record.bitrate, mask)
    if violation is not None:
        raise InvalidProfileError(violation)

    body = struct.pack(
        _BODY_FORMAT,
        PROFILE_MAGIC,
        PROFILE_VERSION,
        PROFILE_RECORD_SIZE,
        _U16_RESERVED,
        record.twai_tx_gpio,
        record.twai_rx_gpio,
        record.bitrate,
        _U8_RESERVED,
        mask,
    )
    return body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)


def parse_profile_record(data: bytes) -> ProfileRecord | None:
    """Decode a record read from a device, or ``None`` if it is invalid or absent.

    Mirrors the updater: the record is valid only when the magic, version, length
    and CRC match and every field rule holds; both reserved fields are ignored.
    ``None`` means the firmware uses ``DEFAULT_PROFILE_RECORD``. Bytes past the
    record are ignored.
    """
    if len(data) < PROFILE_RECORD_SIZE:
        return None
    magic, version, length, _reserved16, tx, rx, bitrate_code, _reserved8, mask = (
        struct.unpack_from(_BODY_FORMAT, data)
    )
    if magic != PROFILE_MAGIC or version != PROFILE_VERSION or length != PROFILE_RECORD_SIZE:
        return None
    (stored_crc,) = struct.unpack_from("<I", data, _BODY_SIZE)
    if zlib.crc32(data[:_BODY_SIZE]) & 0xFFFFFFFF != stored_crc:
        return None
    if _rule_violation(tx, rx, bitrate_code, mask) is not None:
        return None
    return ProfileRecord(tx, rx, Bitrate(bitrate_code), mask)


def build_cancfg_image(config_block: bytes, record: ProfileRecord) -> bytes:
    """Return the 4 KB cancfg image: ``config_block`` at 0x00, the record at 0x50, then 0xFF.

    Raises:
        BoardProfileError: if ``config_block`` is not an 80-byte v2 block.
        InvalidProfileError: if the record breaks its rules.
    """
    if len(config_block) != BLOCK_SIZE:
        raise BoardProfileError(f"config block must be {BLOCK_SIZE} bytes, got {len(config_block)}")
    image = config_block + encode_profile_record(record)
    return image.ljust(CANCFG_IMAGE_SIZE, _PAD_BYTE)
