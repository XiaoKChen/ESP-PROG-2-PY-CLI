"""Modal screens shared by the flasher TUI: file/folder picker, confirm dialog and key help.

FilePickerScreen picks a file of one suffix (or a folder), ConfirmScreen asks before a
destructive write and shows what will be written where, and HelpScreen renders key
sections handed to it. FlasherApp in app.py pushes all three and owns their content.

Owns:        the dialogs' layout and their own key bindings (Enter/Esc semantics).
Not here:    the app key map the help sections come from, and every flash action (app.py).
Invariants:  ConfirmScreen dismisses True only on an explicit confirm (Enter or its button);
             Esc, its Cancel button and any other way out dismiss False.
Depends on:  textual and rich only; no flasher import, so the screens never touch hardware.
See also:    ../app.py, tests/test_app.py
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import ClassVar

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Input, Static

# (key as displayed, what it does) — one line of the help screen.
HelpRow = tuple[str, str]
# (section title, its rows).
HelpSection = tuple[str, Sequence[HelpRow]]
# (label, value) — one line of a confirm dialog's summary.
SummaryRow = tuple[str, str]


class _FilteredDirectoryTree(DirectoryTree):
    """Directory tree showing directories plus, unless dirs_only, files of one suffix + name."""

    name_filter: str = ""
    suffix_filter: str = ""
    dirs_only: bool = False

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        needle = self.name_filter.lower()
        return [
            p
            for p in paths
            if (p.is_dir() and not p.name.startswith("."))
            or (
                not self.dirs_only
                and p.suffix.lower() == self.suffix_filter
                and needle in p.name.lower()
            )
        ]


class FilePickerScreen(ModalScreen[Path | None]):
    """Modal file dialog: type a path, browse the tree, filter files by suffix + name.

    Shared by every picker — only the suffix and dialog title differ. With
    ``select_dirs`` it picks a folder instead: files are hidden and selecting a
    directory node (or submitting a typed directory) makes it the choice.
    """

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Close picker")]

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

    def __init__(self, start: Path, suffix: str, title: str, select_dirs: bool = False) -> None:
        super().__init__()
        self._start = start
        self._suffix = suffix
        self._title = title
        self._select_dirs = select_dirs
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
        tree = self.query_one(_FilteredDirectoryTree)
        tree.suffix_filter = self._suffix
        tree.dirs_only = self._select_dirs
        self.query_one("#picker_filter").display = not self._select_dirs
        self.query_one("#picker_dialog", Vertical).border_title = self._title

    def _choose(self, path: Path) -> None:
        self._chosen = path
        self.query_one("#picker_select", Button).disabled = False

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "picker_path":
            return
        new_root = Path(event.value).expanduser()
        if not new_root.is_dir():
            self.notify(f"Not a directory: {new_root}", severity="error", markup=False)
            return
        self.query_one(_FilteredDirectoryTree).path = new_root
        if self._select_dirs:
            self._choose(new_root)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "picker_filter":
            return
        tree = self.query_one(_FilteredDirectoryTree)
        tree.name_filter = event.value
        tree.reload()

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        if not self._select_dirs:
            self._choose(event.path)

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        if self._select_dirs:
            self._choose(event.path)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "picker_select":
            self.dismiss(self._chosen)
        elif event.button.id == "picker_cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Ask before a destructive write, summarising chip/board, image(s), address and config.

    The confirm button has focus, so Enter confirms; Esc cancels from anywhere.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel the write"),
        Binding("enter", "confirm", "Confirm the write"),
    ]

    CSS = """
    ConfirmScreen { align: center middle; }
    #confirm_dialog {
        width: 76;
        max-width: 100%;
        height: auto;
        max-height: 100%;
        background: $panel;
        border: round $error;
        padding: 0 1;
    }
    #confirm_summary { height: auto; max-height: 16; }
    #confirm_hint { color: $text-muted; padding: 1 0 0 0; }
    #confirm_buttons { height: auto; align: right middle; }
    #confirm_buttons Button { margin: 0 0 0 1; }
    """

    def __init__(self, title: str, rows: Sequence[SummaryRow], confirm_label: str) -> None:
        super().__init__()
        self._title = title
        self._rows = tuple(rows)
        self._confirm_label = confirm_label

    @property
    def rows(self) -> tuple[SummaryRow, ...]:
        """The summary shown to the user, in display order."""
        return self._rows

    def compose(self) -> ComposeResult:
        summary = Table.grid(padding=(0, 1))
        summary.add_column(style="bold", no_wrap=True)
        summary.add_column(overflow="fold")
        for label, value in self._rows:
            summary.add_row(label, Text(value))
        with Vertical(id="confirm_dialog"):
            with VerticalScroll(id="confirm_summary"):
                yield Static(summary)
            yield Static("Enter confirms · Esc cancels", id="confirm_hint")
            with Horizontal(id="confirm_buttons"):
                yield Button(self._confirm_label, id="confirm_yes", variant="error", compact=True)
                yield Button("Cancel", id="confirm_no", compact=True)

    def on_mount(self) -> None:
        self.query_one("#confirm_dialog", Vertical).border_title = self._title
        self.query_one("#confirm_yes", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "confirm_yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class HelpScreen(ModalScreen[None]):
    """Key reference built from the sections it is given; any close key dismisses it."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,question_mark,f1,q", "close", "Close help"),
    ]

    CSS = """
    HelpScreen { align: center middle; }
    #help_dialog {
        width: 76;
        max-width: 100%;
        height: auto;
        max-height: 100%;
        background: $panel;
        border: round $primary;
        padding: 0 1;
    }
    #help_body { height: auto; max-height: 100%; }
    """

    def __init__(self, sections: Sequence[HelpSection]) -> None:
        super().__init__()
        self._sections = tuple(sections)

    @property
    def sections(self) -> tuple[HelpSection, ...]:
        """The key sections shown, in display order."""
        return self._sections

    def compose(self) -> ComposeResult:
        with Vertical(id="help_dialog"), VerticalScroll(id="help_body"):
            for title, rows in self._sections:
                table = Table.grid(padding=(0, 2))
                table.add_column(style="bold", no_wrap=True, min_width=12)
                table.add_column()
                for key, description in rows:
                    table.add_row(key, description)
                yield Static(Text(title, style="bold underline"))
                yield Static(table)

    def on_mount(self) -> None:
        dialog = self.query_one("#help_dialog", Vertical)
        dialog.border_title = "Keys"
        dialog.border_subtitle = "Esc closes"

    def action_close(self) -> None:
        self.dismiss(None)
