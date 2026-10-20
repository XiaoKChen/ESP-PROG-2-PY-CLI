"""The flasher's TTSSH-style terminal UI: theme, dashboard, "/" commands, wizards and browser.

FlasherApp (../app.py) owns the session state and runs every job; this package draws it
and turns keys into requests. theme.py holds the palette, commands.py the "/" command
table, targets.py the dashboard's targets and recents, widgets.py the framing widgets and
the filterable list, screens.py the dashboard and the run/log/help views, wizard.py the
step-by-step wizards, browser.py the file browser and flows.py each target's wizard steps
and jobs.

Not here:    every hardware call and the session state (../app.py, ../flasher.py).
See also:    ../app.py, tests/test_app.py
"""
