"""Tests for app and tui/ — the dashboard TUI driven headlessly with App.run_test() and key presses.

Every hardware seam (flasher.detect/detect_psoc6, find_probe_serial, list_serial_ports and the
flash/provision/read calls) is replaced with a fake, so no probe or board is touched. Each
test runs its pilot coroutine with asyncio.run, so no pytest async plugin is needed.

Covers:       mounting at 80x24 and 120x40 with every panel and every target's details on
              screen, the details pane hidden below 80 columns; type-to-filter and esc; letters
              never acting on the dashboard; the "/" popup listing only valid commands, running
              one, and "no shortcut"; the SOLO flash wizard (n/esc cancel, y flashes like
              cli.py's --flash-solo), provision and read config like --provision-node and
              --read-node-config; the RA4M1 bootloader wizard with a known and a custom device;
              shift+tab and "/" typed into a field; the browser's filter and pick; a finished
              run's Recent row and status; the help table built from the command table; the
              busy guard; ctrl+c; the terminal theme choice.
Not covered:  the flasher calls themselves (tests/test_flasher.py, tests/test_psoc6.py), the
              Windows drive list, spinner animation and exact colours.
See also:     src/esp_prog2_flasher/app.py, src/esp_prog2_flasher/tui/
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot

from esp_prog2_flasher import flasher
from esp_prog2_flasher.app import FlasherApp
from esp_prog2_flasher.board_profile import SOLO_PROFILES
from esp_prog2_flasher.dfu_config import DEFAULT_MANUFACTURER, DEVICE_CAN_IDS, DfuConfig
from esp_prog2_flasher.flasher import DetectResult, TargetState
from esp_prog2_flasher.tui.browser import BrowserScreen
from esp_prog2_flasher.tui.commands import (
    BROWSER_COMMANDS,
    DASHBOARD_COMMANDS,
    GLOBAL_COMMANDS,
    help_rows,
)
from esp_prog2_flasher.tui.screens import CommandPopup, DashboardScreen, HelpScreen, RunScreen
from esp_prog2_flasher.tui.theme import DARK_THEME_NAME, LIGHT_THEME_NAME, theme_name_for_terminal
from esp_prog2_flasher.tui.widgets import DetailsView, FilterList, StatusLine
from esp_prog2_flasher.tui.wizard import ChoiceStep, TextStep, WizardScreen

AppTest = Callable[[FlasherApp, Pilot[None]], Awaitable[None]]
SOLO_PORT = "COM9"
BRIDGE_PORT = "COM3"
PORTS = [
    flasher.SerialPortInfo(BRIDGE_PORT, "USB Serial", flasher.ESP_PROG_VID, flasher.ESP_PROG_PID),
    flasher.SerialPortInfo(SOLO_PORT, "USB Serial", flasher.ESP_PROG_VID, 0x1001),
]
COMMAND_LETTERS = "dfepbamnrglq"


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    no_probe = DetectResult(TargetState.NO_PROBE, "no probe (test)")
    monkeypatch.setattr(flasher, "detect", lambda **_: no_probe)
    monkeypatch.setattr(flasher, "detect_psoc6", lambda **_: no_probe)
    monkeypatch.setattr(flasher, "find_probe_serial", lambda: None)
    monkeypatch.setattr(flasher, "list_serial_ports", lambda: PORTS)


def run_app(test: AppTest, size: tuple[int, int] = (80, 24), app: FlasherApp | None = None) -> None:
    async def main() -> None:
        flasher_app = app or FlasherApp()
        async with flasher_app.run_test(size=size) as pilot:
            await pilot.pause()
            await test(flasher_app, pilot)
            await flasher_app.workers.wait_for_complete()

    asyncio.run(main())


def record_calls(monkeypatch: pytest.MonkeyPatch, name: str) -> list[tuple[object, ...]]:
    """Replace ``flasher.<name>`` with a recorder of (args..., kwargs) tuples."""
    calls: list[tuple[object, ...]] = []

    def fake(*args: object, **kwargs: object) -> None:
        calls.append((*args, kwargs))

    monkeypatch.setattr(flasher, name, fake)
    return calls


def detect_ra4m1(monkeypatch: pytest.MonkeyPatch) -> None:
    connected = DetectResult(TargetState.CONNECTED_BLANK, "RA4M1 blank (test)")
    monkeypatch.setattr(flasher, "detect", lambda **_: connected)


def target_list(app: FlasherApp) -> FilterList:
    return app.screen.query_one(FilterList)


def status_text(app: FlasherApp) -> str:
    return str(app.screen.query_one(StatusLine).render())


async def type_text(pilot: Pilot[None], text: str) -> None:
    await pilot.press(*("space" if char == " " else char for char in text))


async def settle(app: FlasherApp, pilot: Pilot[None]) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()


def wizard(app: FlasherApp) -> WizardScreen:
    assert isinstance(app.screen, WizardScreen)
    return app.screen


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
def test_dashboard_mounts_with_every_details_pane_on_screen(size: tuple[int, int]) -> None:
    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        for selector in ("HeaderLine", "#targets", "#details", "StatusLine", "FooterHints"):
            region = screen.query_one(selector).region
            assert region.area > 0, selector
            assert screen.region.contains_region(region), selector
        details = screen.query_one("#details")
        target_keys = [row.key for row in target_list(app).rows if row.key.startswith("t:")]
        assert len(target_keys) == 4 + len(SOLO_PROFILES)
        for key in target_keys:
            target_list(app).select_key(key)
            screen.rebuild()
            await pilot.pause()
            content = screen.query_one(DetailsView).outer_size.height
            assert content <= details.content_size.height, key

    run_app(check, size)


def test_details_pane_hides_below_80_columns() -> None:
    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        assert not app.screen.query_one("#details").display
        assert app.screen.query_one("#targets").region.width == 79

    run_app(check, (79, 24))


def test_typing_filters_the_targets_and_escape_clears_it() -> None:
    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        all_rows = target_list(app).rows
        await type_text(pilot, "idu")
        assert target_list(app).query_text == "idu"
        assert [row.label for row in target_list(app).rows] == ["SOLO IDU Controller"]
        assert "› idu" in str(app.screen.query_one("#targets").border_title)
        await pilot.press("backspace", "backspace", "backspace", "s", "o", "l", "o")
        assert {row.label for row in target_list(app).rows} == set(SOLO_PROFILES)
        await pilot.press("escape")
        assert target_list(app).query_text == ""
        assert target_list(app).rows == all_rows

    run_app(check)


def test_letters_never_act_on_the_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    detects: list[object] = []
    monkeypatch.setattr(
        flasher,
        "detect",
        lambda **kw: detects.append(kw) or DetectResult(TargetState.NO_PROBE, "none"),
    )

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await settle(app, pilot)
        before = len(detects)
        await type_text(pilot, COMMAND_LETTERS + "?")
        await settle(app, pilot)
        assert app.is_running
        assert isinstance(app.screen, DashboardScreen)
        assert target_list(app).query_text == COMMAND_LETTERS + "?"
        assert len(detects) == before

    run_app(check)


def test_popup_lists_only_the_selection_commands_and_runs_one() -> None:
    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await type_text(pilot, "solo odu")
        await pilot.press("slash")
        popup = app.screen
        assert isinstance(popup, CommandPopup)
        keys = [command.key for command in popup.commands]
        assert keys == ["e", "p", "b", "a", "m", "n", "r", "l", "?", "q"]
        assert "toggle migrate-stock" in [c.description for c in popup.commands]
        await pilot.press("m")
        assert isinstance(app.screen, DashboardScreen)
        assert app.settings.solo_migrate
        assert "Migrate from stock on" in status_text(app)

    run_app(check)


def test_popup_on_a_group_offers_only_global_commands() -> None:
    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await pilot.press("home", "slash")
        assert isinstance(app.screen, CommandPopup)
        assert app.screen.commands == GLOBAL_COMMANDS

    run_app(check)


def test_popup_unknown_key_closes_it_with_no_shortcut() -> None:
    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await pilot.press("slash", "z")
        assert isinstance(app.screen, DashboardScreen)
        assert 'no shortcut "z"' in status_text(app)
        await pilot.press("slash", "slash")
        assert isinstance(app.screen, DashboardScreen)
        assert target_list(app).query_text == ""

    run_app(check)


async def walk_solo_wizard_to_confirm(app: FlasherApp, pilot: Pilot[None]) -> None:
    await type_text(pilot, "solo odu")
    await pilot.press("enter")
    step = wizard(app).step
    assert isinstance(step, ChoiceStep) and step.key == "port"
    assert [row.note for row in wizard(app).choice_list.rows][0].startswith("ESP-Prog-2 bridge")
    await type_text(pilot, SOLO_PORT.lower())
    await pilot.press("enter")
    assert wizard(app).step is not None and wizard(app).step.key == "bundle"
    await pilot.press("enter")
    assert wizard(app).confirm is not None


@pytest.mark.parametrize("cancel_key", ["n", "escape"])
def test_solo_wizard_cancel_never_flashes(monkeypatch: pytest.MonkeyPatch, cancel_key: str) -> None:
    calls = record_calls(monkeypatch, "flash_solo")

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await walk_solo_wizard_to_confirm(app, pilot)
        rows = dict(wizard(app).confirm.rows)  # type: ignore[union-attr]
        assert rows["Port"] == SOLO_PORT
        assert rows["Product"] == "SOLO ODU Controller"
        await pilot.press(cancel_key)
        await settle(app, pilot)
        assert isinstance(app.screen, DashboardScreen)
        assert calls == []
        assert "cancelled" in status_text(app)

    run_app(check)


def test_solo_wizard_yes_flashes_like_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_calls(monkeypatch, "flash_solo")
    fetched: list[tuple[str, Path]] = []
    bundle = Path("fetched-bundle")

    def fake_fetch(tag: str, dest: Path, **_: object) -> Path:
        fetched.append((tag, dest))
        return bundle

    monkeypatch.setattr(flasher, "fetch_solo_bundle", fake_fetch)
    profile = SOLO_PROFILES["SOLO ODU Controller"]

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await walk_solo_wizard_to_confirm(app, pilot)
        await pilot.press("y")
        await settle(app, pilot)
        assert [tag for tag, _ in fetched] == [flasher.DEFAULT_SOLO_BUNDLE_TAG]
        assert len(calls) == 1
        *args, kwargs = calls[0]
        assert args == [profile, SOLO_PORT, bundle]
        assert isinstance(kwargs, dict)
        assert kwargs["app"] is None
        assert kwargs["migrate_stock"] is False
        assert kwargs["manufacturer"] == DEFAULT_MANUFACTURER
        assert app.settings.solo_port == SOLO_PORT

    run_app(check)


def test_solo_provision_writes_the_preset_like_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_calls(monkeypatch, "provision_node")
    profile = SOLO_PROFILES["SOLO IDU Controller"]

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await type_text(pilot, "solo idu")
        await pilot.press("slash", "n")
        await type_text(pilot, SOLO_PORT.lower())
        await pilot.press("enter")
        assert dict(wizard(app).confirm.rows)["Product"] == profile.product  # type: ignore[union-attr]
        await pilot.press("y")
        await settle(app, pilot)
        assert len(calls) == 1
        *args, kwargs = calls[0]
        assert args == [SOLO_PORT, DEFAULT_MANUFACTURER, profile.product]
        assert isinstance(kwargs, dict)
        assert (kwargs["boot_cmd_id"], kwargs["boot_reply_id"]) == profile.boot_ids
        assert kwargs["profile"] is profile

    run_app(check)


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (None, "unprovisioned"),
        (DfuConfig("Normal Corporation", "SOLO ODU Controller", 0x5, 0x6), "host->boot=0x005"),
    ],
)
def test_solo_read_config_runs_without_a_confirm(
    monkeypatch: pytest.MonkeyPatch, config: DfuConfig | None, expected: str
) -> None:
    ports: list[str] = []

    def fake_read(port: str, **_: object) -> DfuConfig | None:
        ports.append(port)
        return config

    monkeypatch.setattr(flasher, "read_node_config", fake_read)

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await type_text(pilot, "solo odu")
        await pilot.press("slash", "r")
        await type_text(pilot, SOLO_PORT.lower())
        await pilot.press("enter")
        await settle(app, pilot)
        assert ports == [SOLO_PORT]
        assert isinstance(app.screen, DashboardScreen)
        assert expected in status_text(app)
        assert app.recents[0].operation == "read"

    run_app(check)


def test_bootloader_wizard_with_a_known_device_fixes_its_can_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detect_ra4m1(monkeypatch)
    calls = record_calls(monkeypatch, "flash")
    device = "IDU Radar"

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await settle(app, pilot)
        await type_text(pilot, "bootloader")
        await pilot.press("enter", "enter")  # accept the bundled image
        assert wizard(app).step is not None and wizard(app).step.key == "device"
        await type_text(pilot, device.lower())
        await pilot.press("enter", "enter")  # pick the device, keep the manufacturer
        rows = dict(wizard(app).confirm.rows)  # type: ignore[union-attr]
        assert rows["Product"] == device
        await pilot.press("y")
        await settle(app, pilot)
        assert len(calls) == 1
        hex_path, kwargs = calls[0]
        assert hex_path == flasher.find_default_hex()
        assert isinstance(kwargs, dict)
        assert kwargs["product"] == device
        assert kwargs["manufacturer"] == DEFAULT_MANUFACTURER
        assert (kwargs["boot_cmd_id"], kwargs["boot_reply_id"]) == DEVICE_CAN_IDS[device]

    run_app(check)


def test_bootloader_needs_a_detected_ra4m1() -> None:
    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await settle(app, pilot)
        await type_text(pilot, "bootloader")
        await pilot.press("enter")
        assert isinstance(app.screen, DashboardScreen)
        assert "No RA4M1 target detected" in status_text(app)

    run_app(check)


def test_custom_device_ids_are_both_or_neither_and_slash_types_literally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detect_ra4m1(monkeypatch)

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await settle(app, pilot)
        await type_text(pilot, "bootloader")
        await pilot.press("enter", "enter")
        await type_text(pilot, "custom")
        await pilot.press("enter")
        assert wizard(app).text_input.has_focus
        await type_text(pilot, "rig/7")
        assert wizard(app).text_input.value == "rig/7"
        assert isinstance(app.screen, WizardScreen)
        await pilot.press("enter")
        await type_text(pilot, "0x700")
        await pilot.press("enter", "enter")  # host->boot given, boot->host left blank
        step = wizard(app).step
        assert isinstance(step, TextStep) and step.key == "reply_id"
        assert "Set both boot CAN IDs" in str(app.screen.query_one("#step-error").render())
        await pilot.press("shift+tab")
        step = wizard(app).step
        assert isinstance(step, TextStep) and step.key == "cmd_id"
        assert wizard(app).text_input.value == "0x700"

    run_app(check)


def test_browser_filters_and_picks_a_file(tmp_path: Path) -> None:
    for name in ("alpha.hex", "beta.hex", "notes.txt"):
        (tmp_path / name).write_text(":00000001FF\n")
    (tmp_path / "zz-folder").mkdir()

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await type_text(pilot, "bootloader")
        await pilot.press("slash", "f")
        browser = app.screen
        assert isinstance(browser, BrowserScreen)
        labels = [row.label for row in browser.entry_list.rows]
        assert labels == ["zz-folder", "alpha.hex", "beta.hex"]
        await pilot.press("b")
        assert [row.label for row in browser.entry_list.rows] == ["beta.hex"]
        await pilot.press("enter")
        assert isinstance(app.screen, DashboardScreen)
        assert app.settings.boot_hex == str(tmp_path / "beta.hex")

    run_app(check, app=FlasherApp(hex_path=tmp_path / "alpha.hex"))


def test_a_finished_run_adds_a_recent_row_and_a_success_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = record_calls(monkeypatch, "flash_psoc6")

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await type_text(pilot, "psoc")
        await pilot.press("enter", "enter")
        assert "Image" in dict(wizard(app).confirm.rows)  # type: ignore[union-attr]
        await pilot.press("y")
        await settle(app, pilot)
        assert len(calls) == 1
        assert isinstance(app.screen, DashboardScreen)
        assert status_text(app).startswith("✓ PSoC 6 flashed")
        await pilot.press("escape")
        recent = [row for row in target_list(app).rows if row.key.startswith("r:")]
        assert len(recent) == 1
        assert recent[0].level == "ok" and recent[0].trail.startswith("✓")

    run_app(check)


def test_help_lists_every_command_from_the_shared_table() -> None:
    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await pilot.press("slash", "question_mark")
        assert isinstance(app.screen, HelpScreen)
        listed = {(key, action) for _, key, action in help_rows()}
        for command in (*DASHBOARD_COMMANDS, *BROWSER_COMMANDS, *GLOBAL_COMMANDS):
            assert any(
                key == f"/ {command.key}" and action.startswith(command.description)
                for key, action in listed
            ), command
        await pilot.press("escape")
        assert isinstance(app.screen, DashboardScreen)

    run_app(check)


def test_a_second_write_is_refused_while_one_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    calls: list[Path] = []

    def slow_flash(hex_path: Path, progress: Callable[[float], None], **_: object) -> None:
        calls.append(hex_path)
        release.wait(5)
        progress(0.5)  # arrives after the run view was closed

    monkeypatch.setattr(flasher, "flash_psoc6", slow_flash)

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await type_text(pilot, "psoc")
        await pilot.press("enter", "enter", "y")
        assert isinstance(app.screen, RunScreen)
        await pilot.press("escape")
        await pilot.press("enter")
        assert isinstance(app.screen, DashboardScreen)
        assert "Busy" in status_text(app)
        await pilot.press("slash", "d")
        assert "Busy" in status_text(app)
        release.set()
        await settle(app, pilot)
        assert len(calls) == 1

    run_app(check)


@pytest.mark.parametrize("in_wizard", [False, True])
def test_ctrl_c_quits_everywhere(monkeypatch: pytest.MonkeyPatch, in_wizard: bool) -> None:
    detect_ra4m1(monkeypatch)

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await settle(app, pilot)
        if in_wizard:
            await type_text(pilot, "bootloader")
            await pilot.press("enter", "enter")
            await type_text(pilot, "custom")
            await pilot.press("enter")
            assert wizard(app).text_input.has_focus
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert not app.is_running

    run_app(check)


def test_detect_on_the_psoc6_row_uses_the_psoc6_target(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_detect_psoc6(**kwargs: object) -> DetectResult:
        calls.append(kwargs)
        return DetectResult(TargetState.CONNECTED, "PSoC 6 (test)")

    monkeypatch.setattr(flasher, "detect_psoc6", fake_detect_psoc6)

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await type_text(pilot, "psoc")
        await pilot.press("slash", "d")
        await settle(app, pilot)
        assert len(calls) == 1
        assert "PSoC 6: connected" in status_text(app)

    run_app(check)


@pytest.mark.parametrize(
    ("colorfgbg", "theme"),
    [
        ("15;0", DARK_THEME_NAME),
        ("0;15", LIGHT_THEME_NAME),
        ("0;7", LIGHT_THEME_NAME),
        ("", DARK_THEME_NAME),
    ],
)
def test_theme_follows_the_terminal_background(colorfgbg: str, theme: str) -> None:
    assert theme_name_for_terminal({"COLORFGBG": colorfgbg}) == theme
