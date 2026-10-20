"""The TUI's palette: TTSSH's colour tokens as two Textual themes, plus the status symbols.

Every colour the flasher TUI draws comes from one Palette, ported token for token from
TTSSH's internal/ui/theme.go so both tools look alike. The palette becomes a light and a dark
Textual Theme whose extra CSS variables ($muted, $subtle, $panel-border, $focus-border,
$selection-bg, $on-accent) the widgets' CSS uses; nothing else names a colour.

Owns:        the palette values, the theme names, the status symbols and their CSS classes,
             and the choice of light or dark theme for the terminal.
Not here:    layout and widget styles (each widget's DEFAULT_CSS, tui/screens.py).
Invariants:  status is never shown by colour alone: every level has a symbol. Only status
             colours are saturated; everything else is greys plus the neutral accent.
Depends on:  textual only.
See also:    widgets.py, screens.py
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from textual.theme import Theme

DARK_THEME_NAME: Final[str] = "flasher-dark"
LIGHT_THEME_NAME: Final[str] = "flasher-light"

StatusLevel = Literal["ok", "warn", "err", "info"]
STATUS_SYMBOLS: Final[dict[StatusLevel, str]] = {"ok": "✓", "warn": "!", "err": "✗", "info": "·"}

# xterm-style COLORFGBG ends in the background's ANSI colour index; these are light ones.
_LIGHT_BACKGROUND_INDEXES: Final[frozenset[str]] = frozenset({"7", "15"})


@dataclass(frozen=True, slots=True)
class Palette:
    """TTSSH's palette tokens for one terminal background."""

    accent: str  # neutral highlight: app title, focus, key names
    on_accent: str  # text drawn on an accent background
    text: str
    muted: str  # labels, hints, descriptions
    subtle: str  # separators
    border: str  # unfocused panels
    success: str
    warning: str
    error: str
    selection_bg: str  # the selected list row
    background: str

    @property
    def focus_border(self) -> str:
        return self.accent


DARK_PALETTE: Final[Palette] = Palette(
    accent="#FFFFFF",
    on_accent="#000000",
    text="#E6E6E6",
    muted="#8B949E",
    subtle="#5A616A",
    border="#3D444D",
    success="#3FB950",
    warning="#D29922",
    error="#F85149",
    selection_bg="#2A2A2E",
    background="#0D1117",
)
LIGHT_PALETTE: Final[Palette] = Palette(
    accent="#000000",
    on_accent="#FFFFFF",
    text="#1F2328",
    muted="#656D76",
    subtle="#A0A7AF",
    border="#D0D7DE",
    success="#1A7F37",
    warning="#9A6700",
    error="#CF222E",
    selection_bg="#E4E4E7",
    background="#FFFFFF",
)


def build_theme(name: str, palette: Palette, dark: bool) -> Theme:
    """Turn a palette into a Textual theme, overriding the built-in variables it touches."""
    return Theme(
        name=name,
        primary=palette.accent,
        secondary=palette.muted,
        accent=palette.accent,
        foreground=palette.text,
        background=palette.background,
        surface=palette.background,
        panel=palette.background,
        success=palette.success,
        warning=palette.warning,
        error=palette.error,
        dark=dark,
        variables={
            "muted": palette.muted,
            "subtle": palette.subtle,
            "panel-border": palette.border,
            "focus-border": palette.focus_border,
            "selection-bg": palette.selection_bg,
            "on-accent": palette.on_accent,
            "text-muted": palette.muted,
            "border": palette.focus_border,
            "border-blurred": palette.border,
            "input-cursor-background": palette.accent,
            "input-cursor-foreground": palette.on_accent,
            "input-selection-background": palette.selection_bg,
            "scrollbar": palette.border,
            "scrollbar-hover": palette.muted,
            "scrollbar-active": palette.accent,
            "scrollbar-background": palette.background,
            "scrollbar-background-hover": palette.background,
            "scrollbar-background-active": palette.background,
            "scrollbar-corner-color": palette.background,
        },
    )


THEMES: Final[tuple[Theme, ...]] = (
    build_theme(DARK_THEME_NAME, DARK_PALETTE, dark=True),
    build_theme(LIGHT_THEME_NAME, LIGHT_PALETTE, dark=False),
)


def theme_name_for_terminal(environ: Mapping[str, str]) -> str:
    """Pick the light theme when COLORFGBG says the terminal is light, else the dark one."""
    background = environ.get("COLORFGBG", "").rpartition(";")[2]
    return LIGHT_THEME_NAME if background in _LIGHT_BACKGROUND_INDEXES else DARK_THEME_NAME
