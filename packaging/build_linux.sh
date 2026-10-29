#!/usr/bin/env bash
# Build the Linux binary. Must be run ON Linux (PyInstaller cannot cross-compile).
# pywebview needs a system WebView backend — install GTK + WebKit2GTK first, e.g. on Debian/Ubuntu:
#   sudo apt install python3-gi gir1.2-webkit2-4.1 libgirepository1.0-dev
#
#   uv sync --extra desktop
#   ./packaging/build_linux.sh
#
# Output: dist/ReadPaper/ReadPaper  (a folder with the launcher binary inside).
set -euo pipefail
cd "$(dirname "$0")/.."

uv run pyinstaller --noconfirm packaging/readpaper.spec

echo
echo "Built dist/ReadPaper/ReadPaper"
