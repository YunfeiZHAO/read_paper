#!/usr/bin/env bash
# Install the local Claude Code bridge as a macOS LaunchAgent (auto-starts at login, stays up).
# One-time setup. Re-run it any time to reinstall/restart. Uninstall with ./uninstall.sh.
set -euo pipefail

LABEL="com.readpaper.bridge"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER="$SCRIPT_DIR/server.py"
PYTHON="$(command -v python3 || true)"
LOCALBIN="$HOME/.local/bin"
LOG="$HOME/Library/Logs/readpaper-bridge.log"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ -z "$PYTHON" ]; then
  echo "error: python3 not found on PATH" >&2
  exit 1
fi
if [ ! -f "$SERVER" ]; then
  echo "error: $SERVER not found" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SERVER</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$LOCALBIN:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
</dict>
</plist>
PLIST_EOF

# Reload (ignore errors if not currently loaded).
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Installed LaunchAgent: $PLIST"
echo "Python:  $PYTHON"
echo "Server:  $SERVER"
echo "Log:     $LOG"
echo
echo "Verify it is up:"
echo "  curl -s http://127.0.0.1:8765/health"
