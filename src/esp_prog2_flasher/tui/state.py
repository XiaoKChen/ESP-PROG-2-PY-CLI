"""Saved TUI state: load and save the settings, Recent runs, selection and recent files as JSON.

The dashboard remembers its choices between launches so a bench session resumes where it
stopped. This module is the only file I/O of that feature: pure load, save and forget
functions over one versioned JSON file in the platform config directory, plus the rules that
turn what was read back into usable state. app.py calls it and decides when.

Owns:        the file location, STATE_VERSION, the JSON shape and its validation, the atomic
             write, and the fallback of saved paths that no longer exist.
Not here:    when to load or save, and the status lines (../app.py); what Settings and
             RecentRun mean (flows.py, targets.py).
Invariants:  nothing sensitive is stored: ports, paths, names, CAN IDs and run results only.
             A file this version cannot positively read as its own or older is never
             overwritten (LoadResult.overwritable is False) until it is forgotten. Loading
             never raises: unreadable, corrupt or foreign content yields no state and a reason.
Errors:      load_state reports a problem in LoadResult.problem; save_state and forget_state
             let OSError through for the caller to report once.
Depends on:  flows.py, targets.py, dfu_config.py and the standard library only.
See also:    ../app.py, tests/test_state.py
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final, get_args

from ..dfu_config import DEVICE_NAMES
from .flows import CUSTOM_DEVICE, KEEP_DEFAULT_DEVICE, Settings
from .targets import MAX_RECENT_FILES, MAX_RECENTS, TARGET_GROUPS, Operation, RecentRun, Target

logger = logging.getLogger(__name__)

STATE_VERSION: Final[int] = 1
APP_DIR_NAME: Final[str] = "esp-prog2-flasher"
STATE_FILE_NAME: Final[str] = "state.json"

# JSON "recent_files" key -> the Settings field holding that picker kind's files.
_RECENT_FILE_FIELDS: Final[dict[str, str]] = {
    "hex": "recent_hex",
    "bin": "recent_bin",
    "bundle": "recent_bundle",
}
# Settings fields holding a file or folder, with the wording of the "path missing" status.
_FILE_FIELDS: Final[dict[str, str]] = {
    "boot_hex": "bootloader .hex",
    "app_bin": "application .bin",
    "psoc6_hex": "PSoC 6 .hex",
    "solo_app": "SOLO app .bin",
}
_FOLDER_FIELDS: Final[dict[str, str]] = {"solo_bundle_dir": "SOLO bundle folder"}
_KNOWN_DEVICES: Final[frozenset[str]] = frozenset(
    {KEEP_DEFAULT_DEVICE, CUSTOM_DEVICE, *DEVICE_NAMES}
)
_OPERATIONS: Final[dict[str, Operation]] = {name: name for name in get_args(Operation)}
_TARGETS: Final[dict[str, Target]] = {t.key: t for g in TARGET_GROUPS for t in g.targets}
_CUSTOM_ONLY_FIELDS: Final[tuple[str, ...]] = ("boot_cmd_id", "boot_reply_id")


@dataclass(frozen=True, slots=True)
class SavedState:
    """Everything remembered between launches."""

    settings: Settings
    recents: tuple[RecentRun, ...]
    selected: str | None  # a dashboard row key: a target ("t:...") or a group ("g:...")
    collapsed: tuple[str, ...]  # folded group ids


@dataclass(frozen=True, slots=True)
class LoadResult:
    """What reading the state file gave: usable state or None, why not, and whether to save."""

    state: SavedState | None
    problem: str | None = None  # one line for the "!" status; None when missing or loaded
    overwritable: bool = True  # False: a newer or foreign file that saving must leave alone


def default_state_path(env: Mapping[str, str] | None = None, windows: bool | None = None) -> Path:
    """The per-user state file: %APPDATA% on Windows, $XDG_CONFIG_HOME or ~/.config elsewhere."""
    env = os.environ if env is None else env
    if os.name == "nt" if windows is None else windows:
        base = Path(env.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_DIR_NAME / STATE_FILE_NAME


def load_state(path: Path) -> LoadResult:
    """Read the state file; never raises. A missing file is a normal first launch."""
    if not path.exists():
        return LoadResult(None)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable state file %s: %s", path, exc)
        return LoadResult(None, f"Saved state unreadable, using defaults ({path.name})")
    if not isinstance(data, dict):
        return LoadResult(None, f"Saved state is not a state file, using defaults ({path.name})")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return LoadResult(
            None, "Saved state has an unknown format, using defaults and leaving it alone", False
        )
    if version > STATE_VERSION:
        return LoadResult(
            None,
            f"Saved state is from a newer version ({version}), using defaults and leaving it alone",
            False,
        )
    return LoadResult(_state_from_json(data))


def save_state(path: Path, state: SavedState) -> None:
    """Write ``state`` atomically: a temp file beside it, then a replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_state_to_json(state), indent=2, ensure_ascii=False)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            temp_file.write(text)
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(temp_name).unlink()
        raise


def forget_state(path: Path) -> None:
    """Delete the state file, whatever it holds; a missing file is fine."""
    path.unlink(missing_ok=True)


def restore_settings(saved: Settings, defaults: Settings) -> tuple[Settings, list[str]]:
    """Fall back to ``defaults`` for each saved path that vanished; list what was replaced.

    Recent files that no longer exist are dropped silently.
    """
    restored = saved
    missing: list[str] = []
    for name, label in {**_FILE_FIELDS, **_FOLDER_FIELDS}.items():
        value = getattr(saved, name)
        exists = Path.is_dir if name in _FOLDER_FIELDS else Path.is_file
        if not value:
            restored = replace(restored, **{name: getattr(defaults, name)})
        elif not exists(Path(value)):
            restored = replace(restored, **{name: getattr(defaults, name)})
            missing.append(label)
    restored = replace(
        restored,
        recent_hex=_existing(saved.recent_hex, Path.is_file),
        recent_bin=_existing(saved.recent_bin, Path.is_file),
        recent_bundle=_existing(saved.recent_bundle, Path.is_dir),
    )
    return restored, missing


def _existing(files: tuple[str, ...], exists: Callable[[Path], bool]) -> tuple[str, ...]:
    return tuple(file for file in files if exists(Path(file)))


def _scalar_setting_fields() -> list[str]:
    return [f.name for f in fields(Settings) if f.name not in _RECENT_FILE_FIELDS.values()]


def _state_to_json(state: SavedState) -> dict[str, Any]:
    settings = state.settings
    custom = settings.boot_device == CUSTOM_DEVICE
    return {
        "version": STATE_VERSION,
        "settings": {
            name: getattr(settings, name)
            for name in _scalar_setting_fields()
            if custom or name not in _CUSTOM_ONLY_FIELDS
        },
        "recent_files": {
            key: list(getattr(settings, name)) for key, name in _RECENT_FILE_FIELDS.items()
        },
        "recents": [
            {
                "target": recent.target.key,
                "operation": recent.operation,
                "detail": recent.detail,
                "ok": recent.ok,
                "message": recent.message,
                "at": recent.at.isoformat(timespec="seconds"),
            }
            for recent in state.recents
        ],
        "selected": state.selected,
        "collapsed": list(state.collapsed),
    }


def _state_from_json(data: dict[str, Any]) -> SavedState:
    selected = data.get("selected")
    collapsed = data.get("collapsed")
    return SavedState(
        settings=_settings_from_json(data),
        recents=_recents_from_json(data.get("recents")),
        selected=selected if isinstance(selected, str) else None,
        collapsed=tuple(c for c in collapsed if isinstance(c, str))
        if isinstance(collapsed, list)
        else (),
    )


def _settings_from_json(data: dict[str, Any]) -> Settings:
    """Settings from the saved fields; a field of the wrong type keeps its default."""
    defaults = Settings()
    saved = data.get("settings")
    values: dict[str, Any] = {}
    if isinstance(saved, dict):
        for name in _scalar_setting_fields():
            value = saved.get(name)
            if isinstance(value, type(getattr(defaults, name))):
                values[name] = value
    if values.get("boot_device", KEEP_DEFAULT_DEVICE) not in _KNOWN_DEVICES:
        del values["boot_device"]
    if values.get("boot_device") != CUSTOM_DEVICE:
        for name in _CUSTOM_ONLY_FIELDS:
            values.pop(name, None)
    recent_files = data.get("recent_files")
    if isinstance(recent_files, dict):
        for key, name in _RECENT_FILE_FIELDS.items():
            files = recent_files.get(key)
            if isinstance(files, list):
                values[name] = tuple(f for f in files if isinstance(f, str) and f)[
                    :MAX_RECENT_FILES
                ]
    return replace(defaults, **values)


def _recents_from_json(items: object) -> tuple[RecentRun, ...]:
    if not isinstance(items, list):
        return ()
    recents: list[RecentRun] = []
    for item in items:
        recent = _recent_from_json(item, len(recents) + 1)
        if recent is not None:
            recents.append(recent)
    return tuple(recents[:MAX_RECENTS])


def _recent_from_json(item: object, run_id: int) -> RecentRun | None:
    if not isinstance(item, dict):
        return None
    target_key, operation_name = item.get("target"), item.get("operation")
    detail, ok, message, saved_at = (item.get(k) for k in ("detail", "ok", "message", "at"))
    if not (
        isinstance(target_key, str)
        and isinstance(operation_name, str)
        and isinstance(detail, str)
        and isinstance(ok, bool)
        and isinstance(message, str)
        and isinstance(saved_at, str)
    ):
        return None
    target, operation = _TARGETS.get(target_key), _OPERATIONS.get(operation_name)
    try:
        at = datetime.fromisoformat(saved_at)
    except ValueError:
        return None
    # The dashboard compares against the naive local clock, so an offset time cannot be shown.
    if target is None or operation is None or at.tzinfo is not None:
        return None
    return RecentRun(run_id, target, operation, detail, ok, message, at)
