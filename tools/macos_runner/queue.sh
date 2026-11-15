#!/bin/bash
# Queue one request for the launchd runner and optionally wait for the result.
#   tools/macos_runner/queue.sh probe|setup|scan [--seconds N] [--gain DB]
#       [--antenna RX2|TX/RX] [--input antenna|terminated] [--wait SECONDS]
# Prints the request id. With --wait: prints the status JSON and the run log tail;
# exit 0 = finished OK, 1 = finished with an error or rejected, 3 = still running.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SRC/../.." && pwd)"
SESSIONS="${SPR_SESSIONS:-$REPO/sessions}"
usage() { sed -n '2,6p' "$0" >&2; exit 2; }
MODE="${1:-}"; [ $# -gt 0 ] && shift
case "$MODE" in probe|setup|scan) ;; *) usage;; esac
SECS=30; GAIN=20; ANT=RX2; IN=antenna; WAIT=0
while [ $# -gt 0 ]; do
  [ $# -ge 2 ] || usage
  case "$1" in
    --seconds) SECS="$2";; --gain) GAIN="$2";; --antenna) ANT="$2";; --input) IN="$2";; --wait) WAIT="$2";;
    *) usage;;
  esac
  shift 2
done
[[ "$SECS" =~ ^[0-9]+$ ]] && [ "$SECS" -ge 5 ] && [ "$SECS" -le 600 ] || { echo "--seconds: 5-600" >&2; exit 2; }
[[ "$GAIN" =~ ^[0-9]+(\.[0-9]+)?$ ]] && awk -v g="$GAIN" 'BEGIN{exit !(g>=0 && g<=70)}' || { echo "--gain: 0-70" >&2; exit 2; }
case "$ANT" in RX2|TX/RX) ;; *) echo "--antenna: RX2 or TX/RX" >&2; exit 2;; esac
case "$IN" in antenna|terminated) ;; *) echo "--input: antenna or terminated" >&2; exit 2;; esac
[[ "$WAIT" =~ ^[0-9]+$ ]] || { echo "--wait: seconds" >&2; exit 2; }
mkdir -p "$SESSIONS/queue" "$SESSIONS/status"
ID="$MODE-$(date +%Y%m%d-%H%M%S)-$$"
TYPE=integer; case "$GAIN" in *.*) TYPE=real;; esac
{
  echo '<?xml version="1.0" encoding="UTF-8"?>'
  echo '<plist version="1.0"><dict>'
  printf '<key>mode</key><string>%s</string><key>scan_seconds</key><integer>%s</integer>\n' "$MODE" "$SECS"
  printf '<key>gain_db</key><%s>%s</%s><key>antenna</key><string>%s</string>\n' "$TYPE" "$GAIN" "$TYPE" "$ANT"
  printf '<key>input</key><string>%s</string>\n' "$IN"
  echo '</dict></plist>'
} > "$SESSIONS/queue/.$ID.tmp"
mv "$SESSIONS/queue/.$ID.tmp" "$SESSIONS/queue/$ID.json"   # atomic: the runner never sees a partial file
echo "$ID"
[ "$WAIT" -gt 0 ] || exit 0
STATUS="$SESSIONS/status/$ID.json"
for _ in $(seq 1 "$WAIT"); do
  grep -Eq '"(finished|rejected)"' "$STATUS" 2>/dev/null && break
  sleep 1
done
if ! grep -Eq '"(finished|rejected)"' "$STATUS" 2>/dev/null; then echo "still running: $STATUS" >&2; exit 3; fi
cat "$STATUS"
LOGF="$(sed -n 's/.*LOG=\([^;]*\);.*/\1/p' "$STATUS")"
[ -n "$LOGF" ] && [ -f "$LOGF" ] && grep -v '^  nei ' "$LOGF" | tail -25
grep -q '"exit":0' "$STATUS"
