#!/usr/bin/env bash
# Remove the local Claude Code bridge LaunchAgent.
set -euo pipefail

LABEL="com.readpaper.bridge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ -f "$PLIST" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $PLIST"
else
  echo "Not installed (no $PLIST)"
fi
