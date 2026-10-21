"""Tests for cli — argument handling that decides what is written to a board.

Covers:       --provision-node keeping a SOLO preset's board profile in the cancfg image, and
              writing the v2 block alone for any other product.
Not covered:  argument parsing as a whole; esptool runs (tests/test_flasher.py).
See also:     src/esp_prog2_flasher/cli.py, src/esp_prog2_flasher/board_profile.py
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from esp_prog2_flasher import cli, flasher
from esp_prog2_flasher.board_profile import SOLO_PROFILES
from esp_prog2_flasher.dfu_config import DEFAULT_MANUFACTURER, DfuConfig


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
    assert "Board profile: TWAI tx GPIO4 rx GPIO5, 250 kbit/s, hold-LOW mask 0x0" in out


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
