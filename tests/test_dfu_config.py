"""Tests for dfu_config — golden vectors, layout invariants, validation, and decoding.

Checks build_config_block against hand-computed golden vectors and an independent re-encoding,
plus the name and CAN ID validation rules and the frozen device-to-CAN-ID table, and
round-trips blocks through parse_config_block.

Covers:       the 80-byte block layout and CRC, name and CAN ID validation errors, unset-ID
              encoding, the DEVICE_CAN_IDS / DEVICE_NAMES / boot_ids_for_device contract,
              parse_config_block (round-trips, v1 acceptance, bad magic/version/length/CRC,
              erased flash, per-ID fallback for out-of-range IDs).
Not covered:  programming the block to hardware and the data-flash read-back (flasher.py).
See also:     src/esp_prog2_flasher/dfu_config.py
"""

from __future__ import annotations

import struct
import zlib

import pytest

from esp_prog2_flasher.dfu_config import (
    BLOCK_SIZE,
    CONFIG_MAGIC,
    CONFIG_VERSION,
    DEFAULT_BOOT_CMD_ID,
    DEFAULT_BOOT_REPLY_ID,
    DEFAULT_MANUFACTURER,
    DEVICE_CAN_IDS,
    DEVICE_NAMES,
    MAX_CAN_ID,
    MAX_STRING_LEN,
    UNSET_CAN_ID,
    DfuConfig,
    InvalidCanIdError,
    InvalidNameError,
    boot_ids_for_device,
    build_config_block,
    parse_config_block,
)


def _expected_block(
    manufacturer: bytes,
    product: bytes,
    boot_cmd_id: int = UNSET_CAN_ID,
    boot_reply_id: int = UNSET_CAN_ID,
) -> bytes:
    """Rebuild the expected 80-byte v2 block independently of the module."""
    body = struct.pack("<IBBBB", 0xDF0C0FDF, 2, len(manufacturer), len(product), 0xFF)
    body += manufacturer + b"\xff" * (32 - len(manufacturer))
    body += product + b"\xff" * (32 - len(product))
    body += struct.pack("<HH", boot_cmd_id, boot_reply_id)
    assert len(body) == 76
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body + struct.pack("<I", crc)


def test_golden_vector_with_ids() -> None:
    # Hand-computed golden vector (CRC 0x529C5DBF over the first 76 bytes).
    expected = bytes.fromhex(
        "df0f0cdf020408ff"
        + ("41636d65" + "ff" * 28)  # "Acme" + pad
        + ("5769646765742d31" + "ff" * 24)  # "Widget-1" + pad
        + "00070107"  # boot_cmd_id=0x0700, boot_reply_id=0x0701 (LE u16 each)
        + "bf5d9c52"  # crc32 little-endian
    )
    assert len(expected) == BLOCK_SIZE
    assert build_config_block("Acme", "Widget-1", 0x700, 0x701) == expected


def test_golden_vector_ids_unset() -> None:
    # Hand-computed golden vector (CRC 0x0E17CF3B over the first 76 bytes).
    expected = bytes.fromhex(
        "df0f0cdf020408ff"
        + ("41636d65" + "ff" * 28)  # "Acme" + pad
        + ("5769646765742d31" + "ff" * 24)  # "Widget-1" + pad
        + "ffffffff"  # boot_cmd_id=0xFFFF, boot_reply_id=0xFFFF (unset)
        + "3bcf170e"  # crc32 little-endian
    )
    assert len(expected) == BLOCK_SIZE
    # Omitting the IDs must produce the unset (0xFFFF/0xFFFF) block.
    assert build_config_block("Acme", "Widget-1") == expected


def test_empty_names_produce_valid_block() -> None:
    block = build_config_block("", "")
    assert block == _expected_block(b"", b"")
    assert block[5] == 0  # mfr_len
    assert block[6] == 0  # prod_len


def test_matches_independent_encoding() -> None:
    block = build_config_block("Normal Lab", "RA4M1 DFU", 0x123, 0x456)
    assert block == _expected_block(b"Normal Lab", b"RA4M1 DFU", 0x123, 0x456)


def test_layout_invariants() -> None:
    block = build_config_block("Acme", "Widget-1", 0x700, 0x701)
    assert len(block) == BLOCK_SIZE == 80
    magic, version = struct.unpack_from("<IB", block, 0)
    assert magic == CONFIG_MAGIC
    assert version == CONFIG_VERSION == 2
    assert block[7] == 0xFF  # reserved
    cmd_id, reply_id = struct.unpack_from("<HH", block, 0x48)
    assert (cmd_id, reply_id) == (0x700, 0x701)
    # CRC field matches a fresh CRC over the leading 76 bytes.
    stored_crc = struct.unpack_from("<I", block, 0x4C)[0]
    assert stored_crc == (zlib.crc32(block[:76]) & 0xFFFFFFFF)


def test_max_length_names_are_accepted() -> None:
    name = "A" * MAX_STRING_LEN
    block = build_config_block(name, name)
    assert block[5] == MAX_STRING_LEN
    assert block[6] == MAX_STRING_LEN
    assert block == _expected_block(name.encode(), name.encode())


def test_boundary_can_ids_are_accepted() -> None:
    block = build_config_block("ok", "ok", 0x000, MAX_CAN_ID)
    cmd_id, reply_id = struct.unpack_from("<HH", block, 0x48)
    assert (cmd_id, reply_id) == (0x000, MAX_CAN_ID)


@pytest.mark.parametrize("field", ["manufacturer", "product"])
def test_rejects_too_long_name(field: str) -> None:
    long_name = "x" * (MAX_STRING_LEN + 1)
    args = {"manufacturer": "ok", "product": "ok", field: long_name}
    with pytest.raises(InvalidNameError, match=field):
        build_config_block(args["manufacturer"], args["product"])


@pytest.mark.parametrize("bad", ["café", "tab\tname", "new\nline", "nul\x00", "ctrl\x7f"])
def test_rejects_non_ascii_or_nonprintable(bad: str) -> None:
    with pytest.raises(InvalidNameError):
        build_config_block(bad, "ok")
    with pytest.raises(InvalidNameError):
        build_config_block("ok", bad)


@pytest.mark.parametrize("bad_id", [MAX_CAN_ID + 1, 0x800, 0x1000, -1])
def test_rejects_out_of_range_can_id(bad_id: int) -> None:
    with pytest.raises(InvalidCanIdError):
        build_config_block("ok", "ok", bad_id, 0x100)
    with pytest.raises(InvalidCanIdError):
        build_config_block("ok", "ok", 0x100, bad_id)


def test_rejects_half_specified_pair() -> None:
    with pytest.raises(InvalidCanIdError, match="together"):
        build_config_block("ok", "ok", 0x100, None)
    with pytest.raises(InvalidCanIdError, match="together"):
        build_config_block("ok", "ok", None, 0x100)


def test_rejects_bool_can_id() -> None:
    with pytest.raises(InvalidCanIdError):
        build_config_block("ok", "ok", True, 0x100)  # type: ignore[arg-type]


def test_default_manufacturer_encodes() -> None:
    build_config_block(DEFAULT_MANUFACTURER, DEVICE_NAMES[0])  # must not raise


def test_device_names_are_unique_and_encode() -> None:
    assert len(DEVICE_NAMES) == len(set(DEVICE_NAMES)), "device names must be unique"
    for name in DEVICE_NAMES:
        block = build_config_block(DEFAULT_MANUFACTURER, name)
        assert len(block) == BLOCK_SIZE


# The frozen board->CAN-ID table (boot_cmd_id = board.rx, boot_reply_id = board.tx).
_FROZEN_DEVICE_CAN_IDS = {
    "ODU Air Sensor": (0x01, 0x02),
    "ODU Superheat": (0x03, 0x04),
    "ODU Controller": (0x05, 0x06),
    "ODU Power Board": (0x07, 0x08),
    "IDU Radar": (0x01, 0x02),
    "IDU Air Sensor": (0x03, 0x04),
    "IDU Articulation": (0x05, 0x06),
    "IDU Controller": (0x07, 0x08),
    "IDU Power Board": (0x09, 0x0A),
}


def test_device_names_match_can_id_keys() -> None:
    # DEVICE_NAMES is derived from DEVICE_CAN_IDS — every name has exactly one pair.
    assert set(DEVICE_NAMES) == set(DEVICE_CAN_IDS)
    assert tuple(DEVICE_CAN_IDS) == DEVICE_NAMES


def test_device_can_ids_match_frozen_table() -> None:
    assert DEVICE_CAN_IDS == _FROZEN_DEVICE_CAN_IDS


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("IDU Controller", (0x07, 0x08)),
        ("IDU Power Board", (0x09, 0x0A)),
        ("ODU Air Sensor", (0x01, 0x02)),
        ("ODU Power Board", (0x07, 0x08)),
    ],
)
def test_boot_ids_for_known_device(name: str, expected: tuple[int, int]) -> None:
    assert boot_ids_for_device(name) == expected


@pytest.mark.parametrize("name", ["Custom Gadget", "unknown", "", "ODU controller"])
def test_boot_ids_for_custom_device_is_none(name: str) -> None:
    assert boot_ids_for_device(name) is None


def test_known_device_ids_round_trip_in_block() -> None:
    cmd_id, reply_id = DEVICE_CAN_IDS["IDU Power Board"]
    block = build_config_block(DEFAULT_MANUFACTURER, "IDU Power Board", cmd_id, reply_id)
    assert len(block) == BLOCK_SIZE
    got_cmd, got_reply = struct.unpack_from("<HH", block, 0x48)
    assert (got_cmd, got_reply) == (cmd_id, reply_id) == (0x09, 0x0A)


def test_every_device_pair_round_trips() -> None:
    for name, (cmd_id, reply_id) in DEVICE_CAN_IDS.items():
        block = build_config_block(DEFAULT_MANUFACTURER, name, cmd_id, reply_id)
        assert struct.unpack_from("<HH", block, 0x48) == (cmd_id, reply_id)


# --- parse_config_block ------------------------------------------------------


def _reseal(body: bytes) -> bytes:
    """Append a correct CRC32 so a crafted body fails only on the field under test."""
    return body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)


def _v2_body(version: int = 2, mfr_len: int = 4, prod_len: int = 8) -> bytes:
    """A 76-byte v2 body with arbitrary header fields, CAN IDs 0x700/0x711."""
    body = struct.pack("<IBBBB", 0xDF0C0FDF, version, mfr_len, prod_len, 0xFF)
    body += b"Acme" + b"\xff" * 28 + b"Widget-1" + b"\xff" * 24
    return body + struct.pack("<HH", 0x700, 0x711)


def _flip_byte(data: bytes, index: int) -> bytes:
    return data[:index] + bytes([data[index] ^ 0x01]) + data[index + 1 :]


_GOOD_BLOCK = build_config_block("Acme", "Widget-1", 0x700, 0x711)


@pytest.mark.parametrize(("name", "ids"), list(DEVICE_CAN_IDS.items()))
def test_known_device_block_round_trips(name: str, ids: tuple[int, int]) -> None:
    block = build_config_block(DEFAULT_MANUFACTURER, name, *ids)
    assert parse_config_block(block) == DfuConfig(DEFAULT_MANUFACTURER, name, *ids)


@pytest.mark.parametrize(
    ("manufacturer", "product", "cmd_id", "reply_id"),
    [
        ("Acme", "Widget-1", 0x700, 0x711),
        ("", "", 0x000, MAX_CAN_ID),
        ("M" * MAX_STRING_LEN, "P" * MAX_STRING_LEN, 0x123, 0x456),
    ],
)
def test_custom_block_round_trips(
    manufacturer: str, product: str, cmd_id: int, reply_id: int
) -> None:
    block = build_config_block(manufacturer, product, cmd_id, reply_id)
    assert parse_config_block(block) == DfuConfig(manufacturer, product, cmd_id, reply_id)


def test_unset_ids_decode_to_firmware_defaults() -> None:
    block = build_config_block("Acme", "Widget-1")
    assert parse_config_block(block) == DfuConfig(
        "Acme", "Widget-1", DEFAULT_BOOT_CMD_ID, DEFAULT_BOOT_REPLY_ID
    )


def test_bytes_past_the_block_are_ignored() -> None:
    expected = DfuConfig("Acme", "Widget-1", 0x700, 0x711)
    assert parse_config_block(_GOOD_BLOCK + b"\xff" * 16) == expected


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"\xff" * BLOCK_SIZE, id="erased-flash"),
        pytest.param(b"\x00" * BLOCK_SIZE, id="all-zero"),
        pytest.param(_flip_byte(_GOOD_BLOCK, 0), id="bad-magic"),
        pytest.param(_flip_byte(_GOOD_BLOCK, BLOCK_SIZE - 1), id="bad-crc"),
        pytest.param(_flip_byte(_GOOD_BLOCK, 0x10), id="corrupt-name"),
        pytest.param(_flip_byte(_GOOD_BLOCK, 0x48), id="corrupt-can-id"),
        pytest.param(_reseal(_v2_body(version=0)), id="version-0"),
        pytest.param(_reseal(_v2_body(version=3)), id="version-3"),
        pytest.param(_reseal(_v2_body(version=0xFF)), id="version-ff"),
        pytest.param(_reseal(_v2_body(mfr_len=MAX_STRING_LEN + 1)), id="mfr-len-33"),
        pytest.param(_reseal(_v2_body(prod_len=MAX_STRING_LEN + 1)), id="prod-len-33"),
        pytest.param(_GOOD_BLOCK[:-1], id="short-read"),
        pytest.param(b"", id="empty"),
    ],
)
def test_invalid_block_decodes_to_none(data: bytes) -> None:
    assert parse_config_block(data) is None


@pytest.mark.parametrize(
    ("cmd_id", "reply_id", "expected"),
    [
        (0x800, 0x123, (DEFAULT_BOOT_CMD_ID, 0x123)),
        (0x123, 0xFFFF, (0x123, DEFAULT_BOOT_REPLY_ID)),
        (0xFFFF, 0x7FF, (DEFAULT_BOOT_CMD_ID, 0x7FF)),
        (0x8000, 0x0800, (DEFAULT_BOOT_CMD_ID, DEFAULT_BOOT_REPLY_ID)),
    ],
)
def test_out_of_range_id_falls_back_per_id(
    cmd_id: int, reply_id: int, expected: tuple[int, int]
) -> None:
    # Mirrors dfu_config_can_ids: each ID falls back to its default independently,
    # while the names of the CRC-valid block are still used.
    body = _v2_body()[:0x48] + struct.pack("<HH", cmd_id, reply_id)
    config = parse_config_block(_reseal(body))
    assert config is not None
    assert (config.boot_cmd_id, config.boot_reply_id) == expected
    assert (config.manufacturer, config.product) == ("Acme", "Widget-1")


def test_v1_block_keeps_names_and_uses_default_ids() -> None:
    # The firmware still accepts the legacy v1 block (CRC over 72 bytes, stored at 0x48).
    v1 = _reseal(_v2_body(version=1)[:0x48]) + b"\xff" * 4
    assert len(v1) == BLOCK_SIZE
    assert parse_config_block(v1) == DfuConfig(
        "Acme", "Widget-1", DEFAULT_BOOT_CMD_ID, DEFAULT_BOOT_REPLY_ID
    )
