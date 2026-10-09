"""Textual TUI: flash the RA4M1 target bootloader/application, or the ESP-Prog-2's own firmware."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Select,
    Static,
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

logger = logging.getLogger(__name__)

# Sentinel value/label for the "type your own name" entry in the device Select.
# A KNOWN device's value is its name (present in DEVICE_CAN_IDS); this is the one
# option that unlocks the custom-name input and the editable boot CAN ID fields.
CUSTOM_DEVICE_VALUE: str = "__custom__"
CUSTOM_DEVICE_LABEL: str = "Custom…"

# Embassy bootloader reserves 32 KiB for itself, so its app slot starts here
# (vs. the 16 KiB C bootloader's flasher.APP_BASE_ADDRESS == 0x4000).
EMBASSY_APP_BASE_ADDRESS: Final[int] = 0x8000


def _parse_can_id(text: str) -> int:
    """Parse a TUI CAN ID accepting hex (``0x700``) or decimal; raise on bad input."""
    value = int(text, 0)
    if not 0 <= value <= MAX_CAN_ID:
        raise ValueError(f"CAN ID must be 0x000..0x{MAX_CAN_ID:03X}")
    return value


class _FilteredDirectoryTree(DirectoryTree):
    """Directory tree showing only directories and files with a given suffix + name filter."""

    name_filter: str = ""
    suffix_filter: str = ""

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        needle = self.name_filter.lower()
        return [
            p
            for p in paths
            if (p.is_dir() and not p.name.startswith("."))
            or (p.suffix.lower() == self.suffix_filter and needle in p.name.lower())
        ]


class FilePickerScreen(ModalScreen[Path | None]):
    """Modal file dialog: type a path, browse the tree, filter files by suffix + name.

    Shared by the bootloader (.hex) and application (.bin) pickers — only the
    suffix and dialog title differ between them.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    FilePickerScreen { align: center middle; }
    #picker_dialog {
        width: 90;
        max-width: 95%;
        height: 80%;
        background: $panel;
        border: round $primary;
        padding: 1 2;
    }
    #picker_dialog Input { margin: 0 0 1 0; }
    #picker_tree { height: 1fr; border: round $accent; }
    #picker_buttons { height: auto; align: right middle; padding: 1 0 0 0; }
    #picker_buttons Button { margin: 0 0 0 1; }
    """

    def __init__(self, start: Path, suffix: str, title: str) -> None:
        super().__init__()
        self._start = start
        self._suffix = suffix
        self._title = title
        self._chosen: Path | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="picker_dialog"):
            yield Input(value=str(self._start), placeholder="directory to browse", id="picker_path")
            yield Input(placeholder=f"filter {self._suffix} file names…", id="picker_filter")
            yield _FilteredDirectoryTree(self._start, id="picker_tree")
            with Horizontal(id="picker_buttons"):
                yield Button("Select", id="picker_select", variant="primary", disabled=True)
                yield Button("Cancel", id="picker_cancel")

    def on_mount(self) -> None:
        self.query_one(_FilteredDirectoryTree).suffix_filter = self._suffix
        self.query_one("#picker_dialog", Vertical).border_title = self._title

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "picker_path":
            return
        new_root = Path(event.value).expanduser()
        if new_root.is_dir():
            self.query_one(_FilteredDirectoryTree).path = new_root
        else:
            self.notify(f"Not a directory: {new_root}", severity="error")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "picker_filter":
            return
        tree = self.query_one(_FilteredDirectoryTree)
        tree.name_filter = event.value
        tree.reload()

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self._chosen = event.path
        self.query_one("#picker_select", Button).disabled = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "picker_select":
            self.dismiss(self._chosen)
        elif event.button.id == "picker_cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class FlasherApp(App):
    """Detect the ESP-Prog-2 + RA4M1, flash the target bootloader, or reflash the probe."""

    TITLE = "ESP-Prog-2 Flasher"
    SUB_TITLE = f"RA4M1 target  +  ESP-Prog-2 firmware  ·  v{__version__}"

    CSS = """
    Screen { background: $surface; }
    #panels { height: auto; padding: 1 2 0 2; }
    .panel {
        width: 1fr;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $primary;
    }
    #target_panel { margin: 0 1 0 0; }
    #right_column { width: 1fr; height: auto; }
    #app_panel, #psoc6_panel { margin: 1 0 0 0; }
    #probe_status, #target_status, #serial_status, #fw_status, #app_status, #psoc6_status {
        height: auto; padding: 0 0 1 0;
    }
    .ok { color: $success; text-style: bold; }
    .warn { color: $warning; text-style: bold; }
    .err { color: $error; text-style: bold; }
    .row { height: auto; padding: 1 0 0 0; }
    .row Label { padding: 1 1 0 0; width: auto; }
    .row Button { margin: 0 1 0 0; }
    #hex_input, #app_input, #psoc6_input { width: 1fr; margin: 0 1 0 0; }
    #boot_cmd_input, #boot_reply_input { width: 1fr; }
    #footer_row { height: auto; padding: 1 2; align: left middle; }
    #progress { width: 1fr; margin: 0 2 0 0; }
    RichLog {
        height: 1fr;
        min-height: 8;
        margin: 0 2 1 2;
        border: round $accent;
        background: $surface-darken-1;
        padding: 0 1;
    }
    """

    BINDINGS = [
        ("d", "detect", "Detect"),
        ("f", "flash_target", "Flash target"),
        ("a", "flash_app", "Flash app"),
        ("6", "flash_psoc6", "Flash PSoC 6"),
        ("p", "flash_probe", "Flash probe"),
        ("u", "update_fw", "Update fw"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, hex_path: Path | None = None, frequency: int = flasher.DEFAULT_FREQUENCY_HZ):
        super().__init__()
        self._frequency = frequency
        self._detect: DetectResult | None = None
        default = hex_path or flasher.find_default_hex()
        self._default_hex = str(default) if default else ""
        psoc6_default = flasher.find_default_psoc6_hex()
        self._default_psoc6_hex = str(psoc6_default) if psoc6_default else ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="panels"):
            with Vertical(id="target_panel", classes="panel"):
                yield Static("Probe:  …", id="probe_status")
                yield Static("Target: …", id="target_status")
                with Horizontal(classes="row"):
                    yield Label("Bootloader (.hex):")
                    yield Input(value=self._default_hex, placeholder="path to .hex", id="hex_input")
                    yield Button("Browse", id="browse_hex")
                with Horizontal(classes="row"):
                    yield Label("USB manufacturer:")
                    yield Input(value=DEFAULT_MANUFACTURER, id="mfr_input")
                with Horizontal(classes="row"):
                    yield Label("USB device:")
                    yield Select(
                        [(name, name) for name in DEVICE_NAMES]
                        + [(CUSTOM_DEVICE_LABEL, CUSTOM_DEVICE_VALUE)],
                        prompt="keep bootloader default",
                        id="prod_select",
                    )
                with Horizontal(classes="row", id="custom_name_row"):
                    yield Label("Custom device name:")
                    yield Input(placeholder="type a device name", id="custom_name_input")
                with Horizontal(classes="row"):
                    yield Label("Boot CAN host->boot:")
                    yield Input(
                        placeholder=f"blank = default 0x{DEFAULT_BOOT_CMD_ID:03X}",
                        id="boot_cmd_input",
                    )
                    yield Label("boot->host:")
                    yield Input(
                        placeholder=f"blank = default 0x{DEFAULT_BOOT_REPLY_ID:03X}",
                        id="boot_reply_input",
                    )
                with Horizontal(classes="row"):
                    yield Button("Detect", id="detect", variant="primary")
                    yield Button(
                        "Flash target", id="flash_target", variant="success", disabled=True
                    )

            with Vertical(id="right_column"):
                with Vertical(id="probe_panel", classes="panel"):
                    yield Static("Serial: …", id="serial_status")
                    yield Static("Firmware: …", id="fw_status")
                    with Horizontal(classes="row"):
                        yield Button("Get official (JTAG)", id="update_fw", variant="primary")
                        yield Button("Flash ESP-Prog-2", id="flash_probe", variant="warning")
                with Vertical(id="app_panel", classes="panel"):
                    yield Static(
                        "Application flashed to the RA4M1 over SWD, above the bootloader.",
                        id="app_status",
                    )
                    with Horizontal(classes="row"):
                        yield Label("Application (.bin):")
                        yield Input(placeholder="path to app .bin", id="app_input")
                        yield Button("Browse", id="browse_app")
                    with Horizontal(classes="row"):
                        yield Checkbox("Embassy bootloader", id="embassy_checkbox")
                    with Horizontal(classes="row"):
                        yield Button("Flash app", id="flash_app", variant="success", disabled=True)
                with Vertical(id="psoc6_panel", classes="panel"):
                    yield Static(
                        "PSoC 6 (IDU Radar) main flash, programmed over SWD.",
                        id="psoc6_status",
                    )
                    with Horizontal(classes="row"):
                        yield Label("PSoC 6 (.hex):")
                        yield Input(
                            value=self._default_psoc6_hex,
                            placeholder="path to PSoC 6 .hex",
                            id="psoc6_input",
                        )
                        yield Button("Browse", id="browse_psoc6")
                    with Horizontal(classes="row"):
                        yield Button("Flash PSoC 6", id="flash_psoc6", variant="success")

        with Horizontal(id="footer_row"):
            yield ProgressBar(total=100, show_eta=False, id="progress")
            yield Button("Quit", id="quit", variant="error")
        yield RichLog(id="log", markup=True, highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#target_panel").border_title = "Target (RA4M1 / UNO R4 Minima)"
        self.query_one("#probe_panel").border_title = "ESP-Prog-2 firmware (ESP32-S3)"
        self.query_one("#app_panel").border_title = "Application (RA4M1 over SWD)"
        self.query_one("#psoc6_panel").border_title = "PSoC 6 (IDU Radar over SWD)"
        self.query_one("#log", RichLog).border_title = "Log"
        # Custom-name input is revealed only when "Custom…" is selected.
        self.query_one("#custom_name_row").display = False
        self._log(
            f"Target: [b]{flasher.TARGET_TYPE}[/b] via pyOCD  •  "
            f"Probe fw: [b]{flasher.PROBE_CHIP}[/b] via esptool"
        )
        self._refresh_fw_status()
        self.action_detect()

    # ----- actions -----------------------------------------------------------

    def action_detect(self) -> None:
        self._set_progress(0)
        self.query_one("#probe_status", Static).update("Probe:  scanning…")
        self.query_one("#target_status", Static).update("Target: scanning…")
        self.detect_worker()
        self._refresh_serial_status()

    def action_flash_target(self) -> None:
        if not (self._detect and self._detect.flashable):
            self._log("[yellow]No RA4M1 target detected. Run Detect first.[/yellow]")
            return
        hex_path = Path(self.query_one("#hex_input", Input).value.strip())
        if not hex_path.is_file():
            self._log(f"[red]Bootloader not found:[/red] {hex_path}")
            return
        prod_value = self.query_one("#prod_select", Select).value
        if prod_value is Select.BLANK:
            product = None
        elif prod_value == CUSTOM_DEVICE_VALUE:
            product = self.query_one("#custom_name_input", Input).value.strip()
            if not product:
                self._log("[red]Enter a custom device name, or pick a device from the list.[/red]")
                return
        else:
            product = str(prod_value)
        mfr = self.query_one("#mfr_input", Input).value.strip()
        if product is not None and not mfr:
            self._log(
                "[red]Enter a USB manufacturer, or clear the device to keep "
                "bootloader defaults.[/red]"
            )
            return
        manufacturer = mfr if product is not None else None

        cmd_raw = self.query_one("#boot_cmd_input", Input).value.strip()
        reply_raw = self.query_one("#boot_reply_input", Input).value.strip()
        if (cmd_raw != "") != (reply_raw != ""):
            self._log(
                "[red]Set both boot CAN IDs (host->boot and boot->host), or leave "
                "both blank to keep bootloader defaults.[/red]"
            )
            return
        boot_cmd_id: int | None = None
        boot_reply_id: int | None = None
        if cmd_raw and reply_raw:
            if product is None:
                self._log(
                    "[red]Boot CAN IDs need a USB device selected; the block is only "
                    "written when a device is chosen.[/red]"
                )
                return
            try:
                boot_cmd_id = _parse_can_id(cmd_raw)
                boot_reply_id = _parse_can_id(reply_raw)
            except ValueError as exc:
                self._log(f"[red]Invalid boot CAN ID:[/red] {exc}")
                return

        self._busy(True)
        self._set_progress(0)
        self._log(f"[b]Flashing target[/b] {hex_path.name} …")
        if manufacturer and product:
            self._log(f"USB names: manufacturer=[b]{manufacturer}[/b] product=[b]{product}[/b]")
        if boot_cmd_id is not None and boot_reply_id is not None:
            self._log(
                f"Boot CAN IDs: host->boot=[b]0x{boot_cmd_id:03X}[/b] "
                f"boot->host=[b]0x{boot_reply_id:03X}[/b]"
            )
        self.flash_target_worker(hex_path, manufacturer, product, boot_cmd_id, boot_reply_id)

    def action_flash_probe(self) -> None:
        fw = flasher.find_probe_firmware()
        if fw is None:
            self._log("[red]No esp-prog2.bin found.[/red] Use 'Download latest' first.")
            return
        serial = flasher.find_probe_serial()
        if serial is None:
            self._log(
                "[yellow]No ESP-Prog-2 serial port found. Plug it in and enter "
                "download mode (hold BOOT, tap RESET).[/yellow]"
            )
            return
        if not serial.in_download_mode:
            self._log(
                f"[yellow]{serial.device} looks like the running bridge (not download "
                f"mode). If flashing fails, hold BOOT + tap RESET, then retry.[/yellow]"
            )
        self._busy(True)
        self._set_progress(0)
        self._log(f"[b]Flashing ESP-Prog-2[/b] {fw.name} via {serial.device} …")
        self.flash_probe_worker(fw, serial.device)

    def action_update_fw(self) -> None:
        self._busy(True)
        self._log(
            "[b]Downloading the OFFICIAL esp-prog2.bin (JTAG build)[/b] — saved separately as "
            "esp-prog2-official-jtag.bin; it will NOT replace the bundled SWD build."
        )
        self.download_fw_worker()

    def action_browse_hex(self) -> None:
        current = self.query_one("#hex_input", Input).value
        start = Path(current).expanduser().parent if current else Path.cwd()
        if not start.is_dir():
            start = Path.cwd()

        def apply_choice(chosen: Path | None) -> None:
            if chosen is not None:
                self.query_one("#hex_input", Input).value = str(chosen)

        self.push_screen(FilePickerScreen(start, ".hex", "Select bootloader .hex"), apply_choice)

    def action_flash_app(self) -> None:
        if not (self._detect and self._detect.flashable):
            self._log("[yellow]No RA4M1 target detected. Run Detect first.[/yellow]")
            return
        bin_path = Path(self.query_one("#app_input", Input).value.strip())
        if not bin_path.is_file():
            self._log(f"[red]Application binary not found:[/red] {bin_path}")
            return
        base_address = (
            EMBASSY_APP_BASE_ADDRESS
            if self.query_one("#embassy_checkbox", Checkbox).value
            else flasher.APP_BASE_ADDRESS
        )

        self._busy(True)
        self._set_progress(0)
        self._log(f"[b]Flashing application[/b] {bin_path.name} …")
        self.flash_app_worker(bin_path, base_address)

    def action_flash_psoc6(self) -> None:
        hex_path = Path(self.query_one("#psoc6_input", Input).value.strip())
        if not hex_path.is_file():
            self._log(f"[red]PSoC 6 hex not found:[/red] {hex_path}")
            return
        self._busy(True)
        self._set_progress(0)
        self._log(f"[b]Flashing PSoC 6[/b] {hex_path.name} …")
        self.flash_psoc6_worker(hex_path)

    def action_browse_psoc6(self) -> None:
        current = self.query_one("#psoc6_input", Input).value
        start = Path(current).expanduser().parent if current else Path.cwd()
        if not start.is_dir():
            start = Path.cwd()

        def apply_choice(chosen: Path | None) -> None:
            if chosen is not None:
                self.query_one("#psoc6_input", Input).value = str(chosen)

        self.push_screen(FilePickerScreen(start, ".hex", "Select PSoC 6 .hex"), apply_choice)

    def action_browse_app(self) -> None:
        current = self.query_one("#app_input", Input).value
        start = Path(current).expanduser().parent if current else Path.cwd()
        if not start.is_dir():
            start = Path.cwd()

        def apply_choice(chosen: Path | None) -> None:
            if chosen is not None:
                self.query_one("#app_input", Input).value = str(chosen)

        self.push_screen(FilePickerScreen(start, ".bin", "Select application .bin"), apply_choice)

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
        custom_row = self.query_one("#custom_name_row")
        cmd_input = self.query_one("#boot_cmd_input", Input)
        reply_input = self.query_one("#boot_reply_input", Input)

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
        handler = {
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
        }.get(event.button.id)
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

    @work(thread=True, exclusive=True, group="flash")
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
                log=lambda m: self.call_from_thread(self._log, m),
                manufacturer=manufacturer,
                product=product,
                boot_cmd_id=boot_cmd_id,
                boot_reply_id=boot_reply_id,
            )
            self.call_from_thread(self._flash_done, True, "Target flashed and reset.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Flash target worker failed")
            self.call_from_thread(self._flash_done, False, f"Target flash failed: {exc!r}")

    @work(thread=True, exclusive=True, group="flash")
    def flash_app_worker(self, bin_path: Path, base_address: int) -> None:
        try:
            flasher.flash_app(
                bin_path,
                base_address=base_address,
                frequency=self._frequency,
                progress=lambda f: self.call_from_thread(self._set_progress, f * 100.0),
                log=lambda m: self.call_from_thread(self._log, m),
            )
            self.call_from_thread(self._flash_done, True, "Application flashed and reset.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Flash app worker failed")
            self.call_from_thread(self._flash_done, False, f"Application flash failed: {exc!r}")

    @work(thread=True, exclusive=True, group="flash")
    def flash_psoc6_worker(self, hex_path: Path) -> None:
        try:
            flasher.flash_psoc6(
                hex_path,
                frequency=self._frequency,
                progress=lambda f: self.call_from_thread(self._set_progress, f * 100.0),
                log=lambda m: self.call_from_thread(self._log, m),
            )
            self.call_from_thread(self._flash_done, True, "PSoC 6 flashed and reset.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Flash PSoC 6 worker failed")
            self.call_from_thread(self._flash_done, False, f"PSoC 6 flash failed: {exc!r}")

    @work(thread=True, exclusive=True, group="flash")
    def flash_probe_worker(self, fw: Path, port: str) -> None:
        try:
            flasher.flash_probe(
                fw,
                port=port,
                progress=lambda f: self.call_from_thread(self._set_progress, f),
                log=lambda m: self.call_from_thread(self._log, m),
            )
            self.call_from_thread(self._flash_done, True, "ESP-Prog-2 firmware flashed.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Flash probe worker failed")
            self.call_from_thread(self._flash_done, False, f"ESP-Prog-2 flash failed: {exc!r}")

    @work(thread=True, exclusive=True, group="flash")
    def download_fw_worker(self) -> None:
        try:
            dest = flasher.download_probe_firmware(
                log=lambda m: self.call_from_thread(self._log, m)
            )
            self.call_from_thread(self._download_done, True, f"Saved {dest}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Download firmware worker failed")
            self.call_from_thread(self._download_done, False, f"Download failed: {exc!r}")

    # ----- UI updates (main thread) -----------------------------------------

    def _apply_detect(self, result: DetectResult) -> None:
        self._detect = result
        probe = self.query_one("#probe_status", Static)
        target = self.query_one("#target_status", Static)

        if result.state is TargetState.NO_PROBE:
            probe.set_classes("err")
            probe.update("Probe:  [b]not found[/b]")
        else:
            label = result.probe.label if result.probe else "unknown"
            probe.set_classes("ok")
            probe.update(f"Probe:  [b]{label}[/b]")

        cls, text = {
            TargetState.NO_PROBE: ("err", "Target: —"),
            TargetState.NO_TARGET: ("warn", "Target: [b]no RA4M1 over SWD[/b]"),
            TargetState.CONNECTED: ("ok", "Target: [b]RA4M1 connected[/b]"),
            TargetState.CONNECTED_BLANK: ("ok", "Target: [b]RA4M1 connected (blank)[/b]"),
        }[result.state]
        target.set_classes(cls)
        target.update(text)
        self.query_one("#flash_target", Button).disabled = not result.flashable
        self.query_one("#flash_app", Button).disabled = not result.flashable
        self._log(result.detail)

    def _refresh_serial_status(self) -> None:
        serial = flasher.find_probe_serial()
        widget = self.query_one("#serial_status", Static)
        if serial is None:
            widget.set_classes("warn")
            widget.update("Serial: [b]no ESP-Prog-2 serial port[/b]")
        elif serial.in_download_mode:
            widget.set_classes("ok")
            widget.update(f"Serial: [b]{serial.device}[/b] (download mode, pid 0x{serial.pid:04X})")
        else:
            widget.set_classes("warn")
            widget.update(
                f"Serial: [b]{serial.device}[/b] (bridge running — hold BOOT + tap RESET to flash)"
            )

    def _refresh_fw_status(self) -> None:
        fw = flasher.find_probe_firmware()
        widget = self.query_one("#fw_status", Static)
        if fw is None:
            widget.set_classes("warn")
            widget.update("Firmware: [b]esp-prog2.bin missing[/b] — use Download latest")
        else:
            widget.set_classes("ok")
            widget.update(f"Firmware: [b]{fw.name}[/b] ({fw.stat().st_size} bytes)")

    def _flash_done(self, ok: bool, message: str) -> None:
        self._busy(False)
        if ok:
            self._set_progress(100)
            self._log(f"[green]{message}[/green]")
        else:
            self._log(f"[red]{message}[/red]")

    def _download_done(self, ok: bool, message: str) -> None:
        self._busy(False)
        self._refresh_fw_status()
        self._log(f"[{'green' if ok else 'red'}]{message}[/]")

    def _busy(self, busy: bool) -> None:
        for bid in (
            "detect",
            "flash_target",
            "flash_app",
            "flash_psoc6",
            "flash_probe",
            "update_fw",
        ):
            btn = self.query_one(f"#{bid}", Button)
            if bid in ("flash_target", "flash_app"):
                btn.disabled = busy or not (self._detect and self._detect.flashable)
            else:
                btn.disabled = busy

    def _set_progress(self, value: float) -> None:
        self.query_one("#progress", ProgressBar).update(progress=value)

    def _log(self, message: str) -> None:
        self.query_one("#log", RichLog).write(message)


def run(hex_path: Path | None = None, frequency: int = flasher.DEFAULT_FREQUENCY_HZ) -> None:
    FlasherApp(hex_path=hex_path, frequency=frequency).run()
