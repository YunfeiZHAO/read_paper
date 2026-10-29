#!/usr/bin/env bash
# Build the macOS .app. Must be run ON macOS (PyInstaller cannot cross-compile).
#
#   uv sync --extra desktop
#   ./packaging/build_macos.sh
#
# Output: dist/ReadPaper.app  (drag into /Applications, or package into a .dmg).
set -euo pipefail
cd "$(dirname "$0")/.."

uv run pyinstaller --noconfirm packaging/readpaper.spec

echo
echo "Built dist/ReadPaper.app"
echo "Optional .dmg:  hdiutil create -volname 'Read Paper' -srcfolder dist/ReadPaper.app -ov -format UDZO dist/ReadPaper.dmg"
