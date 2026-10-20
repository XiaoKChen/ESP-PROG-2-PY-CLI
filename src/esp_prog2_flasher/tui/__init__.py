"""Reusable Textual screens for the flasher TUI: file picker, confirm dialog and key help.

The screens know nothing about flashing; app.py (FlasherApp) builds their content and acts
on their results. Kept apart from app.py so the app module holds only the tabbed layout,
the key map and the flasher wiring.

Not here:    the app, its key map and every hardware call (app.py, flasher.py).
See also:    screens.py, ../app.py
"""
