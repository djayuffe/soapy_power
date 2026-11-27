#!/bin/bash
# One-time: install a launchd agent so queued experiment requests run on this Mac.
#   bash tools/macos_runner/install.sh       (from the soapy_power checkout)
# Undo: bash tools/macos_runner/uninstall.sh
# Requests are queued in <repo>/sessions/queue; results, logs and status land in <repo>/sessions.
set -euo pipefail
PROBE=1
case "${1:-}" in "") ;; --no-probe) PROBE=0;; *) echo "usage: install.sh [--no-probe]" >&2;exit 2;; esac
SRC="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SRC/../.." && pwd)"
SESSIONS="$REPO/sessions"
LABEL=no.djayuffe.soapy-power-runner
APP="$HOME/Library/Application Support/soapy-power-runner"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$APP" "$HOME/Library/LaunchAgents" "$SESSIONS/queue/taken" "$SESSIONS/status"
# The request parser lives outside the repo; it runs tools/macos_runner/run_session.sh from the repo.
if [ -f "$APP/lock/pid" ] && kill -0 "$(cat "$APP/lock/pid")" 2>/dev/null; then
  echo 'Runner is active; wait for the current job before updating its launch agent.' >&2;exit 1
fi
for candidate in "$SESSIONS/.venv/bin/python" /opt/homebrew/bin/python3 python3; do
  if "$candidate" -c 'import sys;sys.exit(sys.version_info < (3,9))' >/dev/null 2>&1; then RPY="$candidate";break;fi
done
[ -n "${RPY:-}" ] || { echo 'Python 3.9+ is required' >&2;exit 1; }
"$RPY" - "$SESSIONS/.runner.lock" <<'PYLOCK'
import fcntl,os,sys
with open(sys.argv[1],'a+') as stream:
    try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:sys.exit('Runner is active; wait before installing the launch agent')
PYLOCK
cp "$SRC/runner.sh" "$APP/.runner.sh.tmp"
chmod 755 "$APP/.runner.sh.tmp"
mv "$APP/.runner.sh.tmp" "$APP/runner.sh"
rm -f "$APP/run-100-300.command"
xml() { printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'; }
ENV="<key>SPR_REPO</key><string>$(xml "$REPO")</string><key>SPR_SESSIONS</key><string>$(xml "$SESSIONS")</string>"
for k in PATH HOME LANG UHD_IMAGES_DIR UHD_CONFIG_DIR SOAPY_SDR_PLUGIN_PATH SOAPY_SDR_ROOT PYTHONPATH SOAPY_POWER PYENV_ROOT VIRTUAL_ENV; do
  if [ -n "${!k:-}" ]; then ENV="$ENV<key>$k</key><string>$(xml "${!k}")</string>"; fi
done
for k in DYLD_LIBRARY_PATH DYLD_FALLBACK_LIBRARY_PATH; do
  if [ -n "${!k:-}" ]; then ENV="$ENV<key>SPR_$k</key><string>$(xml "${!k}")</string>"; fi
done
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$LABEL</string>
<key>ProgramArguments</key><array><string>/bin/bash</string><string>$(xml "$APP/runner.sh")</string></array>
<key>RunAtLoad</key><true/>
<key>StartInterval</key><integer>15</integer>
<key>WatchPaths</key><array><string>$(xml "$SESSIONS/queue")</string></array>
<key>EnvironmentVariables</key><dict>$ENV</dict>
<key>StandardOutPath</key><string>$(xml "$SESSIONS/status/launchd.log")</string>
<key>StandardErrorPath</key><string>$(xml "$SESSIONS/status/launchd.log")</string>
<key>ProcessType</key><string>Interactive</string>
</dict></plist>
PL
plutil -lint "$PLIST"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installert: $PLIST"
if [ "$PROBE" = 1 ]; then
  echo "probe (venter opptil 120 s):"
  bash "$SRC/queue.sh" probe --wait 120 || true
else
  echo "probe skipped (--no-probe)"
fi
