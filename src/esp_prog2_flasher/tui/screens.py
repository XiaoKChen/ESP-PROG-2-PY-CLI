"""The TUI's screens: the shared frame, the dashboard, the "/" popup, and the run/log/help views.

ChromeScreen frames every screen with the header, status line and footer hints. The
DashboardScreen is the TTSSH-style home: a filterable tree of flash targets plus this
session's runs on the left, the selection's details and commands on the right. "/" opens
the CommandPopup over any list screen; RunScreen shows a running job, LogScreen the session
log and HelpScreen the key reference built from commands.py.

Owns:        the frame layout, the dashboard's fold state and row building, the dashboard and
             popup key routing, and the run/log/help views.
Not here:    what targets, commands and jobs do (DashboardHost, i.e. ../app.py), wizards
             (wizard.py), the file browser (browser.py), the command table (commands.py).
Invariants:  on the dashboard a printable key other than "/" only ever edits the filter; the
             popup runs at most one command per "/" and only a command it listed.
Depends on:  textual, rich, commands.py, targets.py, widgets.py.
See also:    ../app.py, wizard.py, browser.py, tests/test_app.py
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import ClassVar, Final, Protocol, TypeVar

from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import ProgressBar, RichLog, Static

from .commands import (
    COMMANDS_KEY,
    DASHBOARD_COMMANDS,
    PRIMARY_ACTIONS,
    Command,
    Tag,
    help_rows,
    popup_commands,
)
from .targets import TARGET_GROUPS, RecentRun, Target, TargetGroup, relative_time
from .theme import STATUS_SYMBOLS, StatusLevel
from .widgets import (
    ChromeState,
    ChromeWidget,
    DetailsView,
    Field,
    FilterList,
    FooterHints,
    HeaderLine,
    Hint,
    ListRow,
    SpinnerLine,
    StatusLine,
    StatusPosted,
)

ResultT = TypeVar("ResultT")

MIN_DETAILS_WIDTH: Final[int] = 80  # narrower terminals hide the details pane
# Screens with a details pane add "-narrow" below MIN_DETAILS_WIDTH columns and hide it.
NARROW_BREAKPOINTS: Final[list[tuple[int, str]]] = [(0, "-narrow"), (MIN_DETAILS_WIDTH, "-wide")]
_RELATIVE_TIME_REFRESH_S: Final[float] = 30.0
_FOCUSED_PANEL: Final[str] = "panel -focused"


class ChromeScreen(Screen[ResultT]):
    """A screen framed by the header, the status line and the footer hints."""

    DEFAULT_CSS = """
    ChromeScreen {
        layout: vertical;
        background: $background;
        & #body { height: 1fr; }
        & .panel {
            height: 1fr;
            border: round $panel-border;
            padding: 0 1;
            border-title-color: $accent;
            border-title-style: bold;
            border-subtitle-color: $muted;
        }
        & .panel.-focused { border: round $focus-border; }
        & RichLog { background: $background; scrollbar-size-vertical: 1; }
    }
    """

    SHOW_BUSY_IN_STATUS: ClassVar[bool] = True

    def __init__(self, chrome: ChromeState) -> None:
        super().__init__()
        self.chrome = chrome

    def compose(self) -> ComposeResult:
        yield HeaderLine(self.chrome)
        with Vertical(id="body"):
            yield from self.compose_body()
        yield StatusLine(self.chrome, show_busy=self.SHOW_BUSY_IN_STATUS)
        yield FooterHints(self.chrome)

    def compose_body(self) -> ComposeResult:
        """The widgets between the header and the status line."""
        yield from ()

    def footer_hints(self) -> Sequence[Hint]:
        return ()

    def refresh_footer(self) -> None:
        self.query_one(FooterHints).set_hints(self.footer_hints())

    def refresh_chrome(self) -> None:
        for widget in self.query(ChromeWidget):
            widget.refresh()

    def on_mount(self) -> None:
        self.refresh_footer()

    def on_screen_resume(self) -> None:
        self.refresh_chrome()
        self.refresh_footer()

    def post_status(self, level: StatusLevel, text: str) -> None:
        self.post_message(StatusPosted(level, text))

    def open_popup(self, commands: Sequence[Command], run: Callable[[Command], object]) -> None:
        """Show ``commands``; the next key runs the matching one, if any."""

        def chosen(key: str | None) -> None:
            if key is None:
                return
            command = next((c for c in commands if c.key == key), None)
            if command is None:
                self.post_status("info", f'no shortcut "{key}"')
            else:
                run(command)

        self.app.push_screen(CommandPopup(self.chrome, commands), chosen)


class CommandPopup(ModalScreen[str | None]):
    """A small accent-bordered box of commands; it returns the next key pressed, or None."""

    DEFAULT_CSS = """
    CommandPopup {
        align: center middle;
        background: transparent;
        & #popup {
            width: auto;
            height: auto;
            border: round $accent;
            padding: 0 2;
            background: $background;
            border-title-color: $accent;
            border-title-style: bold;
            border-subtitle-color: $muted;
        }
        & DetailsView { width: auto; }
        & FooterHints { dock: bottom; background: $background; }
    }
    """

    def __init__(self, chrome: ChromeState, commands: Sequence[Command]) -> None:
        super().__init__()
        self.chrome = chrome
        self.commands = tuple(commands)

    def compose(self) -> ComposeResult:
        with Vertical(id="popup"):
            # Built with its rows: an auto-sized box is laid out before on_mount runs.
            yield DetailsView(commands=[(c.key, c.description) for c in self.commands])
        yield FooterHints(self.chrome)

    def on_mount(self) -> None:
        box = self.query_one("#popup")
        box.border_title = "Commands"
        box.border_subtitle = "esc closes"
        self.query_one(FooterHints).set_hints([("key", "run command"), ("esc", "close")])

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        if event.key == "escape" or event.character == COMMANDS_KEY:
            self.dismiss(None)
        else:
            self.dismiss(event.character if event.is_printable else event.key)


class DashboardHost(Protocol):
    """What the dashboard needs from the app: target state, recents and the actions."""

    @property
    def recents(self) -> Sequence[RecentRun]: ...

    def target_note(self, target: Target) -> str: ...

    def target_fields(self, target: Target) -> list[Field]: ...

    def run_primary(self, target: Target) -> None: ...

    def run_recent(self, recent: RecentRun) -> None: ...

    def run_command(self, command: Command, target: Target | None) -> None: ...


Selection = TargetGroup | Target | RecentRun


class DashboardScreen(ChromeScreen[None]):
    """Targets (tree, or a flat list while filtering) and Recent on the left; details right."""

    HORIZONTAL_BREAKPOINTS = NARROW_BREAKPOINTS
    DEFAULT_CSS = """
    DashboardScreen {
        & #targets { width: 1fr; }
        & #details { width: 50%; }
        &.-narrow #details { display: none; }
    }
    """

    def __init__(self, chrome: ChromeState, host: DashboardHost) -> None:
        super().__init__(chrome)
        self.host = host
        self._collapsed: set[str] = set()
        self._objects: dict[str, Selection] = {}
        self._mounted = False

    def compose_body(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="targets", classes=_FOCUSED_PANEL):
                yield FilterList("Nothing matches.\n\nesc clears the filter.")
            with Vertical(id="details", classes="panel"):
                yield DetailsView()

    @property
    def target_list(self) -> FilterList:
        return self.query_one(FilterList)

    def on_mount(self) -> None:
        self.query_one("#details").border_title = "Details"
        self._mounted = True
        self.rebuild()
        self.set_interval(_RELATIVE_TIME_REFRESH_S, self.rebuild)

    def on_screen_resume(self) -> None:
        self.rebuild()

    @property
    def selection(self) -> Selection | None:
        row = self.target_list.selected
        return self._objects.get(row.key) if row else None

    def rebuild(self, keep_selection: bool = True) -> None:
        """Rebuild the rows from the host's state and the filter, then the title and details."""
        if not self._mounted:  # the app's state can change before the first compose
            return
        current = self.target_list.selected
        query = self.target_list.query_text
        rows = self._filtered_rows(query) if query else self._tree_rows()
        self.target_list.set_rows(rows, current.key if current and keep_selection else None)
        title = Text("Targets")
        if query:
            title.append(f"  › {query}▏")
        self.query_one("#targets").border_title = title
        self._show_details()

    def _tree_rows(self) -> list[ListRow]:
        rows: list[ListRow] = []
        self._objects = {}
        for group in TARGET_GROUPS:
            collapsed = group.id in self._collapsed
            marker = "▸ " if collapsed else "▾ "
            rows.append(self._add(group, ListRow(f"g:{group.id}", group.title, marker=marker)))
            if not collapsed:
                rows += [self._target_row(target, indent=4) for target in group.targets]
        rows += self._recent_rows(self.host.recents)
        return rows

    def _filtered_rows(self, query: str) -> list[ListRow]:
        terms = query.lower().split()
        self._objects = {}
        rows = []
        for group in TARGET_GROUPS:
            for target in group.targets:
                haystack = f"{group.title} {target.label} {self.host.target_note(target)}".lower()
                if all(term in haystack for term in terms):
                    row = ListRow(f"t:{target.key}", target.label, note=group.title)
                    rows.append(self._add(target, row))
        recents = [
            r
            for r in self.host.recents
            if all(term in f"{r.target.label} {r.detail}".lower() for term in terms)
        ]
        return rows + self._recent_rows(recents)

    def _target_row(self, target: Target, indent: int) -> ListRow:
        row = ListRow(f"t:{target.key}", target.label, self.host.target_note(target), indent)
        return self._add(target, row)

    def _recent_rows(self, recents: Sequence[RecentRun]) -> list[ListRow]:
        if not recents:
            return []
        now = datetime.now()
        rows = [ListRow("header:recent", "── Recent ──", selectable=False)]
        for recent in recents:
            level: StatusLevel = "ok" if recent.ok else "err"
            trail = f"{STATUS_SYMBOLS[level]}  {relative_time(recent.at, now)}"
            row = ListRow(
                f"r:{recent.id}", recent.target.short, recent.detail, trail=trail, level=level
            )
            rows.append(self._add(recent, row))
        return rows

    def _add(self, selection: Selection, row: ListRow) -> ListRow:
        self._objects[row.key] = selection
        return row

    def _show_details(self) -> None:
        selection = self.selection
        details = self.query_one(DetailsView)
        if isinstance(selection, Target):
            commands: list[Hint] = [("enter", PRIMARY_ACTIONS[selection.kind])]
            tags: frozenset[Tag] = frozenset({selection.kind})
            commands += [
                (f"{COMMANDS_KEY} {c.key}", c.description)
                for c in DASHBOARD_COMMANDS
                if c.valid_for(tags)
            ]
            details.set_content(self.host.target_fields(selection), commands)
        elif isinstance(selection, TargetGroup):
            labels = ", ".join(target.label for target in selection.targets)
            details.set_content(
                [("Group", selection.title), ("Targets", labels)], [("enter", "fold or unfold")]
            )
        elif isinstance(selection, RecentRun):
            details.set_content(_recent_fields(selection), [("enter", "run again")])
        else:
            details.set_content([])
        self.refresh_footer()

    def footer_hints(self) -> Sequence[Hint]:
        hints: list[Hint] = [("↑/↓", "move")]
        selection = self.selection
        if isinstance(selection, Target):
            hints.append(("enter", "flash"))
        elif isinstance(selection, TargetGroup):
            hints.append(("enter", "fold"))
        elif isinstance(selection, RecentRun):
            hints.append(("enter", "run again"))
        hints += [("type", "to filter"), (COMMANDS_KEY, "commands")]
        if self.target_list.query_text:
            hints.append(("esc", "clear filter"))
        return hints

    def on_key(self, event: events.Key) -> None:
        target_list = self.target_list
        handled = True
        if event.character == COMMANDS_KEY:
            self._open_commands()
        elif event.key == "escape":
            if target_list.query_text:
                target_list.query_text = ""
                self.rebuild()
        elif event.key == "enter":
            self._enter()
        elif event.key in ("left", "right") and not target_list.query_text:
            self._fold(expand=event.key == "right")
        else:
            outcome = target_list.route_key(event)
            handled = outcome is not None
            if outcome == "filtered":
                self.rebuild(keep_selection=False)
            elif outcome == "moved":
                self._show_details()
        if handled:
            event.stop()
            event.prevent_default()

    def _open_commands(self) -> None:
        selection = self.selection
        target = selection if isinstance(selection, Target) else None
        tags: frozenset[Tag] = frozenset({target.kind}) if target else frozenset()
        self.open_popup(
            popup_commands(DASHBOARD_COMMANDS, tags),
            lambda command: self.host.run_command(command, target),
        )

    def _enter(self) -> None:
        selection = self.selection
        if isinstance(selection, TargetGroup):
            self._collapsed ^= {selection.id}
            self.rebuild()
        elif isinstance(selection, Target):
            self.host.run_primary(selection)
        elif isinstance(selection, RecentRun):
            self.host.run_recent(selection)

    def _fold(self, expand: bool) -> None:
        selection = self.selection
        if isinstance(selection, TargetGroup):
            if expand:
                self._collapsed.discard(selection.id)
            else:
                self._collapsed.add(selection.id)
            self.rebuild()
        elif isinstance(selection, Target) and not expand:
            group = next(g for g in TARGET_GROUPS if selection in g.targets)
            self.target_list.select_key(f"g:{group.id}")
            self._show_details()


def _recent_fields(recent: RecentRun) -> list[Field]:
    symbol = STATUS_SYMBOLS["ok" if recent.ok else "err"]
    return [
        ("Target", recent.target.label),
        ("Ran", f"{recent.operation} · {recent.detail}"),
        ("Result", f"{symbol} {recent.message}"),
        (
            "When",
            f"{relative_time(recent.at, datetime.now())} ({recent.at.strftime('%H:%M:%S')})",
        ),
    ]


class LogPanelScreen(ChromeScreen[None]):
    """A panel ending in a RichLog; lines that arrive before it mounts are kept, not lost."""

    PANEL_ID: ClassVar[str] = "log"

    def __init__(self, chrome: ChromeState, title: str, lines: Sequence[str] = ()) -> None:
        super().__init__(chrome)
        self.title_text = title
        self._lines = list(lines)
        self._ready = False

    def compose_body(self) -> ComposeResult:
        with Vertical(id=self.PANEL_ID, classes=_FOCUSED_PANEL):
            yield from self.compose_heading()
            yield RichLog(markup=True, wrap=True, min_width=20)

    def compose_heading(self) -> ComposeResult:
        """Widgets above the log."""
        yield from ()

    def on_mount(self) -> None:
        self.query_one(f"#{self.PANEL_ID}").border_title = Text(self.title_text)
        log = self.query_one(RichLog)
        for line in self._lines:
            log.write(line)
        self._ready = True

    def on_unmount(self) -> None:
        # A job keeps running after its run view is closed; its late updates must not draw.
        self._ready = False

    def append_log(self, line: str) -> None:
        self._lines.append(line)
        if self._ready:
            self.query_one(RichLog).write(line)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)


class RunScreen(LogPanelScreen):
    """A running job: spinner, progress bar and the job's log lines as they arrive."""

    PANEL_ID: ClassVar[str] = "run"
    SHOW_BUSY_IN_STATUS: ClassVar[bool] = False
    DEFAULT_CSS = """
    RunScreen {
        & ProgressBar { margin: 1 0; }
        & ProgressBar Bar { width: 1fr; }
        & ProgressBar Bar > .bar--bar { color: $accent; background: $panel-border; }
        & ProgressBar Bar > .bar--complete { color: $success; background: $panel-border; }
        & RichLog { height: 1fr; }
    }
    """

    def __init__(self, chrome: ChromeState, title: str, label: str) -> None:
        super().__init__(chrome, title)
        self.label = label
        self._progress = 0.0

    def compose_heading(self) -> ComposeResult:
        yield SpinnerLine(self.chrome, self.label)
        yield ProgressBar(total=100, show_eta=False)

    def on_mount(self) -> None:
        self.query_one(ProgressBar).update(progress=self._progress)

    def set_progress(self, percent: float) -> None:
        self._progress = percent
        if self._ready:
            self.query_one(ProgressBar).update(progress=percent)

    def finish(self) -> None:
        if self._ready:
            self.query_one(SpinnerLine).finished = True
            self.refresh_chrome()

    def footer_hints(self) -> Sequence[Hint]:
        return [("esc", "dashboard (the job keeps running)"), ("ctrl+c", "quit")]


class LogScreen(LogPanelScreen):
    """The whole session log in one panel."""

    def on_mount(self) -> None:
        self.query_one(RichLog).focus()

    def footer_hints(self) -> Sequence[Hint]:
        return [("↑/↓", "scroll"), ("esc", "back"), ("ctrl+c", "quit")]


class HelpScreen(ChromeScreen[None]):
    """Every key and "/" command, from commands.help_rows()."""

    DEFAULT_CSS = """
    HelpScreen {
        & #help-table { height: auto; }
        & #help { background: $background; }
    }
    """

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(id="help", classes=_FOCUSED_PANEL):
            yield Static(id="help-table")

    def on_mount(self) -> None:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold", no_wrap=True)
        table.add_column(style="bold", no_wrap=True)
        table.add_column(overflow="fold")
        for where, key, action in help_rows():
            table.add_row(where, key, action)
        self.query_one("#help-table", Static).update(table)
        panel = self.query_one("#help")
        panel.border_title = "Help"
        panel.focus()

    def footer_hints(self) -> Sequence[Hint]:
        return [("↑/↓", "scroll"), ("esc", "back"), ("ctrl+c", "quit")]

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)
