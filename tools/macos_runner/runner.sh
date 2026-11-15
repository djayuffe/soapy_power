#!/bin/bash
# launchd job: runs requests from $SPR_SESSIONS/queue/*.json one at a time.
# Request: an XML property list (plutil-readable; JSON is not) named <id>.json with keys
#   mode (probe|setup|scan), scan_seconds, gain_db, antenna, input (antenna|terminated).
# Only these validated parameters reach tools/macos_runner/run_session.sh in the repo
# (fixes need no reinstall; the repo's Python code runs on every scan anyway).
set -u -o pipefail
: "${SPR_REPO:?}" "${SPR_SESSIONS:?}"
APP="$(cd "$(dirname "$0")" && pwd)"
QUEUE="$SPR_SESSIONS/queue"; STATUSDIR="$SPR_SESSIONS/status"
mkdir -p "$QUEUE/taken" "$STATUSDIR"
LOCK="$APP/lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -f "$LOCK/pid" ] && kill -0 "$(cat "$LOCK/pid" 2>/dev/null)" 2>/dev/null; then exit 0; fi
  rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null || exit 0
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
field() { plutil -extract "$1" raw -o - "$2" 2>/dev/null; }
while :; do
  REQ="$(ls -1 "$QUEUE"/*.json 2>/dev/null | head -1)"
  [ -n "$REQ" ] || break
  ID="$(basename "$REQ" .json)"
  TAKEN="$QUEUE/taken/$ID-$(date +%s).json"
  mv "$REQ" "$TAKEN" || break
  if ! [[ "$ID" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then echo "{\"state\":\"rejected\",\"reason\":\"bad id\",\"utc\":\"$(now)\"}" > "$STATUSDIR/rejected-$(date +%s).json"; continue; fi
  STATUS="$STATUSDIR/$ID.json"
  reject() { echo "{\"id\":\"$ID\",\"state\":\"rejected\",\"reason\":\"$1\",\"utc\":\"$(now)\"}" > "$STATUS"; }
  if ! plutil -lint -s "$TAKEN" >/dev/null 2>&1; then reject "invalid json"; continue; fi
  MODE="$(field mode "$TAKEN")"; SECS="$(field scan_seconds "$TAKEN")"; GAIN="$(field gain_db "$TAKEN")"; ANT="$(field antenna "$TAKEN")"; IN="$(field input "$TAKEN")"
  case "$MODE" in probe|setup|scan) ;; *) reject "mode must be probe, setup or scan"; continue;; esac
  SECS="${SECS:-30}"; GAIN="${GAIN:-20}"; ANT="${ANT:-RX2}"; IN="${IN:-antenna}"
  if ! [[ "$SECS" =~ ^[0-9]+$ ]] || ! [[ "$GAIN" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then reject "bad number"; continue; fi
  case "$ANT" in RX2|TX/RX) ;; *) reject "bad antenna"; continue;; esac
  case "$IN" in antenna|terminated) ;; *) reject "bad input"; continue;; esac
  echo "{\"id\":\"$ID\",\"state\":\"running\",\"mode\":\"$MODE\",\"started_utc\":\"$(now)\",\"pid\":$$}" > "$STATUS"
  rm -f "$SPR_SESSIONS/last-run.env"
  SCRIPT="$SPR_REPO/tools/macos_runner/run_session.sh"
  if [ ! -f "$SCRIPT" ]; then reject "run_session.sh missing in repo"; continue; fi
  MODE="$MODE" SCAN_SECONDS="$SECS" GAIN_DB="$GAIN" ANTENNA="$ANT" INPUT="$IN" REQUEST_FILE="$TAKEN" /bin/bash "$SCRIPT" </dev/null
  CODE=$?
  RESULT="$( [ -f "$SPR_SESSIONS/last-run.env" ] && tr '\n' ';' < "$SPR_SESSIONS/last-run.env" | sed 's/\\/\\\\/g; s/"/\\"/g' )"
  echo "{\"id\":\"$ID\",\"state\":\"finished\",\"mode\":\"$MODE\",\"exit\":$CODE,\"finished_utc\":\"$(now)\",\"result\":\"$RESULT\"}" > "$STATUS"
done
