"""Tests for tui/state — the saved-state file: location, round trip, bad files, path fallback.

Every test works under tmp_path; nothing reads or writes the real user config directory.

Covers:       default_state_path per platform; save/load round trip of settings, recent files,
              Recent runs, selection and folded groups; a missing file; corrupt, foreign and
              future-version files being ignored with a reason and left alone; fields of the
              wrong type keeping their defaults; custom-only CAN IDs; atomic writes leaving no
              temp file; forget_state; restore_settings falling back for vanished paths;
              remember_picked_files ordering, de-duplication and cap.
Not covered:  when the app saves or loads, and its status lines (tests/test_app.py).
See also:     src/esp_prog2_flasher/tui/state.py
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from esp_prog2_flasher.tui import state
from esp_prog2_flasher.tui.flows import CUSTOM_DEVICE, Settings, remember_picked_files
from esp_prog2_flasher.tui.targets import (
    MAX_RECENT_FILES,
    MAX_RECENTS,
    PSOC6_TARGET,
    SOLO_TARGETS,
    RecentRun,
)

WHEN = datetime(2026, 3, 4, 5, 6, 7)


def _recent(run_id: int = 1) -> RecentRun:
    return RecentRun(run_id, SOLO_TARGETS[0], "flash", "COM9", True, "Flashed", WHEN)


def _saved(settings: Settings | None = None) -> state.SavedState:
    return state.SavedState(settings or Settings(), (_recent(),), "t:psoc6:", ("solo",))


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "config" / "state.json"


@pytest.mark.parametrize(
    ("windows", "env", "expected"),
    [
        (True, {"APPDATA": "C:/Roaming"}, Path("C:/Roaming/esp-prog2-flasher/state.json")),
        (False, {"XDG_CONFIG_HOME": "/xdg"}, Path("/xdg/esp-prog2-flasher/state.json")),
    ],
)
def test_state_file_lives_in_the_platform_config_dir(
    windows: bool, env: dict[str, str], expected: Path
) -> None:
    assert state.default_state_path(env, windows) == expected


def test_state_file_falls_back_to_dot_config_without_xdg() -> None:
    expected = Path.home() / ".config" / "esp-prog2-flasher" / "state.json"
    assert state.default_state_path({}, windows=False) == expected


def test_round_trip_keeps_everything(path: Path, tmp_path: Path) -> None:
    settings = replace(
        Settings(),
        boot_hex=str(tmp_path / "a.hex"),
        boot_device=CUSTOM_DEVICE,
        boot_custom_name="My Board",
        boot_cmd_id="0x700",
        boot_reply_id="0x701",
        embassy=True,
        solo_port="COM9",
        solo_tag="v9",
        recent_hex=("a.hex", "b.hex"),
        recent_bin=("c.bin",),
        recent_bundle=("bundle",),
    )
    state.save_state(path, _saved(settings))
    loaded = state.load_state(path)
    assert loaded.problem is None and loaded.overwritable
    assert loaded.state == _saved(settings)


def test_a_missing_file_is_a_normal_first_launch(path: Path) -> None:
    assert state.load_state(path) == state.LoadResult(None)


def test_save_is_atomic_and_leaves_no_temp_file(path: Path) -> None:
    state.save_state(path, _saved())
    state.save_state(path, _saved())
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_json_carries_the_version_and_no_secrets(path: Path) -> None:
    state.save_state(path, _saved())
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == state.STATE_VERSION
    assert set(data) == {"version", "settings", "recent_files", "recents", "selected", "collapsed"}


def test_can_ids_are_saved_for_a_custom_device_only(path: Path) -> None:
    settings = replace(Settings(), boot_cmd_id="0x700", boot_reply_id="0x701")
    state.save_state(path, _saved(settings))
    loaded = state.load_state(path).state
    assert loaded is not None
    assert loaded.settings.boot_cmd_id == "" and loaded.settings.boot_reply_id == ""


@pytest.mark.parametrize("text", ["{not json", "[]", "", "\x00\x01"])
def test_corrupt_file_is_ignored_and_may_be_replaced(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    result = state.load_state(path)
    assert result.state is None and result.problem and result.overwritable


@pytest.mark.parametrize("version", [state.STATE_VERSION + 1, 99])
def test_future_version_is_ignored_and_never_overwritten(path: Path, version: int) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": version}), encoding="utf-8")
    result = state.load_state(path)
    assert result.state is None and "newer" in (result.problem or "")
    assert not result.overwritable


@pytest.mark.parametrize("version", [None, "1", 0, True, 1.5])
def test_unknown_version_is_ignored_and_never_overwritten(path: Path, version: object) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": version, "settings": {}}), encoding="utf-8")
    result = state.load_state(path)
    assert result.state is None and result.problem and not result.overwritable


def test_wrong_typed_or_unknown_fields_keep_their_defaults(path: Path) -> None:
    path.parent.mkdir(parents=True)
    data = {
        "version": 1,
        "settings": {"embassy": "yes", "solo_port": 7, "solo_tag": "v2", "boot_device": "Nope"},
        "recents": [
            "junk",
            {"target": "psoc6:", "operation": "flash"},
            {"target": ["x"], "operation": 1},
            {
                "target": "psoc6:",
                "operation": "flash",
                "detail": "x.hex",
                "ok": True,
                "message": "m",
                "at": "2026-01-01T00:00:00",
            },
        ],
        "selected": 5,
        "collapsed": ["solo", 3],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = state.load_state(path).state
    assert loaded is not None
    defaults = Settings()
    assert loaded.settings == replace(defaults, solo_tag="v2")
    assert [r.target for r in loaded.recents] == [PSOC6_TARGET]
    assert loaded.selected is None and loaded.collapsed == ("solo",)


def test_recents_beyond_the_limit_are_dropped(path: Path) -> None:
    runs = tuple(_recent(i) for i in range(1, MAX_RECENTS + 4))
    state.save_state(path, state.SavedState(Settings(), runs, None, ()))
    loaded = state.load_state(path).state
    assert loaded is not None and len(loaded.recents) == MAX_RECENTS
    assert [r.id for r in loaded.recents] == list(range(1, MAX_RECENTS + 1))


def test_forget_deletes_the_file_and_tolerates_a_missing_one(path: Path) -> None:
    state.save_state(path, _saved())
    state.forget_state(path)
    assert not path.exists()
    state.forget_state(path)


def test_vanished_paths_fall_back_to_the_defaults(tmp_path: Path) -> None:
    kept = tmp_path / "kept.hex"
    kept.write_text("x", encoding="utf-8")
    defaults = replace(Settings(), boot_hex="bundled.hex", psoc6_hex="bundled-psoc.hex")
    saved = replace(
        defaults,
        boot_hex=str(tmp_path / "gone.hex"),
        psoc6_hex=str(kept),
        app_bin=str(tmp_path / "gone.bin"),
        solo_bundle_dir=str(tmp_path / "gone-dir"),
        recent_hex=(str(kept), str(tmp_path / "gone.hex")),
    )
    restored, missing = state.restore_settings(saved, defaults)
    assert restored.boot_hex == "bundled.hex"
    assert restored.psoc6_hex == str(kept)
    assert restored.app_bin == "" and restored.solo_bundle_dir == ""
    assert restored.recent_hex == (str(kept),)
    assert missing == ["bootloader .hex", "application .bin", "SOLO bundle folder"]


def test_an_empty_saved_image_takes_the_default_without_a_complaint() -> None:
    defaults = replace(Settings(), boot_hex="bundled.hex")
    restored, missing = state.restore_settings(replace(defaults, boot_hex=""), defaults)
    assert restored.boot_hex == "bundled.hex" and missing == []


def test_a_changed_image_becomes_the_newest_recent_file_once() -> None:
    before = replace(Settings(), boot_hex="old.hex", app_bin="app.bin")
    after = replace(before, boot_hex="new.hex", psoc6_hex="radar.hex", recent_hex=("old.hex",))
    remembered = remember_picked_files(before, after)
    assert remembered.recent_hex == ("radar.hex", "new.hex", "old.hex")
    assert remembered.recent_bin == ()
    again = remember_picked_files(remembered, remembered)
    assert again.recent_hex == remembered.recent_hex


def test_recent_files_are_deduplicated_and_capped() -> None:
    settings = Settings()
    for index in range(MAX_RECENT_FILES + 3):
        picked = replace(settings, solo_app=f"app{index}.bin")
        settings = remember_picked_files(settings, picked)
    settings = remember_picked_files(settings, replace(settings, solo_app="app5.bin"))
    assert len(settings.recent_bin) == MAX_RECENT_FILES
    assert settings.recent_bin[0] == "app5.bin"
    assert len(set(settings.recent_bin)) == MAX_RECENT_FILES
