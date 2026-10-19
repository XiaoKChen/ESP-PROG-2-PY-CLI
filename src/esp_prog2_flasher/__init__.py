"""Package marker for esp_prog2_flasher and home of the package version string.

A TUI/CLI that flashes an RA4M1 or PSoC 6 over SWD via pyOCD and reflashes the ESP-Prog-2's own
firmware via esptool. This module only marks the package and exposes __version__.

Owns:        __version__ (read by the TUI subtitle in app.py).
Not here:    flashing logic (flasher.py), CLI parsing (cli.py), the TUI (app.py), config-block
             encoding (dfu_config.py).
Depends on:  nothing.
"""

__version__ = "2.2.0"
