"""What each target's wizard asks, what its answers change, and the job they start.

Every write starts as a flow: the steps worth asking (image, device and CAN IDs, port,
bundle source, ...) prefilled from the session's Settings, then the confirm summary that
lists the chip or board, image(s), address or layout and config — the same rows the old
confirm dialog showed. Resolving a flow's answers yields the updated Settings and a typed
job request; FlasherApp runs the job with the same flasher calls cli.py makes.

Owns:        Settings (the per-session choices: images, device and names, CAN IDs, SOLO port,
             bundle, app, migrate flag), the job request types, the wizard steps and their
             validation, the confirm summaries, and the details-pane fields for a target.
Not here:    running jobs and every flasher call (../app.py); the wizard and browser screens
             (wizard.py, browser.py); config-block encoding (dfu_config.py).
Invariants:  a KNOWN device or SOLO preset always carries its fixed CAN ID pair from
             dfu_config.DEVICE_CAN_IDS; a custom pair is both-or-neither and 11-bit. Resolving
             is pure: Settings change only through the returned copy.
Errors:      bad typed input is a validation message on the field, never an exception.
Depends on:  flasher.py constants and SerialPortInfo (no hardware calls), dfu_config.py,
             board_profile.py, wizard.py, browser.py.
See also:    ../app.py, ../cli.py, tests/test_app.py
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal

from .. import flasher
from ..board_profile import CANCFG_IMAGE_SIZE, SOLO_PROFILES, Bitrate, BoardProfile
from ..dfu_config import (
    CONFIG_ADDRESS,
    DEFAULT_BOOT_CMD_ID,
    DEFAULT_BOOT_REPLY_ID,
    DEFAULT_MANUFACTURER,
    DEVICE_CAN_IDS,
    DEVICE_NAMES,
    MAX_CAN_ID,
    boot_ids_for_device,
)
from .browser import BrowseSpec, start_folder
from .targets import Target
from .widgets import Field
from .wizard import BROWSE_VALUE, Answers, Choice, ChoiceStep, Confirm, Step, TextStep, WizardSpec

# Choice values that are not answers in themselves.
KEEP_DEFAULT_DEVICE: Final[str] = "\x00keep"
CUSTOM_DEVICE: Final[str] = "\x00custom"
TYPE_PORT: Final[str] = "\x00type"
RELEASE_BUNDLE: Final[str] = "\x00release"
OTHER_TAG: Final[str] = "\x00tag"
NO_APP: Final[str] = "\x00none"

# Embassy bootloader reserves 32 KiB for itself, so its app slot starts here
# (vs. the 16 KiB C bootloader's flasher.APP_BASE_ADDRESS == 0x4000).
EMBASSY_APP_BASE_ADDRESS: Final[int] = 0x8000
# ESP32-S3 USB-Serial-JTAG / ROM download PID: a SOLO board, or an ESP-Prog-2 in download mode.
_ESP32S3_USB_JTAG_PID: Final[int] = 0x1001
_BITRATE_LABELS: Final[dict[Bitrate, str]] = {
    Bitrate.KBPS_125: "125 kbit/s",
    Bitrate.KBPS_250: "250 kbit/s",
    Bitrate.KBPS_500: "500 kbit/s",
    Bitrate.MBPS_1: "1 Mbit/s",
}
_MISSING_IMAGE: Final[str] = "none — / f picks one"
# The SWD targets that flash one image file, and how the browser picks it.
ImageKind = Literal["boot", "app", "psoc6"]
IMAGE_KINDS: Final[tuple[ImageKind, ...]] = ("boot", "app", "psoc6")
_IMAGE_PICKS: Final[dict[ImageKind, tuple[str, str]]] = {
    "boot": (".hex", "Select bootloader .hex"),
    "app": (".bin", "Select application .bin"),
    "psoc6": (".hex", "Select PSoC 6 .hex"),
}


@dataclass(frozen=True, slots=True)
class Settings:
    """This session's choices, prefilled into every wizard and shown in the details pane."""

    boot_hex: str = ""
    boot_device: str = KEEP_DEFAULT_DEVICE  # KEEP_DEFAULT_DEVICE, a DEVICE_NAMES entry or CUSTOM
    boot_custom_name: str = ""
    boot_cmd_id: str = ""  # custom device only; blank = bootloader default
    boot_reply_id: str = ""
    boot_manufacturer: str = DEFAULT_MANUFACTURER
    app_bin: str = ""
    embassy: bool = False
    psoc6_hex: str = ""
    solo_port: str = ""
    solo_tag: str = flasher.DEFAULT_SOLO_BUNDLE_TAG
    solo_bundle_dir: str = ""  # set: use this unzipped bundle instead of the release
    solo_app: str = ""
    solo_migrate: bool = False
    solo_manufacturer: str = DEFAULT_MANUFACTURER

    @property
    def app_base_address(self) -> int:
        return EMBASSY_APP_BASE_ADDRESS if self.embassy else flasher.APP_BASE_ADDRESS


@dataclass(frozen=True, slots=True)
class BootFlashJob:
    hex_path: Path
    manufacturer: str | None
    product: str | None
    boot_cmd_id: int | None
    boot_reply_id: int | None


@dataclass(frozen=True, slots=True)
class AppFlashJob:
    bin_path: Path
    base_address: int


@dataclass(frozen=True, slots=True)
class Psoc6FlashJob:
    hex_path: Path


@dataclass(frozen=True, slots=True)
class SoloFlashJob:
    profile: BoardProfile
    port: str
    bundle_dir: Path | None  # None: fetch release ``tag`` with gh
    tag: str
    app: Path | None
    migrate_stock: bool
    manufacturer: str


@dataclass(frozen=True, slots=True)
class SoloProvisionJob:
    profile: BoardProfile
    port: str
    manufacturer: str


@dataclass(frozen=True, slots=True)
class SoloReadJob:
    port: str


@dataclass(frozen=True, slots=True)
class ProbeFlashJob:
    firmware: Path
    port: str
    in_download_mode: bool


Job = (
    BootFlashJob
    | AppFlashJob
    | Psoc6FlashJob
    | SoloFlashJob
    | SoloProvisionJob
    | SoloReadJob
    | ProbeFlashJob
)


@dataclass(frozen=True, slots=True)
class JobFlow:
    """A wizard whose answers resolve to new Settings plus the job to run."""

    spec: WizardSpec
    resolve: Callable[[Answers], tuple[Settings, Job]]


@dataclass(frozen=True, slots=True)
class EditFlow:
    """A wizard whose answers only change Settings."""

    spec: WizardSpec
    resolve: Callable[[Answers], Settings]


# ----- small helpers -------------------------------------------------------------------


def parse_can_id(text: str) -> int:
    """Parse a CAN ID accepting hex (``0x700``) or decimal; raise ValueError on bad input."""
    value = int(text, 0)
    if not 0 <= value <= MAX_CAN_ID:
        raise ValueError(f"CAN ID must be 0x000..0x{MAX_CAN_ID:03X}")
    return value


def can_ids_text(boot_cmd_id: int | None, boot_reply_id: int | None) -> str:
    """Describe the boot CAN ID pair a config write carries, defaults included."""
    if boot_cmd_id is None or boot_reply_id is None:
        return f"bootloader defaults 0x{DEFAULT_BOOT_CMD_ID:03X} / 0x{DEFAULT_BOOT_REPLY_ID:03X}"
    return f"host->boot 0x{boot_cmd_id:03X}  ·  boot->host 0x{boot_reply_id:03X}"


def _id_pair(ids: tuple[int, int]) -> str:
    return f"0x{ids[0]:02X} / 0x{ids[1]:02X}"


def port_label(port: flasher.SerialPortInfo) -> str:
    """Name a serial port by what it is when its USB ID says so, else by its OS description."""
    if port.is_esp_prog_bridge:
        return "ESP-Prog-2 bridge (not a SOLO board)"
    if port.vid == flasher.ESP_PROG_VID and port.pid == _ESP32S3_USB_JTAG_PID:
        return "ESP32-S3 USB (SOLO board, or ESP-Prog-2 in download mode)"
    return port.description


def _required(message: str) -> Callable[[str, Answers], str | None]:
    return lambda value, _answers: None if value else message


def _can_id_or_blank(value: str, _answers: Answers) -> str | None:
    if not value:
        return None
    try:
        parse_can_id(value)
    except ValueError as exc:
        return f"Invalid boot CAN ID: {exc}"
    return None


def _reply_id(value: str, answers: Answers) -> str | None:
    if bool(answers.get("cmd_id")) != bool(value):
        return (
            "Set both boot CAN IDs (host->boot and boot->host), or leave both blank to "
            "keep bootloader defaults."
        )
    return _can_id_or_blank(value, answers)


def _job_flow(
    title: str,
    subtitle: str,
    steps: Callable[[Answers], Sequence[Step]],
    resolve: Callable[[Answers], tuple[Settings, Job]],
    confirm: bool = True,
) -> JobFlow:
    """A JobFlow whose confirm summary is the summary of the job its answers resolve to."""
    spec = WizardSpec(
        title, subtitle, steps, (lambda a: summary(resolve(a)[1])) if confirm else None
    )
    return JobFlow(spec, resolve)


# ----- steps ---------------------------------------------------------------------------


def _image_step(key: str, label: str, current: str, suffix: str, title: str) -> ChoiceStep:
    choices = []
    if current and Path(current).is_file():
        choices.append(Choice(current, Path(current).name, str(Path(current).parent)))
    choices.append(Choice(BROWSE_VALUE, "Browse…", f"pick a {suffix} file"))
    browse = BrowseSpec(title, suffix, folder=False, start=start_folder(current))
    return ChoiceStep(key, label, tuple(choices), current, browse)


def _device_steps(settings: Settings, answers: Answers) -> list[Step]:
    choices = [Choice(KEEP_DEFAULT_DEVICE, "Keep bootloader default", "no config block")]
    choices += [Choice(name, name, _id_pair(DEVICE_CAN_IDS[name])) for name in DEVICE_NAMES]
    choices.append(Choice(CUSTOM_DEVICE, "Custom…", "type a name and CAN IDs"))
    steps: list[Step] = [
        ChoiceStep("device", "USB device", tuple(choices), settings.boot_device),
    ]
    device = answers.get("device")
    if device == CUSTOM_DEVICE:
        steps += [
            TextStep(
                "custom_name",
                "Custom device name",
                settings.boot_custom_name,
                "type a device name",
                _required("Enter a custom device name, or pick a device from the list."),
            ),
            TextStep(
                "cmd_id",
                "CAN ID host->boot",
                settings.boot_cmd_id,
                f"blank = 0x{DEFAULT_BOOT_CMD_ID:03X}",
                _can_id_or_blank,
            ),
            TextStep(
                "reply_id",
                "CAN ID boot->host",
                settings.boot_reply_id,
                f"blank = 0x{DEFAULT_BOOT_REPLY_ID:03X}",
                _reply_id,
            ),
        ]
    if device is not None and device != KEEP_DEFAULT_DEVICE:
        steps.append(
            TextStep(
                "manufacturer",
                "USB manufacturer",
                settings.boot_manufacturer,
                validate=_required("Enter a USB manufacturer, or keep the bootloader default."),
            )
        )
    return steps


def _port_steps(
    settings: Settings, ports: Sequence[flasher.SerialPortInfo], answers: Answers
) -> list[Step]:
    # The ESP-Prog-2's bridge is never the board: list it last so the cursor never starts on it.
    ordered = sorted(ports, key=lambda port: port.is_esp_prog_bridge)
    choices = [Choice(port.device, port.device, port_label(port)) for port in ordered]
    note = "not in the list" if ports else "no serial ports detected"
    choices.append(Choice(TYPE_PORT, "Type a port…", note))
    steps: list[Step] = [
        ChoiceStep(
            "port", "Board port (the board's own USB port)", tuple(choices), settings.solo_port
        )
    ]
    if answers.get("port") == TYPE_PORT:
        steps.append(
            TextStep(
                "port_text",
                "Board port",
                settings.solo_port,
                "e.g. COM7 or /dev/ttyACM0",
                _required("Enter the board's own serial port."),
            )
        )
    return steps


def _bundle_steps(settings: Settings, answers: Answers) -> list[Step]:
    choices = [
        Choice(RELEASE_BUNDLE, f"Release {settings.solo_tag}", "fetched with gh"),
        Choice(OTHER_TAG, "Another release tag…", "type the tag"),
    ]
    if settings.solo_bundle_dir:
        folder = Path(settings.solo_bundle_dir)
        choices.append(Choice(settings.solo_bundle_dir, folder.name, "local folder"))
    choices.append(Choice(BROWSE_VALUE, "Local folder…", "an unzipped bundle (manifest.json)"))
    browse = BrowseSpec(
        "Select the unzipped SOLO bundle folder",
        "",
        folder=True,
        start=start_folder(settings.solo_bundle_dir),
    )
    default = settings.solo_bundle_dir or RELEASE_BUNDLE
    steps: list[Step] = [ChoiceStep("bundle", "Bundle source", tuple(choices), default, browse)]
    if answers.get("bundle") == OTHER_TAG:
        steps.append(
            TextStep("tag", "Release tag", settings.solo_tag, validate=_required("Enter a tag."))
        )
    return steps


# ----- resolving answers -----------------------------------------------------------------


def _with_device(settings: Settings, answers: Answers) -> Settings:
    return replace(
        settings,
        boot_device=answers.get("device", settings.boot_device),
        boot_custom_name=answers.get("custom_name", settings.boot_custom_name),
        boot_cmd_id=answers.get("cmd_id", settings.boot_cmd_id),
        boot_reply_id=answers.get("reply_id", settings.boot_reply_id),
        boot_manufacturer=answers.get("manufacturer", settings.boot_manufacturer),
    )


def image_of(settings: Settings, kind: ImageKind) -> str:
    """The image an SWD target flashes."""
    return {"boot": settings.boot_hex, "app": settings.app_bin, "psoc6": settings.psoc6_hex}[kind]


def with_image(settings: Settings, kind: ImageKind, path: str) -> Settings:
    if kind == "boot":
        return replace(settings, boot_hex=path)
    if kind == "app":
        return replace(settings, app_bin=path)
    return replace(settings, psoc6_hex=path)


def image_browse(settings: Settings, kind: ImageKind) -> BrowseSpec:
    """The browser that picks an SWD target's image (``/ f``)."""
    suffix, title = _IMAGE_PICKS[kind]
    return BrowseSpec(title, suffix, folder=False, start=start_folder(image_of(settings, kind)))


def _with_port(settings: Settings, answers: Answers) -> Settings:
    port = answers.get("port", settings.solo_port)
    if port == TYPE_PORT:
        port = answers.get("port_text", settings.solo_port)
    return replace(settings, solo_port=port)


def _with_bundle(settings: Settings, answers: Answers) -> Settings:
    bundle = answers.get("bundle")
    if bundle is None:
        return settings
    if bundle == RELEASE_BUNDLE:
        return replace(settings, solo_bundle_dir="")
    if bundle == OTHER_TAG:
        return replace(settings, solo_bundle_dir="", solo_tag=answers.get("tag", settings.solo_tag))
    return replace(settings, solo_bundle_dir=bundle)


def boot_job(settings: Settings) -> BootFlashJob:
    """The RA4M1 bootloader flash the settings describe, resolved like cli.py's --flash."""
    if settings.boot_device == KEEP_DEFAULT_DEVICE:
        product = None
    elif settings.boot_device == CUSTOM_DEVICE:
        product = settings.boot_custom_name
    else:
        product = settings.boot_device
    known_ids = boot_ids_for_device(product) if product else None
    boot_cmd_id: int | None = None
    boot_reply_id: int | None = None
    if known_ids is not None:
        boot_cmd_id, boot_reply_id = known_ids
    elif product and settings.boot_cmd_id and settings.boot_reply_id:
        boot_cmd_id = parse_can_id(settings.boot_cmd_id)
        boot_reply_id = parse_can_id(settings.boot_reply_id)
    manufacturer = settings.boot_manufacturer if product else None
    return BootFlashJob(Path(settings.boot_hex), manufacturer, product, boot_cmd_id, boot_reply_id)


def solo_flash_job(settings: Settings, product: str) -> SoloFlashJob:
    """The SOLO bundle flash the settings describe, resolved like cli.py's --flash-solo."""
    return SoloFlashJob(
        SOLO_PROFILES[product],
        settings.solo_port,
        Path(settings.solo_bundle_dir) if settings.solo_bundle_dir else None,
        settings.solo_tag or flasher.DEFAULT_SOLO_BUNDLE_TAG,
        Path(settings.solo_app) if settings.solo_app else None,
        settings.solo_migrate,
        settings.solo_manufacturer,
    )


# ----- flows -----------------------------------------------------------------------------


def boot_flash_flow(settings: Settings) -> JobFlow:
    def steps(answers: Answers) -> list[Step]:
        image = _image_step(
            "image", "Bootloader image (.hex)", settings.boot_hex, ".hex", "Select bootloader .hex"
        )
        return [image, *_device_steps(settings, answers)]

    def resolve(answers: Answers) -> tuple[Settings, Job]:
        updated = _with_device(
            replace(settings, boot_hex=answers.get("image", settings.boot_hex)), answers
        )
        return updated, boot_job(updated)

    subtitle = f"RA4M1 ({flasher.TARGET_TYPE}) over SWD"
    return _job_flow("Flash the RA4M1 bootloader", subtitle, steps, resolve)


def app_flash_flow(settings: Settings) -> JobFlow:
    c_base = f"0x{flasher.APP_BASE_ADDRESS:X}"
    embassy_base = f"0x{EMBASSY_APP_BASE_ADDRESS:X}"

    def steps(_answers: Answers) -> list[Step]:
        image = _image_step(
            "image", "Application image (.bin)", settings.app_bin, ".bin", "Select application .bin"
        )
        bases = (
            Choice(c_base, c_base, "C bootloader (16 KiB)"),
            Choice(embassy_base, embassy_base, "Embassy bootloader (32 KiB)"),
        )
        default = embassy_base if settings.embassy else c_base
        return [image, ChoiceStep("base", "Application base address", bases, default)]

    def resolve(answers: Answers) -> tuple[Settings, Job]:
        embassy = answers.get("base") == embassy_base if "base" in answers else settings.embassy
        updated = replace(settings, app_bin=answers.get("image", settings.app_bin), embassy=embassy)
        return updated, AppFlashJob(Path(updated.app_bin), updated.app_base_address)

    subtitle = f"RA4M1 ({flasher.TARGET_TYPE}) over SWD, above the bootloader"
    return _job_flow("Flash the RA4M1 application", subtitle, steps, resolve)


def psoc6_flash_flow(settings: Settings) -> JobFlow:
    def steps(_answers: Answers) -> list[Step]:
        return [
            _image_step(
                "image", "PSoC 6 image (.hex)", settings.psoc6_hex, ".hex", "Select PSoC 6 .hex"
            )
        ]

    def resolve(answers: Answers) -> tuple[Settings, Job]:
        updated = replace(settings, psoc6_hex=answers.get("image", settings.psoc6_hex))
        return updated, Psoc6FlashJob(Path(updated.psoc6_hex))

    subtitle = f"IDU Radar PSoC 6 ({flasher.PSOC6_TARGET_TYPE}) over SWD"
    return _job_flow("Flash the PSoC 6", subtitle, steps, resolve)


def _solo_subtitle(profile: BoardProfile) -> str:
    return f"{profile.product} · {profile.layout} · over the board's own USB port"


def solo_flash_flow(
    settings: Settings, product: str, ports: Sequence[flasher.SerialPortInfo]
) -> JobFlow:
    def steps(answers: Answers) -> list[Step]:
        return [*_port_steps(settings, ports, answers), *_bundle_steps(settings, answers)]

    def resolve(answers: Answers) -> tuple[Settings, Job]:
        updated = _with_bundle(_with_port(settings, answers), answers)
        return updated, solo_flash_job(updated, product)

    profile = SOLO_PROFILES[product]
    return _job_flow(f"Flash the {product}", _solo_subtitle(profile), steps, resolve)


def solo_provision_flow(
    settings: Settings, product: str, ports: Sequence[flasher.SerialPortInfo]
) -> JobFlow:
    profile = SOLO_PROFILES[product]

    def resolve(answers: Answers) -> tuple[Settings, Job]:
        updated = _with_port(settings, answers)
        return updated, SoloProvisionJob(profile, updated.solo_port, updated.solo_manufacturer)

    return _job_flow(
        f"Provision the {product}",
        _solo_subtitle(profile),
        lambda answers: _port_steps(settings, ports, answers),
        resolve,
    )


def solo_read_flow(
    settings: Settings, product: str, ports: Sequence[flasher.SerialPortInfo]
) -> JobFlow:
    def resolve(answers: Answers) -> tuple[Settings, Job]:
        updated = _with_port(settings, answers)
        return updated, SoloReadJob(updated.solo_port)

    return _job_flow(
        "Read the board's config",
        _solo_subtitle(SOLO_PROFILES[product]),
        lambda answers: _port_steps(settings, ports, answers),
        resolve,
        confirm=False,
    )


def probe_flash_flow(settings: Settings, job: ProbeFlashJob) -> JobFlow:
    return _job_flow(
        "Flash the ESP-Prog-2 firmware",
        f"ESP-Prog-2 ({flasher.PROBE_CHIP}) via esptool",
        lambda _answers: [],
        lambda _answers: (settings, job),
    )


def boot_config_flow(settings: Settings) -> EditFlow:
    spec = WizardSpec(
        "Edit the RA4M1 config block",
        f"written to data flash 0x{CONFIG_ADDRESS:08X} with the bootloader",
        lambda answers: _device_steps(settings, answers),
    )
    return EditFlow(spec, lambda answers: _with_device(settings, answers))


def solo_manufacturer_flow(settings: Settings) -> EditFlow:
    step = TextStep(
        "manufacturer",
        "USB manufacturer",
        settings.solo_manufacturer,
        validate=_required("Enter a USB manufacturer."),
    )
    spec = WizardSpec(
        "Edit the SOLO config", "the product and CAN IDs come from the preset", lambda _a: [step]
    )
    return EditFlow(
        spec,
        lambda answers: replace(
            settings, solo_manufacturer=answers.get("manufacturer", settings.solo_manufacturer)
        ),
    )


def port_flow(settings: Settings, ports: Sequence[flasher.SerialPortInfo]) -> EditFlow:
    spec = WizardSpec(
        "Pick the SOLO board's port",
        "auto-detect finds the ESP-Prog-2, not the board, so the port is yours to pick",
        lambda answers: _port_steps(settings, ports, answers),
    )
    return EditFlow(spec, lambda answers: _with_port(settings, answers))


def bundle_flow(settings: Settings) -> EditFlow:
    spec = WizardSpec(
        "SOLO bundle source",
        "a release fetched with gh, or an unzipped bundle on disk",
        lambda answers: _bundle_steps(settings, answers),
    )
    return EditFlow(spec, lambda answers: _with_bundle(settings, answers))


def solo_app_flow(settings: Settings) -> EditFlow:
    choices = [Choice(NO_APP, "None", "the board boots the CAN updater")]
    if settings.solo_app:
        choices.append(Choice(settings.solo_app, Path(settings.solo_app).name, "current"))
    choices.append(Choice(BROWSE_VALUE, "Browse…", "pick a .bin file"))
    browse = BrowseSpec(
        "Select SOLO app .bin", ".bin", folder=False, start=start_folder(settings.solo_app)
    )
    step = ChoiceStep(
        "app", "App image for ota_0 (optional)", tuple(choices), settings.solo_app or NO_APP, browse
    )
    spec = WizardSpec(
        "SOLO app image", "written to ota_0 and selected in otadata", lambda _a: [step]
    )

    def resolve(answers: Answers) -> Settings:
        app = answers.get("app", settings.solo_app)
        return replace(settings, solo_app="" if app == NO_APP else app)

    return EditFlow(spec, resolve)


# ----- summaries and details -------------------------------------------------------------


def _solo_config_rows(profile: BoardProfile, manufacturer: str) -> list[Field]:
    record = profile.record
    return [
        ("Manufacturer", manufacturer),
        ("Product", profile.product),
        ("CAN IDs", can_ids_text(*profile.boot_ids)),
        (
            "Profile",
            f"TWAI tx GPIO{record.twai_tx_gpio} rx GPIO{record.twai_rx_gpio}, "
            f"{_BITRATE_LABELS[record.bitrate]}, hold-LOW mask 0x{record.hold_low_mask:X}",
        ),
    ]


def _migration_range() -> str:
    start = flasher.STOCK_MIGRATION_ERASE_OFFSET
    return f"0x{start:X}–0x{start + flasher.STOCK_MIGRATION_ERASE_SIZE - 1:X}"


def summary(job: Job) -> Confirm:
    """What a job will write where — the confirm step's rows, title and verb."""
    rows: list[Field]
    match job:
        case BootFlashJob():
            rows = [
                ("Chip", f"RA4M1 ({flasher.TARGET_TYPE}) over SWD"),
                ("Image", str(job.hex_path)),
                ("Address", "as given by the .hex records (bootloader region)"),
            ]
            if job.product is None:
                rows.append(("Config", "none — the bootloader keeps its compiled-in names"))
            else:
                rows += [
                    ("Config", f"DFU config block @ 0x{CONFIG_ADDRESS:08X} (data flash)"),
                    ("Manufacturer", job.manufacturer or ""),
                    ("Product", job.product),
                    ("CAN IDs", can_ids_text(job.boot_cmd_id, job.boot_reply_id)),
                ]
            return Confirm("Flash the RA4M1 bootloader", tuple(rows), "Flash")
        case AppFlashJob():
            size = job.bin_path.stat().st_size
            base = job.base_address
            rows = [
                ("Chip", f"RA4M1 ({flasher.TARGET_TYPE}) over SWD"),
                ("Image", str(job.bin_path)),
                ("Address", f"0x{base:X}–0x{base + size - 1:X} ({size} bytes)"),
                ("Erase", f"those sectors only; the bootloader below 0x{base:X} is kept"),
            ]
            return Confirm("Flash the RA4M1 application", tuple(rows), "Flash")
        case Psoc6FlashJob():
            rows = [
                ("Chip", f"PSoC 6 ({flasher.PSOC6_TARGET_TYPE}) over SWD"),
                ("Image", str(job.hex_path)),
                (
                    "Address",
                    f"main flash 0x{flasher.PSOC6_FLASH_START:08X}–"
                    f"0x{flasher.PSOC6_FLASH_END - 1:08X} only",
                ),
                ("Skipped", "Cypress metadata is dropped; SFLASH/eFuse data aborts the flash"),
            ]
            return Confirm("Flash the PSoC 6", tuple(rows), "Flash")
        case SoloFlashJob():
            profile = job.profile
            rows = [
                ("Board", f"{profile.product} ({profile.layout}, {profile.flash_size} flash)"),
                ("Port", job.port),
                (
                    "Bundle",
                    str(job.bundle_dir)
                    if job.bundle_dir
                    else f"release {job.tag} (fetched with gh)",
                ),
                (
                    "Writes",
                    "bootloader, partition table, otadata, CAN updater and cancfg at the "
                    "bundle manifest's offsets",
                ),
                (
                    "App",
                    f"{job.app} at ota_0, selected in otadata"
                    if job.app
                    else "none — the board boots the CAN updater",
                ),
            ]
            if job.migrate_stock:
                rows.append(("Erase first", f"{_migration_range()} (stock leftovers)"))
            rows += _solo_config_rows(profile, job.manufacturer)
            return Confirm(f"Flash the {profile.product}", tuple(rows), "Flash")
        case SoloProvisionJob():
            rows = [
                ("Board", job.profile.product),
                ("Port", job.port),
                (
                    "Writes",
                    f"{CANCFG_IMAGE_SIZE}-byte cancfg image (v2 block + board profile) where "
                    "the node's partition table puts cancfg "
                    f"(else 0x{flasher.DEFAULT_CANCFG_OFFSET:X})",
                ),
                *_solo_config_rows(job.profile, job.manufacturer),
            ]
            return Confirm(f"Provision the {job.profile.product}", tuple(rows), "Provision")
        case SoloReadJob():
            return Confirm("Read the board's config", (("Port", job.port),), "Read")
        case ProbeFlashJob():
            mode = "download mode" if job.in_download_mode else "bridge running"
            rows = [
                ("Chip", f"ESP-Prog-2 ({flasher.PROBE_CHIP}) via esptool"),
                ("Port", f"{job.port} ({mode})"),
                ("Image", str(job.firmware)),
                ("Address", f"{flasher.PROBE_FLASH_OFFSET} (merged bootloader + partitions + app)"),
            ]
            return Confirm("Flash the ESP-Prog-2 firmware", tuple(rows), "Flash")


def _device_text(settings: Settings) -> str:
    if settings.boot_device == KEEP_DEFAULT_DEVICE:
        return "keep bootloader default (no config block)"
    if settings.boot_device == CUSTOM_DEVICE:
        return f"{settings.boot_custom_name} (custom)"
    return settings.boot_device


def _bitrate_short(bitrate: Bitrate) -> str:
    """``250 kbit/s`` -> ``250k``, for the narrow details pane."""
    return _BITRATE_LABELS[bitrate].replace(" kbit/s", "k").replace(" Mbit/s", "M")


def _hold_low_text(mask: int) -> str:
    gpios = [f"GPIO{bit}" for bit in range(mask.bit_length()) if mask >> bit & 1]
    return ", ".join(gpios) or "none"


def target_note(target: Target, settings: Settings, probe_firmware: Path | None) -> str:
    """The muted second column of a target's dashboard row."""
    if target.kind == "boot":
        return Path(settings.boot_hex).name or "no image"
    if target.kind == "app":
        return Path(settings.app_bin).name or "no image"
    if target.kind == "psoc6":
        return Path(settings.psoc6_hex).name or "no image"
    if target.kind == "solo" and target.product is not None:
        profile = SOLO_PROFILES[target.product]
        return f"{profile.layout} · {_id_pair(profile.boot_ids)}"
    return probe_firmware.name if probe_firmware else "firmware missing"


def target_fields(target: Target, settings: Settings, probe_firmware: Path | None) -> list[Field]:
    """The settings-derived details of a target (the app adds detect and last-run lines)."""
    if target.kind == "boot":
        job = boot_job(settings)
        fields: list[Field] = [
            ("Chip", f"RA4M1 ({flasher.TARGET_TYPE}) · SWD"),
            ("Image", settings.boot_hex or _MISSING_IMAGE),
            ("Device", _device_text(settings)),
        ]
        if job.product is not None:
            fields += [
                ("Maker", settings.boot_manufacturer),
                ("CAN IDs", can_ids_text(job.boot_cmd_id, job.boot_reply_id)),
            ]
        return fields
    if target.kind == "app":
        loader = "Embassy" if settings.embassy else "C"
        return [
            ("Chip", f"RA4M1 ({flasher.TARGET_TYPE}) · SWD"),
            ("Image", settings.app_bin or _MISSING_IMAGE),
            ("Address", f"0x{settings.app_base_address:X} ({loader} bootloader)"),
        ]
    if target.kind == "psoc6":
        return [
            ("Chip", f"PSoC 6 ({flasher.PSOC6_TARGET_TYPE}) · SWD"),
            ("Image", settings.psoc6_hex or _MISSING_IMAGE),
            (
                "Address",
                f"0x{flasher.PSOC6_FLASH_START:08X}–0x{flasher.PSOC6_FLASH_END - 1:08X}",
            ),
        ]
    if target.kind == "solo" and target.product is not None:
        profile = SOLO_PROFILES[target.product]
        record = profile.record
        # Names only here: the confirm summary shows the full paths.
        bundle = (
            f"{Path(settings.solo_bundle_dir).name} (folder)"
            if settings.solo_bundle_dir
            else f"release {settings.solo_tag}"
        )
        app = Path(settings.solo_app).name if settings.solo_app else "none"
        migrate = "on" if settings.solo_migrate else "off"
        return [
            ("Board", profile.product),
            ("Port", settings.solo_port or "none — / p picks one"),
            ("Layout", f"{profile.layout} ({profile.flash_size} flash)"),
            ("CAN IDs", _id_pair(profile.boot_ids)),
            (
                "TWAI",
                f"{record.twai_tx_gpio}/{record.twai_rx_gpio} @ {_bitrate_short(record.bitrate)}, "
                f"hold LOW {_hold_low_text(record.hold_low_mask)}",
            ),
            ("Maker", settings.solo_manufacturer),
            ("Bundle", bundle),
            ("Options", f"app {app} · migrate {migrate}"),
        ]
    firmware = (
        f"{probe_firmware.name} ({probe_firmware.stat().st_size} bytes)"
        if probe_firmware
        else "missing — get the official image with / g"
    )
    return [
        ("Chip", f"ESP-Prog-2 ({flasher.PROBE_CHIP}) · esptool"),
        ("Firmware", firmware),
        ("Address", flasher.PROBE_FLASH_OFFSET),
    ]
