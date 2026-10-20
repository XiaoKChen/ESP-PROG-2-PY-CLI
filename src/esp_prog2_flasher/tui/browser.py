"""TTSSH-style file and folder browser: a filterable list of one folder's entries.

Typing filters the current folder's entries (folders first, then files with the wanted
suffix), enter opens a folder or picks a file, ← or backspace on an empty filter goes up
(to a drive list from a Windows drive root), and "/" offers "." (use this folder, in folder
mode) and "t" (type a path). It replaces the old DirectoryTree picker and returns the
chosen Path, or None when the user backs out.

Owns:        BrowseSpec, the folder listing and its ordering, and the typed-path prompt.
Not here:    what the chosen path is for (wizard.py, ../app.py).
Invariants:  only an existing folder (folder mode) or an existing file with the wanted
             suffix (file mode) is ever returned. Hidden entries (dot-names) are not listed.
Errors:      an unreadable folder or a bad typed path is a status message; the browser stays.
Depends on:  textual, rich, commands.py, screens.py, widgets.py, the standard library.
See also:    wizard.py, ../app.py, tests/test_app.py
"""

from __future__ import annotations

import os
import string
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Label

from .commands import BROWSER_COMMANDS, COMMANDS_KEY, BrowserMode, Command, popup_commands
from .screens import NARROW_BREAKPOINTS, ChromeScreen
from .widgets import ChromeState, DetailsView, Field, FilterList, GlobalCommand, Hint, ListRow

_DRIVE_KEY_PREFIX: Final[str] = "drive:"
_BYTES_PER_KB: Final[int] = 1024
_TITLE_MARGIN: Final[int] = 6


@dataclass(frozen=True, slots=True)
class BrowseSpec:
    """What to pick: a file with ``suffix`` (e.g. ``.hex``), or a folder; where to start."""

    title: str
    suffix: str
    folder: bool
    start: Path

    @property
    def mode(self) -> BrowserMode:
        return "folder" if self.folder else "file"


def start_folder(current: str) -> Path:
    """Where a browser opens: the current value's folder, else the working directory."""
    start = Path(current).expanduser().parent if current else Path.cwd()
    return start if start.is_dir() else Path.cwd()


def _size_text(size: int) -> str:
    return f"{size} bytes" if size < _BYTES_PER_KB else f"{size / _BYTES_PER_KB:.1f} KB"


def _ellipsize_left(text: str, width: int) -> str:
    """Keep the informative end of a long path."""
    return text if len(text) <= width or width < 2 else "…" + text[-(width - 1) :]


def _windows_drives() -> list[Path]:
    return [
        Path(f"{letter}:\\") for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()
    ]


class BrowserScreen(ChromeScreen[Path | None]):
    """Browse folders; return the picked file or folder, or None on esc."""

    AUTO_FOCUS = ""  # nothing: keys go to the screen, so typing filters
    HORIZONTAL_BREAKPOINTS = NARROW_BREAKPOINTS
    DEFAULT_CSS = """
    BrowserScreen {
        & #entries { width: 1fr; }
        & #info { width: 40%; }
        &.-narrow #info { display: none; }
        & #path-row { height: auto; display: none; }
        & #path-row.-open { display: block; }
        & #path-row Label { color: $accent; text-style: bold; padding: 0 1 0 0; }
        & #path-row Input { width: 1fr; background: $background; }
    }
    """

    def __init__(self, chrome: ChromeState, spec: BrowseSpec) -> None:
        super().__init__(chrome)
        self.spec = spec
        self.folder: Path | None = spec.start

    def compose_body(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="entries", classes="panel -focused"):
                yield FilterList()
                with Horizontal(id="path-row"):
                    yield Label("Path ›")
                    yield Input(compact=True, id="path-input")
            with Vertical(id="info", classes="panel"):
                yield DetailsView()

    @property
    def entry_list(self) -> FilterList:
        return self.query_one(FilterList)

    @property
    def path_input(self) -> Input:
        return self.query_one("#path-input", Input)

    def on_mount(self) -> None:
        self.query_one("#info").border_title = self.spec.title
        self.rebuild()

    def _entries(self) -> list[Path]:
        if self.folder is None:
            return _windows_drives()
        children = [p for p in self.folder.iterdir() if not p.name.startswith(".")]
        folders = sorted((p for p in children if p.is_dir()), key=lambda p: p.name.lower())
        if self.spec.folder:
            return folders
        files = sorted(
            (p for p in children if p.is_file() and p.suffix.lower() == self.spec.suffix),
            key=lambda p: p.name.lower(),
        )
        return folders + files

    def rebuild(self) -> None:
        query = self.entry_list.query_text
        terms = query.lower().split()
        rows = []
        for path in self._entries():
            if self.folder is None:
                rows.append(ListRow(f"{_DRIVE_KEY_PREFIX}{path}", str(path), "drive"))
            elif all(term in path.name.lower() for term in terms):
                note = "folder" if path.is_dir() else _size_text(path.stat().st_size)
                rows.append(ListRow(str(path), path.name, note))
        self.entry_list.empty_text = (
            "Nothing matches.\n\nesc clears the filter."
            if query
            else f"No {'folders' if self.spec.folder else f'{self.spec.suffix} files or folders'}"
            " here.\n\n← goes up."
        )
        self.entry_list.set_rows(rows)
        where = "Drives" if self.folder is None else str(self.folder)
        title = Text(_ellipsize_left(where, max(self.size.width // 2 - _TITLE_MARGIN, 10)))
        if query:
            title.append(f"  › {query}▏")
        self.query_one("#entries").border_title = title
        self._show_details()

    def _open(self, folder: Path | None) -> None:
        """Show ``folder`` (None: the drive list); stay put if it cannot be read."""
        previous = self.folder
        self.folder = folder
        self.entry_list.query_text = ""
        try:
            self.rebuild()
        except OSError as exc:
            self.folder = previous
            self.rebuild()
            self.post_status("err", f"Cannot open {folder}: {exc.strerror or exc}")

    def _selected_path(self) -> Path | None:
        row = self.entry_list.selected
        if row is None:
            return None
        return Path(row.key.removeprefix(_DRIVE_KEY_PREFIX))

    def _show_details(self) -> None:
        path = self._selected_path()
        fields: list[Field] = [("Picking", self.spec.title)]
        if path is not None and path.is_dir():
            fields.append(("Folder", str(path)))
        elif path is not None:
            stat = path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            fields += [
                ("File", str(path)),
                ("Size", _size_text(stat.st_size)),
                ("Modified", modified),
            ]
        self.query_one(DetailsView).set_content(fields)
        self.refresh_footer()

    def footer_hints(self) -> Sequence[Hint]:
        if self.path_input.has_focus:
            return [("enter", "go"), ("esc", "close the path"), ("ctrl+c", "quit")]
        path = self._selected_path()
        action = "pick" if path is not None and path.is_file() else "open"
        hints: list[Hint] = [("↑/↓", "move"), ("enter", action), ("←", "up")]
        hints += [("type", "to filter"), (COMMANDS_KEY, "commands")]
        hints.append(("esc", "clear filter" if self.entry_list.query_text else "cancel"))
        return hints

    def on_key(self, event: events.Key) -> None:
        if self.path_input.has_focus:
            if event.key == "escape":
                event.stop()
                self._close_path_prompt()
            return
        entry_list = self.entry_list
        handled = True
        if event.character == COMMANDS_KEY:
            self.open_popup(
                popup_commands(BROWSER_COMMANDS, frozenset({self.spec.mode})), self._run
            )
        elif event.key == "escape":
            if entry_list.query_text:
                entry_list.query_text = ""
                self.rebuild()
            else:
                self.dismiss(None)
        elif event.key == "enter":
            self._enter()
        elif event.key == "left" or (event.key == "backspace" and not entry_list.query_text):
            self._go_up()
        else:
            outcome = entry_list.route_key(event)
            handled = outcome is not None
            if outcome == "filtered":
                self.rebuild()
            elif outcome == "moved":
                self._show_details()
        if handled:
            event.stop()
            event.prevent_default()

    def _enter(self) -> None:
        path = self._selected_path()
        if path is None:
            return
        if path.is_dir():
            self._open(path)
        else:
            self.dismiss(path)

    def _go_up(self) -> None:
        if self.folder is None:
            return
        parent = self.folder.parent
        if parent != self.folder:
            self._open(parent)
        elif os.name == "nt":
            self._open(None)

    def _run(self, command: Command) -> None:
        if command.action == "use_folder":
            if self.folder is not None:
                self.dismiss(self.folder)
        elif command.action == "type_path":
            self.query_one("#path-row").add_class("-open")
            self.path_input.value = str(self.folder or "")
            self.path_input.focus()
            self.refresh_footer()
        else:
            self.post_message(GlobalCommand(command))

    def _close_path_prompt(self) -> None:
        self.query_one("#path-row").remove_class("-open")
        self.set_focus(None)
        self.refresh_footer()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        path = Path(event.value.strip()).expanduser()
        if path.is_dir() and self.spec.folder:
            self.dismiss(path)
        elif path.is_dir():
            self._close_path_prompt()
            self._open(path)
        elif path.is_file() and not self.spec.folder and path.suffix.lower() == self.spec.suffix:
            self.dismiss(path)
        else:
            wanted = "folder" if self.spec.folder else f"{self.spec.suffix} file or folder"
            self.post_status("err", f"Not a {wanted}: {path}")
