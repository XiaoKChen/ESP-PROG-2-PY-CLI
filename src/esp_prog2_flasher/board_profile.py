"""Encode and decode the ESP32-S3 board-profile record and build a SOLO board's cancfg image.

The SOLO boards share one CAN bootloader and updater, so each board's TWAI pins, CAN bitrate,
early hold-LOW/hold-HIGH GPIOs and I2C safe writes come from a board-profile record stored
right after the v2 DFU config block in the cancfg partition. This module encodes and
validates that record (v1, 24 bytes, or v2, 56 bytes), pads v2 block + record into the 4 KB
cancfg image flasher.py writes, decodes a record read back exactly as the firmware would (and
words it for the CLI and TUI), and owns the SOLO ODU/IDU presets that cli.py offers.

Owns:        the record layouts, magic, versions, refused GPIOs, bitrate codes and I2C limits;
             the firmware's fallback record (DEFAULT_PROFILE_RECORD, mirrored here, owned by
             the updater); the cancfg image layout (v2 block at 0x00, record at 0x50, 0xFF to
             4 KB); and SOLO_PROFILES, the SOLO presets (flash layout, TWAI pins, holds, I2C
             safe writes, and the v1 record each writes on a bundle without v2 support).
Not here:    the v2 block and the known devices' CAN ID pairs (dfu_config.py, which a preset's
             product name keys into); the release bundle, its manifest, which record version
             a bundle takes, and writing the image to flash (flasher.py); the flags (cli.py).
Invariants:  FROZEN CONTRACT — both record layouts, the magic, versions, lengths, CRC, refused
             GPIOs, bitrate codes, I2C limits and the record's 0x50 offset are shared
             byte-for-byte with the Embedded-CAN-Bootloaders ESP32 bootloader and updater that
             read them. Only c-cpp-esp32-v1.3.0 and later read v2; older ones reject it and
             fall back to DEFAULT_PROFILE_RECORD. v1/v2 DFU-config readers only read the first
             80 bytes, so the record never disturbs them. Every SOLO_PROFILES product is a key
             of dfu_config.DEVICE_CAN_IDS, and every preset's v1 record encodes as v1.
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

Board-profile record v2 (little-endian, 56 bytes, same offset and magic; 0x00..0x13 as v1
except version = 2 and length = 56):

    0x14  u64  hold_high_mask   bit n = drive GPIOn HIGH early; hold_low rules, and
                                disjoint from hold_low_mask
    0x1C  u8   i2c_sda_gpio     0xFF = no I2C safe writes (then scl = addr = 0xFF, count 0);
                                else 0..48, not refused, not TWAI, not held, != scl
    0x1D  u8   i2c_scl_gpio     same rules as SDA
    0x1E  u8   i2c_addr7        0x08..0x77
    0x1F  u8   i2c_write_count  1..8 (0 with no I2C)
    0x20  8 x {u8 reg, u8 value}  register writes in order; unused pairs = 0xFF 0xFF
                                (ignored on read)
    0x30  u32  reserved         = 0xFFFFFFFF (ignored on read)
    0x34  u32  crc32            = zlib.crc32 over bytes 0x00..0x33
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
ProfileVersion = Literal[1, 2]
PROFILE_VERSION_V1: Final = 1
PROFILE_VERSION_V2: Final = 2
# magic, version, length, reserved u16, tx, rx, bitrate, reserved u8, hold_low_mask.
_BODY_FORMAT: Final[str] = "<IBBHBBBBQ"
_BODY_SIZE: Final[int] = struct.calcsize(_BODY_FORMAT)  # 20 — the v1 CRC's coverage
PROFILE_RECORD_SIZE: Final[int] = _BODY_SIZE + 4  # 24, the v1 record
# The v1 body, then hold_high_mask, SDA, SCL, addr7, write count, 8 reg/value pairs and a
# reserved u32.
_V2_BODY_FORMAT: Final[str] = "<IBBHBBBBQQBBBB16sI"
_V2_BODY_SIZE: Final[int] = struct.calcsize(_V2_BODY_FORMAT)  # 52 — the v2 CRC's coverage
PROFILE_RECORD_V2_SIZE: Final[int] = _V2_BODY_SIZE + 4  # 56
PROFILE_RECORD_MAX_SIZE: Final[int] = PROFILE_RECORD_V2_SIZE  # what a reader must fetch
PROFILE_RECORD_OFFSET: Final[int] = BLOCK_SIZE  # 0x50, right after the v2 block
CANCFG_IMAGE_SIZE: Final[int] = 0x1000  # the whole cancfg partition
MAX_GPIO: Final[int] = 48  # ESP32-S3 GPIO0..GPIO48
# 0 is the BOOT strap, 19/20 are USB D-/D+, 26..32 are flash/PSRAM on the
# WROOM-1U-N16R8 and the S3FH4R2: never TWAI or I2C pins, never driven.
REFUSED_GPIOS: Final[frozenset[int]] = frozenset({0, 19, 20, *range(26, 33)})
MAX_I2C_WRITES: Final[int] = 8
# 7-bit addresses outside 0x08..0x77 are reserved by the I2C specification.
I2C_ADDR_MIN: Final[int] = 0x08
I2C_ADDR_MAX: Final[int] = 0x77
NO_I2C: Final[int] = 0xFF  # i2c_sda_gpio/scl/addr when the record has no I2C safe writes
_U32_RESERVED: Final[int] = 0xFFFFFFFF
_U16_RESERVED: Final[int] = 0xFFFF
_U8_RESERVED: Final[int] = 0xFF
_PAD_BYTE: Final[bytes] = b"\xff"
_MAX_BYTE: Final[int] = 0xFF


class Bitrate(IntEnum):
    """CAN bitrate codes stored in the record."""

    KBPS_125 = 0
    KBPS_250 = 1
    KBPS_500 = 2
    MBPS_1 = 3


BITRATE_LABELS: Final[dict[Bitrate, str]] = {
    Bitrate.KBPS_125: "125 kbit/s",
    Bitrate.KBPS_250: "250 kbit/s",
    Bitrate.KBPS_500: "500 kbit/s",
    Bitrate.MBPS_1: "1 Mbit/s",
}


# Flash layouts the release bundle carries; each name maps to the chip's flash size.
LayoutName = Literal["node-4MB", "idu-16MB"]
FlashSize = Literal["4MB", "16MB"]
LAYOUT_FLASH_SIZES: Final[dict[LayoutName, FlashSize]] = {"node-4MB": "4MB", "idu-16MB": "16MB"}


class BoardProfileError(Exception):
    """Base error for this module — callers catch this, not Exception."""


class InvalidProfileError(BoardProfileError):
    """Raised when a record field breaks one of the record's rules."""


@dataclass(frozen=True, slots=True)
class I2cSafeWrites:
    """Register writes the bootloader sends to one I2C device as early as possible (v2 only).

    ``writes`` holds ``(register, value)`` byte pairs, sent in order.
    """

    sda_gpio: int
    scl_gpio: int
    addr7: int
    writes: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class ProfileRecord:
    """The board-profile record's fields: TWAI pins, bitrate, held GPIOs and I2C safe writes.

    ``hold_high_mask`` and ``i2c`` exist only in record v2; a record using either needs v2.
    """

    twai_tx_gpio: int
    twai_rx_gpio: int
    bitrate: Bitrate
    hold_low_mask: int
    hold_high_mask: int = 0
    i2c: I2cSafeWrites | None = None

    @property
    def needs_v2(self) -> bool:
        """Whether a field only record v2 can carry is in use."""
        return self.hold_high_mask != 0 or self.i2c is not None


# The updater's fallback for an invalid or absent record (today's ODU behaviour).
DEFAULT_PROFILE_RECORD: Final[ProfileRecord] = ProfileRecord(4, 5, Bitrate.KBPS_250, 0)


@dataclass(frozen=True, slots=True)
class BoardProfile:
    """A SOLO preset: which device it is, its flash layout, and its profile records.

    ``product`` names a KNOWN device in ``dfu_config.DEVICE_CAN_IDS``, which supplies
    the fixed CAN ID pair of the v2 block; the flash size follows from ``layout``.
    ``record`` is written as record v2 on a bundle that reads it. ``v1_record`` is written
    on an older bundle instead; ``None`` means ``record`` itself, which must then fit v1.
    """

    product: str
    layout: LayoutName
    record: ProfileRecord
    v1_record: ProfileRecord | None = None

    def __post_init__(self) -> None:
        if self.product not in DEVICE_CAN_IDS:
            raise BoardProfileError(
                f"{self.product!r} is not a known device (dfu_config.DEVICE_CAN_IDS)"
            )
        if self.record_for(PROFILE_VERSION_V1).needs_v2:
            raise BoardProfileError(f"{self.product!r} needs a v1 record for older bundles")

    @property
    def flash_size(self) -> FlashSize:
        return LAYOUT_FLASH_SIZES[self.layout]

    @property
    def boot_ids(self) -> tuple[int, int]:
        """The fixed ``(boot_cmd_id, boot_reply_id)`` pair from the device table."""
        return DEVICE_CAN_IDS[self.product]

    def record_for(self, version: ProfileVersion) -> ProfileRecord:
        """The record this preset writes as record ``version``."""
        if version == PROFILE_VERSION_V1 and self.v1_record is not None:
            return self.v1_record
        return self.record

    def config_block(self, manufacturer: str) -> bytes:
        """Encode this preset's 80-byte v2 DFU config block.

        Raises:
            InvalidNameError: if ``manufacturer`` violates the block contract.
        """
        boot_cmd_id, boot_reply_id = self.boot_ids
        return build_config_block(manufacturer, self.product, boot_cmd_id, boot_reply_id)


def _gpio_mask(gpios: frozenset[int]) -> int:
    mask = 0
    for gpio in gpios:
        mask |= 1 << gpio
    return mask


_REFUSED_MASK: Final[int] = _gpio_mask(REFUSED_GPIOS)

# Pins the ODU app (Airform-SOLO-ODU-Controller src/main.cpp setup()) drives to safe levels
# first thing: reversing valve PULSE/DIR, relays 1-2, fan 6 PWM, EXV STEP/DIR/SM0/SM1/RESET.
_ODU_HOLD_LOW: Final[int] = _gpio_mask(frozenset({1, 2, 17, 18, 6, 35, 34, 33, 47, 48}))
_ODU_HOLD_HIGH: Final[int] = 1 << 21  # EXV nENBL, active low: HIGH disables the driver.
# EMC2305 at 0x2C on SDA 8/SCL 9: Fan 1-5 Setting registers = 0. Any Fan Setting write
# also disables the chip's 4 s power-up watchdog, which would drive every fan to full scale.
_ODU_EMC2305_FANS_OFF: Final[I2cSafeWrites] = I2cSafeWrites(
    8, 9, 0x2C, ((0x30, 0x00), (0x40, 0x00), (0x50, 0x00), (0x60, 0x00), (0x70, 0x00))
)

SOLO_PROFILES: Final[dict[str, BoardProfile]] = {
    profile.product: profile
    for profile in (
        BoardProfile(
            "SOLO ODU Controller",
            "node-4MB",
            ProfileRecord(
                4, 5, Bitrate.KBPS_250, _ODU_HOLD_LOW, _ODU_HOLD_HIGH, _ODU_EMC2305_FANS_OFF
            ),
            # Older bundles get today's record unchanged: no holds the app has not been
            # bench-tested with on those bootloaders.
            v1_record=ProfileRecord(4, 5, Bitrate.KBPS_250, 0),
        ),
        BoardProfile(
            "SOLO IDU Controller", "idu-16MB", ProfileRecord(6, 7, Bitrate.KBPS_250, 1 << 3)
        ),
    )
}


def _mask_violation(field: str, mask: int, twai_mask: int) -> str | None:
    if not 0 <= mask < (1 << (MAX_GPIO + 1)):
        return f"{field} 0x{mask:X} names a GPIO above {MAX_GPIO}"
    if mask & _REFUSED_MASK:
        return f"{field} 0x{mask:X} holds a refused GPIO"
    if mask & twai_mask:
        return f"{field} 0x{mask:X} holds a TWAI pin"
    return None


def _rule_violation(
    twai_tx_gpio: int, twai_rx_gpio: int, bitrate_code: int, mask: int
) -> str | None:
    """Return why the v1 fields break the record's rules, or ``None`` when they are valid."""
    for field, gpio in (("twai_tx_gpio", twai_tx_gpio), ("twai_rx_gpio", twai_rx_gpio)):
        if not 0 <= gpio <= MAX_GPIO:
            return f"{field} must be GPIO 0..{MAX_GPIO}, got {gpio}"
        if gpio in REFUSED_GPIOS:
            return f"{field} GPIO{gpio} is refused (strap, USB or flash/PSRAM pin)"
    if twai_tx_gpio == twai_rx_gpio:
        return f"twai_tx_gpio and twai_rx_gpio must differ, both are GPIO{twai_tx_gpio}"
    if bitrate_code not in tuple(Bitrate):
        return f"bitrate code must be 0..{max(Bitrate)}, got {bitrate_code}"
    return _mask_violation("hold_low_mask", mask, (1 << twai_tx_gpio) | (1 << twai_rx_gpio))


def _v2_rule_violation(
    tx: int,
    rx: int,
    bitrate_code: int,
    hold_low: int,
    hold_high: int,
    i2c: I2cSafeWrites | None,
) -> str | None:
    """Return why the v2 fields break the record's rules, or ``None`` when they are valid."""
    violation = _rule_violation(tx, rx, bitrate_code, hold_low)
    if violation is not None:
        return violation
    twai_mask = (1 << tx) | (1 << rx)
    violation = _mask_violation("hold_high_mask", hold_high, twai_mask)
    if violation is not None:
        return violation
    if hold_low & hold_high:
        return f"hold_low_mask and hold_high_mask overlap (0x{hold_low & hold_high:X})"
    if i2c is None:
        return None
    for field, gpio in (("i2c_sda_gpio", i2c.sda_gpio), ("i2c_scl_gpio", i2c.scl_gpio)):
        if not 0 <= gpio <= MAX_GPIO:
            return f"{field} must be GPIO 0..{MAX_GPIO}, got {gpio}"
        if gpio in REFUSED_GPIOS:
            return f"{field} GPIO{gpio} is refused (strap, USB or flash/PSRAM pin)"
        if (1 << gpio) & twai_mask:
            return f"{field} GPIO{gpio} is a TWAI pin"
        if (1 << gpio) & (hold_low | hold_high):
            return f"{field} GPIO{gpio} is in a hold mask"
    if i2c.sda_gpio == i2c.scl_gpio:
        return f"i2c_sda_gpio and i2c_scl_gpio must differ, both are GPIO{i2c.sda_gpio}"
    if not I2C_ADDR_MIN <= i2c.addr7 <= I2C_ADDR_MAX:
        return f"i2c_addr7 must be 0x{I2C_ADDR_MIN:02X}..0x{I2C_ADDR_MAX:02X}, got 0x{i2c.addr7:X}"
    if not 1 <= len(i2c.writes) <= MAX_I2C_WRITES:
        return f"i2c_write_count must be 1..{MAX_I2C_WRITES}, got {len(i2c.writes)}"
    for reg, value in i2c.writes:
        if not (0 <= reg <= _MAX_BYTE and 0 <= value <= _MAX_BYTE):
            return f"I2C write ({reg}, {value}) must be two bytes"
    return None


def _all_ints(values: list[object]) -> bool:
    return all(isinstance(value, int) and not isinstance(value, bool) for value in values)


def _crc_sealed(body: bytes) -> bytes:
    return body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)


def encode_profile_record(record: ProfileRecord, version: ProfileVersion = 1) -> bytes:
    """Encode the board-profile record as v1 (24 bytes) or v2 (56 bytes).

    Raises:
        InvalidProfileError: if a field breaks the record's rules, ``version`` is not 1 or
            2, or a v1 record would drop a hold-HIGH mask or I2C safe writes.
    """
    fields: list[object] = [
        record.twai_tx_gpio,
        record.twai_rx_gpio,
        record.hold_low_mask,
        record.hold_high_mask,
    ]
    if record.i2c is not None:
        fields += [record.i2c.sda_gpio, record.i2c.scl_gpio, record.i2c.addr7]
        fields += [byte for pair in record.i2c.writes for byte in pair]
    if not _all_ints(fields):
        raise InvalidProfileError(f"record fields must be integers, got {record!r}")
    if version == PROFILE_VERSION_V1:
        return _encode_v1(record)
    if version == PROFILE_VERSION_V2:
        return _encode_v2(record)
    raise InvalidProfileError(f"record version must be 1 or 2, got {version!r}")


def _encode_v1(record: ProfileRecord) -> bytes:
    if record.needs_v2:
        raise InvalidProfileError("a hold-HIGH mask or I2C safe writes need record v2")
    mask = record.hold_low_mask
    violation = _rule_violation(record.twai_tx_gpio, record.twai_rx_gpio, record.bitrate, mask)
    if violation is not None:
        raise InvalidProfileError(violation)
    body = struct.pack(
        _BODY_FORMAT,
        PROFILE_MAGIC,
        PROFILE_VERSION_V1,
        PROFILE_RECORD_SIZE,
        _U16_RESERVED,
        record.twai_tx_gpio,
        record.twai_rx_gpio,
        record.bitrate,
        _U8_RESERVED,
        mask,
    )
    return _crc_sealed(body)


def _encode_v2(record: ProfileRecord) -> bytes:
    violation = _v2_rule_violation(
        record.twai_tx_gpio,
        record.twai_rx_gpio,
        record.bitrate,
        record.hold_low_mask,
        record.hold_high_mask,
        record.i2c,
    )
    if violation is not None:
        raise InvalidProfileError(violation)
    i2c = record.i2c
    if i2c is None:
        sda = scl = addr = NO_I2C
        writes: tuple[tuple[int, int], ...] = ()
    else:
        sda, scl, addr, writes = i2c.sda_gpio, i2c.scl_gpio, i2c.addr7, i2c.writes
    pairs = bytes(byte for pair in writes for byte in pair).ljust(2 * MAX_I2C_WRITES, _PAD_BYTE)
    body = struct.pack(
        _V2_BODY_FORMAT,
        PROFILE_MAGIC,
        PROFILE_VERSION_V2,
        PROFILE_RECORD_V2_SIZE,
        _U16_RESERVED,
        record.twai_tx_gpio,
        record.twai_rx_gpio,
        record.bitrate,
        _U8_RESERVED,
        record.hold_low_mask,
        record.hold_high_mask,
        sda,
        scl,
        addr,
        len(writes),
        pairs,
        _U32_RESERVED,
    )
    return _crc_sealed(body)


def profile_record_version(data: bytes) -> ProfileVersion | None:
    """The version of the valid record at the start of ``data``, or ``None`` if invalid."""
    if parse_profile_record(data) is None:
        return None
    return PROFILE_VERSION_V2 if data[4] == PROFILE_VERSION_V2 else PROFILE_VERSION_V1


def parse_profile_record(data: bytes) -> ProfileRecord | None:
    """Decode a v1 or v2 record read from a device, or ``None`` if it is invalid or absent.

    Mirrors the updater: the record is valid only when the magic, version, length
    and CRC match and every field rule holds; the reserved fields and unused I2C
    pairs are ignored. ``None`` means the firmware uses ``DEFAULT_PROFILE_RECORD``.
    Bytes past the record are ignored.
    """
    if len(data) < PROFILE_RECORD_SIZE:
        return None
    magic, version, length, _reserved16, tx, rx, bitrate_code, _reserved8, mask = (
        struct.unpack_from(_BODY_FORMAT, data)
    )
    if magic != PROFILE_MAGIC:
        return None
    if version == PROFILE_VERSION_V2 and length == PROFILE_RECORD_V2_SIZE:
        return _parse_v2(data)
    if version != PROFILE_VERSION_V1 or length != PROFILE_RECORD_SIZE:
        return None
    (stored_crc,) = struct.unpack_from("<I", data, _BODY_SIZE)
    if zlib.crc32(data[:_BODY_SIZE]) & 0xFFFFFFFF != stored_crc:
        return None
    if _rule_violation(tx, rx, bitrate_code, mask) is not None:
        return None
    return ProfileRecord(tx, rx, Bitrate(bitrate_code), mask)


def _parse_v2(data: bytes) -> ProfileRecord | None:
    if len(data) < PROFILE_RECORD_V2_SIZE:
        return None
    (stored_crc,) = struct.unpack_from("<I", data, _V2_BODY_SIZE)
    if zlib.crc32(data[:_V2_BODY_SIZE]) & 0xFFFFFFFF != stored_crc:
        return None
    (_, _, _, _, tx, rx, bitrate_code, _, hold_low, hold_high, sda, scl, addr, count, pairs, _) = (
        struct.unpack_from(_V2_BODY_FORMAT, data)
    )
    i2c: I2cSafeWrites | None = None
    if sda == NO_I2C:
        if (scl, addr, count) != (NO_I2C, NO_I2C, 0):
            return None
    else:
        if count > MAX_I2C_WRITES:
            return None
        writes = tuple((pairs[2 * i], pairs[2 * i + 1]) for i in range(count))
        i2c = I2cSafeWrites(sda, scl, addr, writes)
    if _v2_rule_violation(tx, rx, bitrate_code, hold_low, hold_high, i2c) is not None:
        return None
    return ProfileRecord(tx, rx, Bitrate(bitrate_code), hold_low, hold_high, i2c)


def describe_profile_record(record: ProfileRecord) -> str:
    """One line naming the record's TWAI pins, bitrate, held GPIOs and I2C safe writes."""
    text = (
        f"TWAI tx GPIO{record.twai_tx_gpio} rx GPIO{record.twai_rx_gpio}, "
        f"{BITRATE_LABELS[record.bitrate]}, hold-LOW mask 0x{record.hold_low_mask:X}"
    )
    if record.hold_high_mask:
        text += f", hold-HIGH mask 0x{record.hold_high_mask:X}"
    if record.i2c is not None:
        i2c = record.i2c
        writes = " ".join(f"0x{reg:02X}=0x{value:02X}" for reg, value in i2c.writes)
        text += f", I2C SDA GPIO{i2c.sda_gpio} SCL GPIO{i2c.scl_gpio} @ 0x{i2c.addr7:02X}: {writes}"
    return text


def describe_read_profile(record: ProfileRecord | None) -> str:
    """Describe a record read from a device; ``None`` names the updater's fallback instead."""
    if record is None:
        defaults = describe_profile_record(DEFAULT_PROFILE_RECORD)
        return f"no board profile (updater defaults: {defaults})"
    return describe_profile_record(record)


def build_cancfg_image(
    config_block: bytes, record: ProfileRecord, version: ProfileVersion = 1
) -> bytes:
    """Return the 4 KB cancfg image: ``config_block`` at 0x00, the record at 0x50, then 0xFF.

    Raises:
        BoardProfileError: if ``config_block`` is not an 80-byte v2 block.
        InvalidProfileError: if the record breaks its rules or does not fit ``version``.
    """
    if len(config_block) != BLOCK_SIZE:
        raise BoardProfileError(f"config block must be {BLOCK_SIZE} bytes, got {len(config_block)}")
    image = config_block + encode_profile_record(record, version)
    return image.ljust(CANCFG_IMAGE_SIZE, _PAD_BYTE)
