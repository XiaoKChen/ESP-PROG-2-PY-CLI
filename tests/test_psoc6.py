"""Tests for PSoC 6 hex segment filtering and default-hex resolution."""

from __future__ import annotations

import pytest
from intelhex import IntelHex

from esp_prog2_flasher import flasher
from esp_prog2_flasher.flasher import FlasherError, psoc6_flash_segments


def _image(*regions: tuple[int, bytes]) -> IntelHex:
    image = IntelHex()
    for address, data in regions:
        image.puts(address, data)
    return image


def test_keeps_main_flash_segments() -> None:
    image = _image((0x10000000, b"\x01\x02"), (0x10010000, b"\x03\x04\x05"))

    assert psoc6_flash_segments(image) == [
        (0x10000000, b"\x01\x02"),
        (0x10010000, b"\x03\x04\x05"),
    ]


def test_drops_cypress_metadata_sections() -> None:
    image = _image(
        (0x10000000, b"\xaa"),
        (0x90300000, b"\x12\x34"),
        (0x90500000, bytes(12)),
    )

    assert psoc6_flash_segments(image) == [(0x10000000, b"\xaa")]


@pytest.mark.parametrize("address", [0x16000000, 0x0, 0x10080000])
def test_rejects_data_outside_main_flash(address: int) -> None:
    image = _image((0x10000000, b"\xaa"), (address, b"\xbb"))

    with pytest.raises(FlasherError, match="outside PSoC 6 main flash"):
        psoc6_flash_segments(image)


def test_rejects_segment_straddling_flash_end() -> None:
    image = _image((0x1007FFFF, b"\xaa\xbb"))

    with pytest.raises(FlasherError, match="outside PSoC 6 main flash"):
        psoc6_flash_segments(image)


def test_rejects_image_with_only_metadata() -> None:
    with pytest.raises(FlasherError, match="no data"):
        psoc6_flash_segments(_image((0x90300000, b"\x12\x34")))


def test_default_hex_resolves_to_bundled_image() -> None:
    path = flasher.find_default_psoc6_hex()

    assert path is not None
    assert path.name == "psoc6_radar_full_image.hex"


def test_bundled_image_keeps_only_main_flash_segments() -> None:
    path = flasher.find_default_psoc6_hex()
    assert path is not None

    segments = psoc6_flash_segments(IntelHex(str(path)))

    assert [(start, start + len(data)) for start, data in segments] == [
        (0x10000000, 0x10006600),
        (0x10010000, 0x10044200),
        (0x1007F800, 0x1007FC00),
    ]
