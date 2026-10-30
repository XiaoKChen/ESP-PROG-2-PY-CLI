"""Tests for flasher's SOLO path — the esptool command plan, bundle validation, otadata, fetch.

Drives flash_solo, provision_node and read_node_config through an injected esptool runner
that emulates the board's flash (writes land in a dict, reads come back from it), so the exact
command plan, the bytes written and the read-back verification are checked without hardware;
checks load_solo_bundle against a synthetic bundle and every manifest rule; pins the ota_0 otadata
image to the CRC verified on the SOLO bench; and drives fetch_solo_bundle with a fake gh.

Covers:       flash_solo (both presets, migrate-stock erase, app + otadata, --after modes,
              read-back mismatch, esptool failure, validation before any esptool call, the
              record version a bundle gets), bundle_profile_version, provision_node with and
              without a profile (v1, or v2 over a v2 record), read_node_config, the no-reset
              steps plus final watchdog reset of every node path, the 1200-baud touch through
              the injected port/touch/clock seams (a running board followed by its MAC to its
              ROM port for every esptool call, a ROM port never appearing failing before
              esptool, an already-ROM, serial-less, non-MAC or unlisted port used as given),
              the hold-BOOT guidance on an esptool failure, the esptool 5 progress lines that
              drive the progress bar, load_solo_bundle,
              otadata_selecting_ota0, fetch_solo_bundle (success, missing release, missing gh,
              no asset).
Not covered:  real esptool runs against a board, a real 1200-baud touch or port listing, the
              real gh download, the pyOCD paths.
See also:     src/esp_prog2_flasher/flasher.py, tests/test_board_profile.py
"""

from __future__ import annotations

import copy
import functools
import json
import struct
import subprocess
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path

import pytest

from esp_prog2_flasher.board_profile import (
    CANCFG_IMAGE_SIZE,
    PROFILE_RECORD_MAX_SIZE,
    PROFILE_RECORD_OFFSET,
    SOLO_PROFILES,
    ProfileVersion,
    build_cancfg_image,
)
from esp_prog2_flasher.dfu_config import DEFAULT_MANUFACTURER, InvalidNameError
from esp_prog2_flasher.flasher import (
    DEFAULT_BAUD_RATE,
    ESP32S3_USB_JTAG_PID,
    ESP_PROG_VID,
    ESPTOOL_PROGRESS,
    NODE_CHIP,
    NODE_ROM_PORT_TIMEOUT_S,
    BundleError,
    DownloadModeEntry,
    FlasherError,
    FlashFailedError,
    LogCallback,
    ProgressCallback,
    SerialPortInfo,
    _enter_node_download_mode,
    _esptool_command,
    bundle_profile_version,
    fetch_solo_bundle,
    flash_solo,
    load_solo_bundle,
    otadata_selecting_ota0,
    provision_node,
    read_bundle_version,
    read_node_config,
    release_tag_profile_version,
)

_PORT = "COM7"
_ODU = SOLO_PROFILES["SOLO ODU Controller"]
_IDU = SOLO_PROFILES["SOLO IDU Controller"]
_WRITE_FLASH_OPTIONS = ("--flash-mode", "--flash-freq", "--flash-size")

_MANIFEST: dict[str, object] = {
    "format": 1,
    "version": "1.2.0",
    "chip": "esp32s3",
    "flash_mode": "dio",
    "flash_freq": "80m",
    "files": {
        "bootloader": "bootloader.bin",
        "updater": "can_updater.bin",
        "otadata": "ota_data_initial.bin",
    },
    "layouts": {
        "node-4MB": {
            "table": "partition-table-node-4MB.bin",
            "flash_size": "4MB",
            "bootloader": "0x0",
            "table_offset": "0x8000",
            "otadata": "0xe000",
            "cancfg": "0x11000",
            "factory": "0x20000",
            "ota_0": "0x80000",
        },
        "idu-16MB": {
            "table": "partition-table-idu-16MB.bin",
            "flash_size": "16MB",
            "bootloader": "0x0",
            "table_offset": "0x8000",
            "otadata": "0xe000",
            "cancfg": "0x11000",
            "factory": "0xfa0000",
            "ota_0": "0x20000",
        },
    },
}
_BUNDLE_FILES = {
    "bootloader.bin": b"\xe9bootloader",
    "can_updater.bin": b"\xe9updater",
    "ota_data_initial.bin": b"\xff" * 0x2000,
    "partition-table-node-4MB.bin": b"\xaa\x50node",
    "partition-table-idu-16MB.bin": b"\xaa\x50idu",
}


def _versioned(version: str) -> dict[str, object]:
    return {**_MANIFEST, "version": version}


def _make_bundle(root: Path, manifest: dict[str, object] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, data in _BUNDLE_FILES.items():
        (root / name).write_bytes(data)
    (root / "manifest.json").write_text(json.dumps(manifest or _MANIFEST), encoding="utf-8")
    return root


def _app(tmp_path: Path, data: bytes = b"\xe9app-image") -> Path:
    path = tmp_path / "app.bin"
    path.write_bytes(data)
    return path


class FakeEsptool:
    """An injected esptool runner that emulates the board's flash and records each command.

    Recorded commands drop the ``python -m esptool --chip esp32s3`` prefix and show
    every absolute path as its bare file name, so plans compare as plain lists.
    """

    def __init__(
        self, returncode: int = 0, corrupt_reads: bool = False, reset_returncode: int = 0
    ) -> None:
        self.calls: list[list[str]] = []
        self.flash: dict[int, bytes] = {}
        self._returncode = returncode
        self._corrupt_reads = corrupt_reads
        self._reset_returncode = reset_returncode

    def __call__(
        self, args: list[str], progress: ProgressCallback | None, log: LogCallback | None
    ) -> int:
        prefix = _esptool_command(NODE_CHIP)
        assert args[: len(prefix)] == prefix
        command = args[len(prefix) :]
        self.calls.append([Path(a).name if Path(a).is_absolute() else a for a in command])
        if command == _RESET_COMMAND:
            return self._reset_returncode
        if self._returncode != 0:
            return self._returncode
        if "write-flash" in command:
            self._write(command[command.index("write-flash") + 1 :])
        elif "read-flash" in command:
            start = command.index("read-flash")
            offset, size, out = command[start + 1 : start + 4]
            data = self.flash.get(int(offset, 16), b"\xff" * int(size))[: int(size)]
            if self._corrupt_reads:
                data = bytes([data[0] ^ 0xFF]) + data[1:]
            Path(out).write_bytes(data)
        return 0

    def _write(self, rest: list[str]) -> None:
        while rest and rest[0] in _WRITE_FLASH_OPTIONS:
            rest = rest[2:]
        for offset, path in zip(rest[::2], rest[1::2], strict=True):
            self.flash[int(offset, 16)] = Path(path).read_bytes()


def _port_as_is(port: str, _log: LogCallback | None) -> str:
    return port


def _conn(after: str) -> list[str]:
    return ["--port", _PORT, "--baud", str(DEFAULT_BAUD_RATE), "--after", after]


_RESET_COMMAND = [
    "--port",
    _PORT,
    "--before",
    "no-reset",
    "--after",
    "watchdog-reset",
    "chip-id",
]


def _write_bundle(flash_size: str, table: str, factory: str) -> list[str]:
    return [
        *_conn("no-reset"),
        "write-flash",
        "--flash-mode",
        "dio",
        "--flash-freq",
        "80m",
        "--flash-size",
        flash_size,
        "0x0",
        "bootloader.bin",
        "0x8000",
        table,
        "0xE000",
        "ota_data_initial.bin",
        factory,
        "can_updater.bin",
        "0x11000",
        "cancfg.bin",
    ]


def _golden_cancfg(product: str, version: ProfileVersion = 1) -> bytes:
    profile = SOLO_PROFILES[product]
    block = profile.config_block(DEFAULT_MANUFACTURER)
    return build_cancfg_image(block, profile.record_for(version), version)


def test_odu_plan_without_app(tmp_path: Path) -> None:
    fake = FakeEsptool()
    flash_solo(
        _ODU, _PORT, _make_bundle(tmp_path / "bundle"), enter_download_mode=_port_as_is, runner=fake
    )
    assert fake.calls == [
        _write_bundle("4MB", "partition-table-node-4MB.bin", "0x20000"),
        [*_conn("no-reset"), "read-flash", "0x11000", "4096", "read.bin"],
        _RESET_COMMAND,
    ]
    assert fake.flash[0x11000] == _golden_cancfg("SOLO ODU Controller")
    assert fake.flash[0xE000] == _BUNDLE_FILES["ota_data_initial.bin"]


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.2.0", 1),
        ("1.2.1", 1),
        ("1.2.1-4-gabc1234-dirty", 1),
        ("0.9.9", 1),
        ("1.3.0", 2),
        ("v1.3.0", 2),
        ("1.3.0-2-gabc1234", 2),
        ("1.3.1", 2),
        ("1.10.0", 2),
        ("2.0.0", 2),
        ("1.3", 1),
        ("", 1),
        ("snapshot", 1),
    ],
)
def test_bundle_version_picks_the_record_version(version: str, expected: int) -> None:
    assert bundle_profile_version(version) == expected


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("c-cpp-esp32-v1.2.1", 1),
        ("c-cpp-esp32-v1.3.0", 2),
        ("c-cpp-esp32-v2.0.0", 2),
        ("v9", None),
        ("", None),
    ],
)
def test_release_tag_predicts_the_record_version(tag: str, expected: int | None) -> None:
    assert release_tag_profile_version(tag) == expected


def test_read_bundle_version_reads_the_manifest(tmp_path: Path) -> None:
    assert read_bundle_version(_make_bundle(tmp_path / "b", _versioned("1.3.0"))) == "1.3.0"


@pytest.mark.parametrize(
    "manifest", [None, "{not json", '["list"]', '{"version": 13}', '{"format": 1}']
)
def test_read_bundle_version_without_a_readable_version_is_none(
    tmp_path: Path, manifest: str | None
) -> None:
    if manifest is not None:
        (tmp_path / "manifest.json").write_text(manifest, encoding="utf-8")
    assert read_bundle_version(tmp_path) is None


@pytest.mark.parametrize("product", ["SOLO ODU Controller", "SOLO IDU Controller"])
def test_bundle_with_v2_support_gets_the_v2_record(tmp_path: Path, product: str) -> None:
    fake = FakeEsptool()
    logs: list[str] = []
    bundle = _make_bundle(tmp_path / "b", _versioned("1.3.0"))
    flash_solo(
        SOLO_PROFILES[product],
        _PORT,
        bundle,
        enter_download_mode=_port_as_is,
        runner=fake,
        log=logs.append,
    )
    assert fake.flash[0x11000] == _golden_cancfg(product, 2)
    assert "Board-profile record v2" in logs
    assert not any("WARNING" in line for line in logs)


def test_older_bundle_gets_todays_odu_record_and_a_warning(tmp_path: Path) -> None:
    fake = FakeEsptool()
    logs: list[str] = []
    bundle = _make_bundle(tmp_path / "b", _versioned("1.2.1"))
    flash_solo(_ODU, _PORT, bundle, enter_download_mode=_port_as_is, runner=fake, log=logs.append)
    assert fake.flash[0x11000] == _golden_cancfg("SOLO ODU Controller", 1)
    assert "Board-profile record v1" in logs
    assert any("WARNING: bundle 1.2.1 predates c-cpp-esp32-v1.3.0" in line for line in logs)


def test_older_bundle_gets_the_idu_record_without_a_warning(tmp_path: Path) -> None:
    fake = FakeEsptool()
    logs: list[str] = []
    bundle = _make_bundle(tmp_path / "b", _versioned("1.2.1"))
    flash_solo(_IDU, _PORT, bundle, enter_download_mode=_port_as_is, runner=fake, log=logs.append)
    assert fake.flash[0x11000] == _golden_cancfg("SOLO IDU Controller", 1)
    assert not any("WARNING" in line for line in logs)


def test_idu_plan_with_migration_and_app(tmp_path: Path) -> None:
    fake = FakeEsptool()
    app = _app(tmp_path)
    flash_solo(
        _IDU,
        _PORT,
        _make_bundle(tmp_path / "bundle"),
        app=app,
        migrate_stock=True,
        enter_download_mode=_port_as_is,
        runner=fake,
    )
    assert fake.calls == [
        [*_conn("no-reset"), "erase-region", "0x10000", "0x10000"],
        _write_bundle("16MB", "partition-table-idu-16MB.bin", "0xFA0000"),
        [*_conn("no-reset"), "read-flash", "0x11000", "4096", "read.bin"],
        [
            *_conn("no-reset"),
            "write-flash",
            "0x20000",
            "app.bin",
            "0xE000",
            "otadata_ota0.bin",
        ],
        _RESET_COMMAND,
    ]
    assert fake.flash[0x11000] == _golden_cancfg("SOLO IDU Controller")
    assert fake.flash[0xFA0000] == _BUNDLE_FILES["can_updater.bin"]
    assert fake.flash[0x20000] == app.read_bytes()
    assert fake.flash[0xE000] == otadata_selecting_ota0()


def test_odu_app_goes_to_its_own_ota_0(tmp_path: Path) -> None:
    fake = FakeEsptool()
    flash_solo(
        _ODU,
        _PORT,
        _make_bundle(tmp_path / "bundle"),
        app=_app(tmp_path),
        enter_download_mode=_port_as_is,
        runner=fake,
    )
    assert fake.calls[-2][-5:] == [
        "write-flash",
        "0x80000",
        "app.bin",
        "0xE000",
        "otadata_ota0.bin",
    ]


def test_custom_manufacturer_lands_in_cancfg(tmp_path: Path) -> None:
    fake = FakeEsptool()
    flash_solo(
        _ODU,
        _PORT,
        _make_bundle(tmp_path / "b"),
        manufacturer="Acme",
        enter_download_mode=_port_as_is,
        runner=fake,
    )
    assert fake.flash[0x11000] == build_cancfg_image(_ODU.config_block("Acme"), _ODU.record_for(1))


def test_readback_mismatch_fails_before_the_app(tmp_path: Path) -> None:
    fake = FakeEsptool(corrupt_reads=True)
    with pytest.raises(FlashFailedError, match=r"read-back mismatch at 0x11000.*\+0x0"):
        flash_solo(
            _IDU,
            _PORT,
            _make_bundle(tmp_path / "b"),
            app=_app(tmp_path),
            enter_download_mode=_port_as_is,
            runner=fake,
        )
    assert [call[6] for call in fake.calls] == ["write-flash", "read-flash"]


def test_esptool_failure_raises(tmp_path: Path) -> None:
    fake = FakeEsptool(returncode=2)
    with pytest.raises(FlashFailedError, match="exited with code 2.*Hold BOOT"):
        flash_solo(
            _ODU, _PORT, _make_bundle(tmp_path / "b"), enter_download_mode=_port_as_is, runner=fake
        )
    assert len(fake.calls) == 1


def test_failing_final_reset_does_not_fail_the_flash(tmp_path: Path) -> None:
    fake = FakeEsptool(reset_returncode=1)
    logs: list[str] = []
    flash_solo(
        _ODU,
        _PORT,
        _make_bundle(tmp_path / "b"),
        enter_download_mode=_port_as_is,
        runner=fake,
        log=logs.append,
    )
    assert fake.calls[-1] == _RESET_COMMAND
    assert "board rebooted (the USB port re-enumerates)" in logs


def test_failing_write_still_fails_and_skips_the_reset(tmp_path: Path) -> None:
    fake = FakeEsptool(returncode=1, reset_returncode=1)
    with pytest.raises(FlashFailedError, match="exited with code 1"):
        flash_solo(
            _ODU, _PORT, _make_bundle(tmp_path / "b"), enter_download_mode=_port_as_is, runner=fake
        )
    assert _RESET_COMMAND not in fake.calls


def _mutated(mutate: Callable[[dict[str, object]], None]) -> dict[str, object]:
    manifest = copy.deepcopy(_MANIFEST)
    mutate(manifest)
    return manifest


def _layout(manifest: dict[str, object], name: str) -> dict[str, object]:
    layouts = manifest["layouts"]
    assert isinstance(layouts, dict)
    layout = layouts[name]
    assert isinstance(layout, dict)
    return layout


def _files(manifest: dict[str, object]) -> dict[str, object]:
    files = manifest["files"]
    assert isinstance(files, dict)
    return files


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        pytest.param(lambda m: m.update(format=2), "'format' is 2", id="format"),
        pytest.param(lambda m: m.update(format=True), "'format' is True", id="format-bool"),
        pytest.param(lambda m: m.update(chip="esp32"), "'chip'", id="chip"),
        pytest.param(lambda m: m.update(flash_mode="qio"), "'flash_mode'", id="flash-mode"),
        pytest.param(lambda m: m.update(flash_freq="40m"), "'flash_freq'", id="flash-freq"),
        pytest.param(lambda m: m.pop("version"), "no 'version'", id="no-version"),
        pytest.param(lambda m: m.pop("files"), "no 'files'", id="no-files"),
        pytest.param(lambda m: m.update(files=[]), "'files' must be an object", id="files-list"),
        pytest.param(lambda m: _files(m).pop("updater"), "no 'files.updater'", id="no-updater"),
        pytest.param(
            lambda m: _files(m).update(updater="missing.bin"), "is missing", id="missing-file"
        ),
        pytest.param(
            lambda m: _files(m).update(updater="../can_updater.bin"),
            "must be a file name",
            id="path-escape",
        ),
        pytest.param(
            lambda m: _layout(m, "node-4MB").pop("factory"),
            "no 'layouts.node-4MB.factory'",
            id="no-factory",
        ),
        pytest.param(
            lambda m: _layout(m, "node-4MB").update(ota_0="80000"),
            "must be a hex offset",
            id="offset-not-hex",
        ),
        pytest.param(
            lambda m: _layout(m, "node-4MB").update(ota_0=0x80000),
            "must be a string",
            id="offset-int",
        ),
        pytest.param(
            lambda m: _layout(m, "node-4MB").update(cancfg="0x12000"),
            "reads cancfg at 0x11000",
            id="cancfg-moved",
        ),
        pytest.param(
            lambda m: _layout(m, "node-4MB").update(flash_size="8MB"),
            "is 8MB",
            id="flash-size-mismatch",
        ),
        pytest.param(
            lambda m: m.update(layouts={"idu-16MB": _layout(m, "idu-16MB")}),
            "no 'node-4MB' layout",
            id="layout-absent",
        ),
    ],
)
def test_bad_manifest_fails_before_esptool(
    tmp_path: Path, mutate: Callable[[dict[str, object]], None], reason: str
) -> None:
    fake = FakeEsptool()
    bundle = _make_bundle(tmp_path / "b", _mutated(mutate))
    with pytest.raises(BundleError, match=reason):
        flash_solo(_ODU, _PORT, bundle, enter_download_mode=_port_as_is, runner=fake)
    assert fake.calls == []


def test_missing_manifest_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="No manifest.json"):
        load_solo_bundle(tmp_path)


def test_manifest_that_is_not_json_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(BundleError, match="not valid JSON"):
        load_solo_bundle(tmp_path)


def test_valid_bundle_loads(tmp_path: Path) -> None:
    bundle = load_solo_bundle(_make_bundle(tmp_path))
    assert bundle.version == "1.2.0"
    assert bundle.updater == tmp_path / "can_updater.bin"
    idu = bundle.layouts["idu-16MB"]
    assert (idu.flash_size, idu.factory, idu.ota_0, idu.otadata) == (
        "16MB",
        0xFA0000,
        0x20000,
        0xE000,
    )


def test_non_esp_app_fails_before_esptool(tmp_path: Path) -> None:
    fake = FakeEsptool()
    with pytest.raises(FlasherError, match="not an ESP app image"):
        flash_solo(
            _ODU,
            _PORT,
            _make_bundle(tmp_path / "b"),
            app=_app(tmp_path, b"PK"),
            enter_download_mode=_port_as_is,
            runner=fake,
        )
    assert fake.calls == []


def test_missing_app_fails_before_esptool(tmp_path: Path) -> None:
    fake = FakeEsptool()
    with pytest.raises(FileNotFoundError):
        flash_solo(
            _ODU,
            _PORT,
            _make_bundle(tmp_path / "b"),
            app=tmp_path / "x.bin",
            enter_download_mode=_port_as_is,
            runner=fake,
        )
    assert fake.calls == []


def test_bad_manufacturer_fails_before_esptool(tmp_path: Path) -> None:
    fake = FakeEsptool()
    with pytest.raises(InvalidNameError):
        flash_solo(
            _ODU,
            _PORT,
            _make_bundle(tmp_path / "b"),
            manufacturer="café",
            enter_download_mode=_port_as_is,
            runner=fake,
        )
    assert fake.calls == []


def test_otadata_selects_ota0_with_bench_crc() -> None:
    image = otadata_selecting_ota0()
    assert len(image) == 0x2000
    expected_entry = bytes.fromhex("01000000" + "ff" * 20 + "ffffffff" + "9a984347")
    assert image[:32] == expected_entry
    assert image[32:] == b"\xff" * (0x2000 - 32)
    (crc,) = struct.unpack_from("<I", image, 28)
    assert crc == 0x4743989A == zlib.crc32(b"\x01\x00\x00\x00", 0xFFFFFFFF)


def test_provision_node_without_profile_writes_the_block_only() -> None:
    fake = FakeEsptool()
    provision_node(
        _PORT,
        DEFAULT_MANUFACTURER,
        "SOLO ODU Controller",
        0x05,
        0x06,
        enter_download_mode=_port_as_is,
        runner=fake,
    )
    assert fake.flash[0x11000] == _ODU.config_block(DEFAULT_MANUFACTURER)
    assert [call[6] for call in fake.calls[:-1]] == ["read-flash", "write-flash", "read-flash"]


def test_provision_node_with_profile_writes_the_cancfg_image() -> None:
    fake = FakeEsptool()
    provision_node(
        _PORT,
        DEFAULT_MANUFACTURER,
        "SOLO IDU Controller",
        0x07,
        0x08,
        profile=_IDU,
        enter_download_mode=_port_as_is,
        runner=fake,
    )
    assert fake.flash[0x11000] == _golden_cancfg("SOLO IDU Controller")
    assert [call[6] for call in fake.calls[:-1]] == [
        "read-flash",
        "read-flash",
        "write-flash",
        "read-flash",
    ]
    assert fake.calls[1][-3:] == ["0x11000", str(0x50 + 56), "read.bin"]
    assert fake.calls[-2][-3:] == ["0x11000", "4096", "read.bin"]


@pytest.mark.parametrize(("held", "written"), [(1, 1), (2, 2)])
def test_provision_node_keeps_the_nodes_record_version(
    held: ProfileVersion, written: ProfileVersion
) -> None:
    fake = FakeEsptool()
    fake.flash[0x11000] = _golden_cancfg("SOLO ODU Controller", held)
    logs: list[str] = []
    provision_node(
        _PORT,
        DEFAULT_MANUFACTURER,
        "SOLO ODU Controller",
        0x05,
        0x06,
        profile=_ODU,
        enter_download_mode=_port_as_is,
        runner=fake,
        log=logs.append,
    )
    assert fake.flash[0x11000] == _golden_cancfg("SOLO ODU Controller", written)
    assert any(f"Writing board-profile record v{written}" in line for line in logs)


def _node_with_cancfg(image: bytes) -> FakeEsptool:
    fake = FakeEsptool()
    fake.flash[0x11000] = image
    return fake


def test_read_node_config_decodes_block_and_profile_in_one_read() -> None:
    fake = _node_with_cancfg(_golden_cancfg("SOLO IDU Controller"))
    node = read_node_config(_PORT, enter_download_mode=_port_as_is, runner=fake)
    assert fake.calls[-2][-3:] == [
        "0x11000",
        str(PROFILE_RECORD_OFFSET + PROFILE_RECORD_MAX_SIZE),
        "read.bin",
    ]
    assert node.config is not None and node.config.product == "SOLO IDU Controller"
    assert node.profile == _IDU.record


def test_read_node_config_decodes_a_v2_profile() -> None:
    node = read_node_config(
        _PORT,
        enter_download_mode=_port_as_is,
        runner=_node_with_cancfg(_golden_cancfg("SOLO ODU Controller", 2)),
    )
    assert node.profile == _ODU.record


def test_read_node_config_reports_an_erased_partition_as_undecoded() -> None:
    node = read_node_config(
        _PORT,
        enter_download_mode=_port_as_is,
        runner=_node_with_cancfg(bytes([0xFF]) * CANCFG_IMAGE_SIZE),
    )
    assert node.config is None and node.profile is None


# Each node path, run on ``port`` with the given download-mode entry; tmp_path holds a bundle.
NodeRun = Callable[[FakeEsptool, str, DownloadModeEntry, Path], None]


def _run_read_node_config(
    fake: FakeEsptool, port: str, entry: DownloadModeEntry, _tmp: Path
) -> None:
    read_node_config(port, enter_download_mode=entry, runner=fake)


def _run_provision_node_without_profile(
    fake: FakeEsptool, port: str, entry: DownloadModeEntry, _tmp: Path
) -> None:
    provision_node(
        port,
        DEFAULT_MANUFACTURER,
        "SOLO ODU Controller",
        0x05,
        0x06,
        enter_download_mode=entry,
        runner=fake,
    )


def _run_provision_node_with_profile(
    fake: FakeEsptool, port: str, entry: DownloadModeEntry, _tmp: Path
) -> None:
    provision_node(
        port,
        DEFAULT_MANUFACTURER,
        "SOLO IDU Controller",
        0x07,
        0x08,
        profile=_IDU,
        enter_download_mode=entry,
        runner=fake,
    )


def _run_flash_solo(fake: FakeEsptool, port: str, entry: DownloadModeEntry, tmp: Path) -> None:
    flash_solo(_ODU, port, _make_bundle(tmp / "bundle"), enter_download_mode=entry, runner=fake)


_NODE_RUNS = pytest.mark.parametrize(
    "run",
    [
        _run_read_node_config,
        _run_provision_node_without_profile,
        _run_provision_node_with_profile,
        _run_flash_solo,
    ],
)


@_NODE_RUNS
def test_node_calls_never_reset_until_the_final_watchdog_reset(
    tmp_path: Path, run: NodeRun
) -> None:
    fake = FakeEsptool()
    run(fake, _PORT, _port_as_is, tmp_path)
    *steps, last = fake.calls
    assert steps
    assert all(call[call.index("--after") + 1] == "no-reset" for call in steps)
    assert last == _RESET_COMMAND


_APP_PORT = "COM71"
_ROM_PORT = "COM63"
_MAC = "94A990F0DB1C"
_ROM_SERIAL = "94:A9:90:F0:DB:1C"
_ROM_APPEARS_AT_S = 3.0


def _usb_port(device: str, serial_number: str | None) -> SerialPortInfo:
    return SerialPortInfo(device, "USB Serial", ESP_PROG_VID, ESP32S3_USB_JTAG_PID, serial_number)


class FakePorts:
    """The port-listing, 1200-baud-touch, sleep and clock seams of a board on USB.

    ``listed`` is always there; ``rom`` joins it once the board was touched and the fake
    clock reaches ``_ROM_APPEARS_AT_S``. Sleeping only advances the fake clock.
    """

    def __init__(self, listed: list[SerialPortInfo], rom: SerialPortInfo | None) -> None:
        self.listed = listed
        self.rom = rom
        self.touched: list[str] = []
        self.now = 0.0

    def list_ports(self) -> list[SerialPortInfo]:
        if self.rom is not None and self.touched and self.now >= _ROM_APPEARS_AT_S:
            return [*self.listed, self.rom]
        return list(self.listed)

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def entry(self) -> DownloadModeEntry:
        return functools.partial(
            _enter_node_download_mode,
            list_ports=self.list_ports,
            touch=self.touched.append,
            sleep=self.sleep,
            clock=self.clock,
        )


@_NODE_RUNS
@pytest.mark.parametrize("rom_serial", [_ROM_SERIAL, _ROM_SERIAL.lower()])
def test_running_board_is_touched_and_followed_to_its_rom_port(
    tmp_path: Path, run: NodeRun, rom_serial: str
) -> None:
    ports = FakePorts([_usb_port(_APP_PORT, _MAC)], _usb_port(_ROM_PORT, rom_serial))
    fake = FakeEsptool()
    run(fake, _APP_PORT, ports.entry(), tmp_path)
    assert ports.touched == [_APP_PORT]
    assert fake.calls
    assert all(call[call.index("--port") + 1] == _ROM_PORT for call in fake.calls)
    assert fake.calls[-1][-1] == "chip-id"


def test_switch_to_the_rom_port_is_logged() -> None:
    ports = FakePorts([_usb_port(_APP_PORT, _MAC)], _usb_port(_ROM_PORT, _ROM_SERIAL))
    logs: list[str] = []
    assert ports.entry()(_APP_PORT, logs.append) == _ROM_PORT
    assert f"{_APP_PORT} -> ROM download port {_ROM_PORT}" in logs


@_NODE_RUNS
def test_rom_port_never_appearing_fails_before_esptool(tmp_path: Path, run: NodeRun) -> None:
    # Another board's ROM port is listed: a 303A:1001 port alone is never followed.
    other_board = _usb_port("COM9", "AA:BB:CC:DD:EE:FF")
    ports = FakePorts([_usb_port(_APP_PORT, _MAC), other_board], rom=None)
    fake = FakeEsptool()
    with pytest.raises(
        FlashFailedError, match=r"Hold BOOT while plugging in or resetting.*different COM number"
    ):
        run(fake, _APP_PORT, ports.entry(), tmp_path)
    assert ports.touched == [_APP_PORT]
    assert fake.calls == []
    assert NODE_ROM_PORT_TIMEOUT_S <= ports.now < NODE_ROM_PORT_TIMEOUT_S + 1


@pytest.mark.parametrize(
    "listed",
    [
        pytest.param([_usb_port(_PORT, _ROM_SERIAL)], id="already-rom-port"),
        pytest.param([_usb_port(_PORT, None)], id="no-serial"),
        pytest.param([_usb_port(_PORT, "A50285BI")], id="not-a-mac"),
        pytest.param([_usb_port("COM9", _MAC)], id="port-not-listed"),
    ],
)
def test_other_ports_are_used_as_given_without_a_touch(
    tmp_path: Path, listed: list[SerialPortInfo]
) -> None:
    ports = FakePorts(listed, _usb_port(_ROM_PORT, _ROM_SERIAL))
    fake = FakeEsptool()
    _run_read_node_config(fake, _PORT, ports.entry(), tmp_path)
    assert ports.touched == []
    assert fake.calls
    assert all(call[call.index("--port") + 1] == _PORT for call in fake.calls)


def _fake_gh(
    returncode: int = 0, stderr: str = "", zip_members: dict[str, bytes] | None = None
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if zip_members is not None:
            out_dir = Path(args[args.index("-D") + 1])
            with zipfile.ZipFile(out_dir / "esp32s3-can-updater-1.2.0.zip", "w") as archive:
                for name, data in zip_members.items():
                    archive.writestr(name, data)
        return subprocess.CompletedProcess(["gh", *args], returncode, "", stderr)

    return run


@pytest.mark.parametrize("folder", ["", "esp32s3-can-updater-1.2.0/"])
def test_fetch_downloads_and_unzips_the_bundle(tmp_path: Path, folder: str) -> None:
    members = {folder + name: data for name, data in _BUNDLE_FILES.items()}
    members[folder + "manifest.json"] = json.dumps(_MANIFEST).encode()
    calls: list[list[str]] = []
    gh = _fake_gh(zip_members=members)

    def recording_gh(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return gh(args)

    bundle_dir = fetch_solo_bundle("c-cpp-esp32-v1.2.1", tmp_path, run_gh=recording_gh)
    assert calls[0][:7] == [
        "release",
        "download",
        "-R",
        "XiaoKChen/Embedded-CAN-Bootloaders",
        "c-cpp-esp32-v1.2.1",
        "-p",
        "esp32s3-can-updater-*.zip",
    ]
    assert load_solo_bundle(bundle_dir).version == "1.2.0"


def test_fetch_reports_a_missing_release(tmp_path: Path) -> None:
    gh = _fake_gh(returncode=1, stderr="release not found")
    with pytest.raises(BundleError, match="'c-cpp-esp32-v9.9.9' not found.*not be published"):
        fetch_solo_bundle("c-cpp-esp32-v9.9.9", tmp_path, run_gh=gh)


def test_fetch_reports_missing_gh(tmp_path: Path) -> None:
    def no_gh(args: list[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("gh")

    with pytest.raises(BundleError, match="not on PATH"):
        fetch_solo_bundle("c-cpp-esp32-v1.2.1", tmp_path, run_gh=no_gh)


def test_fetch_reports_no_asset(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="got 0"):
        fetch_solo_bundle("c-cpp-esp32-v1.2.1", tmp_path, run_gh=_fake_gh())


@pytest.mark.parametrize(
    ("line", "percent"),
    [
        ("Writing at 0x00020000 [                              ]   0.0% 0/146216 bytes...", 0.0),
        (
            "Writing at 0x0002c88d [==>                           ]  11.2% 16384/146216 bytes...",
            11.2,
        ),
        (
            "Writing at 0x00fe23b0 [==============================] 100.0% 146216/146216 bytes...",
            100.0,
        ),
        ("Reading from 0x00011088 [==============================] 100.0% 136/136 bytes...", 100.0),
    ],
)
def test_esptool_progress_reads_esptool_5_progress_lines(line: str, percent: float) -> None:
    match = ESPTOOL_PROGRESS.search(line)
    assert match is not None
    assert float(match.group(1)) == percent


@pytest.mark.parametrize(
    "line",
    [
        "Wrote 271280 bytes (146216 compressed) at 0x00020000 in 1.6 seconds (1352.7 kbit/s).",
        "Hash of data verified.",
    ],
)
def test_esptool_progress_ignores_other_esptool_lines(line: str) -> None:
    assert ESPTOOL_PROGRESS.search(line) is None
