"""Textual TUI: flash the RA4M1 target bootloader, or the ESP-Prog-2's own firmware."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Input, Label, ProgressBar, RichLog, Static

from . import flasher
from .flasher import DetectResult, TargetState


class FlasherApp(App):
    """Detect the ESP-Prog-2 + RA4M1, flash the target bootloader, or reflash the probe."""

    TITLE = "ESP-Prog-2 Flasher"
    SUB_TITLE = "RA4M1 target  +  ESP-Prog-2 firmware"

    CSS = """
    #panel { padding: 1 2; height: auto; }
    #probe_status, #target_status, #serial_status, #fw_status { height: auto; padding: 0 1; }
    .ok { color: $success; }
    .warn { color: $warning; }
    .err { color: $error; }
    .row { height: auto; padding: 1 0 0 0; }
    .row Label { padding: 1 1 0 0; width: auto; }
    .row Button { margin: 0 1 0 0; }
    .section { color: $text-muted; padding: 1 1 0 1; text-style: bold; }
    ProgressBar { padding: 1 1; }
    RichLog { height: 1fr; border: round $primary; padding: 0 1; }
    """

    BINDINGS = [
        ("d", "detect", "Detect"),
        ("f", "flash_target", "Flash target"),
        ("p", "flash_probe", "Flash probe"),
        ("u", "update_fw", "Update fw"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, hex_path: Optional[Path] = None, frequency: int = flasher.DEFAULT_FREQUENCY_HZ):
        super().__init__()
        self._frequency = frequency
        self._detect: Optional[DetectResult] = None
        default = hex_path or flasher.find_default_hex()
        self._default_hex = str(default) if default else ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="panel"):
            yield Static("── Target (RA4M1 / UNO R4 Minima) ──", classes="section")
            yield Static("Probe:  …", id="probe_status")
            yield Static("Target: …", id="target_status")
            with Horizontal(classes="row"):
                yield Label("Bootloader (.hex):")
                yield Input(value=self._default_hex, placeholder="path to .hex", id="hex_input")
            with Horizontal(classes="row"):
                yield Button("Detect", id="detect", variant="primary")
                yield Button("Flash target", id="flash_target", variant="success", disabled=True)

            yield Static("── ESP-Prog-2 firmware (ESP32-S3) ──", classes="section")
            yield Static("Serial: …", id="serial_status")
            yield Static("Firmware: …", id="fw_status")
            with Horizontal(classes="row"):
                yield Button("Get official (JTAG)", id="update_fw", variant="primary")
                yield Button("Flash ESP-Prog-2", id="flash_probe", variant="warning")

            yield ProgressBar(total=100, show_eta=False, id="progress")
            yield Button("Quit", id="quit", variant="error")
        yield RichLog(id="log", markup=True, highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        self._log(f"Target: [b]{flasher.TARGET_TYPE}[/b] via pyOCD  •  "
                  f"Probe fw: [b]{flasher.PROBE_CHIP}[/b] via esptool")
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
        self._busy(True)
        self._set_progress(0)
        self._log(f"[b]Flashing target[/b] {hex_path.name} …")
        self.flash_target_worker(hex_path)

    def action_flash_probe(self) -> None:
        fw = flasher.find_probe_firmware()
        if fw is None:
            self._log("[red]No esp-prog2.bin found.[/red] Use 'Download latest' first.")
            return
        serial = flasher.find_probe_serial()
        if serial is None:
            self._log("[yellow]No ESP-Prog-2 serial port found. Plug it in and enter "
                      "download mode (hold BOOT, tap RESET).[/yellow]")
            return
        if not serial.in_download_mode:
            self._log(f"[yellow]{serial.device} looks like the running bridge (not download "
                      f"mode). If flashing fails, hold BOOT + tap RESET, then retry.[/yellow]")
        self._busy(True)
        self._set_progress(0)
        self._log(f"[b]Flashing ESP-Prog-2[/b] {fw.name} via {serial.device} …")
        self.flash_probe_worker(fw, serial.device)

    def action_update_fw(self) -> None:
        self._busy(True)
        self._log("[b]Downloading the OFFICIAL esp-prog2.bin (JTAG build)[/b] — saved separately as "
                  "esp-prog2-official-jtag.bin; it will NOT replace the bundled SWD build.")
        self.download_fw_worker()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handler = {
            "detect": self.action_detect,
            "flash_target": self.action_flash_target,
            "flash_probe": self.action_flash_probe,
            "update_fw": self.action_update_fw,
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
            result = DetectResult(TargetState.NO_PROBE, f"Detection error: {exc!r}")
        self.call_from_thread(self._apply_detect, result)

    @work(thread=True, exclusive=True, group="flash")
    def flash_target_worker(self, hex_path: Path) -> None:
        try:
            flasher.flash(
                hex_path,
                frequency=self._frequency,
                progress=lambda f: self.call_from_thread(self._set_progress, f * 100.0),
                log=lambda m: self.call_from_thread(self._log, m),
            )
            self.call_from_thread(self._flash_done, True, "Target flashed and reset.")
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._flash_done, False, f"Target flash failed: {exc!r}")

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
            self.call_from_thread(self._flash_done, False, f"ESP-Prog-2 flash failed: {exc!r}")

    @work(thread=True, exclusive=True, group="flash")
    def download_fw_worker(self) -> None:
        try:
            dest = flasher.download_probe_firmware(log=lambda m: self.call_from_thread(self._log, m))
            self.call_from_thread(self._download_done, True, f"Saved {dest}")
        except Exception as exc:  # noqa: BLE001
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
            widget.update(f"Serial: [b]{serial.device}[/b] (bridge running — hold BOOT + "
                          f"tap RESET to flash)")

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
        for bid in ("detect", "flash_target", "flash_probe", "update_fw"):
            btn = self.query_one(f"#{bid}", Button)
            if bid == "flash_target":
                btn.disabled = busy or not (self._detect and self._detect.flashable)
            else:
                btn.disabled = busy

    def _set_progress(self, value: float) -> None:
        self.query_one("#progress", ProgressBar).update(progress=value)

    def _log(self, message: str) -> None:
        self.query_one("#log", RichLog).write(message)


def run(hex_path: Optional[Path] = None, frequency: int = flasher.DEFAULT_FREQUENCY_HZ) -> None:
    FlasherApp(hex_path=hex_path, frequency=frequency).run()
