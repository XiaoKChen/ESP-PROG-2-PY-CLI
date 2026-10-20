"""The TUI's building blocks: header, status line, footer hints, filterable list and details view.

Every screen is framed TTSSH-style: a one-line header (app name, version, probe/serial/target
state), a body, a status line (✓/!/✗ messages or a spinner) and a one-line footer of key
hints. The framing widgets render from one shared ChromeState that FlasherApp updates; the
FilterList is the type-to-filter list used by the dashboard, the wizards' pick-lists and
the file browser, and DetailsView draws label/value pairs plus the selection's commands.

Owns:        ChromeState (header items, status message, busy label, spinner frame), the
             spinner frames, the list's cursor/scroll/filter-query handling and every
             widget's component styles.
Not here:    what the rows mean and what keys do beyond moving and filtering (screens.py,
             wizard.py, browser.py); the palette (theme.py).
Invariants:  "/" is never added to a filter query: it opens the command popup. A list cursor
             never rests on an unselectable row while a selectable one exists.
Depends on:  textual, rich, theme.py.
See also:    screens.py, wizard.py, browser.py
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Final, Literal

from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events
from textual.geometry import Size
from textual.message import Message
from textual.widget import Widget

from .commands import COMMANDS_KEY, Command
from .theme import STATUS_SYMBOLS, StatusLevel

SPINNER_FRAMES: Final[str] = "⣾⣽⣻⢿⡿⣟⣯⣷"
APP_TITLE: Final[str] = "ESP-PROG-2 FLASHER"
HINT_SEPARATOR: Final[str] = " • "
# (key as shown, what it does) — one footer hint or one details-pane command line.
Hint = tuple[str, str]
# (label, value) — one line of the details pane or a confirm summary.
Field = tuple[str, str]
ListOutcome = Literal["moved", "filtered"]

_NAVIGATION_STEPS: Final[dict[str, int]] = {"up": -1, "down": 1}
_PAGE_KEYS: Final[dict[str, int]] = {"pageup": -1, "pagedown": 1}
_COLUMN_GAP: Final[str] = "  "
_ROW_PREFIX_WIDTH: Final[int] = 2
_SELECTED_BAR: Final[str] = "┃ "


@dataclass(slots=True)
class ChromeState:
    """What the header and status line show; FlasherApp owns and updates the one instance."""

    version: str
    header_items: list[tuple[StatusLevel | None, str]] = field(default_factory=list)
    status: tuple[StatusLevel, str] | None = None
    busy_label: str | None = None
    spinner_frame: int = 0

    @property
    def spinner(self) -> str:
        return SPINNER_FRAMES[self.spinner_frame % len(SPINNER_FRAMES)]


class StatusPosted(Message):
    """Ask the app to show a status message; screens post it instead of calling the app."""

    def __init__(self, level: StatusLevel, text: str) -> None:
        super().__init__()
        self.level = level
        self.text = text


class GlobalCommand(Message):
    """Ask the app to run a global "/" command (log, help, quit) picked on any screen."""

    def __init__(self, command: Command) -> None:
        super().__init__()
        self.command = command


class ChromeWidget(Widget):
    """A widget drawn from the shared ChromeState; the app refreshes them all on change."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "chrome--accent",
        "chrome--muted",
        "chrome--subtle",
        "chrome--ok",
        "chrome--warn",
        "chrome--err",
        "chrome--info",
    }
    DEFAULT_CSS = """
    ChromeWidget {
        height: 1;
        padding: 0 1;
        & > .chrome--accent { color: $accent; text-style: bold; }
        & > .chrome--muted { color: $muted; }
        & > .chrome--subtle { color: $subtle; }
        & > .chrome--ok { color: $success; text-style: bold; }
        & > .chrome--warn { color: $warning; text-style: bold; }
        & > .chrome--err { color: $error; text-style: bold; }
        & > .chrome--info { color: $muted; }
    }
    """

    def __init__(self, chrome: ChromeState, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.chrome = chrome

    def style(self, name: str) -> Style:
        return self.get_component_rich_style(f"chrome--{name}")

    def status_text(self, level: StatusLevel, text: str) -> Text:
        """A status message as symbol + text in its level's style, never colour alone."""
        return Text(f"{STATUS_SYMBOLS[level]} {text}", style=self.style(level))


class HeaderLine(ChromeWidget):
    """``ESP-PROG-2 FLASHER vX.Y.Z`` on the left, muted probe/serial/target state on the right."""

    def render(self) -> RenderableType:
        left = Text(APP_TITLE, style=self.style("accent"))
        left.append(f" v{self.chrome.version}", style=self.style("muted"))
        right = Text()
        for index, (level, text) in enumerate(self.chrome.header_items):
            if index:
                right.append("  ·  ", style=self.style("subtle"))
            if level is not None:
                right.append(f"{STATUS_SYMBOLS[level]} ", style=self.style(level))
            right.append(text, style=self.style("muted"))
        width = self.size.width
        room = max(width - left.cell_len - 1, 0)
        right.truncate(room, overflow="ellipsis")
        gap = max(width - left.cell_len - right.cell_len, 1)
        return Text.assemble(left, " " * gap, right, no_wrap=True, overflow="ellipsis")


class StatusLine(ChromeWidget):
    """The last status message, or a spinner with the running job's label."""

    def __init__(self, chrome: ChromeState, *, show_busy: bool = True) -> None:
        super().__init__(chrome)
        self._show_busy = show_busy

    def render(self) -> RenderableType:
        if self.chrome.status is not None:
            line = self.status_text(*self.chrome.status)
        elif self._show_busy and self.chrome.busy_label:
            line = Text(f"{self.chrome.spinner} ", style=self.style("accent"))
            line.append(self.chrome.busy_label)
        else:
            line = Text()
        line.no_wrap = True
        line.overflow = "ellipsis"
        return line


class SpinnerLine(ChromeWidget):
    """The run view's heading: a spinner while the job runs, then nothing but the label."""

    DEFAULT_CSS = """
    SpinnerLine { padding: 0; }
    """

    def __init__(self, chrome: ChromeState, label: str) -> None:
        super().__init__(chrome)
        self.label = label
        self.finished = False

    def render(self) -> RenderableType:
        line = Text()
        if not self.finished:
            line.append(f"{self.chrome.spinner} ", style=self.style("accent"))
        line.append(self.label, style=Style(bold=True))
        return line


class FooterHints(ChromeWidget):
    """One row of ``key description`` hints separated by `` • ``."""

    hints: Sequence[Hint] = ()

    def set_hints(self, hints: Sequence[Hint]) -> None:
        self.hints = tuple(hints)
        self.refresh()

    def render(self) -> RenderableType:
        line = Text(no_wrap=True, overflow="ellipsis")
        for index, (key, description) in enumerate(self.hints):
            if index:
                line.append(HINT_SEPARATOR, style=self.style("subtle"))
            line.append(key, style=self.style("accent"))
            line.append(f" {description}", style=self.style("muted"))
        return line


@dataclass(frozen=True, slots=True)
class _Columns:
    label: int  # label width of rows with a note
    trail_label: int  # label width of rows with a trail
    trail_note: int  # note width (gap included) of rows with a trail


@dataclass(frozen=True, slots=True)
class ListRow:
    """One row of a FilterList; ``key`` identifies it across rebuilds."""

    key: str
    label: str
    note: str = ""  # muted second column
    indent: int = 0
    marker: str = ""  # e.g. a fold arrow, drawn before the label
    trail: str = ""  # a third column; its first character takes ``level``'s style
    level: StatusLevel | None = None
    selectable: bool = True


class FilterList(Widget):
    """A cursor list with a type-to-filter query; its owner rebuilds the rows on each change.

    The widget never takes focus: the screen hands it keys through route_key, so letters
    reach the query instead of any binding.
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "filter-list--selected",
        "filter-list--bar",
        "filter-list--muted",
        "filter-list--ok",
        "filter-list--warn",
        "filter-list--err",
        "filter-list--info",
    }
    DEFAULT_CSS = """
    FilterList {
        height: 1fr;
        & > .filter-list--selected { background: $selection-bg; color: $accent; text-style: bold; }
        & > .filter-list--bar { background: $selection-bg; color: $accent; text-style: bold; }
        & > .filter-list--muted { color: $muted; }
        & > .filter-list--ok { color: $success; text-style: bold; }
        & > .filter-list--warn { color: $warning; text-style: bold; }
        & > .filter-list--err { color: $error; text-style: bold; }
        & > .filter-list--info { color: $muted; }
    }
    """

    def __init__(self, empty_text: str = "Nothing here.", *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.query_text = ""
        self.empty_text = empty_text
        self._rows: tuple[ListRow, ...] = ()
        self._cursor = 0
        self._offset = 0

    @property
    def rows(self) -> tuple[ListRow, ...]:
        return self._rows

    @property
    def selected(self) -> ListRow | None:
        if not self._rows or not self._rows[self._cursor].selectable:
            return None
        return self._rows[self._cursor]

    def set_rows(self, rows: Sequence[ListRow], keep_key: str | None = None) -> None:
        """Replace the rows; the cursor stays on ``keep_key`` if present, else goes to the top."""
        self._rows = tuple(rows)
        keys = [row.key for row in self._rows]
        self._cursor = keys.index(keep_key) if keep_key in keys else 0
        self._settle(1)
        self.refresh(layout=True)

    def select_key(self, key: str) -> None:
        keys = [row.key for row in self._rows]
        if key in keys:
            self._cursor = keys.index(key)
            self.refresh()

    def route_key(self, event: events.Key) -> ListOutcome | None:
        """Move on arrow/page keys, edit the query on typing; None leaves the key to the caller."""
        if event.key in _NAVIGATION_STEPS:
            self._move(_NAVIGATION_STEPS[event.key])
            return "moved"
        if event.key in _PAGE_KEYS:
            self._move(_PAGE_KEYS[event.key] * max(self.size.height - 1, 1))
            return "moved"
        if event.key in ("home", "end"):
            self._cursor = 0 if event.key == "home" else max(len(self._rows) - 1, 0)
            self._settle(1 if event.key == "home" else -1)
            self.refresh()
            return "moved"
        if event.key == "backspace" and self.query_text:
            self.query_text = self.query_text[:-1]
            return "filtered"
        if event.is_printable and event.character and event.character != COMMANDS_KEY:
            self.query_text += event.character
            return "filtered"
        return None

    def _move(self, delta: int) -> None:
        if not self._rows:
            return
        self._cursor = min(max(self._cursor + delta, 0), len(self._rows) - 1)
        self._settle(1 if delta > 0 else -1)
        self.refresh()

    def _settle(self, direction: int) -> None:
        """Step off unselectable rows, first in ``direction``, then back the other way."""
        if not self._rows:
            self._cursor = 0
            return
        for step in (direction, -direction):
            index = self._cursor
            while 0 <= index < len(self._rows) and not self._rows[index].selectable:
                index += step
            if 0 <= index < len(self._rows):
                self._cursor = index
                return

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return max(len(self._rows), 1)

    def render(self) -> RenderableType:
        muted = self.get_component_rich_style("filter-list--muted")
        if not self._rows:
            return Text(self.empty_text, style=muted)
        height = max(self.size.height, 1)
        self._offset = min(max(self._offset, self._cursor - height + 1), self._cursor)
        self._offset = max(min(self._offset, len(self._rows) - height), 0)
        width = self.size.width
        columns = self._columns(width - _ROW_PREFIX_WIDTH)
        visible = self._rows[self._offset : self._offset + height]
        lines = [
            self._render_row(row, self._offset + i == self._cursor, width, columns)
            for i, row in enumerate(visible)
        ]
        return Text("\n").join(lines)

    def _columns(self, room: int) -> _Columns:
        """Column widths: rows with a trail keep the whole trail and split the rest."""
        note_rows = [r for r in self._rows if r.note and not r.trail]
        trail_rows = [r for r in self._rows if r.trail]
        label = max((cell_len(self._label_text(r)) for r in note_rows), default=0)
        trail = max((cell_len(r.trail) for r in trail_rows), default=0)
        rest = max(room - trail - 2 * len(_COLUMN_GAP), 2)
        trail_label = max((cell_len(self._label_text(r)) for r in trail_rows), default=0)
        trail_label = min(trail_label, rest // 2)
        return _Columns(min(label, max(room * 2 // 3, 8)), trail_label, rest - trail_label)

    @staticmethod
    def _label_text(row: ListRow) -> str:
        return f"{' ' * row.indent}{row.marker}{row.label}"

    def _render_row(self, row: ListRow, selected: bool, width: int, columns: _Columns) -> Text:
        muted = self.get_component_rich_style("filter-list--muted")
        line = Text(no_wrap=True, overflow="ellipsis")
        if selected:
            line.append(_SELECTED_BAR, style=self.get_component_rich_style("filter-list--bar"))
        else:
            line.append(" " * _ROW_PREFIX_WIDTH)
        label = Text(self._label_text(row))
        if row.trail:
            label.truncate(columns.trail_label, overflow="ellipsis", pad=True)
        elif row.note:
            label.truncate(columns.label, overflow="ellipsis", pad=True)
        line.append_text(label)
        if row.note:
            note = Text(_COLUMN_GAP + row.note, style=muted)
            if row.trail:
                note.truncate(columns.trail_note, overflow="ellipsis", pad=True)
            line.append_text(note)
        if row.trail:
            line.append(_COLUMN_GAP)
            symbol_style = (
                self.get_component_rich_style(f"filter-list--{row.level}") if row.level else muted
            )
            line.append(row.trail[:1], style=symbol_style)
            line.append(row.trail[1:], style=muted)
        line.truncate(width, overflow="ellipsis", pad=selected)
        if selected:
            line.stylize(self.get_component_rich_style("filter-list--selected"), _ROW_PREFIX_WIDTH)
        return line


class DetailsView(Widget):
    """Muted labels with their values, then ``key  description`` command lines."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {"details--muted", "details--key"}
    DEFAULT_CSS = """
    DetailsView {
        height: auto;
        & > .details--muted { color: $muted; }
        & > .details--key { color: $accent; text-style: bold; }
    }
    """

    def __init__(
        self, fields: Sequence[Field] = (), commands: Sequence[Hint] = (), *, id: str | None = None
    ) -> None:
        super().__init__(id=id)
        self._fields = tuple(fields)
        self._commands = tuple(commands)

    def set_content(self, fields: Sequence[Field], commands: Sequence[Hint] = ()) -> None:
        self._fields = tuple(fields)
        self._commands = tuple(commands)
        self.clear_cached_dimensions()
        self.refresh(layout=True)

    def get_content_width(self, container: Size, viewport: Size) -> int:
        # Measured from the rows: an auto-width parent asks before it has a width of its own.
        widths = [
            *(_column_width(self._fields, 0) + 2 + cell_len(v) for _, v in self._fields),
            *(_column_width(self._commands, 0) + 2 + cell_len(d) for _, d in self._commands),
        ]
        return max(widths, default=0)

    def render(self) -> RenderableType:
        parts: list[RenderableType] = []
        if self._fields:
            grid = Table.grid(padding=(0, 2))
            grid.add_column(style=self.get_component_rich_style("details--muted"), no_wrap=True)
            grid.add_column(overflow="fold")
            for label, value in self._fields:
                grid.add_row(label, Text(value))
            parts.append(grid)
        if self._commands:
            if parts:
                parts.append(Text())
            grid = Table.grid(padding=(0, 2))
            grid.add_column(style=self.get_component_rich_style("details--key"), no_wrap=True)
            grid.add_column(overflow="fold")
            for key, description in self._commands:
                grid.add_row(key, description)
            parts.append(grid)
        return Group(*parts)


def _column_width(rows: Sequence[tuple[str, str]], column: int) -> int:
    return max((cell_len(row[column]) for row in rows), default=0)
