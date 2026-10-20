"""The "/" commands and the key reference: one table that the popup, details pane and help share.

On list screens letters filter the list, so actions live behind "/": the popup lists the
commands valid for the current screen and selection, and the next key runs one. A command
is valid when one of its tags matches the screen's current tags (the selected target's kind
on the dashboard, "file" or "folder" in the browser); untagged commands are valid on every
popup. help_rows() renders the same tables, plus the navigation keys, as the "/ ?" screen.

Owns:        DASHBOARD_COMMANDS, BROWSER_COMMANDS, GLOBAL_COMMANDS, PRIMARY_ACTIONS (what
             enter does per target kind) and the navigation rows of the help table.
Not here:    what each action does (../app.py, browser.py), the popup's drawing (screens.py).
Invariants:  within one screen's tag set no two valid commands share a key; GLOBAL_COMMANDS
             keys are never used by a scoped command.
Depends on:  targets.py and the standard library.
See also:    screens.py (CommandPopup, HelpScreen), browser.py, ../app.py
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from .targets import TargetKind

CommandAction = Literal[
    "detect",
    "pick_file",
    "edit_config",
    "pick_port",
    "bundle",
    "solo_app",
    "toggle_embassy",
    "toggle_migrate",
    "provision",
    "read_config",
    "get_official",
    "log",
    "help",
    "quit",
    "use_folder",
    "type_path",
]
BrowserMode = Literal["file", "folder"]
Tag = TargetKind | BrowserMode
# (where, key, action) — one row of the help table; "where" is blank inside a section.
HelpRow = tuple[str, str, str]

COMMANDS_KEY: Final[str] = "/"

_TAG_NAMES: Final[dict[Tag, str]] = {
    "boot": "RA4M1 bootloader",
    "app": "RA4M1 app",
    "psoc6": "PSoC 6",
    "solo": "SOLO boards",
    "probe": "ESP-Prog-2",
    "file": "picking a file",
    "folder": "picking a folder",
}
_TAG_ORDER: Final[tuple[Tag, ...]] = tuple(_TAG_NAMES)


@dataclass(frozen=True, slots=True)
class Command:
    """One "/" popup entry: the key after "/", what it does, and where it is valid."""

    key: str
    description: str
    action: CommandAction
    tags: frozenset[Tag] = frozenset()  # empty: valid on every popup

    def valid_for(self, tags: frozenset[Tag]) -> bool:
        return not self.tags or bool(self.tags & tags)


def _tags(*tags: Tag) -> frozenset[Tag]:
    return frozenset(tags)


_SWD: Final[frozenset[Tag]] = _tags("boot", "app", "psoc6")

DASHBOARD_COMMANDS: Final[tuple[Command, ...]] = (
    Command("d", "detect over SWD", "detect", _SWD),
    Command("f", "pick the image file", "pick_file", _SWD),
    Command("e", "edit config", "edit_config", _tags("boot", "solo")),
    Command("p", "pick port", "pick_port", _tags("solo")),
    Command("b", "bundle source", "bundle", _tags("solo")),
    Command("a", "optional app .bin", "solo_app", _tags("solo")),
    Command("a", "toggle Embassy 0x8000", "toggle_embassy", _tags("app")),
    Command("m", "toggle migrate-stock", "toggle_migrate", _tags("solo")),
    Command("n", "provision cancfg", "provision", _tags("solo")),
    Command("r", "read config", "read_config", _tags("solo")),
    Command("g", "get official probe firmware", "get_official", _tags("probe")),
)
BROWSER_COMMANDS: Final[tuple[Command, ...]] = (
    Command(".", "use this folder", "use_folder", _tags("folder")),
    Command("t", "type a path", "type_path"),
)
GLOBAL_COMMANDS: Final[tuple[Command, ...]] = (
    Command("l", "log", "log"),
    Command("?", "full help", "help"),
    Command("q", "quit", "quit"),
)

PRIMARY_ACTIONS: Final[dict[TargetKind, str]] = {
    "boot": "flash bootloader + config",
    "app": "flash application",
    "psoc6": "flash full image",
    "solo": "flash bundle",
    "probe": "flash probe firmware",
}


def popup_commands(scoped: Iterable[Command], tags: frozenset[Tag]) -> tuple[Command, ...]:
    """The scoped commands valid for ``tags``, then the global ones."""
    return (*(c for c in scoped if c.valid_for(tags)), *GLOBAL_COMMANDS)


def _command_rows(commands: Iterable[Command]) -> list[tuple[str, str]]:
    rows = []
    for command in commands:
        where = ", ".join(_TAG_NAMES[tag] for tag in _TAG_ORDER if tag in command.tags)
        suffix = f"  ({where})" if where else ""
        rows.append((f"{COMMANDS_KEY} {command.key}", f"{command.description}{suffix}"))
    return rows


_SECTIONS: Final[tuple[tuple[str, tuple[tuple[str, str], ...]], ...]] = (
    (
        "Every list",
        (
            ("↑/↓, pgup/pgdn, home/end", "move"),
            ("any character except /", "filter the list"),
            ("backspace", "edit the filter"),
            ("esc", "clear the filter, or go back one screen"),
            ("/", "open the command popup (esc or / closes it)"),
            ("ctrl+c / ctrl+q", "quit (works everywhere)"),
        ),
    ),
    (
        "Dashboard",
        (
            ("enter", "run the target's wizard, fold a group, or rerun a recent run"),
            ("→ / ←", "expand / collapse a group; ← on a target selects its group"),
        ),
    ),
    (
        "Wizard",
        (
            ("enter", "accept the field (or the highlighted choice) and go on"),
            ("shift+tab", "previous field"),
            ("esc", "back out to where the wizard started"),
        ),
    ),
    ("y/n questions", (("y", "start the write"), ("n / esc", "cancel"))),
    ("Run view", (("esc", "back to the dashboard; the job keeps running"),)),
    (
        "Folder browser",
        (
            ("enter", "open the folder, or pick the file"),
            ("← or backspace on an empty filter", "parent folder (drive list at a drive root)"),
        ),
    ),
    ("Log, help", (("↑/↓, pgup/pgdn", "scroll"), ("esc", "back"))),
)
_SECTION_COMMANDS: Final[dict[str, tuple[Command, ...]]] = {
    "Every list": GLOBAL_COMMANDS,
    "Dashboard": DASHBOARD_COMMANDS,
    "Folder browser": BROWSER_COMMANDS,
}


def help_rows() -> list[HelpRow]:
    """The full key reference: navigation keys plus every "/" command, grouped by screen."""
    rows: list[HelpRow] = []
    for where, keys in _SECTIONS:
        section = [*keys, *_command_rows(_SECTION_COMMANDS.get(where, ()))]
        rows += [(where if i == 0 else "", key, action) for i, (key, action) in enumerate(section)]
    return rows
