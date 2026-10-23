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
              busy guard; ctrl+c; the terminal theme choice; the saved state (restore on launch,
              saves as things change, a Recent row and recent files surviving a restart, corrupt,
              newer and vanished-path handling, the forget command and no_state).
Not covered:  the flasher calls themselves (tests/test_flasher.py, tests/test_psoc6.py), the
              Windows drive list, spinner animation and exact colours.
See also:     src/esp_prog2_flasher/app.py, src/esp_prog2_flasher/tui/
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from textual.pilot import Pilot

from esp_prog2_flasher import flasher
from esp_prog2_flasher.app import FlasherApp
from esp_prog2_flasher.board_profile import SOLO_PROFILES
from esp_prog2_flasher.dfu_config import DEFAULT_MANUFACTURER, DEVICE_CAN_IDS, DfuConfig
from esp_prog2_flasher.flasher import DetectResult, TargetState
from esp_prog2_flasher.tui import state
from esp_prog2_flasher.tui.browser import BrowserScreen
from esp_prog2_flasher.tui.commands import (
    BROWSER_COMMANDS,
    DASHBOARD_COMMANDS,
    GLOBAL_COMMANDS,
    help_rows,
)
from esp_prog2_flasher.tui.flows import Settings, target_fields
from esp_prog2_flasher.tui.screens import CommandPopup, DashboardScreen, HelpScreen, RunScreen
from esp_prog2_flasher.tui.targets import SOLO_TARGETS, RecentRun
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
COMMAND_LETTERS = "dfepbamnrglcq"


@pytest.fixture(autouse=True)
def hermetic_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the default state file into tmp_path so no test touches the user's config dir."""
    path = tmp_path / "default-config" / "state.json"
    monkeypatch.setattr(state, "default_state_path", lambda: path)
    return path


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


def _solo_bundle_settings(bundle: str, tmp_path: Path) -> Settings:
    """``folder`` is a bundle folder without a manifest, ``folder:X.Y.Z`` one at that version."""
    if bundle.startswith("folder"):
        folder = tmp_path / "esp32s3-can-updater-x"
        folder.mkdir()
        if ":" in bundle:
            manifest = {"version": bundle.partition(":")[2]}
            (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return replace(Settings(), solo_bundle_dir=str(folder))
    return replace(Settings(), solo_tag=bundle)


# Every way the ODU's rows can grow: the default release (record v1), a release and a folder
# with record v2 (holds and I2C), a folder on v1, and a tag or folder that names no version.
@pytest.mark.parametrize(
    "bundle",
    ["c-cpp-esp32-v1.2.1", "c-cpp-esp32-v1.3.0", "folder:1.3.0", "folder:1.2.1", "v9", "folder"],
)
@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
def test_dashboard_mounts_with_every_details_pane_on_screen(
    size: tuple[int, int], bundle: str, tmp_path: Path
) -> None:
    state_file = tmp_path / "state.json"
    save(state_file, settings=_solo_bundle_settings(bundle, tmp_path))

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

    run_app(check, size, FlasherApp(state_path=state_file))


_ODU_V2_ROWS = {"Holds": "10 LOW · GPIO21 HIGH", "I2C": "0x2C: 5 writes"}


@pytest.mark.parametrize(
    ("bundle", "expected"),
    [
        (
            "c-cpp-esp32-v1.2.1",
            {
                "Record": "v1 · TWAI 4/5 @ 250k",
                "Holds": "none (v2 needs ≥ 1.3.0)",
                "Bundle": "c-cpp-esp32-v1.2.1",
            },
        ),
        ("c-cpp-esp32-v1.3.0", {"Record": "v2 · TWAI 4/5 @ 250k", **_ODU_V2_ROWS}),
        ("folder:1.3.0", {"Record": "v2 · TWAI 4/5 @ 250k", **_ODU_V2_ROWS}),
        ("folder:1.2.1", {"Record": "v1 · TWAI 4/5 @ 250k"}),
        ("folder", {"Record": "v? · TWAI 4/5 @ 250k", "Note": "v2 needs bundle ≥ 1.3.0"}),
        ("v9", {"Record": "v? · TWAI 4/5 @ 250k", "Note": "v2 needs bundle ≥ 1.3.0"}),
    ],
)
def test_odu_details_show_the_record_the_bundle_writes(
    bundle: str, expected: dict[str, str], tmp_path: Path
) -> None:
    odu = next(t for t in SOLO_TARGETS if t.product == "SOLO ODU Controller")
    fields = dict(target_fields(odu, _solo_bundle_settings(bundle, tmp_path), None))
    assert {key: fields.get(key) for key in expected} == expected
    assert fields["Layout"] == "node-4MB · 0x05 / 0x06"
    if expected["Record"].startswith("v1"):
        assert "I2C" not in fields and "Note" not in fields
    if expected["Record"].startswith("v?"):
        assert "Holds" not in fields and "I2C" not in fields


def test_idu_details_keep_its_holds_when_the_bundle_cannot_say(tmp_path: Path) -> None:
    idu = next(t for t in SOLO_TARGETS if t.product == "SOLO IDU Controller")
    fields = dict(target_fields(idu, _solo_bundle_settings("folder", tmp_path), None))
    assert (fields["Record"], fields["Holds"]) == ("v? · TWAI 6/7 @ 250k", "GPIO3 LOW")
    assert "Note" not in fields


def test_idu_details_show_its_one_hold_and_no_i2c() -> None:
    idu = next(t for t in SOLO_TARGETS if t.product == "SOLO IDU Controller")
    fields = dict(target_fields(idu, Settings(), None))
    assert (fields["Record"], fields["Holds"]) == ("v1 · TWAI 6/7 @ 250k", "GPIO3 LOW")
    assert "I2C" not in fields and "Note" not in fields


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
        assert keys == ["e", "p", "b", "a", "m", "n", "r", "l", "c", "?", "q"]
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
    rows = wizard(app).choice_list.rows
    assert [row.key for row in rows[:2]] == [SOLO_PORT, BRIDGE_PORT]  # the bridge never first
    assert rows[1].note.startswith("ESP-Prog-2 bridge")
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

    def fake_read(port: str, **_: object) -> flasher.NodeConfig:
        ports.append(port)
        return flasher.NodeConfig(config, None)

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


# ----- saved state (issue #19) -----------------------------------------------------------


def launch(state_file: Path, test: AppTest) -> None:
    run_app(test, app=FlasherApp(state_path=state_file))


def save(state_file: Path, **overrides: object) -> None:
    base = state.SavedState(Settings(), (), None, ())
    state.save_state(state_file, replace(base, **overrides))  # type: ignore[arg-type]


def test_settings_selection_and_folds_are_restored_on_launch(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    settings = replace(Settings(), solo_port=SOLO_PORT, solo_tag="v9", solo_migrate=True)
    save(state_file, settings=settings, selected="t:solo:SOLO IDU Controller", collapsed=("psoc6",))

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        assert app.settings.solo_port == SOLO_PORT and app.settings.solo_migrate
        assert app.settings.solo_tag == "v9"
        keys = [row.key for row in target_list(app).rows]
        assert "g:psoc6" in keys and "t:psoc6:" not in keys
        assert "t:boot:" in keys
        selected = target_list(app).selected
        assert selected is not None and selected.key == "t:solo:SOLO IDU Controller"

    launch(state_file, check)


def test_an_edit_and_folding_are_saved_as_they_happen(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await type_text(pilot, "solo odu")
        await pilot.press("slash", "m")
        saved = state.load_state(state_file).state
        assert saved is not None and saved.settings.solo_migrate
        await pilot.press("escape", "home", "enter")  # fold the first group
        saved = state.load_state(state_file).state
        assert saved is not None and saved.collapsed == ("ra4m1",)
        assert saved.selected == "g:ra4m1"

    launch(state_file, check)


def test_a_recent_run_survives_a_restart_and_reruns_through_the_confirm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "state.json"
    node = flasher.NodeConfig(None, None)
    monkeypatch.setattr(flasher, "read_node_config", lambda *_a, **_k: node)

    async def first(app: FlasherApp, pilot: Pilot[None]) -> None:
        await type_text(pilot, "solo odu")
        await pilot.press("slash", "r")
        await type_text(pilot, SOLO_PORT.lower())
        await pilot.press("enter")
        await settle(app, pilot)
        assert [r.operation for r in app.recents] == ["read"]

    launch(state_file, first)

    async def second(app: FlasherApp, pilot: Pilot[None]) -> None:
        assert [(r.operation, r.detail) for r in app.recents] == [("read", f"{SOLO_PORT} · read")]
        recent_rows = [row for row in target_list(app).rows if row.key.startswith("r:")]
        assert len(recent_rows) == 1
        assert app.recents[0].at <= datetime.now()
        target_list(app).select_key(recent_rows[0].key)
        await pilot.press("enter")
        assert isinstance(app.screen, WizardScreen)
        assert wizard(app).step is not None and wizard(app).step.key == "port"

    launch(state_file, second)


def test_recent_files_head_the_browser_and_missing_ones_are_dropped(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    image = tmp_path / "picked.hex"
    image.write_text(":00000001FF\n", encoding="utf-8")
    settings = replace(Settings(), recent_hex=(str(tmp_path / "gone.hex"), str(image)))
    save(state_file, settings=settings)

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await type_text(pilot, "bootloader")
        await pilot.press("slash", "f")
        assert isinstance(app.screen, BrowserScreen)
        keys = [row.key for row in app.screen.entry_list.rows]
        assert keys[:2] == ["header:recent", f"recent:{image}"]
        assert not any("gone.hex" in key for key in keys)
        await type_text(pilot, "picked")
        assert [row.key for row in app.screen.entry_list.rows][-1] == f"recent:{image}"
        await pilot.press("enter")
        assert isinstance(app.screen, DashboardScreen)
        assert app.settings.boot_hex == str(image)
        assert app.settings.recent_hex[0] == str(image)

    launch(state_file, check)


def test_a_corrupt_state_file_starts_with_defaults_and_is_replaced(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text("{not json", encoding="utf-8")

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        assert status_text(app).startswith("! Saved state unreadable")
        assert app.settings == FlasherApp(no_state=True).settings
        await type_text(pilot, "solo odu")
        await pilot.press("slash", "m")
        assert state.load_state(state_file).state is not None

    launch(state_file, check)


def test_a_newer_state_file_is_ignored_and_left_untouched(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    newer = '{"version": 99, "future": true}'
    state_file.write_text(newer, encoding="utf-8")

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        assert "newer version" in status_text(app)
        await type_text(pilot, "solo odu")
        await pilot.press("slash", "m")
        assert app.settings.solo_migrate
        assert state_file.read_text(encoding="utf-8") == newer

    launch(state_file, check)


def test_a_vanished_saved_path_falls_back_with_one_status(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    save(state_file, settings=replace(Settings(), app_bin=str(tmp_path / "gone.bin")))

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        assert app.settings.app_bin == ""
        assert status_text(app).startswith("! Saved path no longer exists")
        assert "application .bin" in status_text(app)

    launch(state_file, check)


def test_the_forget_command_deletes_the_file_and_resets_the_session(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    run = RecentRun(1, SOLO_TARGETS[0], "read", "COM9", True, "ok", datetime.now())
    save(state_file, settings=replace(Settings(), solo_port="COM9"), recents=(run,))

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        assert app.settings.solo_port == "COM9" and len(app.recents) == 1
        await pilot.press("slash", "c")
        assert not state_file.exists()
        assert app.settings.solo_port == "" and not app.recents
        assert "Saved state forgotten" in status_text(app)

    launch(state_file, check)


def test_no_state_neither_loads_nor_saves(hermetic_state: Path) -> None:
    save(hermetic_state, settings=replace(Settings(), solo_port="COM9"))
    before = hermetic_state.read_text(encoding="utf-8")

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        assert app.settings.solo_port == ""
        await type_text(pilot, "solo odu")
        await pilot.press("slash", "m")
        await pilot.press("slash", "c")
        assert "--no-state" in status_text(app)

    run_app(check, app=FlasherApp(no_state=True))
    assert hermetic_state.read_text(encoding="utf-8") == before
