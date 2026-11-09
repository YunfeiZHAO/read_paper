#!/usr/bin/env bash
# Install the local Claude Code bridge as a macOS LaunchAgent (auto-starts at login, stays up).
# One-time setup. Re-run it any time to reinstall/restart. Uninstall with ./uninstall.sh.
#
# Security: the bridge only accepts requests from origins you allow. Set the origin the app is
# served from (otherwise it defaults to local dev origins and the remote app will be blocked):
#   READPAPER_BRIDGE_ORIGINS="https://app.example.com" ./install.sh
# Optional extra shared secret (also put the same value in the app's localStorage "bridgeToken"):
#   READPAPER_BRIDGE_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" ./install.sh
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

# Optional security env vars baked into the LaunchAgent (empty = use the server defaults).
ORIGINS_ENTRY=""
if [ -n "${READPAPER_BRIDGE_ORIGINS:-}" ]; then
  ORIGINS_ENTRY="        <key>READPAPER_BRIDGE_ORIGINS</key>
        <string>$READPAPER_BRIDGE_ORIGINS</string>"
fi
TOKEN_ENTRY=""
if [ -n "${READPAPER_BRIDGE_TOKEN:-}" ]; then
  TOKEN_ENTRY="        <key>READPAPER_BRIDGE_TOKEN</key>
        <string>$READPAPER_BRIDGE_TOKEN</string>"
fi

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
$ORIGINS_ENTRY
$TOKEN_ENTRY
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
echo "Origins: ${READPAPER_BRIDGE_ORIGINS:-(default: http://localhost:8080,http://127.0.0.1:8080)}"
echo "Token:   ${READPAPER_BRIDGE_TOKEN:+set}${READPAPER_BRIDGE_TOKEN:-(none)}"
echo
echo "Verify it is up:"
echo "  curl -s http://127.0.0.1:8765/health"
