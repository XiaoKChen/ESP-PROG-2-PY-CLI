"""Tests for the ESP-Prog-2 port rule — only the running bridge is ever auto-detected.

Feeds fake serial ports to flasher's classification, to the CLI's --flash-probe port choice
and to the TUI's probe-flash wizard, so no serial port is opened.

Covers:       find_probe_serial / find_download_mode_candidates on bridge, 303A:1001 and foreign
              ports; --flash-probe exiting 2 with the candidates listed when only 1001 ports
              exist; the probe wizard always asking for the port and preselecting none.
Not covered:  the esptool run itself (tests/test_flasher.py) and the --list print layout.
See also:     src/esp_prog2_flasher/flasher.py, src/esp_prog2_flasher/cli.py,
              src/esp_prog2_flasher/tui/flows.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from esp_prog2_flasher import cli, flasher
from esp_prog2_flasher.tui import flows
from esp_prog2_flasher.tui.wizard import ChoiceStep

BRIDGE = flasher.SerialPortInfo("COM3", "bridge", flasher.ESP_PROG_VID, flasher.ESP_PROG_PID)
DOWNLOAD = flasher.SerialPortInfo(
    "COM9", "usb jtag", flasher.ESP_PROG_VID, flasher.ESP32S3_USB_JTAG_PID
)
OTHER = flasher.SerialPortInfo("COM1", "modem", 0x0403, 0x6001)


def _ports(monkeypatch: pytest.MonkeyPatch, *ports: flasher.SerialPortInfo) -> None:
    monkeypatch.setattr(flasher, "list_serial_ports", lambda: list(ports))


def test_only_the_bridge_is_auto_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    _ports(monkeypatch, OTHER, DOWNLOAD, BRIDGE)
    found = flasher.find_probe_serial()
    assert found is not None and found.device == BRIDGE.device


def test_a_download_mode_port_is_never_auto_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    _ports(monkeypatch, OTHER, DOWNLOAD)
    assert flasher.find_probe_serial() is None
    assert flasher.find_download_mode_candidates() == [DOWNLOAD]


def test_flash_probe_refuses_and_lists_the_candidates(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _ports(monkeypatch, DOWNLOAD)
    with pytest.raises(SystemExit) as exit_info:
        cli._probe_port_or_exit()
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert DOWNLOAD.device in err and "--port" in err


def test_flash_probe_uses_the_bridge_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _ports(monkeypatch, DOWNLOAD, BRIDGE)
    assert cli._probe_port_or_exit() == BRIDGE.device


def test_flash_probe_without_any_port_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    _ports(monkeypatch, OTHER)
    with pytest.raises(SystemExit) as exit_info:
        cli._probe_port_or_exit()
    assert exit_info.value.code == 2


def test_probe_wizard_asks_for_the_port_and_preselects_none() -> None:
    settings = flows.Settings(solo_port=DOWNLOAD.device)
    flow = flows.probe_flash_flow(settings, Path("esp-prog2.bin"), [BRIDGE, DOWNLOAD])
    step = flow.spec.steps({})[0]
    assert isinstance(step, ChoiceStep) and step.key == "port"
    assert step.default == ""
    assert [choice.value for choice in step.choices[:2]] == [DOWNLOAD.device, BRIDGE.device]


@pytest.mark.parametrize(
    ("port", "download_mode"), [(DOWNLOAD.device, True), (BRIDGE.device, False)]
)
def test_probe_wizard_answer_becomes_the_job_port(port: str, download_mode: bool) -> None:
    settings = flows.Settings()
    flow = flows.probe_flash_flow(settings, Path("esp-prog2.bin"), [BRIDGE, DOWNLOAD])
    updated, job = flow.resolve({"port": port})
    assert updated == settings
    assert job == flows.ProbeFlashJob(Path("esp-prog2.bin"), port, download_mode)
