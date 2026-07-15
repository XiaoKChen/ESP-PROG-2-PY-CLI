"""Tests for dfu_config — golden vectors, layout invariants, and ASCII validation."""

from __future__ import annotations

import struct
import zlib

import pytest

from esp_prog2_flasher.dfu_config import (
    BLOCK_SIZE,
    CONFIG_MAGIC,
    CONFIG_VERSION,
    DEFAULT_MANUFACTURER,
    DEVICE_NAMES,
    MAX_STRING_LEN,
    InvalidNameError,
    build_config_block,
)


def _expected_block(manufacturer: bytes, product: bytes) -> bytes:
    """Rebuild the expected 76-byte block independently of the module under test."""
    body = struct.pack(
        "<IBBBB", 0xDF0C0FDF, 1, len(manufacturer), len(product), 0xFF
    )
    body += manufacturer + b"\xff" * (32 - len(manufacturer))
    body += product + b"\xff" * (32 - len(product))
    assert len(body) == 72
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body + struct.pack("<I", crc)


def test_golden_vector_acme_widget() -> None:
    # Hand-computed golden vector (CRC 0x1EC9D08F over the first 72 bytes).
    expected = bytes.fromhex(
        "df0f0cdf010408ff"
        "41636d65" + "ff" * 28  # "Acme" + pad
        + "5769646765742d31" + "ff" * 24  # "Widget-1" + pad
        + "8fd0c91e"  # crc32 little-endian
    )
    assert len(expected) == BLOCK_SIZE
    assert build_config_block("Acme", "Widget-1") == expected


def test_empty_names_produce_valid_block() -> None:
    block = build_config_block("", "")
    assert block == _expected_block(b"", b"")
    assert block[5] == 0  # mfr_len
    assert block[6] == 0  # prod_len


def test_matches_independent_encoding() -> None:
    block = build_config_block("Normal Lab", "RA4M1 DFU")
    assert block == _expected_block(b"Normal Lab", b"RA4M1 DFU")


def test_layout_invariants() -> None:
    block = build_config_block("Acme", "Widget-1")
    assert len(block) == BLOCK_SIZE
    magic, version = struct.unpack_from("<IB", block, 0)
    assert magic == CONFIG_MAGIC
    assert version == CONFIG_VERSION
    assert block[7] == 0xFF  # reserved
    # CRC field matches a fresh CRC over the leading 72 bytes.
    stored_crc = struct.unpack_from("<I", block, 72)[0]
    assert stored_crc == (zlib.crc32(block[:72]) & 0xFFFFFFFF)


def test_max_length_names_are_accepted() -> None:
    name = "A" * MAX_STRING_LEN
    block = build_config_block(name, name)
    assert block[5] == MAX_STRING_LEN
    assert block[6] == MAX_STRING_LEN
    assert block == _expected_block(name.encode(), name.encode())


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


def test_default_manufacturer_encodes() -> None:
    build_config_block(DEFAULT_MANUFACTURER, DEVICE_NAMES[0])  # must not raise


def test_device_names_are_unique_and_encode() -> None:
    assert len(DEVICE_NAMES) == len(set(DEVICE_NAMES)), "device names must be unique"
    for name in DEVICE_NAMES:
        block = build_config_block(DEFAULT_MANUFACTURER, name)
        assert len(block) == BLOCK_SIZE
