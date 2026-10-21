"""What the dashboard lists: the flash targets, grouped by chip and link, and this session's runs.

The targets panel shows four groups (RA4M1 and PSoC 6 over SWD, SOLO boards and the
ESP-Prog-2 itself over USB), one row per flash target, then the runs of this session under
"Recent". This module is the plain data behind those rows; screens.py draws them and app.py
acts on them.

Owns:        TARGET_GROUPS (one SOLO row per board_profile.SOLO_PROFILES preset), the
             Target/RecentRun shapes, the recent-run and recent-file limits and the relative-time
             wording.
Not here:    rendering (widgets.py, screens.py), per-target settings and jobs (flows.py),
             saving recents between launches (state.py).
Invariants:  a Target is hashable and its key is unique across TARGET_GROUPS.
Depends on:  board_profile.py and the standard library.
See also:    screens.py (DashboardScreen), ../app.py
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from ..board_profile import SOLO_PROFILES

TargetKind = Literal["boot", "app", "psoc6", "solo", "probe"]
Operation = Literal["flash", "provision", "read", "download"]

MAX_RECENTS: Final[int] = 10
MAX_RECENT_FILES: Final[int] = 5  # per picker kind, atop the file browser
_SECONDS_PER_MINUTE: Final[int] = 60
_SECONDS_PER_HOUR: Final[int] = 3600
_SECONDS_PER_DAY: Final[int] = 86400
# SOLO preset products end in this; the Recent rows drop it to stay short.
_PRODUCT_SUFFIX: Final[str] = " Controller"


@dataclass(frozen=True, slots=True)
class Target:
    """One thing the TUI can flash; ``product`` names the SOLO preset for kind "solo"."""

    kind: TargetKind
    label: str
    product: str | None = None

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.product or ''}"

    @property
    def short(self) -> str:
        """A compact name for the Recent rows."""
        if self.product is not None:
            return self.product.removesuffix(_PRODUCT_SUFFIX)
        return _SHORT_NAMES[self.kind]


_SHORT_NAMES: Final[dict[TargetKind, str]] = {
    "boot": "RA4M1 boot",
    "app": "RA4M1 app",
    "psoc6": "PSoC 6",
    "solo": "SOLO",
    "probe": "ESP-Prog-2",
}


@dataclass(frozen=True, slots=True)
class TargetGroup:
    """A fold-able group of targets that share a chip family and a link."""

    id: str
    title: str
    targets: tuple[Target, ...]


BOOT_TARGET: Final[Target] = Target("boot", "Bootloader + config")
APP_TARGET: Final[Target] = Target("app", "Application")
PSOC6_TARGET: Final[Target] = Target("psoc6", "Full image")
PROBE_TARGET: Final[Target] = Target("probe", "Probe firmware")
SOLO_TARGETS: Final[tuple[Target, ...]] = tuple(
    Target("solo", product, product) for product in SOLO_PROFILES
)

TARGET_GROUPS: Final[tuple[TargetGroup, ...]] = (
    TargetGroup("ra4m1", "RA4M1 · SWD", (BOOT_TARGET, APP_TARGET)),
    TargetGroup("psoc6", "PSoC 6 · SWD", (PSOC6_TARGET,)),
    TargetGroup("solo", "SOLO boards · USB", SOLO_TARGETS),
    TargetGroup("probe", "ESP-Prog-2 · USB", (PROBE_TARGET,)),
)


@dataclass(frozen=True, slots=True)
class RecentRun:
    """One finished job, shown under Recent (restored from the saved state on launch)."""

    id: int
    target: Target
    operation: Operation
    detail: str  # the port or image it ran against
    ok: bool
    message: str
    at: datetime


def relative_time(then: datetime, now: datetime) -> str:
    """Say how long ago ``then`` was, coarsely: ``just now``, ``5m ago``, ``2h ago``, ``1d ago``."""
    seconds = int((now - then).total_seconds())
    if seconds < _SECONDS_PER_MINUTE:
        return "just now"
    if seconds < _SECONDS_PER_HOUR:
        return f"{seconds // _SECONDS_PER_MINUTE}m ago"
    if seconds < _SECONDS_PER_DAY:
        return f"{seconds // _SECONDS_PER_HOUR}h ago"
    return f"{seconds // _SECONDS_PER_DAY}d ago"
