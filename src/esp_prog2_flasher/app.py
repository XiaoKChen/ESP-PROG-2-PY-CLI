"""Textual TUI: a TTSSH-style dashboard to flash the RA4M1, the PSoC 6, a SOLO board or the probe.

FlasherApp opens on the dashboard (tui/screens.py): every flash target grouped by chip and
link, this session's runs under Recent, and the selection's details and commands. Enter
runs the selection's wizard (tui/wizard.py, steps from tui/flows.py), which ends on a confirm
summary; "/" opens the command popup. This module holds the session state and runs every
job in a thread worker with the same flasher calls cli.py makes. cli.py launches it when no
flags are given.

Owns:        the session state (Settings, detect results, serial port, recents, log lines,
             status message and activity), when it is loaded from and saved to the state file,
             the command dispatch, the job workers and the header/details text derived from that
             state.
Not here:    USB/serial/SWD access and all flashing logic (flasher.py); the state file's format
             and I/O (tui/state.py); the screens, wizard steps, browser and command table (tui/);
             config-block encoding (dfu_config.py).
Invariants:  no flash, erase or provision starts before its wizard's confirm returns "y".
             Flashing the RA4M1 needs a Detect that found a flashable RA4M1. A SOLO port is
             never guessed: the user picks or types it. No write starts while one runs.
Concurrency: flasher calls run in Textual thread workers (@work(thread=True)); results and
             progress reach the UI only via call_from_thread. Every write and serial read
             runs in the exclusive "flash" worker group, and no new one (nor a Detect) starts
             while one is running.
Depends on:  flasher.py, dfu_config.py, board_profile.py, tui/, textual, rich.
See also:    cli.py, tests/test_app.py
"""

from __future__ import annotations

import itertools
import logging
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Final

from rich.console import RenderableType
from rich.markup import escape
from textual import work
from textual.app import App
from textual.binding import Binding, BindingType
from textual.timer import Timer

from . import __version__, flasher
from .board_profile import BoardProfile, describe_read_profile
from .dfu_config import DEFAULT_BOOT_CMD_ID, DEFAULT_BOOT_REPLY_ID
from .flasher import DetectResult, TargetState
from .tui import flows, state
from .tui.browser import BrowserScreen
from .tui.commands import GLOBAL_COMMANDS, Command, CommandAction
from .tui.screens import ChromeScreen, DashboardScreen, HelpScreen, LogScreen, RunScreen
from .tui.targets import MAX_RECENTS, Operation, RecentRun, Target, relative_time
from .tui.theme import STATUS_SYMBOLS, THEMES, StatusLevel, theme_name_for_terminal
from .tui.widgets import ChromeState, Field, GlobalCommand, StatusPosted
from .tui.wizard import Answers, WizardScreen

logger = logging.getLogger(__name__)

STATUS_LIFETIME_S: Final[float] = 5.0
_SPINNER_INTERVAL_S: Final[float] = 0.1
_FLASH_GROUP: Final[str] = "flash"
_PROBE_GROUP: Final[str] = "probe"
_DETECT_TEXT: Final[dict[TargetState, tuple[StatusLevel, str]]] = {
    TargetState.NO_PROBE: ("err", "no probe found"),
    TargetState.NO_TARGET: ("warn", "probe found, no target over SWD"),
    TargetState.CONNECTED: ("ok", "connected"),
    TargetState.CONNECTED_BLANK: ("ok", "connected (blank flash)"),
}
_HEADER_TARGET: Final[dict[TargetState, tuple[StatusLevel | None, str]]] = {
    TargetState.NO_PROBE: (None, "RA4M1 —"),
    TargetState.NO_TARGET: ("warn", "no RA4M1"),
    TargetState.CONNECTED: ("ok", "RA4M1"),
    TargetState.CONNECTED_BLANK: ("ok", "RA4M1 blank"),
}
_GLOBAL_ACTIONS: Final[frozenset[CommandAction]] = frozenset(c.action for c in GLOBAL_COMMANDS)


class FlasherApp(App[None]):
    """Flash the RA4M1 (bootloader/config, app), the PSoC 6, a SOLO board or the ESP-Prog-2."""

    TITLE = "ESP-Prog-2 Flasher"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(
        self,
        hex_path: Path | None = None,
        frequency: int = flasher.DEFAULT_FREQUENCY_HZ,
        state_path: Path | None = None,
        no_state: bool = False,
    ):
        super().__init__()
        self._frequency = frequency
        boot_hex = hex_path or flasher.find_default_hex()
        psoc6_hex = flasher.find_default_psoc6_hex()
        self._defaults = flows.Settings(
            boot_hex=str(boot_hex) if boot_hex else "",
            psoc6_hex=str(psoc6_hex) if psoc6_hex else "",
        )
        self._settings = self._defaults
        self._state_path = None if no_state else state_path or state.default_state_path()
        self._state_writable = True  # False: leave a newer-format file alone
        self._state_save_failed = False
        self._startup_notice: tuple[StatusLevel, str] | None = None
        self._recents: list[RecentRun] = []
        saved_selection, saved_collapsed = self._load_saved_state(hex_path)
        self._chrome = ChromeState(version=__version__)
        self._detect: DetectResult | None = None
        self._psoc6_detect: DetectResult | None = None
        self._serial: flasher.ProbeSerial | None = None
        self._download_candidates: list[flasher.SerialPortInfo] = []
        self._run_ids = itertools.count(len(self._recents) + 1)
        self._log_lines: list[str] = []
        self._activities: dict[str, str] = {}  # worker group -> what it is doing
        self._spinner: Timer | None = None
        self._status_ids = itertools.count(1)
        self._status_id = 0
        self._job: tuple[Target, Operation, str] | None = None  # target, operation, detail
        self._run_screen: RunScreen | None = None
        self._dashboard = DashboardScreen(self._chrome, self, saved_collapsed, saved_selection)
        for theme in THEMES:
            self.register_theme(theme)
        self.theme = theme_name_for_terminal(os.environ)

    def get_default_screen(self) -> DashboardScreen:
        return self._dashboard

    def get_theme_variable_defaults(self) -> dict[str, str]:
        return dict(THEMES[0].variables)

    def _load_saved_state(self, hex_path: Path | None) -> tuple[str | None, tuple[str, ...]]:
        """Apply the saved state to the session; return the saved selection and folded groups."""
        if self._state_path is None:
            return None, ()
        loaded = state.load_state(self._state_path)
        self._state_writable = loaded.overwritable
        if loaded.problem is not None:
            self._startup_notice = ("warn", loaded.problem)
        if loaded.state is None:
            return None, ()
        settings, missing = state.restore_settings(loaded.state.settings, self._defaults)
        if hex_path is not None:  # an explicit --hex beats the saved bootloader image
            settings = replace(settings, boot_hex=str(hex_path))
        self._settings = settings
        self._recents = list(loaded.state.recents)
        if missing:
            self._startup_notice = (
                "warn",
                f"Saved path no longer exists, using the default: {', '.join(missing)}",
            )
        return loaded.state.selected, loaded.state.collapsed

    def exit(
        self,
        result: None = None,
        return_code: int = 0,
        message: RenderableType | None = None,
    ) -> None:
        self._save_state()
        super().exit(result, return_code, message)

    def on_mount(self) -> None:
        if self._startup_notice is not None:
            self._post_status(*self._startup_notice)
        self._write_log(
            f"Target: [b]{flasher.TARGET_TYPE}[/b] via pyOCD  •  "
            f"Probe fw: [b]{flasher.PROBE_CHIP}[/b] via esptool"
        )
        self._detect_ra4m1(announce=False)

    # ----- dashboard host --------------------------------------------------------------

    @property
    def recents(self) -> Sequence[RecentRun]:
        return self._recents

    @property
    def settings(self) -> flows.Settings:
        return self._settings

    def target_note(self, target: Target) -> str:
        return flows.target_note(target, self._settings, flasher.find_probe_firmware())

    def target_fields(self, target: Target) -> list[Field]:
        fields = flows.target_fields(target, self._settings, flasher.find_probe_firmware())
        if target.kind in ("boot", "app"):
            fields.append(("Target", self._detect_text(self._detect, "RA4M1")))
        elif target.kind == "psoc6":
            fields.append(("Target", self._detect_text(self._psoc6_detect, "PSoC 6")))
        elif target.kind == "probe":
            fields.append(("Serial", self._serial_text()))
        last = next((r for r in self._recents if r.target == target), None)
        if last is not None:
            symbol = STATUS_SYMBOLS["ok" if last.ok else "err"]
            when = relative_time(last.at, datetime.now())
            fields.append(("Last", f"{symbol} {last.operation} {when}"))
        return fields

    def run_primary(self, target: Target) -> None:
        if self._refuse_if_busy():
            return
        if target.kind == "boot" and self._ra4m1_ready():
            self._run_flow(flows.boot_flash_flow(self._settings), target, "flash")
        elif target.kind == "app" and self._ra4m1_ready():
            self._run_flow(flows.app_flash_flow(self._settings), target, "flash")
        elif target.kind == "psoc6":
            self._run_flow(flows.psoc6_flash_flow(self._settings), target, "flash")
        elif target.kind == "solo" and target.product is not None:
            ports = flasher.list_serial_ports()
            flow = flows.solo_flash_flow(self._settings, target.product, ports)
            self._run_flow(flow, target, "flash")
        elif target.kind == "probe":
            self._flash_probe(target)

    def run_recent(self, recent: RecentRun) -> None:
        reruns: dict[Operation, Callable[[Target], None]] = {
            "flash": self.run_primary,
            "provision": self._provision,
            "read": self._read_config,
            "download": self._get_official,
        }
        reruns[recent.operation](recent.target)

    def run_command(self, command: Command, target: Target | None) -> None:
        if command.action in _GLOBAL_ACTIONS or target is None:
            self._run_global(command)
            return
        handlers: dict[CommandAction, Callable[[Target], None]] = {
            "detect": self._detect_target,
            "pick_file": self._pick_file,
            "edit_config": self._edit_config,
            "pick_port": self._pick_port,
            "bundle": self._pick_bundle,
            "solo_app": self._pick_solo_app,
            "toggle_embassy": self._toggle_embassy,
            "toggle_migrate": self._toggle_migrate,
            "provision": self._provision,
            "read_config": self._read_config,
            "get_official": self._get_official,
        }
        handlers[command.action](target)

    def on_global_command(self, message: GlobalCommand) -> None:
        self._run_global(message.command)

    def on_status_posted(self, message: StatusPosted) -> None:
        self._post_status(message.level, message.text)

    def view_changed(self) -> None:
        self._save_state()

    def _run_global(self, command: Command) -> None:
        if command.action == "forget":
            self._forget_state()
        elif command.action == "log":
            self.push_screen(LogScreen(self._chrome, "Session log", self._log_lines))
        elif command.action == "help":
            self.push_screen(HelpScreen(self._chrome))
        elif command.action == "quit":
            self.exit()

    # ----- commands --------------------------------------------------------------------

    def _refuse_if_busy(self) -> bool:
        if not self._flash_running():
            return False
        self._post_status("warn", "Busy: wait for the current operation to finish.")
        return True

    def _ra4m1_ready(self) -> bool:
        if self._detect is not None and self._detect.flashable:
            return True
        self._post_status("warn", "No RA4M1 target detected. Run / d (detect) first.")
        return False

    def _detect_target(self, target: Target) -> None:
        if self._refuse_if_busy():
            return
        if target.kind == "psoc6":
            self._set_activity(_PROBE_GROUP, "Detecting the PSoC 6 over SWD…")
            self.detect_psoc6_worker()
        else:
            self._detect_ra4m1(announce=True)

    def _detect_ra4m1(self, announce: bool) -> None:
        self._set_activity(_PROBE_GROUP, "Detecting the probe and RA4M1…")
        self.detect_worker(announce)
        self._refresh_serial()

    def _pick_file(self, target: Target) -> None:
        kind = next((k for k in flows.IMAGE_KINDS if k == target.kind), None)
        if kind is None:
            return

        def picked(path: Path | None) -> None:
            if path is not None:
                self._update_settings(flows.with_image(self._settings, kind, str(path)))
                self._post_status("ok", f"Image: {path.name}")

        browser = BrowserScreen(self._chrome, flows.image_browse(self._settings, kind))
        self.push_screen(browser, picked)

    def _edit_config(self, target: Target) -> None:
        if target.kind == "boot":
            self._run_edit(flows.boot_config_flow(self._settings), "Config block updated")
        else:
            self._run_edit(flows.solo_manufacturer_flow(self._settings), "Manufacturer updated")

    def _pick_port(self, _target: Target) -> None:
        flow = flows.port_flow(self._settings, flasher.list_serial_ports())
        self._run_edit(flow, "Board port set")

    def _pick_bundle(self, _target: Target) -> None:
        self._run_edit(flows.bundle_flow(self._settings), "Bundle source set")

    def _pick_solo_app(self, _target: Target) -> None:
        self._run_edit(flows.solo_app_flow(self._settings), "App image set")

    def _toggle_embassy(self, _target: Target) -> None:
        self._update_settings(replace(self._settings, embassy=not self._settings.embassy))
        loader = "Embassy" if self._settings.embassy else "C"
        self._post_status(
            "ok", f"App base 0x{self._settings.app_base_address:X} ({loader} bootloader)"
        )

    def _toggle_migrate(self, _target: Target) -> None:
        self._update_settings(replace(self._settings, solo_migrate=not self._settings.solo_migrate))
        state = "on: erase the stock leftovers first" if self._settings.solo_migrate else "off"
        self._post_status("ok", f"Migrate from stock {state}")

    def _provision(self, target: Target) -> None:
        if self._refuse_if_busy() or target.product is None:
            return
        ports = flasher.list_serial_ports()
        flow = flows.solo_provision_flow(self._settings, target.product, ports)
        self._run_flow(flow, target, "provision")

    def _read_config(self, target: Target) -> None:
        if self._refuse_if_busy() or target.product is None:
            return
        ports = flasher.list_serial_ports()
        self._run_flow(flows.solo_read_flow(self._settings, target.product, ports), target, "read")

    def _get_official(self, target: Target) -> None:
        if self._refuse_if_busy():
            return
        self._begin_job(
            target,
            "download",
            "official image",
            "Get the official probe firmware",
            "Downloading the OFFICIAL esp-prog2.bin (JTAG build)",
        )
        self._write_log(
            "Saved separately as esp-prog2-official-jtag.bin; it will NOT replace the bundled "
            "SWD build."
        )
        self.download_fw_worker()

    def _flash_probe(self, target: Target) -> None:
        firmware = flasher.find_probe_firmware()
        if firmware is None:
            self._post_status("err", "No esp-prog2.bin found. Get the official image with / g.")
            return
        ports = flasher.list_serial_ports()
        self._run_flow(flows.probe_flash_flow(self._settings, firmware, ports), target, "flash")

    def _run_flow(self, flow: flows.JobFlow, target: Target, operation: Operation) -> None:
        """Walk the flow's wizard; on its final answer, keep the settings and start the job."""

        def done(answers: Answers | None) -> None:
            if answers is None:
                self._post_status("warn", f"{flow.spec.title} cancelled.")
                return
            settings, job = flow.resolve(answers)
            self._update_settings(settings)
            self._start_job(job, target, operation)

        self.push_screen(WizardScreen(self._chrome, flow.spec), done)

    def _run_edit(self, flow: flows.EditFlow, done_message: str) -> None:
        def done(answers: Answers | None) -> None:
            if answers is not None:
                self._update_settings(flow.resolve(answers))
                self._post_status("ok", done_message)

        self.push_screen(WizardScreen(self._chrome, flow.spec), done)

    def _update_settings(self, settings: flows.Settings) -> None:
        self._settings = flows.remember_picked_files(self._settings, settings)
        self._dashboard.rebuild()
        self._save_state()

    def _save_state(self) -> None:
        if self._state_path is None or not self._state_writable:
            return
        selected, collapsed = self._dashboard.view_state
        snapshot = state.SavedState(self._settings, tuple(self._recents), selected, collapsed)
        try:
            state.save_state(self._state_path, snapshot)
        except OSError as exc:
            logger.warning("Could not save state to %s", self._state_path, exc_info=True)
            if not self._state_save_failed:
                self._state_save_failed = True
                self._post_status("warn", f"Cannot save state: {exc.strerror or exc}")

    def _forget_state(self) -> None:
        if self._state_path is None:
            self._post_status("warn", "Saved state is off (--no-state): nothing to forget.")
            return
        try:
            state.forget_state(self._state_path)
        except OSError as exc:
            self._post_status("err", f"Cannot delete the saved state: {exc.strerror or exc}")
            return
        self._state_writable = True
        self._settings = self._defaults
        self._recents = []
        self._dashboard.reset_view()
        self._post_status("ok", "Saved state forgotten; defaults restored.")

    # ----- jobs ------------------------------------------------------------------------

    def _start_job(self, job: flows.Job, target: Target, operation: Operation) -> None:
        if self._refuse_if_busy():
            return
        title = flows.summary(job).title
        match job:
            case flows.BootFlashJob():
                self._begin_job(
                    target,
                    operation,
                    job.hex_path.name,
                    title,
                    f"Flashing target {job.hex_path.name}",
                )
                if job.manufacturer and job.product:
                    self._write_log(
                        f"USB names: manufacturer=[b]{escape(job.manufacturer)}[/b] "
                        f"product=[b]{escape(job.product)}[/b]"
                    )
                if job.boot_cmd_id is not None and job.boot_reply_id is not None:
                    self._write_log(
                        f"Boot CAN IDs: host->boot=[b]0x{job.boot_cmd_id:03X}[/b] "
                        f"boot->host=[b]0x{job.boot_reply_id:03X}[/b]"
                    )
                self.flash_target_worker(job)
            case flows.AppFlashJob():
                name = job.bin_path.name
                self._begin_job(target, operation, name, title, f"Flashing application {name}")
                self.flash_app_worker(job.bin_path, job.base_address)
            case flows.Psoc6FlashJob():
                name = job.hex_path.name
                self._begin_job(target, operation, name, title, f"Flashing PSoC 6 {name}")
                self.flash_psoc6_worker(job.hex_path)
            case flows.SoloFlashJob():
                label = f"Flashing {job.profile.product} via {job.port}"
                self._begin_job(target, operation, job.port, title, label)
                self.flash_solo_worker(job)
            case flows.SoloProvisionJob():
                label = f"Provisioning cancfg on {job.port}"
                self._begin_job(target, operation, f"{job.port} · provision", title, label)
                self.provision_solo_worker(job.port, job.manufacturer, job.profile)
            case flows.SoloReadJob():
                label = f"Reading cancfg from {job.port}"
                self._begin_job(target, operation, f"{job.port} · read", title, label)
                self.read_solo_config_worker(job.port)
            case flows.ProbeFlashJob():
                if not job.in_download_mode:
                    self._write_log(
                        f"[yellow]{escape(job.port)} is the running bridge (not download mode). "
                        "If flashing fails, hold BOOT + tap RESET, then retry.[/yellow]"
                    )
                label = f"Flashing ESP-Prog-2 {job.firmware.name} via {job.port}"
                self._begin_job(target, operation, job.port, title, label)
                self.flash_probe_worker(job.firmware, job.port)

    def _begin_job(
        self, target: Target, operation: Operation, detail: str, title: str, label: str
    ) -> None:
        self._job = (target, operation, detail)
        self._run_screen = RunScreen(self._chrome, title, label)
        self.push_screen(self._run_screen)
        self._set_activity(_FLASH_GROUP, label)
        self._write_log(f"[b]{escape(label)}[/b] …")

    def _job_done(self, ok: bool, message: str) -> None:
        self._clear_activity(_FLASH_GROUP)
        self._write_log(f"[{'green' if ok else 'red'}]{escape(message)}[/]")
        if self._job is not None:
            target, operation, detail = self._job
            recent = RecentRun(
                next(self._run_ids), target, operation, detail, ok, message, datetime.now()
            )
            self._recents = [recent, *self._recents][:MAX_RECENTS]
            self._save_state()
        self._job = None
        run_screen, self._run_screen = self._run_screen, None
        if run_screen is not None:
            run_screen.finish()
            if self.screen is run_screen:
                self.pop_screen()
        self._post_status("ok" if ok else "err", message)
        self._dashboard.rebuild()

    def _solo_config_read(self, node: flasher.NodeConfig) -> None:
        config = node.config
        if config is None:
            message = (
                "Node config: unprovisioned/invalid (node uses defaults "
                f"0x{DEFAULT_BOOT_CMD_ID:03X}/0x{DEFAULT_BOOT_REPLY_ID:03X})"
            )
        else:
            message = (
                f"Node config: {config.manufacturer} / {config.product}, "
                f"host->boot=0x{config.boot_cmd_id:03X} boot->host=0x{config.boot_reply_id:03X}"
            )
        self._job_done(True, f"{message}; {describe_read_profile(node.profile)}")

    def _flash_running(self) -> bool:
        return any(w.group == _FLASH_GROUP and not w.is_finished for w in self.workers)

    # ----- workers ---------------------------------------------------------------------

    @work(thread=True, exclusive=True, group=_PROBE_GROUP)
    def detect_worker(self, announce: bool) -> None:
        try:
            result = flasher.detect(frequency=self._frequency)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Detection worker failed")
            result = DetectResult(TargetState.NO_PROBE, f"Detection error: {exc!r}")
        self.call_from_thread(self._apply_detect, result, announce)

    @work(thread=True, exclusive=True, group=_PROBE_GROUP)
    def detect_psoc6_worker(self) -> None:
        try:
            result = flasher.detect_psoc6(frequency=self._frequency)
        except Exception as exc:  # noqa: BLE001
            logger.exception("PSoC 6 detection worker failed")
            result = DetectResult(TargetState.NO_PROBE, f"Detection error: {exc!r}")
        self.call_from_thread(self._apply_psoc6_detect, result)

    @work(thread=True, exclusive=True, group=_FLASH_GROUP)
    def flash_target_worker(self, job: flows.BootFlashJob) -> None:
        try:
            flasher.flash(
                job.hex_path,
                frequency=self._frequency,
                progress=lambda f: self.call_from_thread(self._set_progress, f * 100.0),
                log=self._worker_log,
                manufacturer=job.manufacturer,
                product=job.product,
                boot_cmd_id=job.boot_cmd_id,
                boot_reply_id=job.boot_reply_id,
            )
            self.call_from_thread(self._job_done, True, "Target flashed and reset.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Flash target worker failed")
            self.call_from_thread(self._job_done, False, f"Target flash failed: {exc!r}")

    @work(thread=True, exclusive=True, group=_FLASH_GROUP)
    def flash_app_worker(self, bin_path: Path, base_address: int) -> None:
        try:
            flasher.flash_app(
                bin_path,
                base_address=base_address,
                frequency=self._frequency,
                progress=lambda f: self.call_from_thread(self._set_progress, f * 100.0),
                log=self._worker_log,
            )
            self.call_from_thread(self._job_done, True, "Application flashed and reset.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Flash app worker failed")
            self.call_from_thread(self._job_done, False, f"Application flash failed: {exc!r}")

    @work(thread=True, exclusive=True, group=_FLASH_GROUP)
    def flash_psoc6_worker(self, hex_path: Path) -> None:
        try:
            flasher.flash_psoc6(
                hex_path,
                frequency=self._frequency,
                progress=lambda f: self.call_from_thread(self._set_progress, f * 100.0),
                log=self._worker_log,
            )
            self.call_from_thread(self._job_done, True, "PSoC 6 flashed and reset.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Flash PSoC 6 worker failed")
            self.call_from_thread(self._job_done, False, f"PSoC 6 flash failed: {exc!r}")

    @work(thread=True, exclusive=True, group=_FLASH_GROUP)
    def flash_probe_worker(self, fw: Path, port: str) -> None:
        try:
            flasher.flash_probe(
                fw,
                port=port,
                progress=lambda f: self.call_from_thread(self._set_progress, f),
                log=self._worker_log,
            )
            self.call_from_thread(self._job_done, True, "ESP-Prog-2 firmware flashed.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Flash probe worker failed")
            self.call_from_thread(self._job_done, False, f"ESP-Prog-2 flash failed: {exc!r}")

    @work(thread=True, exclusive=True, group=_FLASH_GROUP)
    def download_fw_worker(self) -> None:
        try:
            dest = flasher.download_probe_firmware(log=self._worker_log)
            self.call_from_thread(self._job_done, True, f"Saved {dest}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Download firmware worker failed")
            self.call_from_thread(self._job_done, False, f"Download failed: {exc!r}")

    @work(thread=True, exclusive=True, group=_FLASH_GROUP)
    def flash_solo_worker(self, job: flows.SoloFlashJob) -> None:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                bundle = job.bundle_dir or flasher.fetch_solo_bundle(
                    job.tag, Path(tmp), log=self._worker_log
                )
                flasher.flash_solo(
                    job.profile,
                    job.port,
                    bundle,
                    app=job.app,
                    migrate_stock=job.migrate_stock,
                    manufacturer=job.manufacturer,
                    progress=lambda f: self.call_from_thread(self._set_progress, f),
                    log=self._worker_log,
                )
            self.call_from_thread(
                self._job_done, True, f"{job.profile.product} flashed and rebooted."
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("SOLO flash worker failed")
            self.call_from_thread(self._job_done, False, f"SOLO flash failed: {exc}")

    @work(thread=True, exclusive=True, group=_FLASH_GROUP)
    def provision_solo_worker(self, port: str, manufacturer: str, profile: BoardProfile) -> None:
        boot_cmd_id, boot_reply_id = profile.boot_ids
        try:
            flasher.provision_node(
                port,
                manufacturer,
                profile.product,
                boot_cmd_id=boot_cmd_id,
                boot_reply_id=boot_reply_id,
                progress=lambda f: self.call_from_thread(self._set_progress, f),
                log=self._worker_log,
                # A SOLO product rewrites the whole cancfg sector, so keep its board profile.
                profile=profile,
            )
            self.call_from_thread(
                self._job_done, True, f"cancfg provisioned for {profile.product}."
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("SOLO provision worker failed")
            self.call_from_thread(self._job_done, False, f"Node provisioning failed: {exc}")

    @work(thread=True, exclusive=True, group=_FLASH_GROUP)
    def read_solo_config_worker(self, port: str) -> None:
        try:
            node = flasher.read_node_config(port, log=self._worker_log)
        except Exception as exc:  # noqa: BLE001
            logger.exception("SOLO config read worker failed")
            self.call_from_thread(self._job_done, False, f"Node config read failed: {exc}")
            return
        self.call_from_thread(self._solo_config_read, node)

    def _worker_log(self, message: str) -> None:
        """Log callback for flasher calls in workers; flasher text is plain, never markup."""
        self.call_from_thread(self._write_log, escape(message))

    # ----- state shown in the chrome (main thread) ---------------------------------------

    def _apply_detect(self, result: DetectResult, announce: bool) -> None:
        self._detect = result
        self._clear_activity(_PROBE_GROUP)
        self._write_log(escape(result.detail))
        if announce:
            level, text = _DETECT_TEXT[result.state]
            self._post_status(level, f"RA4M1: {text}")
        self._dashboard.rebuild()

    def _apply_psoc6_detect(self, result: DetectResult) -> None:
        self._psoc6_detect = result
        self._clear_activity(_PROBE_GROUP)
        self._write_log(escape(result.detail))
        level, text = _DETECT_TEXT[result.state]
        self._post_status(level, f"PSoC 6: {text}")
        self._dashboard.rebuild()

    @staticmethod
    def _detect_text(result: DetectResult | None, chip: str) -> str:
        if result is None:
            return "not detected — / d"
        return f"{chip} {_DETECT_TEXT[result.state][1]}"

    def _serial_text(self) -> str:
        if self._serial is not None:
            return f"{self._serial.device} (bridge running)"
        if self._download_candidates:
            ports = ", ".join(port.device for port in self._download_candidates)
            return f"{ports} (ESP32-S3 download mode: ESP-Prog-2 or SOLO board)"
        return "no ESP-Prog-2 port"

    def _serial_header_text(self) -> str:
        if self._serial is not None:
            return self._serial.device
        if self._download_candidates:
            return f"{len(self._download_candidates)} ESP32-S3 port(s)"
        return "no serial port"

    def _refresh_serial(self) -> None:
        self._serial = flasher.find_probe_serial()
        self._download_candidates = flasher.find_download_mode_candidates()
        self._refresh_chrome()

    def _header_items(self) -> list[tuple[StatusLevel | None, str]]:
        detecting = _PROBE_GROUP in self._activities
        if detecting or self._detect is None:
            probe: tuple[StatusLevel | None, str] = (None, "probe …")
            target: tuple[StatusLevel | None, str] = (None, "RA4M1 …")
        elif self._detect.state is TargetState.NO_PROBE or self._detect.probe is None:
            probe = ("err", "probe not found")
            target = _HEADER_TARGET[self._detect.state]
        else:
            info = self._detect.probe
            probe = ("ok", f"probe {info.description or info.product_name}")
            target = _HEADER_TARGET[self._detect.state]
        return [probe, (None, self._serial_header_text()), target]

    def _refresh_chrome(self) -> None:
        self._chrome.header_items = self._header_items()
        self._chrome.busy_label = next(reversed(self._activities.values()), None)
        for screen in self.screen_stack:
            if isinstance(screen, ChromeScreen):
                screen.refresh_chrome()

    def _set_activity(self, group: str, label: str) -> None:
        self._activities[group] = label
        if self._spinner is None:
            self._spinner = self.set_interval(_SPINNER_INTERVAL_S, self._tick_spinner)
        self._refresh_chrome()

    def _clear_activity(self, group: str) -> None:
        self._activities.pop(group, None)
        if not self._activities and self._spinner is not None:
            self._spinner.stop()
            self._spinner = None
        self._refresh_chrome()

    def _tick_spinner(self) -> None:
        self._chrome.spinner_frame += 1
        self._refresh_chrome()

    def _post_status(self, level: StatusLevel, text: str) -> None:
        self._status_id = next(self._status_ids)
        status_id = self._status_id
        self._chrome.status = (level, text)
        self._refresh_chrome()
        self.set_timer(STATUS_LIFETIME_S, lambda: self._expire_status(status_id))

    def _expire_status(self, status_id: int) -> None:
        if status_id == self._status_id:
            self._chrome.status = None
            self._refresh_chrome()

    def _set_progress(self, value: float) -> None:
        if self._run_screen is not None:
            self._run_screen.set_progress(value)

    def _write_log(self, message: str) -> None:
        self._log_lines.append(message)
        for screen in self.screen_stack:
            if isinstance(screen, (LogScreen, RunScreen)):
                screen.append_log(message)


def run(
    hex_path: Path | None = None,
    frequency: int = flasher.DEFAULT_FREQUENCY_HZ,
    no_state: bool = False,
) -> None:
    FlasherApp(hex_path=hex_path, frequency=frequency, no_state=no_state).run()
