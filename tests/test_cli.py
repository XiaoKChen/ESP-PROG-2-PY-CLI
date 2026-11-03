"""Tests for cli — argument handling that decides what is written to a board.

Covers:       --version printing the package version and exiting 0; --provision-node keeping a
              SOLO preset's board profile in the cancfg image, and writing the v2 block alone
              for any other product; --flash-app resolving --app-address to 0x4000, or to 0x8000
              with --embassy, and passing --embassy through to flash_app; a refused app range
              exiting 2 with one line on stderr and no traceback; --embassy or --app-address without
              --flash-app exiting 2 before any flash or detect.
Not covered:  argument parsing as a whole; esptool runs (tests/test_flasher.py).
See also:     src/esp_prog2_flasher/cli.py, src/esp_prog2_flasher/board_profile.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pytest

from esp_prog2_flasher import __version__, cli, flasher
from esp_prog2_flasher.board_profile import SOLO_PROFILES
from esp_prog2_flasher.dfu_config import DEFAULT_MANUFACTURER, DfuConfig


def test_version_prints_the_package_version_and_exits_0(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli._build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == f"esp-prog2-flasher {__version__}\n"


def _provision(monkeypatch: pytest.MonkeyPatch, product: str) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(flasher, "provision_node", lambda *a, **kw: calls.append(kw))
    args = argparse.Namespace(
        port="COM9",
        manufacturer=DEFAULT_MANUFACTURER,
        product=product,
        boot_cmd_id=None,
        boot_reply_id=None,
    )
    cli._provision_node(args)
    assert len(calls) == 1
    return calls[0]


@pytest.mark.parametrize("product", list(SOLO_PROFILES))
def test_provision_node_keeps_the_solo_board_profile(
    monkeypatch: pytest.MonkeyPatch, product: str
) -> None:
    assert _provision(monkeypatch, product)["profile"] == SOLO_PROFILES[product]


def test_provision_node_writes_the_block_alone_for_other_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _provision(monkeypatch, "Some Other Board")["profile"] is None


def test_read_node_config_prints_the_block_and_the_board_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record = SOLO_PROFILES["SOLO ODU Controller"].record
    node = flasher.NodeConfig(DfuConfig(DEFAULT_MANUFACTURER, "SOLO ODU Controller", 5, 6), record)
    monkeypatch.setattr(flasher, "read_node_config", lambda *_a, **_k: node)
    cli._read_node_config("COM9")
    out = capsys.readouterr().out
    assert "Product:      SOLO ODU Controller" in out
    assert (
        "Board profile: TWAI tx GPIO4 rx GPIO5, 250 kbit/s, hold-LOW mask 0x1800E00060046, "
        "hold-HIGH mask 0x200000, I2C SDA GPIO8 SCL GPIO9 @ 0x2C: 0x30=0x00 0x40=0x00 "
        "0x50=0x00 0x60=0x00 0x70=0x00"
    ) in out


def test_read_node_config_says_when_there_is_no_board_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        flasher, "read_node_config", lambda *_a, **_k: flasher.NodeConfig(None, None)
    )
    cli._read_node_config("COM9")
    out = capsys.readouterr().out
    assert "unprovisioned/invalid" in out
    assert "Board profile: no board profile (updater defaults: TWAI tx GPIO4" in out


@pytest.mark.parametrize(
    ("argv", "base_address", "embassy"),
    [
        ([], flasher.APP_BASE_ADDRESS, False),
        (["--embassy"], flasher.EMBASSY_APP_BASE_ADDRESS, True),
        (["--app-address", "0x9000"], 0x9000, False),
        (["--embassy", "--app-address", "0x9000"], 0x9000, True),
    ],
    ids=["default", "embassy", "explicit", "embassy-explicit"],
)
def test_flash_app_resolves_the_app_address_and_passes_embassy_through(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
    base_address: int,
    embassy: bool,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(flasher, "flash_app", lambda *_a, **kw: calls.append(kw))
    image = tmp_path / "app.bin"
    monkeypatch.setattr(sys, "argv", ["esp-prog2-flasher", "--flash-app", str(image), *argv])
    cli.main()
    assert len(calls) == 1
    assert calls[0]["base_address"] == base_address
    assert calls[0]["embassy"] is embassy


@pytest.mark.parametrize(
    ("argv", "flag"),
    [
        (["--flash", "--embassy"], "--embassy"),
        (["--detect", "--app-address", "0x8000"], "--app-address"),
    ],
    ids=["flash-embassy", "detect-app-address"],
)
def test_app_flash_flags_without_flash_app_exit_2_before_touching_hardware(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    flag: str,
) -> None:
    def fail(*_a: object, **_k: object) -> None:
        raise AssertionError("hardware call reached")

    monkeypatch.setattr(flasher, "flash", fail)
    monkeypatch.setattr(flasher, "detect", fail)
    monkeypatch.setattr(sys, "argv", ["esp-prog2-flasher", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert capsys.readouterr().err == f"{flag} only applies with --flash-app.\n"


def test_flash_app_refusal_exits_2_with_one_line_and_no_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image = tmp_path / "app.bin"
    image.write_bytes(b"\x00\x01")
    monkeypatch.setattr(
        sys, "argv", ["esp-prog2-flasher", "--flash-app", str(image), "--app-address", "0x0"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("Application flash refused: Refusing app address 0x0")
    assert "Traceback" not in err
