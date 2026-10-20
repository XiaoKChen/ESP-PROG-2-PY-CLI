"""Tests for app — the Textual TUI driven headlessly with App.run_test() and pilot key presses.

Every hardware seam (flasher.detect, find_probe_serial and the flash/provision/read calls) is
replaced with a fake, so no probe or board is touched. Each test runs its pilot coroutine with
asyncio.run, so no pytest async plugin is needed.

Covers:       mounting at 80x24 and 120x40 with every tab's controls on screen; number keys
              switching tabs; ctrl keys working while an Input has focus; bare keys typing
              into an Input instead of firing; Esc handing focus back to the tab strip; the
              help screen listing the key map; the confirm dialog gating a flash (Esc never
              calls the flasher, Enter does); global keys staying inert under a modal; the
              palette commands; the SOLO tab's provision and read calls.
Not covered:  the flasher calls themselves (tests/test_flasher.py, tests/test_psoc6.py), the
              file picker's tree browsing, and visual styling.
See also:     src/esp_prog2_flasher/app.py, src/esp_prog2_flasher/tui/screens.py
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from textual.command import CommandPalette
from textual.pilot import Pilot
from textual.widgets import Button, Checkbox, Input, RichLog, Select, Static, TabPane, Tabs

from esp_prog2_flasher import flasher
from esp_prog2_flasher.app import GLOBAL_BINDINGS, TAB_BINDINGS, TABS, FlasherApp
from esp_prog2_flasher.board_profile import SOLO_PROFILES
from esp_prog2_flasher.dfu_config import DEFAULT_MANUFACTURER, DfuConfig
from esp_prog2_flasher.flasher import DetectResult, TargetState
from esp_prog2_flasher.tui.screens import ConfirmScreen, FilePickerScreen, HelpScreen

AppTest = Callable[[FlasherApp, Pilot[None]], Awaitable[None]]
SOLO_PORT = "COM9"
APP_WORKER_GROUPS = frozenset({"probe", "flash"})


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        flasher, "detect", lambda **_: DetectResult(TargetState.NO_PROBE, "no probe (test)")
    )
    monkeypatch.setattr(flasher, "find_probe_serial", lambda: None)


def run_app(test: AppTest, size: tuple[int, int] = (80, 24)) -> None:
    async def main() -> None:
        app = FlasherApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await test(app, pilot)
            await app_workers_done(app)

    asyncio.run(main())


async def app_workers_done(app: FlasherApp) -> None:
    """Wait for the app's detect/flash workers; a file picker's tree loader never finishes."""
    await app.workers.wait_for_complete(w for w in app.workers if w.group in APP_WORKER_GROUPS)


def active_tab(app: FlasherApp) -> str:
    return app.screen_stack[0].query_one("#tabs").active  # type: ignore[attr-defined, no-any-return]


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
def test_mounts_with_every_tab_control_on_screen(size: tuple[int, int]) -> None:
    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        for tab in TABS:
            await pilot.press(tab.key)
            await pilot.pause()
            pane = app.query_one(f"#{tab.id}", TabPane)
            for widget in pane.query("Button, Input, Select, Checkbox"):
                if widget.region.area == 0:  # hidden, e.g. the Custom… name row
                    continue
                assert app.screen.region.contains_region(widget.region), (tab.id, widget.id)
            assert app.query_one("#log", RichLog).region.height >= 3

    run_app(check, size)


def test_log_toggle_expands_and_restores_the_log() -> None:
    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        log = app.query_one("#log", RichLog)
        normal_height = log.region.height
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert log.region.height > normal_height
        await pilot.press("escape")
        await pilot.pause()
        assert log.region.height == normal_height

    run_app(check)


def test_number_keys_switch_tabs() -> None:
    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        for tab in reversed(TABS):
            await pilot.press(tab.key)
            assert active_tab(app) == tab.id

    run_app(check)


def test_ctrl_keys_work_while_an_input_has_focus() -> None:
    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        app.query_one("#hex_input", Input).focus()
        await pilot.press("ctrl+l")
        assert app.screen.has_class("-log-expanded")
        await pilot.press("ctrl+l")
        app.query_one("#hex_input", Input).focus()
        await pilot.press("ctrl+o")
        assert isinstance(app.screen, FilePickerScreen)

    run_app(check)


def test_bare_keys_type_into_a_focused_input_instead_of_firing() -> None:
    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        field = app.query_one("#mfr_input", Input)
        field.value = ""
        field.focus()
        await pilot.press("q", "2", "question_mark", "l")
        assert field.value == "q2?l"
        assert active_tab(app) == "boot"
        assert len(app.screen_stack) == 1
        assert app.is_running

    run_app(check)


def test_escape_leaves_an_input_so_bare_keys_work_again() -> None:
    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        app.query_one("#mfr_input", Input).focus()
        await pilot.press("escape")
        assert isinstance(app.focused, Tabs)
        await pilot.press("2")
        assert active_tab(app) == "app"

    run_app(check)


def test_question_mark_opens_help_listing_the_key_map() -> None:
    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await pilot.press("3", "question_mark")
        assert isinstance(app.screen, HelpScreen)
        sections = dict(app.screen.sections)
        global_rows = {description for _, description in sections["Global"]}
        assert {binding.description for binding in GLOBAL_BINDINGS} <= global_rows
        tab_rows = set(sections["Current tab: PSoC 6 (IDU Radar)"])
        assert ("^r", "Flash") in tab_rows
        assert {binding.description for binding in TAB_BINDINGS["psoc6"]} == {
            description for _, description in tab_rows
        }
        await pilot.press("escape")
        assert len(app.screen_stack) == 1

    run_app(check)


def record_calls(monkeypatch: pytest.MonkeyPatch, name: str) -> list[tuple[object, ...]]:
    """Replace ``flasher.<name>`` with a recorder of (args..., kwargs) tuples."""
    calls: list[tuple[object, ...]] = []

    def fake(*args: object, **kwargs: object) -> None:
        calls.append((*args, kwargs))

    monkeypatch.setattr(flasher, name, fake)
    return calls


def test_escape_cancels_the_confirm_without_flashing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_calls(monkeypatch, "flash_psoc6")

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await pilot.press("3", "ctrl+r")
        assert isinstance(app.screen, ConfirmScreen)
        assert dict(app.screen.rows)["Image"] == app.query_one("#psoc6_input", Input).value
        await pilot.press("escape")
        await app_workers_done(app)
        assert len(app.screen_stack) == 1
        assert calls == []

    run_app(check)


def test_enter_confirms_and_flashes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_calls(monkeypatch, "flash_psoc6")

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await pilot.press("3", "ctrl+r", "enter")
        await app_workers_done(app)
        await pilot.pause()
        hex_path = Path(app.query_one("#psoc6_input", Input).value)
        assert [call[0] for call in calls] == [hex_path]
        assert str(app.query_one("#psoc6_result", Static).content).startswith("✓")
        assert not app.query_one("#flash_psoc6", Button).disabled

    run_app(check)


def test_global_keys_are_inert_under_a_modal(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_calls(monkeypatch, "flash_psoc6")

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await pilot.press("3", "question_mark", "ctrl+r", "ctrl+d")
        assert isinstance(app.screen, HelpScreen)
        assert len(app.screen_stack) == 2
        assert calls == []

    run_app(check)


def test_palette_lists_every_action_with_its_key() -> None:
    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        commands = {command.title: command.help for command in app.get_system_commands(app.screen)}
        for tab in TABS:
            assert f"Tab {tab.key}: {tab.label}" in commands
            for binding in TAB_BINDINGS[tab.id]:
                title = f"{binding.description} ({tab.label})"
                assert title in commands
                assert app.get_key_display(binding) in commands[title]
        for title in ("Help", "Detect", "Expand/restore log", "Browse app .bin (SOLO board)"):
            assert title in commands
        await pilot.press("ctrl+p")
        assert isinstance(app.screen, CommandPalette)

    run_app(check)


def test_palette_command_switches_tab_before_acting(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_calls(monkeypatch, "flash_psoc6")

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        commands = {command.title: command for command in app.get_system_commands(app.screen)}
        commands["Flash (PSoC 6)"].callback()
        await pilot.pause()
        assert active_tab(app) == "psoc6"
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("escape")
        assert calls == []

    run_app(check)


def test_solo_actions_need_the_board_port(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_calls(monkeypatch, "read_node_config")

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await pilot.press("4", "ctrl+r", "ctrl+n", "ctrl+g")
        await app_workers_done(app)
        assert len(app.screen_stack) == 1
        assert calls == []

    run_app(check)


def test_solo_provision_writes_the_preset_like_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_calls(monkeypatch, "provision_node")
    profile = SOLO_PROFILES["SOLO IDU Controller"]

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await pilot.press("4")
        app.query_one("#solo_port", Input).value = SOLO_PORT
        app.query_one("#solo_preset", Select).value = profile.product
        await pilot.press("ctrl+n")
        assert isinstance(app.screen, ConfirmScreen)
        assert dict(app.screen.rows)["Product"] == profile.product
        await pilot.press("enter")
        await app_workers_done(app)
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
def test_solo_read_config_shows_the_result_without_confirm(
    monkeypatch: pytest.MonkeyPatch, config: DfuConfig | None, expected: str
) -> None:
    ports: list[str] = []

    def fake_read(port: str, **_: object) -> DfuConfig | None:
        ports.append(port)
        return config

    monkeypatch.setattr(flasher, "read_node_config", fake_read)

    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        await pilot.press("4")
        app.query_one("#solo_port", Input).value = SOLO_PORT
        await pilot.press("ctrl+g")
        assert len(app.screen_stack) == 1
        await app_workers_done(app)
        await pilot.pause()
        assert ports == [SOLO_PORT]
        assert expected in str(app.query_one("#solo_result", Static).content)

    run_app(check)


def test_embassy_key_toggles_the_app_base_address_checkbox() -> None:
    async def check(app: FlasherApp, pilot: Pilot[None]) -> None:
        checkbox = app.query_one("#embassy_checkbox", Checkbox)
        await pilot.press("2", "ctrl+b")
        assert checkbox.value
        await pilot.press("1", "ctrl+b")
        assert checkbox.value  # ctrl+b belongs to tab 2 only

    run_app(check)
