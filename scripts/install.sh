#!/usr/bin/env bash
# Install the released esp-prog2-flasher binary to ~/.local/bin (macOS/Linux).
# Usage: install.sh [vX.Y.Z]   (defaults to the latest release)
set -euo pipefail

REPO="XiaoKChen/ESP-PROG-2-PY-CLI"
VERSION="${1:-latest}"
INSTALL_DIR="$HOME/.local/bin"
DEST="$INSTALL_DIR/esp-prog2-flasher"

case "$(uname -s)" in
    Darwin) ASSET="esp-prog2-flasher-macos" ;;
    Linux)  ASSET="esp-prog2-flasher-linux" ;;
    *)
        echo "Error: unsupported OS '$(uname -s)' — use scripts/install.ps1 on Windows." >&2
        exit 1
        ;;
esac

mkdir -p "$INSTALL_DIR"

if command -v gh >/dev/null 2>&1; then
    # The repo is private, so downloads go through the GitHub CLI
    # (requires a one-time `gh auth login`).
    if [ "$VERSION" = "latest" ]; then
        gh release download --repo "$REPO" --pattern "$ASSET" --output "$DEST" --clobber
    else
        gh release download "$VERSION" --repo "$REPO" --pattern "$ASSET" --output "$DEST" --clobber
    fi
elif command -v curl >/dev/null 2>&1; then
    # Direct release URL — only works if/when the repo is public.
    if [ "$VERSION" = "latest" ]; then
        URL="https://github.com/$REPO/releases/latest/download/$ASSET"
    else
        URL="https://github.com/$REPO/releases/download/$VERSION/$ASSET"
    fi
    if ! curl -fSL --progress-bar "$URL" -o "$DEST"; then
        echo "Error: download failed. The repo is private — install the GitHub CLI (gh)," >&2
        echo "run 'gh auth login', then re-run this script." >&2
        exit 1
    fi
else
    echo "Error: need the GitHub CLI (gh) or curl to download the release." >&2
    exit 1
fi

chmod +x "$DEST"
echo "Installed: $DEST"

case ":$PATH:" in
    *":$INSTALL_DIR:"*) ;;
    *)
        # RC_FILE and the export line are printed for the user, never expanded here.
        # shellcheck disable=SC2088,SC2016
        {
            RC_FILE="~/.bashrc"
            if [ "$(basename "${SHELL:-bash}")" = "zsh" ]; then
                RC_FILE="~/.zshrc"
            fi
            echo ""
            echo "$INSTALL_DIR is not on your PATH. Add this line to $RC_FILE, then restart your shell:"
            echo '  export PATH="$HOME/.local/bin:$PATH"'
        }
        ;;
esac
