"""Tests for board_profile — the shared golden vectors, record rules, decoding and SOLO presets.

Checks the v1 and v2 board-profile records and the full cancfg image of both SOLO presets
against golden vectors shared with the firmware, every encoder rejection rule of both
versions, round-trips and every rejection rule of parse_profile_record, and a frozen copy of
SOLO_PROFILES.

Covers:       encode_profile_record (v1 and v2), parse_profile_record, profile_record_version,
              describe_read_profile, build_cancfg_image (v2 block at 0x00, record at 0x50,
              0xFF to 4 KB), BoardProfile (flash size, CAN ID pair, unknown product, the
              record per version), SOLO_PROFILES.
Not covered:  which record version a bundle takes and writing the image to a board
              (tests/test_flasher.py covers the gate and the command plan).
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
    PROFILE_RECORD_V2_SIZE,
    REFUSED_GPIOS,
    SOLO_PROFILES,
    Bitrate,
    BoardProfile,
    BoardProfileError,
    I2cSafeWrites,
    InvalidProfileError,
    ProfileRecord,
    ProfileVersion,
    build_cancfg_image,
    describe_read_profile,
    encode_profile_record,
    parse_profile_record,
    profile_record_version,
)
from esp_prog2_flasher.dfu_config import DEFAULT_MANUFACTURER, parse_config_block

# Golden vectors from the SOLO one-bootloader spec, shared byte-for-byte with the firmware.
_GOLDEN_RECORDS = {
    "SOLO ODU Controller": "d1a7b0df0118ffff040501ff0000000000000000209e783a",
    "SOLO IDU Controller": "d1a7b0df0118ffff060701ff0800000000000000cba2ca5d",
}
_GOLDEN_V2_CRCS = {"SOLO ODU Controller": "02bd587b", "SOLO IDU Controller": "e12a8269"}
# Record v2 (c-cpp-esp32-v1.3.0+): ODU holds, EXV nENBL high, EMC2305 fans 1-5 off.
_GOLDEN_V2_RECORDS = {
    "SOLO ODU Controller": (
        "d1a7b0df0238ffff040501ff460006000e800100000020000000000008092c05"
        "30004000500060007000ffffffffffffffffffff89eb5aaf"
    ),
    "SOLO IDU Controller": (
        "d1a7b0df0238ffff060701ff08000000000000000000000000000000ffffff00"
        "ffffffffffffffffffffffffffffffffffffffff46c50b86"
    ),
}

_ODU_HOLD_LOW = sum(1 << gpio for gpio in (1, 2, 6, 17, 18, 33, 34, 35, 47, 48))
_EMC2305_FANS_OFF = I2cSafeWrites(
    8, 9, 0x2C, ((0x30, 0x00), (0x40, 0x00), (0x50, 0x00), (0x60, 0x00), (0x70, 0x00))
)
_FROZEN_SOLO_PROFILES = {
    "SOLO ODU Controller": BoardProfile(
        "SOLO ODU Controller",
        "node-4MB",
        ProfileRecord(4, 5, Bitrate.KBPS_250, _ODU_HOLD_LOW, 1 << 21, _EMC2305_FANS_OFF),
        v1_record=ProfileRecord(4, 5, Bitrate.KBPS_250, 0),
    ),
    "SOLO IDU Controller": BoardProfile(
        "SOLO IDU Controller", "idu-16MB", ProfileRecord(6, 7, Bitrate.KBPS_250, 1 << 3)
    ),
}

_VALID = ProfileRecord(6, 7, Bitrate.KBPS_250, 1 << 3)
_VALID_V2 = ProfileRecord(
    6, 7, Bitrate.KBPS_250, 1 << 3, 1 << 21, I2cSafeWrites(8, 9, 0x2C, ((0x30, 0x00),))
)


def _reseal(body: bytes) -> bytes:
    """Return ``body`` followed by its correct CRC."""
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


def _raw_v2(
    hold_low: int = 1 << 3,
    hold_high: int = 1 << 21,
    sda: int = 8,
    scl: int = 9,
    addr: int = 0x2C,
    count: int = 1,
    pairs: bytes = b"\x30\x00" + b"\xff" * 14,
    tx: int = 6,
    rx: int = 7,
    version: int = 2,
    length: int = 56,
    reserved16: int = 0xFFFF,
    reserved8: int = 0xFF,
    reserved32: int = 0xFFFFFFFF,
) -> bytes:
    """A v2 record built straight from the contract layout, independent of the encoder."""
    body = struct.pack(
        "<IBBHBBBBQQBBBB16sI",
        0xDFB0A7D1,
        version,
        length,
        reserved16,
        tx,
        rx,
        1,
        reserved8,
        hold_low,
        hold_high,
        sda,
        scl,
        addr,
        count,
        pairs,
        reserved32,
    )
    return _reseal(body)


@pytest.mark.parametrize("name", list(_GOLDEN_RECORDS))
def test_v1_record_matches_golden_vector(name: str) -> None:
    record = SOLO_PROFILES[name].record_for(1)
    assert encode_profile_record(record).hex() == _GOLDEN_RECORDS[name]


@pytest.mark.parametrize("name", list(_GOLDEN_V2_RECORDS))
def test_v2_record_matches_golden_vector(name: str) -> None:
    encoded = encode_profile_record(SOLO_PROFILES[name].record, 2)
    assert len(encoded) == PROFILE_RECORD_V2_SIZE
    assert encoded.hex() == _GOLDEN_V2_RECORDS[name]


def test_odu_v2_record_matches_the_contract_layout() -> None:
    expected = _raw_v2(
        tx=4,
        rx=5,
        hold_low=_ODU_HOLD_LOW,
        count=5,
        pairs=bytes.fromhex("30004000500060007000") + b"\xff" * 6,
    )
    assert encode_profile_record(SOLO_PROFILES["SOLO ODU Controller"].record, 2) == expected


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("name", list(_GOLDEN_RECORDS))
def test_cancfg_image_matches_golden_vectors(name: str, version: ProfileVersion) -> None:
    profile = SOLO_PROFILES[name]
    record = profile.record_for(version)
    image = build_cancfg_image(profile.config_block(DEFAULT_MANUFACTURER), record, version)
    golden = (_GOLDEN_RECORDS if version == 1 else _GOLDEN_V2_RECORDS)[name]
    assert len(image) == CANCFG_IMAGE_SIZE
    assert image[0x4C:0x50].hex() == _GOLDEN_V2_CRCS[name]
    record_end = PROFILE_RECORD_OFFSET + len(golden) // 2
    assert image[PROFILE_RECORD_OFFSET:record_end].hex() == golden
    assert image[record_end:] == b"\xff" * (CANCFG_IMAGE_SIZE - record_end)
    config = parse_config_block(image)
    assert config is not None
    assert (config.manufacturer, config.product) == (DEFAULT_MANUFACTURER, name)
    assert (config.boot_cmd_id, config.boot_reply_id) == profile.boot_ids


def test_solo_profiles_match_frozen_table() -> None:
    assert SOLO_PROFILES == _FROZEN_SOLO_PROFILES


def test_idu_preset_writes_the_same_record_in_both_versions() -> None:
    idu = SOLO_PROFILES["SOLO IDU Controller"]
    assert idu.record_for(1) == idu.record_for(2) == _VALID


def test_odu_preset_keeps_todays_record_for_v1() -> None:
    odu = SOLO_PROFILES["SOLO ODU Controller"]
    assert odu.record_for(1) == ProfileRecord(4, 5, Bitrate.KBPS_250, 0)
    assert odu.record_for(2) == odu.record


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


def test_profile_needing_v2_requires_a_v1_record() -> None:
    with pytest.raises(BoardProfileError, match="needs a v1 record"):
        BoardProfile("SOLO ODU Controller", "node-4MB", _VALID_V2)


def test_cancfg_image_rejects_wrong_size_block() -> None:
    with pytest.raises(BoardProfileError, match="80 bytes"):
        build_cancfg_image(b"\xff" * 76, _VALID)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("bitrate", list(Bitrate))
def test_record_round_trips(bitrate: Bitrate, version: ProfileVersion) -> None:
    record = ProfileRecord(21, 47, bitrate, (1 << 1) | (1 << 48))
    assert parse_profile_record(encode_profile_record(record, version)) == record


@pytest.mark.parametrize(
    "record",
    [
        _VALID_V2,
        ProfileRecord(4, 5, Bitrate.MBPS_1, 0, 1 << 48),
        ProfileRecord(
            4, 5, Bitrate.KBPS_125, 0, 0, I2cSafeWrites(47, 48, 0x77, ((0xFF, 0xFF),) * 8)
        ),
        ProfileRecord(4, 5, Bitrate.KBPS_500, 0, 0, I2cSafeWrites(1, 2, 0x08, ((0, 1),))),
    ],
)
def test_v2_record_round_trips(record: ProfileRecord) -> None:
    assert parse_profile_record(encode_profile_record(record, 2)) == record


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("name", list(SOLO_PROFILES))
def test_preset_records_round_trip(name: str, version: ProfileVersion) -> None:
    record = SOLO_PROFILES[name].record_for(version)
    assert parse_profile_record(encode_profile_record(record, version)) == record


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
        (ProfileRecord(4, 5, Bitrate.KBPS_250, 0, 1 << 21), "need record v2"),
        (ProfileRecord(4, 5, Bitrate.KBPS_250, 0, 0, _EMC2305_FANS_OFF), "need record v2"),
    ],
)
def test_v1_encoder_rejects_rule_violation(record: ProfileRecord, reason: str) -> None:
    with pytest.raises(InvalidProfileError, match=reason):
        encode_profile_record(record)


def _v2(
    hold_low: int = 0,
    hold_high: int = 0,
    i2c: I2cSafeWrites | None = None,
    tx: int = 4,
    rx: int = 5,
) -> ProfileRecord:
    return ProfileRecord(tx, rx, Bitrate.KBPS_250, hold_low, hold_high, i2c)


def _i2c(
    sda: int = 8, scl: int = 9, addr: int = 0x2C, writes: tuple[tuple[int, int], ...] = ((0, 0),)
) -> I2cSafeWrites:
    return I2cSafeWrites(sda, scl, addr, writes)


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (_v2(tx=49), "twai_tx_gpio must be GPIO 0..48"),
        (_v2(hold_low=1 << 5), "hold_low_mask .* holds a TWAI pin"),
        (_v2(hold_high=1 << 49), "hold_high_mask .* above 48"),
        (_v2(hold_high=-1), "hold_high_mask .* above 48"),
        (_v2(hold_high=1 << 19), "hold_high_mask .* holds a refused GPIO"),
        (_v2(hold_high=1 << 4), "hold_high_mask .* holds a TWAI pin"),
        (_v2(hold_low=1 << 21, hold_high=1 << 21), "overlap"),
        (_v2(i2c=_i2c(sda=49)), "i2c_sda_gpio must be GPIO 0..48"),
        (_v2(i2c=_i2c(sda=0xFF)), "i2c_sda_gpio must be GPIO 0..48"),
        (_v2(i2c=_i2c(scl=-1)), "i2c_scl_gpio must be GPIO 0..48"),
        (_v2(i2c=_i2c(sda=0)), "i2c_sda_gpio GPIO0 is refused"),
        (_v2(i2c=_i2c(scl=26)), "i2c_scl_gpio GPIO26 is refused"),
        (_v2(i2c=_i2c(sda=4)), "i2c_sda_gpio GPIO4 is a TWAI pin"),
        (_v2(i2c=_i2c(scl=5)), "i2c_scl_gpio GPIO5 is a TWAI pin"),
        (_v2(hold_low=1 << 8, i2c=_i2c()), "i2c_sda_gpio GPIO8 is in a hold mask"),
        (_v2(hold_high=1 << 9, i2c=_i2c()), "i2c_scl_gpio GPIO9 is in a hold mask"),
        (_v2(i2c=_i2c(sda=9)), "must differ"),
        (_v2(i2c=_i2c(addr=0x07)), "i2c_addr7 must be 0x08..0x77"),
        (_v2(i2c=_i2c(addr=0x78)), "i2c_addr7 must be 0x08..0x77"),
        (_v2(i2c=_i2c(writes=())), "i2c_write_count must be 1..8, got 0"),
        (_v2(i2c=_i2c(writes=((0, 0),) * 9)), "i2c_write_count must be 1..8, got 9"),
        (_v2(i2c=_i2c(writes=((0x100, 0),))), "two bytes"),
        (_v2(i2c=_i2c(writes=((0, -1),))), "two bytes"),
        (_v2(i2c=_i2c(writes=((0, True),))), "integers"),
        (_v2(i2c=_i2c(addr=True)), "integers"),
        (_v2(hold_high=True), "integers"),
    ],
)
def test_v2_encoder_rejects_rule_violation(record: ProfileRecord, reason: str) -> None:
    with pytest.raises(InvalidProfileError, match=reason):
        encode_profile_record(record, 2)


def test_encoder_rejects_an_unknown_version() -> None:
    with pytest.raises(InvalidProfileError, match="version must be 1 or 2"):
        encode_profile_record(_VALID, 3)  # type: ignore[arg-type]


def test_v2_without_i2c_writes_the_no_i2c_sentinels() -> None:
    encoded = encode_profile_record(_VALID, 2)
    assert encoded[0x1C:0x34] == b"\xff\xff\xff\x00" + b"\xff" * 20


@pytest.mark.parametrize("gpio", sorted(REFUSED_GPIOS))
def test_every_refused_gpio_is_rejected_as_hold(gpio: int) -> None:
    with pytest.raises(InvalidProfileError, match="refused"):
        encode_profile_record(ProfileRecord(4, 5, Bitrate.KBPS_250, 1 << gpio))
    with pytest.raises(InvalidProfileError, match="refused"):
        encode_profile_record(ProfileRecord(4, 5, Bitrate.KBPS_250, 0, 1 << gpio), 2)


def test_refused_gpios_match_spec() -> None:
    assert frozenset({0, 19, 20, 26, 27, 28, 29, 30, 31, 32}) == REFUSED_GPIOS


def test_parser_ignores_reserved_fields() -> None:
    raw = _raw_record(reserved16=0x1234, reserved8=0x00)
    assert parse_profile_record(raw) == _VALID


def test_v2_parser_ignores_reserved_fields_and_unused_pairs() -> None:
    raw = _raw_v2(reserved16=0x1234, reserved8=0, reserved32=0, pairs=b"\x30\x00" + b"\x12" * 14)
    assert parse_profile_record(raw) == _VALID_V2


def test_parser_ignores_trailing_bytes() -> None:
    assert parse_profile_record(encode_profile_record(_VALID) + b"\x00" * 8) == _VALID
    assert parse_profile_record(encode_profile_record(_VALID_V2, 2) + b"\x00" * 8) == _VALID_V2


def _bad_crc(record: ProfileRecord = _VALID, version: ProfileVersion = 1) -> bytes:
    raw = bytearray(encode_profile_record(record, version))
    raw[-1] ^= 0xFF
    return bytes(raw)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"\xff" * PROFILE_RECORD_SIZE, id="erased"),
        pytest.param(encode_profile_record(_VALID)[:-1], id="short"),
        pytest.param(_raw_record(magic=0xDF0C0FDF), id="magic"),
        pytest.param(_raw_record(version=2), id="version-2-at-v1-length"),
        pytest.param(_raw_record(version=3), id="version"),
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
        pytest.param(b"\xff" * PROFILE_RECORD_V2_SIZE, id="v2-erased"),
        pytest.param(encode_profile_record(_VALID_V2, 2)[:-1], id="v2-short"),
        pytest.param(_raw_v2(version=1), id="version-1-at-v2-length"),
        pytest.param(_raw_v2(length=24), id="v2-length"),
        pytest.param(_bad_crc(_VALID_V2, 2), id="v2-crc"),
        pytest.param(_raw_v2(tx=19), id="v2-tx-refused"),
        pytest.param(_raw_v2(hold_low=1 << 6), id="v2-hold-low-tx"),
        pytest.param(_raw_v2(hold_high=1 << 7), id="v2-hold-high-rx"),
        pytest.param(_raw_v2(hold_high=1 << 20), id="v2-hold-high-refused"),
        pytest.param(_raw_v2(hold_high=1 << 49), id="v2-hold-high-above-48"),
        pytest.param(_raw_v2(hold_low=1 << 21), id="v2-holds-overlap"),
        pytest.param(_raw_v2(sda=0xFF), id="no-i2c-with-scl"),
        pytest.param(_raw_v2(sda=0xFF, scl=0xFF), id="no-i2c-with-addr"),
        pytest.param(_raw_v2(sda=0xFF, scl=0xFF, addr=0xFF), id="no-i2c-with-writes"),
        pytest.param(_raw_v2(sda=49), id="sda-range"),
        pytest.param(_raw_v2(scl=0xFF), id="scl-sentinel-alone"),
        pytest.param(_raw_v2(sda=31), id="sda-refused"),
        pytest.param(_raw_v2(scl=6), id="scl-twai"),
        pytest.param(_raw_v2(sda=21), id="sda-held-high"),
        pytest.param(_raw_v2(scl=3), id="scl-held-low"),
        pytest.param(_raw_v2(sda=9), id="sda-equals-scl"),
        pytest.param(_raw_v2(addr=0x07), id="addr-low"),
        pytest.param(_raw_v2(addr=0x78), id="addr-high"),
        pytest.param(_raw_v2(count=0), id="no-writes"),
        pytest.param(_raw_v2(count=9), id="too-many-writes"),
    ],
)
def test_invalid_record_decodes_to_none(data: bytes) -> None:
    assert parse_profile_record(data) is None


def test_v2_without_i2c_decodes() -> None:
    raw = _raw_v2(sda=0xFF, scl=0xFF, addr=0xFF, count=0, pairs=b"\xff" * 16)
    assert parse_profile_record(raw) == ProfileRecord(6, 7, Bitrate.KBPS_250, 1 << 3, 1 << 21)


@pytest.mark.parametrize(
    ("data", "version"),
    [
        (encode_profile_record(_VALID), 1),
        (encode_profile_record(_VALID, 2), 2),
        (encode_profile_record(_VALID_V2, 2), 2),
        (b"\xff" * PROFILE_RECORD_V2_SIZE, None),
        (_bad_crc(_VALID_V2, 2), None),
    ],
)
def test_profile_record_version_names_a_valid_records_version(
    data: bytes, version: int | None
) -> None:
    assert profile_record_version(data) == version


def test_read_profile_text_names_pins_bitrate_and_mask() -> None:
    record = ProfileRecord(6, 7, Bitrate.MBPS_1, 0b101000)
    assert describe_read_profile(record) == ("TWAI tx GPIO6 rx GPIO7, 1 Mbit/s, hold-LOW mask 0x28")


def test_read_profile_text_names_v2_holds_and_i2c_writes() -> None:
    assert describe_read_profile(SOLO_PROFILES["SOLO ODU Controller"].record) == (
        "TWAI tx GPIO4 rx GPIO5, 250 kbit/s, hold-LOW mask 0x1800E00060046, "
        "hold-HIGH mask 0x200000, I2C SDA GPIO8 SCL GPIO9 @ 0x2C: "
        "0x30=0x00 0x40=0x00 0x50=0x00 0x60=0x00 0x70=0x00"
    )


def test_read_profile_text_without_a_record_names_the_updater_defaults() -> None:
    assert describe_read_profile(None) == (
        "no board profile (updater defaults: TWAI tx GPIO4 rx GPIO5, 250 kbit/s, hold-LOW mask 0x0)"
    )
