"""Tests for board_profile — the shared golden vectors, record rules, decoding and SOLO presets.

Checks the board-profile record and the full cancfg image of both SOLO presets against the
golden vectors the firmware commits too, every encoder rejection rule, round-trips and
every rejection rule of parse_profile_record, and a frozen copy of SOLO_PROFILES.

Covers:       encode_profile_record, parse_profile_record, build_cancfg_image (v2 block at
              0x00, record at 0x50, 0xFF to 4 KB), BoardProfile (flash size, CAN ID pair,
              unknown product), SOLO_PROFILES.
Not covered:  writing the image to a board (tests/test_flasher.py covers the command plan).
See also:     src/esp_prog2_flasher/board_profile.py, src/esp_prog2_flasher/dfu_config.py
"""

from __future__ import annotations

import struct
import zlib

import pytest

from esp_prog2_flasher.board_profile import (
    CANCFG_IMAGE_SIZE,
    PROFILE_RECORD_OFFSET,
    PROFILE_RECORD_SIZE,
    REFUSED_GPIOS,
    SOLO_PROFILES,
    Bitrate,
    BoardProfile,
    BoardProfileError,
    InvalidProfileError,
    ProfileRecord,
    build_cancfg_image,
    describe_read_profile,
    encode_profile_record,
    parse_profile_record,
)
from esp_prog2_flasher.dfu_config import DEFAULT_MANUFACTURER, parse_config_block

# Golden vectors from the SOLO one-bootloader spec, shared byte-for-byte with the firmware.
_GOLDEN_RECORDS = {
    "SOLO ODU Controller": "d1a7b0df0118ffff040501ff0000000000000000209e783a",
    "SOLO IDU Controller": "d1a7b0df0118ffff060701ff0800000000000000cba2ca5d",
}
_GOLDEN_V2_CRCS = {"SOLO ODU Controller": "02bd587b", "SOLO IDU Controller": "e12a8269"}

_FROZEN_SOLO_PROFILES = {
    "SOLO ODU Controller": BoardProfile(
        "SOLO ODU Controller", "node-4MB", ProfileRecord(4, 5, Bitrate.KBPS_250, 0)
    ),
    "SOLO IDU Controller": BoardProfile(
        "SOLO IDU Controller", "idu-16MB", ProfileRecord(6, 7, Bitrate.KBPS_250, 1 << 3)
    ),
}

_VALID = ProfileRecord(6, 7, Bitrate.KBPS_250, 1 << 3)


def _reseal(body: bytes) -> bytes:
    """Return a 24-byte record with ``body`` (20 bytes) and a correct CRC."""
    return body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)


def _raw_record(
    tx: int = 6,
    rx: int = 7,
    bitrate: int = 1,
    mask: int = 1 << 3,
    magic: int = 0xDFB0A7D1,
    version: int = 1,
    length: int = 24,
    reserved16: int = 0xFFFF,
    reserved8: int = 0xFF,
) -> bytes:
    body = struct.pack(
        "<IBBHBBBBQ", magic, version, length, reserved16, tx, rx, bitrate, reserved8, mask
    )
    return _reseal(body)


@pytest.mark.parametrize("name", list(_GOLDEN_RECORDS))
def test_record_matches_golden_vector(name: str) -> None:
    assert encode_profile_record(SOLO_PROFILES[name].record).hex() == _GOLDEN_RECORDS[name]


@pytest.mark.parametrize("name", list(_GOLDEN_RECORDS))
def test_cancfg_image_matches_golden_vectors(name: str) -> None:
    profile = SOLO_PROFILES[name]
    image = build_cancfg_image(profile.config_block(DEFAULT_MANUFACTURER), profile.record)
    assert len(image) == CANCFG_IMAGE_SIZE
    assert image[0x4C:0x50].hex() == _GOLDEN_V2_CRCS[name]
    record_end = PROFILE_RECORD_OFFSET + PROFILE_RECORD_SIZE
    assert image[PROFILE_RECORD_OFFSET:record_end].hex() == _GOLDEN_RECORDS[name]
    assert image[record_end:] == b"\xff" * (CANCFG_IMAGE_SIZE - record_end)
    config = parse_config_block(image)
    assert config is not None
    assert (config.manufacturer, config.product) == (DEFAULT_MANUFACTURER, name)
    assert (config.boot_cmd_id, config.boot_reply_id) == profile.boot_ids


def test_solo_profiles_match_frozen_table() -> None:
    assert SOLO_PROFILES == _FROZEN_SOLO_PROFILES


@pytest.mark.parametrize(
    ("name", "flash_size", "boot_ids"),
    [("SOLO ODU Controller", "4MB", (0x05, 0x06)), ("SOLO IDU Controller", "16MB", (0x07, 0x08))],
)
def test_profile_derives_flash_size_and_can_ids(
    name: str, flash_size: str, boot_ids: tuple[int, int]
) -> None:
    assert SOLO_PROFILES[name].flash_size == flash_size
    assert SOLO_PROFILES[name].boot_ids == boot_ids


def test_profile_rejects_unknown_product() -> None:
    with pytest.raises(BoardProfileError, match="not a known device"):
        BoardProfile("SOLO Mystery Board", "node-4MB", _VALID)


def test_cancfg_image_rejects_wrong_size_block() -> None:
    with pytest.raises(BoardProfileError, match="80 bytes"):
        build_cancfg_image(b"\xff" * 76, _VALID)


@pytest.mark.parametrize("bitrate", list(Bitrate))
def test_record_round_trips(bitrate: Bitrate) -> None:
    record = ProfileRecord(21, 47, bitrate, (1 << 1) | (1 << 48))
    assert parse_profile_record(encode_profile_record(record)) == record


@pytest.mark.parametrize("name", list(SOLO_PROFILES))
def test_preset_records_round_trip(name: str) -> None:
    record = SOLO_PROFILES[name].record
    assert parse_profile_record(encode_profile_record(record)) == record


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (ProfileRecord(49, 5, Bitrate.KBPS_250, 0), "twai_tx_gpio must be GPIO 0..48"),
        (ProfileRecord(4, -1, Bitrate.KBPS_250, 0), "twai_rx_gpio must be GPIO 0..48"),
        (ProfileRecord(0, 5, Bitrate.KBPS_250, 0), "twai_tx_gpio GPIO0 is refused"),
        (ProfileRecord(4, 20, Bitrate.KBPS_250, 0), "twai_rx_gpio GPIO20 is refused"),
        (ProfileRecord(4, 4, Bitrate.KBPS_250, 0), "must differ"),
        (ProfileRecord(4, 5, 4, 0), "bitrate code"),  # type: ignore[arg-type]
        (ProfileRecord(4, 5, Bitrate.KBPS_250, 1 << 26), "holds a refused GPIO"),
        (ProfileRecord(4, 5, Bitrate.KBPS_250, 1 << 5), "holds a TWAI pin"),
        (ProfileRecord(4, 5, Bitrate.KBPS_250, 1 << 49), "above 48"),
        (ProfileRecord(4, 5, Bitrate.KBPS_250, -1), "above 48"),
        (ProfileRecord(True, 5, Bitrate.KBPS_250, 0), "integers"),
    ],
)
def test_encoder_rejects_rule_violation(record: ProfileRecord, reason: str) -> None:
    with pytest.raises(InvalidProfileError, match=reason):
        encode_profile_record(record)


@pytest.mark.parametrize("gpio", sorted(REFUSED_GPIOS))
def test_every_refused_gpio_is_rejected_as_hold(gpio: int) -> None:
    with pytest.raises(InvalidProfileError, match="refused"):
        encode_profile_record(ProfileRecord(4, 5, Bitrate.KBPS_250, 1 << gpio))


def test_refused_gpios_match_spec() -> None:
    assert frozenset({0, 19, 20, 26, 27, 28, 29, 30, 31, 32}) == REFUSED_GPIOS


def test_parser_ignores_reserved_fields() -> None:
    raw = _raw_record(reserved16=0x1234, reserved8=0x00)
    assert parse_profile_record(raw) == _VALID


def test_parser_ignores_trailing_bytes() -> None:
    assert parse_profile_record(encode_profile_record(_VALID) + b"\x00" * 8) == _VALID


def _bad_crc() -> bytes:
    raw = bytearray(encode_profile_record(_VALID))
    raw[-1] ^= 0xFF
    return bytes(raw)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"\xff" * PROFILE_RECORD_SIZE, id="erased"),
        pytest.param(encode_profile_record(_VALID)[:-1], id="short"),
        pytest.param(_raw_record(magic=0xDF0C0FDF), id="magic"),
        pytest.param(_raw_record(version=2), id="version"),
        pytest.param(_raw_record(length=28), id="length"),
        pytest.param(_bad_crc(), id="crc"),
        pytest.param(_raw_record(tx=49), id="tx-range"),
        pytest.param(_raw_record(rx=49), id="rx-range"),
        pytest.param(_raw_record(tx=19), id="tx-refused"),
        pytest.param(_raw_record(rx=32), id="rx-refused"),
        pytest.param(_raw_record(tx=7, rx=7, mask=0), id="tx-equals-rx"),
        pytest.param(_raw_record(bitrate=4), id="bitrate"),
        pytest.param(_raw_record(mask=1 << 0), id="hold-refused"),
        pytest.param(_raw_record(mask=1 << 6), id="hold-tx"),
        pytest.param(_raw_record(mask=1 << 7), id="hold-rx"),
        pytest.param(_raw_record(mask=1 << 49), id="hold-above-48"),
    ],
)
def test_invalid_record_decodes_to_none(data: bytes) -> None:
    assert parse_profile_record(data) is None


def test_read_profile_text_names_pins_bitrate_and_mask() -> None:
    record = ProfileRecord(6, 7, Bitrate.MBPS_1, 0b101000)
    assert describe_read_profile(record) == ("TWAI tx GPIO6 rx GPIO7, 1 Mbit/s, hold-LOW mask 0x28")


def test_read_profile_text_without_a_record_names_the_updater_defaults() -> None:
    assert describe_read_profile(None) == (
        "no board profile (updater defaults: TWAI tx GPIO4 rx GPIO5, 250 kbit/s, hold-LOW mask 0x0)"
    )
