#!/bin/bash
# Queue one request for the launchd runner and optionally wait for the result.
#   tools/macos_runner/queue.sh probe|setup|scan [--seconds N] [--gain DB]
#       [--antenna RX2|TX/RX] [--input antenna|terminated] [--procedure suite|baseline|fullrange]
#       [--device b200|bladerf] [--bladerf-job dense-dc|survey|survey-anchor|dense-dc+survey]
#       [--plan-args "--no-telemetry"] [--overflow-probe 1] [--hopscan 1] [--superscan 1] [--superscan-plan sat|sat-full|sat-s]
#       [--superscan-gain auto|DB] [--superscan-rate 8|4|2] [--superscan-dither KHZ] [--satdb-refresh 1]
#       [--reanalyze hopscan-YYYYMMDD-HHMMSS|superscan-YYYYMMDD-HHMMSS] [--gui start|stop [--gui-port N]] [--wait SECONDS]
# --gui start|stop: local browser workspace on 127.0.0.1 (no SDR access); scan mode. Prints the request id.
# With --wait: prints the status JSON and the run log tail;
# exit 0 = finished OK, 1 = finished with an error or rejected, 3 = still running.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SRC/../.." && pwd)"
SESSIONS="${SPR_SESSIONS:-$REPO/sessions}"
usage() { sed -n '2,11p' "$0" >&2; exit 2; }
MODE="${1:-}"; [ $# -gt 0 ] && shift
case "$MODE" in probe|setup|scan) ;; *) usage;; esac
SECS=30; GAIN=20; ANT=RX2; IN=antenna; PROC=suite; DEV=b200; JOB=dense-dc+survey; WAIT=0; PLANARGS=; OVP=; HOP=; SUP=; REAN=; SPLAN=; SATDB=; SGAIN=; SRATE=; SDITH=; GUI=; GUIPORT=
while [ $# -gt 0 ]; do
  [ $# -ge 2 ] || usage
  case "$1" in
    --seconds) SECS="$2";; --gain) GAIN="$2";; --antenna) ANT="$2";; --input) IN="$2";; --procedure) PROC="$2";;
    --device) DEV="$2";; --bladerf-job) JOB="$2";; --plan-args) PLANARGS="$2";; --overflow-probe) OVP="$2";; --hopscan) HOP="$2";; --superscan) SUP="$2";; --superscan-plan) SPLAN="$2";; --superscan-gain) SGAIN="$2";; --superscan-rate) SRATE="$2";; --superscan-dither) SDITH="$2";; --satdb-refresh) SATDB="$2";; --reanalyze) REAN="$2";; --gui) GUI="$2";; --gui-port) GUIPORT="$2";; --wait) WAIT="$2";;
    *) usage;;
  esac
  shift 2
done
[[ "$SECS" =~ ^[0-9]+$ ]] && [ "$SECS" -ge 5 ] && [ "$SECS" -le 3600 ] || { echo "--seconds: 5-3600" >&2; exit 2; }
[[ "$GAIN" =~ ^[0-9]+(\.[0-9]+)?$ ]] && awk -v g="$GAIN" 'BEGIN{exit !(g>=0 && g<=70)}' || { echo "--gain: 0-70" >&2; exit 2; }
case "$ANT" in RX2|TX/RX) ;; *) echo "--antenna: RX2 or TX/RX" >&2; exit 2;; esac
case "$IN" in antenna|terminated) ;; *) echo "--input: antenna or terminated" >&2; exit 2;; esac
case "$PROC" in suite|baseline|fullrange) ;; *) echo "--procedure: suite, baseline or fullrange" >&2; exit 2;; esac
case "$DEV" in b200|bladerf) ;; *) echo "--device: b200 or bladerf" >&2; exit 2;; esac
case "$JOB" in dense-dc|survey|survey-anchor|dense-dc+survey) ;; *) echo "--bladerf-job: dense-dc, survey, survey-anchor or dense-dc+survey" >&2; exit 2;; esac
[[ "$WAIT" =~ ^[0-9]+$ ]] || { echo "--wait: seconds" >&2; exit 2; }
[ -z "$GUI" ] || [[ "$GUI" =~ ^(start|stop)$ ]] || { echo "--gui: start or stop" >&2; exit 2; }
[ -z "$GUIPORT" ] || { [[ "$GUIPORT" =~ ^[0-9]{4,5}$ ]] && [ "$GUIPORT" -ge 1024 ] && [ "$GUIPORT" -le 65535 ]; } || { echo "--gui-port: 1024-65535" >&2; exit 2; }
case "$PLANARGS" in *[!A-Za-z0-9\ .=_-]*) echo "--plan-args: letters, digits, space and . = _ - only" >&2; exit 2;; esac
[ -z "$SGAIN" ] || [ "$SGAIN" = auto ] || { [[ "$SGAIN" =~ ^[0-9]{1,2}(\.[0-9])?$ ]] && awk -v g="$SGAIN" 'BEGIN{exit !(g<=76)}'; } || { echo "--superscan-gain: auto or 0-76 dB" >&2; exit 2; }
[ -z "$SDITH" ] || [[ "$SDITH" =~ ^[0-9]{1,4}$ ]] || { echo "--superscan-dither: kHz" >&2; exit 2; }
[ -z "$SRATE" ] || [[ "$SRATE" =~ ^(2|4|8)$ ]] || { echo "--superscan-rate: 2 | 4 | 8" >&2; exit 2; }
[ -z "$SPLAN" ] || [[ "$SPLAN" =~ ^(sat|sat-full|sat-s)$ ]] || { echo "--superscan-plan: sat | sat-full | sat-s" >&2; exit 2; }
[ -z "$REAN" ] || [[ "$REAN" =~ ^(hopscan|superscan)-[0-9]{8}-[0-9]{6}$ ]] || { echo "--reanalyze: hopscan-YYYYMMDD-HHMMSS or superscan-YYYYMMDD-HHMMSS" >&2; exit 2; }
for flag in "$OVP" "$HOP" "$SUP" "$SATDB"; do
  [ -z "$flag" ] || [ "$flag" = 1 ] || { echo "boolean flags must be 1" >&2;exit 2; }
done
[ -z "$GUIPORT" ] || [ -n "$GUI" ] || { echo "--gui-port requires --gui" >&2;exit 2; }
OPERATIONS=0
for operation in "$GUI" "$REAN" "$OVP" "$HOP" "$SUP"; do
  [ -z "$operation" ] || OPERATIONS=$(( OPERATIONS + 1 ))
done
[ "$OPERATIONS" -le 1 ] && { [ "$OPERATIONS" = 0 ] || [ "$MODE" = scan ]; } || { echo "conflicting operations" >&2;exit 2; }
if [ "$DEV" = bladerf ] && { [ -n "$OVP" ] || [ -n "$HOP" ] || [ -n "$SUP" ]; }; then echo "unsupported device workflow" >&2;exit 2;fi
mkdir -p "$SESSIONS/queue" "$SESSIONS/status"
ID="$MODE-$(date +%Y%m%d-%H%M%S)-$$"
TMP=$(mktemp "$SESSIONS/queue/.$ID.XXXXXX")
trap 'rm -f "$TMP"' EXIT
ID="$ID-${TMP##*.}"
TYPE=integer; case "$GAIN" in *.*) TYPE=real;; esac
{
  echo '<?xml version="1.0" encoding="UTF-8"?>'
  echo '<plist version="1.0"><dict>'
  printf '<key>mode</key><string>%s</string><key>scan_seconds</key><integer>%s</integer>\n' "$MODE" "$SECS"
  printf '<key>gain_db</key><%s>%s</%s><key>antenna</key><string>%s</string>\n' "$TYPE" "$GAIN" "$TYPE" "$ANT"
  printf '<key>input</key><string>%s</string><key>procedure</key><string>%s</string>\n' "$IN" "$PROC"
  printf '<key>device</key><string>%s</string><key>bladerf_job</key><string>%s</string>\n' "$DEV" "$JOB"
  [ "$OVP" != 1 ] || printf '<key>overflow_probe</key><integer>1</integer>\n'
  [ "$HOP" != 1 ] || printf '<key>hopscan</key><integer>1</integer>\n'
  [ "$SUP" != 1 ] || printf '<key>superscan</key><integer>1</integer>\n'
  [ -z "$SPLAN" ] || printf '<key>superscan_plan</key><string>%s</string>\n' "$SPLAN"
  [ -z "$SGAIN" ] || printf '<key>superscan_gain</key><string>%s</string>\n' "$SGAIN"
  [ -z "$SRATE" ] || printf '<key>superscan_rate</key><string>%s</string>\n' "$SRATE"
  [ -z "$SDITH" ] || printf '<key>superscan_dither</key><string>%s</string>\n' "$SDITH"
  [ "$SATDB" != 1 ] || printf '<key>satdb_refresh</key><integer>1</integer>\n'
  [ -z "$REAN" ] || printf '<key>reanalyze</key><string>%s</string>\n' "$REAN"
  [ -z "$GUI" ] || printf '<key>gui</key><string>%s</string>\n' "$GUI"
  [ -z "$GUIPORT" ] || printf '<key>gui_port</key><string>%s</string>\n' "$GUIPORT"
  [ -z "$PLANARGS" ] || printf '<key>plan_args</key><string>%s</string>\n' "$PLANARGS"
  echo '</dict></plist>'
} > "$TMP"
mv "$TMP" "$SESSIONS/queue/$ID.json"   # atomic: the runner never sees a partial file
echo "$ID"
[ "$WAIT" -gt 0 ] || exit 0
STATUS="$SESSIONS/status/$ID.json"
for _ in $(seq 1 "$WAIT"); do
  grep -Eq '"(finished|rejected|interrupted)"' "$STATUS" 2>/dev/null && break
  sleep 1
done
if ! grep -Eq '"(finished|rejected|interrupted)"' "$STATUS" 2>/dev/null; then echo "still running: $STATUS" >&2; exit 3; fi
cat "$STATUS"
LOGF="$(sed -n 's/.*LOG=\([^;]*\);.*/\1/p' "$STATUS")"
[ -n "$LOGF" ] && [ -f "$LOGF" ] && grep -v '^  nei ' "$LOGF" | tail -25
grep -q '"exit":0' "$STATUS"
