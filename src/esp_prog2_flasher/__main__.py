"""Module entry point so ``python -m esp_prog2_flasher`` runs the CLI.

Delegates straight to cli.main(). It is also the script PyInstaller freezes
(esp-prog2-flasher.spec), so the frozen executable behaves identically.

Not here:    argument parsing and dispatch — those live in cli.py.
Depends on:  cli.py.
See also:    esp-prog2-flasher.spec, cli.py
"""

from esp_prog2_flasher.cli import main

if __name__ == "__main__":
    main()
