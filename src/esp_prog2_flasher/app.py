"""Textual TUI: flash the RA4M1 bootloader/app, the PSoC 6 image, or the ESP-Prog-2 firmware.

FlasherApp shows one tab per flash path — RA4M1 bootloader + DFU config, RA4M1 application,
PSoC 6 (IDU Radar) and the ESP-Prog-2's own firmware — under a persistent probe/serial/target
status bar, with a shared progress bar and a toggleable log. It sits on top of flasher.py and
is launched by cli.py when no flags are given.

Owns:        the tab layout, the key map (GLOBAL_BINDINGS + TAB_BINDINGS, the single source
             the footer, the help screen and the command palette are all derived from), the
             per-tab result lines and the UI-side parsing of the CAN ID inputs.
Not here:    USB/serial/SWD access and all flashing logic (flasher.py), config-block encoding
             and the device table (dfu_config.py), the modal screens (tui/screens.py); the TUI
             never touches hardware directly.
Invariants:  a bare printable key never fires while an Input has focus (the Input consumes
             it); only ctrl/function-key bindings are priority bindings, and none of them
             runs while a modal screen is open. Flash target and Flash app stay disabled
             until a Detect finds a flashable RA4M1.
Concurrency: flasher calls run in Textual thread workers (@work(thread=True)); results and
             progress reach the UI only via call_from_thread. Every write and serial read
             runs in the exclusive "flash" worker group, and no new one (nor a Detect) starts
             while one is running.
Depends on:  flasher.py, dfu_config.py, tui/screens.py, textual, rich.
See also:    cli.py, tests/test_app.py
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Final, Literal

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
)

from . import __version__, flasher
from .dfu_config import (
    DEFAULT_BOOT_CMD_ID,
    DEFAULT_BOOT_REPLY_ID,
    DEFAULT_MANUFACTURER,
    DEVICE_CAN_IDS,
    DEVICE_NAMES,
    MAX_CAN_ID,
)
from .flasher import DetectResult, TargetState
from .tui.screens import ConfirmScreen, FilePickerScreen, HelpRow, HelpScreen

logger = logging.getLogger(__name__)

# Sentinel value/label for the "type your own name" entry in the device Select.
# A KNOWN device's value is its name (present in DEVICE_CAN_IDS); this is the one
# option that unlocks the custom-name input and the editable boot CAN ID fields.
CUSTOM_DEVICE_VALUE: str = "__custom__"
CUSTOM_DEVICE_LABEL: str = "Custom…"

# Embassy bootloader reserves 32 KiB for itself, so its app slot starts here
# (vs. the 16 KiB C bootloader's flasher.APP_BASE_ADDRESS == 0x4000).
EMBASSY_APP_BASE_ADDRESS: Final[int] = 0x8000

TabId = Literal["boot", "app", "psoc6", "probe"]
StatusLevel = Literal["ok", "warn", "err", "busy"]
StatusKey = Literal["probe", "serial", "target"]

# Status is shown as symbol + words, never by colour alone.
_STATUS_SYMBOLS: Final[dict[StatusLevel, str]] = {"ok": "✓", "warn": "!", "err": "✗", "busy": "…"}
_STATUS_STYLES: Final[dict[StatusLevel, str]] = {
    "ok": "bold green",
    "warn": "bold yellow",
    "err": "bold red",
    "busy": "dim",
}
_TARGET_STATUS: Final[dict[TargetState, tuple[StatusLevel, str]]] = {
    TargetState.NO_PROBE: ("err", "Target —"),
    TargetState.NO_TARGET: ("warn", "Target: no RA4M1 over SWD"),
    TargetState.CONNECTED: ("ok", "Target: RA4M1 connected"),
    TargetState.CONNECTED_BLANK: ("ok", "Target: RA4M1 connected (blank)"),
}
_FLASH_GROUP: Final[str] = "flash"
_LOG_EXPANDED_CLASS: Final[str] = "-log-expanded"


@dataclass(frozen=True, slots=True)
class TabSpec:
    """One flash path's tab: its pane id, its number key and its labels."""

    id: TabId
    key: str
    label: str
    title: str


TABS: Final[tuple[TabSpec, ...]] = (
    TabSpec("boot", "1", "RA4M1 bootloader", "RA4M1 bootloader + DFU config"),
    TabSpec("app", "2", "RA4M1 app", "RA4M1 application"),
    TabSpec("psoc6", "3", "PSoC 6", "PSoC 6 (IDU Radar)"),
    TabSpec("probe", "5", "ESP-Prog-2", "ESP-Prog-2 probe firmware"),
)
TAB_TITLES: Final[dict[TabId, str]] = {tab.id: tab.title for tab in TABS}

_TABS_GROUP: Final = Binding.Group("Tabs", compact=True)

# The key map. Everything that shows keys — footer, help screen, command palette — is
# derived from these two tables. Bare printable keys are plain bindings, so an Input
# with focus swallows them; ctrl/function keys are priority bindings so they also work
# while typing (check_action keeps them from firing under a modal screen).
GLOBAL_BINDINGS: Final[tuple[Binding, ...]] = (
    *(Binding(tab.key, f"switch_tab('{tab.id}')", tab.label, group=_TABS_GROUP) for tab in TABS),
    Binding("question_mark,f1", "help", "Help", key_display="?"),
    Binding("ctrl+d", "detect", "Detect", priority=True),
    Binding("ctrl+l", "toggle_log", "Log", show=False, priority=True),
    Binding("escape", "leave_input", "Leave input", show=False),
    Binding("q", "quit", "Quit", show=False),
    Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
)
TAB_BINDINGS: Final[dict[TabId, tuple[Binding, ...]]] = {
    "boot": (
        Binding("ctrl+r", "flash_target", "Flash", priority=True),
        Binding("ctrl+o", "browse_hex", "Browse", priority=True),
    ),
    "app": (
        Binding("ctrl+r", "flash_app", "Flash", priority=True),
        Binding("ctrl+o", "browse_app", "Browse", priority=True),
        Binding("ctrl+b", "toggle_embassy", "Embassy", priority=True),
    ),
    "psoc6": (
        Binding("ctrl+r", "flash_psoc6", "Flash", priority=True),
        Binding("ctrl+o", "browse_psoc6", "Browse", priority=True),
    ),
    "probe": (
        Binding("ctrl+r", "flash_probe", "Flash", priority=True),
        Binding("ctrl+g", "update_fw", "Get official", priority=True),
    ),
}
# Which tab each tab-scoped action belongs to; actions not listed are global.
ACTION_TABS: Final[dict[str, TabId]] = {
    binding.action: tab for tab, bindings in TAB_BINDINGS.items() for binding in bindings
}
# Actions of the key map that must not fire through a modal screen (quit always may).
_GUARDED_ACTIONS: Final[frozenset[str]] = frozenset(
    binding.action.partition("(")[0]
    for binding in (*GLOBAL_BINDINGS, *(b for bs in TAB_BINDINGS.values() for b in bs))
    if binding.action != "quit"
)
# Global bindings the palette leaves out: Textual's own commands already cover quitting,
# and leaving an input only means something with the keyboard.
_PALETTE_SKIPPED_ACTIONS: Final[frozenset[str]] = frozenset({"quit", "leave_input"})


def _parse_can_id(text: str) -> int:
    """Parse a TUI CAN ID accepting hex (``0x700``) or decimal; raise on bad input."""
    value = int(text, 0)
    if not 0 <= value <= MAX_CAN_ID:
        raise ValueError(f"CAN ID must be 0x000..0x{MAX_CAN_ID:03X}")
    return value


def _picker_start(current: str) -> Path:
    """Directory a picker opens in: the current value's parent, else the working directory."""
    start = Path(current).expanduser().parent if current else Path.cwd()
    return start if start.is_dir() else Path.cwd()


class FlasherApp(App[None]):
    """Detect the ESP-Prog-2; flash the RA4M1 (bootloader/config, app), PSoC 6, or the probe."""

    TITLE = "ESP-Prog-2 Flasher"
    SUB_TITLE = f"v{__version__}"

    CSS = """
    Screen { background: $surface; }
    #status_bar { height: auto; padding: 0 1; background: $panel; }
    #tabs { height: auto; }
    TabPane { padding: 0 1; height: auto; }
    .pane { height: auto; max-height: 14; }
    .info { color: $text-muted; height: auto; }
    .row { height: auto; }
    .row Label { width: 19; padding: 0 1 0 0; }
    .row Input { width: 1fr; }
    .row Button { margin: 0 0 0 1; }
    .buttons { height: auto; margin: 1 0 0 0; }
    .buttons Button { margin: 0 1 0 0; }
    #boot_reply_label { width: auto; padding: 0 1; }
    .result { height: auto; margin: 1 0 0 0; }
    .ok { color: $success; text-style: bold; }
    .warn { color: $warning; text-style: bold; }
    .err { color: $error; text-style: bold; }
    #progress_row { height: 1; padding: 0 1; }
    #progress { width: 1fr; }
    #log {
        height: 1fr;
        min-height: 5;
        border: round $accent;
        background: $surface-darken-1;
        padding: 0 1;
    }
    Screen.-log-expanded #tabs, Screen.-log-expanded #progress_row { display: none; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        *GLOBAL_BINDINGS,
        *(binding for bindings in TAB_BINDINGS.values() for binding in bindings),
    ]

    def __init__(self, hex_path: Path | None = None, frequency: int = flasher.DEFAULT_FREQUENCY_HZ):
        super().__init__()
        self._frequency = frequency
        self._detect: DetectResult | None = None
        default = hex_path or flasher.find_default_hex()
        self._default_hex = str(default) if default else ""
        psoc6_default = flasher.find_default_psoc6_hex()
        self._default_psoc6_hex = str(psoc6_default) if psoc6_default else ""
        self._status: dict[StatusKey, tuple[StatusLevel, str]] = {
            "probe": ("busy", "Probe …"),
            "serial": ("busy", "Serial …"),
            "target": ("busy", "Target …"),
        }

    # ----- layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="status_bar")
        with TabbedContent(id="tabs"):
            with TabPane(self._tab_label("boot"), id="boot"):
                yield from self._compose_boot()
            with TabPane(self._tab_label("app"), id="app"):
                yield from self._compose_app()
            with TabPane(self._tab_label("psoc6"), id="psoc6"):
                yield from self._compose_psoc6()
            with TabPane(self._tab_label("probe"), id="probe"):
                yield from self._compose_probe()
        with Horizontal(id="progress_row"):
            yield ProgressBar(total=100, show_eta=False, id="progress")
            yield Button("Quit", id="quit", variant="error", compact=True)
        yield RichLog(id="log", markup=True, highlight=True, wrap=True, min_width=40)
        yield Footer(compact=True)

    @staticmethod
    def _tab_label(tab: TabId) -> str:
        spec = next(spec for spec in TABS if spec.id == tab)
        return f"{spec.key} {spec.label}"

    def _compose_boot(self) -> ComposeResult:
        with VerticalScroll(classes="pane"):
            with Horizontal(classes="row"):
                yield Label("Bootloader (.hex):")
                yield Input(
                    value=self._default_hex,
                    placeholder="path to .hex",
                    id="hex_input",
                    compact=True,
                )
                yield Button("Browse", id="browse_hex", compact=True)
            with Horizontal(classes="row"):
                yield Label("USB manufacturer:")
                yield Input(value=DEFAULT_MANUFACTURER, id="mfr_input", compact=True)
            with Horizontal(classes="row"):
                yield Label("USB device:")
                yield Select(
                    [(name, name) for name in DEVICE_NAMES]
                    + [(CUSTOM_DEVICE_LABEL, CUSTOM_DEVICE_VALUE)],
                    prompt="keep bootloader default",
                    id="prod_select",
                    compact=True,
                )
            with Horizontal(classes="row", id="custom_name_row"):
                yield Label("Custom device name:")
                yield Input(placeholder="type a device name", id="custom_name_input", compact=True)
            with Horizontal(classes="row"):
                yield Label("CAN host->boot:")
                yield Input(
                    placeholder=f"blank = 0x{DEFAULT_BOOT_CMD_ID:03X}",
                    id="boot_cmd_input",
                    compact=True,
                )
                yield Label("boot->host:", id="boot_reply_label")
                yield Input(
                    placeholder=f"blank = 0x{DEFAULT_BOOT_REPLY_ID:03X}",
                    id="boot_reply_input",
                    compact=True,
                )
            with Horizontal(classes="buttons"):
                yield Button("Detect", id="detect", variant="primary", compact=True)
                yield Button(
                    "Flash target",
                    id="flash_target",
                    variant="success",
                    disabled=True,
                    compact=True,
                )
            yield Static("No result yet.", id="boot_result", classes="result")

    def _compose_app(self) -> ComposeResult:
        with VerticalScroll(classes="pane"):
            yield Static(
                "Application flashed to the RA4M1 over SWD, above the bootloader.", classes="info"
            )
            with Horizontal(classes="row"):
                yield Label("Application (.bin):")
                yield Input(placeholder="path to app .bin", id="app_input", compact=True)
                yield Button("Browse", id="browse_app", compact=True)
            with Horizontal(classes="row"):
                yield Checkbox(
                    "Embassy bootloader (app at 0x8000)", id="embassy_checkbox", compact=True
                )
            with Horizontal(classes="buttons"):
                yield Button(
                    "Flash app", id="flash_app", variant="success", disabled=True, compact=True
                )
            yield Static("No result yet.", id="app_result", classes="result")

    def _compose_psoc6(self) -> ComposeResult:
        with VerticalScroll(classes="pane"):
            yield Static("PSoC 6 (IDU Radar) main flash, programmed over SWD.", classes="info")
            with Horizontal(classes="row"):
                yield Label("PSoC 6 (.hex):")
                yield Input(
                    value=self._default_psoc6_hex,
                    placeholder="path to PSoC 6 .hex",
                    id="psoc6_input",
                    compact=True,
                )
                yield Button("Browse", id="browse_psoc6", compact=True)
            with Horizontal(classes="buttons"):
                yield Button("Flash PSoC 6", id="flash_psoc6", variant="success", compact=True)
            yield Static("No result yet.", id="psoc6_result", classes="result")

    def _compose_probe(self) -> ComposeResult:
        with VerticalScroll(classes="pane"):
            yield Static("ESP-Prog-2's own ESP32-S3 firmware, via esptool.", classes="info")
            yield Static("Firmware: …", id="fw_status")
            with Horizontal(classes="buttons"):
                yield Button("Get official (JTAG)", id="update_fw", variant="primary", compact=True)
                yield Button("Flash ESP-Prog-2", id="flash_probe", variant="warning", compact=True)
            yield Static("No result yet.", id="probe_result", classes="result")

    def on_mount(self) -> None:
        log_toggle = next(b for b in GLOBAL_BINDINGS if b.action == "toggle_log")
        self._main.query_one(
            "#log", RichLog
        ).border_title = f"Log  ({self._key_text(log_toggle)} expands)"
        # Custom-name input is revealed only when "Custom…" is selected.
        self._main.query_one("#custom_name_row").display = False
        self._render_status()
        self._write_log(
            f"Target: [b]{flasher.TARGET_TYPE}[/b] via pyOCD  •  "
            f"Probe fw: [b]{flasher.PROBE_CHIP}[/b] via esptool"
        )
        self._refresh_fw_status()
        self._focus_tabs()
        self.action_detect()

    # ----- key map plumbing ----------------------------------------------------

    @property
    def _main(self) -> Screen[object]:
        """The tab screen. App.query_one would search whatever modal is on top instead."""
        return self.screen_stack[0]

    def _active_tab(self) -> TabId | None:
        if not self.screen_stack:
            return None
        tabs = self._main.query(TabbedContent)
        if not tabs:
            return None
        active = tabs.first().active
        return next((tab.id for tab in TABS if tab.id == active), None)

    def _flash_running(self) -> bool:
        return any(w.group == _FLASH_GROUP and not w.is_finished for w in self.workers)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide tab-scoped actions on other tabs, and keep the key map inert under modals."""
        if action in _GUARDED_ACTIONS and len(self.screen_stack) > 1:
            return False
        tab = ACTION_TABS.get(action)
        return tab is None or tab == self._active_tab()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self.refresh_bindings()

    def _focus_tabs(self) -> None:
        self._main.query_one("#tabs", TabbedContent).query_one(Tabs).focus()

    def _key_text(self, binding: Binding) -> str:
        """Every key of a binding as displayed, e.g. ``? / f1``; key_display names the first."""
        first, *others = binding.key.split(",")
        shown = [self.get_key_display(replace(binding, key=first))]
        shown += [self.get_key_display(replace(binding, key=k, key_display=None)) for k in others]
        return " / ".join(shown)

    def _help_rows(self, bindings: Iterable[BindingType]) -> list[HelpRow]:
        return [(self._key_text(b), b.description) for b in bindings if isinstance(b, Binding)]

    def get_system_commands(self, screen: Screen[object]) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        # Opened over a dialog: offer only Textual's own commands, never a second flash.
        if screen is not self._main:
            return
        for binding in GLOBAL_BINDINGS:
            name = binding.action.partition("(")[0]
            if name in _PALETTE_SKIPPED_ACTIONS or name == "switch_tab":
                continue
            yield SystemCommand(
                binding.description,
                f"Global · {self._key_text(binding)}",
                getattr(self, f"action_{name}"),
            )
        for tab in TABS:
            yield SystemCommand(
                f"Tab {tab.key}: {tab.label}",
                f"Switch tab · {tab.key} (Esc first if typing)",
                lambda tab_id=tab.id: self.action_switch_tab(tab_id),
            )
            for binding in TAB_BINDINGS[tab.id]:
                yield SystemCommand(
                    f"{binding.description} ({tab.label})",
                    f"{tab.title} · {self._key_text(binding)} on tab {tab.key}",
                    self._on_tab(tab.id, getattr(self, f"action_{binding.action}")),
                )
        for title, tab_id, help_text, callback in self._palette_extras():
            yield SystemCommand(title, f"{TAB_TITLES[tab_id]} · {help_text}", callback)

    def _palette_extras(self) -> Iterable[tuple[str, TabId, str, Callable[[], None]]]:
        """Palette-only commands: actions without a key of their own."""
        yield (
            "Select USB device…",
            "boot",
            "palette only",
            self._on_tab("boot", lambda: self._main.query_one("#prod_select", Select).focus()),
        )

    def _on_tab(self, tab: TabId, action: Callable[[], object]) -> Callable[[], None]:
        """Wrap an action so it first switches to the tab it acts on."""

        def run() -> None:
            self.action_switch_tab(tab)
            action()

        return run

    # ----- global actions ------------------------------------------------------

    def action_switch_tab(self, tab: str) -> None:
        self._main.query_one("#tabs", TabbedContent).active = tab
        self._focus_tabs()

    def action_help(self) -> None:
        tab = self._active_tab()
        sections: list[tuple[str, list[HelpRow]]] = [
            ("Global", self._help_rows(GLOBAL_BINDINGS)),
        ]
        if tab is not None:
            sections.append((f"Current tab: {TAB_TITLES[tab]}", self._help_rows(TAB_BINDINGS[tab])))
        palette_key = self.get_key_display(Binding(self.COMMAND_PALETTE_BINDING, "command_palette"))
        sections.append(("Command palette", [(palette_key, "search every action, with its key")]))
        dialogs = (*ConfirmScreen.BINDINGS, *FilePickerScreen.BINDINGS, *HelpScreen.BINDINGS)
        sections.append(("Dialogs", self._help_rows(dialogs)))
        self.push_screen(HelpScreen(sections))

    def action_leave_input(self) -> None:
        """Back out one level: collapse an expanded log, else move focus to the tab strip."""
        if self._main.has_class(_LOG_EXPANDED_CLASS):
            self.action_toggle_log()
        else:
            self._focus_tabs()

    def action_toggle_log(self) -> None:
        expanded = self._main.toggle_class(_LOG_EXPANDED_CLASS).has_class(_LOG_EXPANDED_CLASS)
        if expanded:
            self._main.query_one("#log", RichLog).focus()
        else:
            self._focus_tabs()

    def action_detect(self) -> None:
        if self._flash_running():
            self._write_log("[yellow]Busy: wait for the current operation to finish.[/yellow]")
            return
        self._set_progress(0)
        self._set_status("probe", "busy", "Probe scanning…")
        self._set_status("target", "busy", "Target scanning…")
        self.detect_worker()
        self._refresh_serial_status()

    # ----- tab actions -----------------------------------------------------------

    def _ready_to_start(self) -> bool:
        if self._flash_running():
            self._write_log("[yellow]Busy: wait for the current operation to finish.[/yellow]")
            return False
        return True

    def action_flash_target(self) -> None:
        if not self._ready_to_start():
            return
        if not (self._detect and self._detect.flashable):
            self._write_log("[yellow]No RA4M1 target detected. Run Detect first.[/yellow]")
            return
        hex_path = Path(self._main.query_one("#hex_input", Input).value.strip())
        if not hex_path.is_file():
            self._write_log(f"[red]Bootloader not found:[/red] {hex_path}")
            return
        prod_value = self._main.query_one("#prod_select", Select).value
        if prod_value is Select.BLANK:
            product = None
        elif prod_value == CUSTOM_DEVICE_VALUE:
            product = self._main.query_one("#custom_name_input", Input).value.strip()
            if not product:
                self._write_log(
                    "[red]Enter a custom device name, or pick a device from the list.[/red]"
                )
                return
        else:
            product = str(prod_value)
        mfr = self._main.query_one("#mfr_input", Input).value.strip()
        if product is not None and not mfr:
            self._write_log(
                "[red]Enter a USB manufacturer, or clear the device to keep "
                "bootloader defaults.[/red]"
            )
            return
        manufacturer = mfr if product is not None else None

        cmd_raw = self._main.query_one("#boot_cmd_input", Input).value.strip()
        reply_raw = self._main.query_one("#boot_reply_input", Input).value.strip()
        if (cmd_raw != "") != (reply_raw != ""):
            self._write_log(
                "[red]Set both boot CAN IDs (host->boot and boot->host), or leave "
                "both blank to keep bootloader defaults.[/red]"
            )
            return
        boot_cmd_id: int | None = None
        boot_reply_id: int | None = None
        if cmd_raw and reply_raw:
            if product is None:
                self._write_log(
                    "[red]Boot CAN IDs need a USB device selected; the block is only "
                    "written when a device is chosen.[/red]"
                )
                return
            try:
                boot_cmd_id = _parse_can_id(cmd_raw)
                boot_reply_id = _parse_can_id(reply_raw)
            except ValueError as exc:
                self._write_log(f"[red]Invalid boot CAN ID:[/red] {exc}")
                return

        self._busy(True)
        self._set_progress(0)
        self._write_log(f"[b]Flashing target[/b] {hex_path.name} …")
        if manufacturer and product:
            self._write_log(
                f"USB names: manufacturer=[b]{manufacturer}[/b] product=[b]{product}[/b]"
            )
        if boot_cmd_id is not None and boot_reply_id is not None:
            self._write_log(
                f"Boot CAN IDs: host->boot=[b]0x{boot_cmd_id:03X}[/b] "
                f"boot->host=[b]0x{boot_reply_id:03X}[/b]"
            )
        self.flash_target_worker(hex_path, manufacturer, product, boot_cmd_id, boot_reply_id)

    def action_flash_probe(self) -> None:
        if not self._ready_to_start():
            return
        fw = flasher.find_probe_firmware()
        if fw is None:
            self._write_log("[red]No esp-prog2.bin found.[/red] Use 'Download latest' first.")
            return
        serial = flasher.find_probe_serial()
        if serial is None:
            self._write_log(
                "[yellow]No ESP-Prog-2 serial port found. Plug it in and enter "
                "download mode (hold BOOT, tap RESET).[/yellow]"
            )
            return
        if not serial.in_download_mode:
            self._write_log(
                f"[yellow]{serial.device} looks like the running bridge (not download "
                f"mode). If flashing fails, hold BOOT + tap RESET, then retry.[/yellow]"
            )
        self._busy(True)
        self._set_progress(0)
        self._write_log(f"[b]Flashing ESP-Prog-2[/b] {fw.name} via {serial.device} …")
        self.flash_probe_worker(fw, serial.device)

    def action_update_fw(self) -> None:
        if not self._ready_to_start():
            return
        self._busy(True)
        self._write_log(
            "[b]Downloading the OFFICIAL esp-prog2.bin (JTAG build)[/b] — saved separately as "
            "esp-prog2-official-jtag.bin; it will NOT replace the bundled SWD build."
        )
        self.download_fw_worker()

    def action_flash_app(self) -> None:
        if not self._ready_to_start():
            return
        if not (self._detect and self._detect.flashable):
            self._write_log("[yellow]No RA4M1 target detected. Run Detect first.[/yellow]")
            return
        bin_path = Path(self._main.query_one("#app_input", Input).value.strip())
        if not bin_path.is_file():
            self._write_log(f"[red]Application binary not found:[/red] {bin_path}")
            return
        base_address = (
            EMBASSY_APP_BASE_ADDRESS
            if self._main.query_one("#embassy_checkbox", Checkbox).value
            else flasher.APP_BASE_ADDRESS
        )

        self._busy(True)
        self._set_progress(0)
        self._write_log(f"[b]Flashing application[/b] {bin_path.name} …")
        self.flash_app_worker(bin_path, base_address)

    def action_toggle_embassy(self) -> None:
        self._main.query_one("#embassy_checkbox", Checkbox).toggle()

    def action_flash_psoc6(self) -> None:
        if not self._ready_to_start():
            return
        hex_path = Path(self._main.query_one("#psoc6_input", Input).value.strip())
        if not hex_path.is_file():
            self._write_log(f"[red]PSoC 6 hex not found:[/red] {hex_path}")
            return
        self._busy(True)
        self._set_progress(0)
        self._write_log(f"[b]Flashing PSoC 6[/b] {hex_path.name} …")
        self.flash_psoc6_worker(hex_path)

    def _browse(self, input_id: str, suffix: str, title: str, select_dirs: bool = False) -> None:
        field = self._main.query_one(f"#{input_id}", Input)

        def apply_choice(chosen: Path | None) -> None:
            if chosen is not None:
                field.value = str(chosen)

        start = _picker_start(field.value)
        self.push_screen(FilePickerScreen(start, suffix, title, select_dirs), apply_choice)

    def action_browse_hex(self) -> None:
        self._browse("hex_input", ".hex", "Select bootloader .hex")

    def action_browse_psoc6(self) -> None:
        self._browse("psoc6_input", ".hex", "Select PSoC 6 .hex")

    def action_browse_app(self) -> None:
        self._browse("app_input", ".bin", "Select application .bin")

    def on_select_changed(self, event: Select.Changed) -> None:
        """Tie CAN-ID editability to the device choice.

        KNOWN device → fill both boot CAN ID inputs from the board table and lock
        them (read-only). "Custom…" → reveal the name input and unlock the ID
        fields (editable; blank → bootloader defaults). BLANK (keep default) →
        hide the name input and leave the ID fields empty and editable.
        """
        if event.select.id != "prod_select":
            return
        value = event.value
        custom_row = self._main.query_one("#custom_name_row")
        cmd_input = self._main.query_one("#boot_cmd_input", Input)
        reply_input = self._main.query_one("#boot_reply_input", Input)

        known_ids = (
            None if value in (Select.BLANK, CUSTOM_DEVICE_VALUE) else DEVICE_CAN_IDS.get(str(value))
        )
        custom_row.display = value == CUSTOM_DEVICE_VALUE
        if known_ids is not None:
            cmd_id, reply_id = known_ids
            cmd_input.value = f"0x{cmd_id:02X}"
            reply_input.value = f"0x{reply_id:02X}"
            cmd_input.disabled = True
            reply_input.disabled = True
        else:
            cmd_input.value = ""
            reply_input.value = ""
            cmd_input.disabled = False
            reply_input.disabled = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers: dict[str, Callable[[], object]] = {
            "detect": self.action_detect,
            "flash_target": self.action_flash_target,
            "flash_app": self.action_flash_app,
            "flash_psoc6": self.action_flash_psoc6,
            "flash_probe": self.action_flash_probe,
            "update_fw": self.action_update_fw,
            "browse_hex": self.action_browse_hex,
            "browse_app": self.action_browse_app,
            "browse_psoc6": self.action_browse_psoc6,
            "quit": self.exit,
        }
        handler = handlers.get(event.button.id or "")
        if handler:
            handler()

    # ----- workers -----------------------------------------------------------

    @work(thread=True, exclusive=True, group="probe")
    def detect_worker(self) -> None:
        try:
            result = flasher.detect(frequency=self._frequency)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Detection worker failed")
            result = DetectResult(TargetState.NO_PROBE, f"Detection error: {exc!r}")
        self.call_from_thread(self._apply_detect, result)

    @work(thread=True, exclusive=True, group=_FLASH_GROUP)
    def flash_target_worker(
        self,
        hex_path: Path,
        manufacturer: str | None,
        product: str | None,
        boot_cmd_id: int | None,
        boot_reply_id: int | None,
    ) -> None:
        try:
            flasher.flash(
                hex_path,
                frequency=self._frequency,
                progress=lambda f: self.call_from_thread(self._set_progress, f * 100.0),
                log=lambda m: self.call_from_thread(self._write_log, m),
                manufacturer=manufacturer,
                product=product,
                boot_cmd_id=boot_cmd_id,
                boot_reply_id=boot_reply_id,
            )
            self.call_from_thread(self._flash_done, "boot", True, "Target flashed and reset.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Flash target worker failed")
            self.call_from_thread(self._flash_done, "boot", False, f"Target flash failed: {exc!r}")

    @work(thread=True, exclusive=True, group=_FLASH_GROUP)
    def flash_app_worker(self, bin_path: Path, base_address: int) -> None:
        try:
            flasher.flash_app(
                bin_path,
                base_address=base_address,
                frequency=self._frequency,
                progress=lambda f: self.call_from_thread(self._set_progress, f * 100.0),
                log=lambda m: self.call_from_thread(self._write_log, m),
            )
            self.call_from_thread(self._flash_done, "app", True, "Application flashed and reset.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Flash app worker failed")
            self.call_from_thread(
                self._flash_done, "app", False, f"Application flash failed: {exc!r}"
            )

    @work(thread=True, exclusive=True, group=_FLASH_GROUP)
    def flash_psoc6_worker(self, hex_path: Path) -> None:
        try:
            flasher.flash_psoc6(
                hex_path,
                frequency=self._frequency,
                progress=lambda f: self.call_from_thread(self._set_progress, f * 100.0),
                log=lambda m: self.call_from_thread(self._write_log, m),
            )
            self.call_from_thread(self._flash_done, "psoc6", True, "PSoC 6 flashed and reset.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Flash PSoC 6 worker failed")
            self.call_from_thread(self._flash_done, "psoc6", False, f"PSoC 6 flash failed: {exc!r}")

    @work(thread=True, exclusive=True, group=_FLASH_GROUP)
    def flash_probe_worker(self, fw: Path, port: str) -> None:
        try:
            flasher.flash_probe(
                fw,
                port=port,
                progress=lambda f: self.call_from_thread(self._set_progress, f),
                log=lambda m: self.call_from_thread(self._write_log, m),
            )
            self.call_from_thread(self._flash_done, "probe", True, "ESP-Prog-2 firmware flashed.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Flash probe worker failed")
            self.call_from_thread(
                self._flash_done, "probe", False, f"ESP-Prog-2 flash failed: {exc!r}"
            )

    @work(thread=True, exclusive=True, group=_FLASH_GROUP)
    def download_fw_worker(self) -> None:
        try:
            dest = flasher.download_probe_firmware(
                log=lambda m: self.call_from_thread(self._write_log, m)
            )
            self.call_from_thread(self._download_done, True, f"Saved {dest}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Download firmware worker failed")
            self.call_from_thread(self._download_done, False, f"Download failed: {exc!r}")

    # ----- UI updates (main thread) -----------------------------------------

    def _set_status(self, key: StatusKey, level: StatusLevel, text: str) -> None:
        self._status[key] = (level, text)
        self._render_status()

    def _render_status(self) -> None:
        line = Text()
        for index, (level, text) in enumerate(self._status.values()):
            if index:
                line.append("   │   ", style="dim")
            line.append(f"{_STATUS_SYMBOLS[level]} {text}", style=_STATUS_STYLES[level])
        self._main.query_one("#status_bar", Static).update(line)

    def _apply_detect(self, result: DetectResult) -> None:
        self._detect = result
        if result.state is TargetState.NO_PROBE:
            self._set_status("probe", "err", "Probe not found")
        else:
            label = result.probe.label if result.probe else "unknown"
            self._set_status("probe", "ok", f"Probe {label}")

        self._set_status("target", *_TARGET_STATUS[result.state])
        busy = self._flash_running()
        self._main.query_one("#flash_target", Button).disabled = busy or not result.flashable
        self._main.query_one("#flash_app", Button).disabled = busy or not result.flashable
        self._write_log(result.detail)

    def _refresh_serial_status(self) -> None:
        serial = flasher.find_probe_serial()
        if serial is None:
            self._set_status("serial", "warn", "Serial: no ESP-Prog-2 port")
        elif serial.in_download_mode:
            self._set_status(
                "serial", "ok", f"Serial: {serial.device} (download mode, pid 0x{serial.pid:04X})"
            )
        else:
            self._set_status(
                "serial", "warn", f"Serial: {serial.device} (bridge running — BOOT+RESET to flash)"
            )

    def _refresh_fw_status(self) -> None:
        fw = flasher.find_probe_firmware()
        widget = self._main.query_one("#fw_status", Static)
        if fw is None:
            widget.set_classes("warn")
            widget.update("! Firmware: esp-prog2.bin missing — use Get official")
        else:
            widget.set_classes("ok")
            widget.update(f"✓ Firmware: {fw.name} ({fw.stat().st_size} bytes)")

    def _set_result(self, tab: TabId, ok: bool, message: str) -> None:
        widget = self._main.query_one(f"#{tab}_result", Static)
        widget.set_classes(f"result {'ok' if ok else 'err'}")
        stamp = datetime.now().strftime("%H:%M:%S")
        widget.update(Text(f"{'✓' if ok else '✗'} {stamp}  {message}"))
        self.notify(
            message,
            title=TAB_TITLES[tab],
            severity="information" if ok else "error",
            markup=False,
        )

    def _flash_done(self, tab: TabId, ok: bool, message: str) -> None:
        self._busy(False)
        if ok:
            self._set_progress(100)
            self._write_log(f"[green]{message}[/green]")
        else:
            self._write_log(f"[red]{message}[/red]")
        self._set_result(tab, ok, message)

    def _download_done(self, ok: bool, message: str) -> None:
        self._busy(False)
        self._refresh_fw_status()
        self._write_log(f"[{'green' if ok else 'red'}]{message}[/]")
        self._set_result("probe", ok, message)

    def _busy(self, busy: bool) -> None:
        for bid in (
            "detect",
            "flash_target",
            "flash_app",
            "flash_psoc6",
            "flash_probe",
            "update_fw",
        ):
            btn = self._main.query_one(f"#{bid}", Button)
            if bid in ("flash_target", "flash_app"):
                btn.disabled = busy or not (self._detect and self._detect.flashable)
            else:
                btn.disabled = busy

    def _set_progress(self, value: float) -> None:
        self._main.query_one("#progress", ProgressBar).update(progress=value)

    def _write_log(self, message: str) -> None:
        self._main.query_one("#log", RichLog).write(message)


def run(hex_path: Path | None = None, frequency: int = flasher.DEFAULT_FREQUENCY_HZ) -> None:
    FlasherApp(hex_path=hex_path, frequency=frequency).run()
