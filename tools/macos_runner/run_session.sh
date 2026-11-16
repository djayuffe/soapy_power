#!/bin/bash
# B200 (UHD via SoapySDR) 100-300 MHz experiment session on macOS, receive only.
#   bash tools/macos_runner/run_session.sh            (or through runner.sh / launchd)
# Environment: MODE=probe|setup|scan (default scan), SCAN_SECONDS (5-600, default 30),
# GAIN_DB (0-70, default 20), ANTENNA (RX2 | TX/RX, default RX2),
# INPUT (antenna | terminated, default antenna; what the operator connected to the port),
# PROCEDURE (suite | baseline, default suite):
#   suite:    the 100-300 MHz experiment suite (plan, run, analyze)
#   baseline: fixed-tune captures (110/150/190/230/270 MHz, 200 x 5 ms each, stream reopened
#             per run like the survey: continuous 100 ms blocks overflowed at 56 MS/s) for
#             soapy_power_calibration baseline/compare, plus a SHA-256 manifest
# Under the runner, REQUEST_FILE (or the newest queue/taken request) supplies INPUT/PROCEDURE,
# and the optional keys device (b200 | bladerf) and bladerf_job:
#   device=bladerf (bladeRF1 x40/x115 + XB-200, tools/bladerf/; stack and profile copied to
#   sessions/bladerf-stack/{modules,lib} and sessions/bladerf-profile/, see tools/bladerf/README.md):
#     probe: enumerate, report FPGA/firmware/LMS state against the profile
#     scan with bladerf_job dense-dc | survey | survey-anchor | dense-dc+survey (default):
#       dense-dc: per-center DC Jacobian + verification; survey: SCAN_SECONDS timed survey using the
#       session's dense DC table; survey-anchor: the same survey with anchor DC only (control)
#   probe: report every Python candidate (SoapySDR import result) and the B200 serial
#   setup: create sessions/.venv (system site-packages) with numpy scipy simplesoapy simplespectral
set -u -o pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${SPR_REPO:-$(cd "$HERE/../.." && pwd)}"
OUTDIR="${SPR_SESSIONS:-$REPO/sessions}"
mkdir -p "$OUTDIR"
REQ="${REQUEST_FILE:-}"
if [ -z "$REQ" ] && [ -n "${SPR_SESSIONS:-}" ]; then REQ="$(ls -t "$OUTDIR"/queue/taken/*.json 2>/dev/null | head -1)"; fi
req_field() { if [ -n "$REQ" ] && [ -f "$REQ" ]; then plutil -extract "$1" raw -o - "$REQ" 2>/dev/null || true; fi; }
INPUT="${INPUT:-$(req_field input)}"; INPUT="${INPUT:-antenna}"
PROCEDURE="${PROCEDURE:-$(req_field procedure)}"; PROCEDURE="${PROCEDURE:-suite}"
MODE="${MODE:-scan}"; [ "${PROBE_ONLY:-0}" = 1 ] && MODE=probe
SCAN_SECONDS="${SCAN_SECONDS:-30}"; GAIN_DB="${GAIN_DB:-20}"; ANTENNA="${ANTENNA:-RX2}"
case "$MODE" in probe|setup|scan) ;; *) echo "FEIL: MODE må være probe, setup eller scan"; exit 2;; esac
if ! [[ "$SCAN_SECONDS" =~ ^[0-9]+$ ]] || [ "$SCAN_SECONDS" -lt 5 ] || [ "$SCAN_SECONDS" -gt 600 ]; then echo "FEIL: SCAN_SECONDS må være 5-600"; exit 2; fi
if ! [[ "$GAIN_DB" =~ ^[0-9]+(\.[0-9]+)?$ ]] || ! awk -v g="$GAIN_DB" 'BEGIN{exit !(g>=0 && g<=70)}'; then echo "FEIL: GAIN_DB må være 0-70"; exit 2; fi
case "$ANTENNA" in RX2|TX/RX) ;; *) echo "FEIL: ANTENNA må være RX2 eller TX/RX"; exit 2;; esac
case "$INPUT" in antenna|terminated) ;; *) echo "FEIL: INPUT må være antenna eller terminated"; exit 2;; esac
case "$PROCEDURE" in suite|baseline) ;; *) echo "FEIL: PROCEDURE må være suite eller baseline"; exit 2;; esac
DEVICE_KIND="${DEVICE_KIND:-$(req_field device)}"; DEVICE_KIND="${DEVICE_KIND:-b200}"
BLADERF_JOB="${BLADERF_JOB:-$(req_field bladerf_job)}"; BLADERF_JOB="${BLADERF_JOB:-dense-dc+survey}"
case "$DEVICE_KIND" in b200|bladerf) ;; *) echo "FEIL: device må være b200 eller bladerf"; exit 2;; esac
case "$BLADERF_JOB" in dense-dc|survey|survey-anchor|dense-dc+survey) ;; *) echo "FEIL: bladerf_job ukjent"; exit 2;; esac
for v in DYLD_LIBRARY_PATH DYLD_FALLBACK_LIBRARY_PATH; do s="SPR_$v"; if [ -n "${!s:-}" ]; then export "$v=${!s}"; fi; done
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$OUTDIR/run-$STAMP.log"
LAST="$OUTDIR/last-run.env"
exec > >(tee "$LOG") 2>&1
finish() { { echo "STAMP=$STAMP"; echo "LOG=$LOG"; echo "INPUT=$INPUT"; echo "PROCEDURE=$PROCEDURE"; echo "$1"; } > "$LAST"; }
echo "== soapy_power $MODE $STAMP  repo=$REPO  $(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null) $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null)"
echo "   scan_seconds=$SCAN_SECONDS gain_db=$GAIN_DB antenna=$ANTENNA input=$INPUT procedure=$PROCEDURE device=$DEVICE_KIND  PATH=$PATH"

if [ "$DEVICE_KIND" = bladerf ]; then
  # ---- bladeRF1 + XB-200 (receive only): patched SoapyBladeRF module + its libbladeRF from sessions/
  STACK="$OUTDIR/bladerf-stack"; PROF="$OUTDIR/bladerf-profile"
  for f in "$STACK/modules/libbladeRFSupport.so" "$STACK/lib/libbladeRF.2.dylib" "$PROF/receiver-profile-final.json" \
           "$PROF/validated-full-grid-profile.json" "$PROF/schedule-selection.json"; do
    [ -f "$f" ] || { echo "FEIL: mangler $f (se tools/bladerf/README.md)"; finish "RESULT=bladerf_stack_missing"; exit 1; }
  done
  unset PYTHONPATH
  export SOAPY_SDR_PLUGIN_PATH="$STACK/modules"
  export DYLD_LIBRARY_PATH="$STACK/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
  PY=""
  for v in "$OUTDIR"/.venv*/bin/python /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3; do
    if [ -x "$v" ] && "$v" -c 'import SoapySDR, numpy, simplesoapy, simplespectral' 2>/dev/null; then PY="$v"; break; fi
  done
  [ -n "$PY" ] || { echo "FEIL: ingen Python med SoapySDR, numpy, simplesoapy og simplespectral"; finish "RESULT=no_python"; exit 1; }
  export PYTHONPATH="$REPO:$REPO/tools/bladerf"
  SERIAL="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["serial"])' "$PROF/receiver-profile-final.json")"
  BDEV="driver=bladerf,serial=$SERIAL,xb200=custom"
  echo "python: $PY ($("$PY" --version 2>&1))  SOAPY_SDR_PLUGIN_PATH=$SOAPY_SDR_PLUGIN_PATH  DYLD_LIBRARY_PATH=$DYLD_LIBRARY_PATH"
  echo "device: $BDEV  job: $BLADERF_JOB"
  "$PY" - "$BDEV" "$PROF/receiver-profile-final.json" <<'PYEOF'
import json, sys, SoapySDR
args, profile = sys.argv[1], json.load(open(sys.argv[2]))
found = [dict(d) for d in SoapySDR.Device.enumerate('driver=bladerf')]
print('modules:', list(SoapySDR.listModules()))
print('enumerated:', found)
if not any(d.get('serial') == profile['serial'] for d in found):
    sys.exit('FEIL: fant ikke bladeRF ' + profile['serial'])
dev = SoapySDR.Device(args)
info = dict(dev.getHardwareInfo())
print('hardware_info:', json.dumps(info))
state = info.get('lms_dc_state_at_open')
print('lms_state:', state, 'profile:', profile['lms_state_at_fit'], 'MATCH' if state == profile['lms_state_at_fit'] else 'DIFFERS')
del dev
sys.exit(0 if state == profile['lms_state_at_fit'] else 3)
PYEOF
  PROBE=$?
  if [ "$MODE" != scan ] || [ $PROBE -ne 0 ]; then
    finish "RESULT=$([ $PROBE -eq 0 ] && echo bladerf_probe_ok || echo bladerf_probe_exit_$PROBE);SERIAL=$SERIAL"
    echo "== bladerf probe exit $PROBE"; exit $PROBE
  fi
  SESSION="$OUTDIR/bladerf-$STAMP"; mkdir -p "$SESSION"
  TABLE=""; RC=0
  if [ "$BLADERF_JOB" = dense-dc ] || [ "$BLADERF_JOB" = dense-dc+survey ]; then
    "$PY" "$REPO/tools/bladerf/bladerf_dense_dc.py" --device "$BDEV" --profile-dir "$PROF" --output "$SESSION/dense-dc"; RC=$?
    echo "== dense-dc exit $RC"
    TABLE="$SESSION/dense-dc/dc-table-dense.json"
  fi
  if [ $RC -eq 0 ] && [ "$BLADERF_JOB" != dense-dc ]; then
    if [ "$BLADERF_JOB" = survey ]; then TABLE="$(ls -t "$OUTDIR"/bladerf-*/dense-dc/dc-table-dense.json 2>/dev/null | head -1)"; fi
    if [ "$BLADERF_JOB" != survey-anchor ] && [ -n "$TABLE" ]; then
      "$PY" "$REPO/tools/bladerf/bladerf_scan.py" --device "$BDEV" --profile-dir "$PROF" --duration "$SCAN_SECONDS" \
        --output "$SESSION/survey" --dc-table "$TABLE"; RC=$?
      echo "== survey exit $RC (dense DC: $TABLE)"
    else
      "$PY" "$REPO/tools/bladerf/bladerf_scan.py" --device "$BDEV" --profile-dir "$PROF" --duration "$SCAN_SECONDS" \
        --output "$SESSION/survey"; RC=$?
      echo "== survey exit $RC (anchor DC only)"
    fi
  fi
  finish "SESSION=$SESSION
RESULT=$([ $RC -eq 0 ] && echo done || echo failed_$RC)"
  echo "== ferdig: $SESSION"
  exit $RC
fi

# ---- 1+2. find a Python + site-packages + SoapySDR module dir that enumerates the B200
# Inherited PYTHONPATH is dropped: a site-packages of another Python version breaks imports.
echo "   inherited PYTHONPATH=${PYTHONPATH:-<unset>} SOAPY_SDR_PLUGIN_PATH=${SOAPY_SDR_PLUGIN_PATH:-<unset>} (ignored)"
unset PYTHONPATH SOAPY_SDR_PLUGIN_PATH
CANDIDATES=()
for v in "$OUTDIR"/.venv*/bin/python; do [ -x "$v" ] && CANDIDATES+=("$v"); done
for exe in "${SOAPY_POWER:-}" "$(command -v soapy_power 2>/dev/null || true)"; do
  if [ -n "$exe" ] && [ -f "$exe" ]; then CANDIDATES+=("$(head -1 "$exe" | sed -n 's/^#! *//p' | awk '{print ($1 ~ /env$/) ? $2 : $1}')"); fi
done
for dir in /opt/local/bin /usr/local/bin /opt/homebrew/bin "$HOME/.homebrew/bin" "$HOME/.venv/bin"; do
  for minor in 9 10 11 12 13 14; do [ -x "$dir/python3.$minor" ] && CANDIDATES+=("$dir/python3.$minor"); done
  [ -x "$dir/python3" ] && CANDIDATES+=("$dir/python3")
done
for p in /opt/local/Library/Frameworks/Python.framework/Versions/*/bin/python3 /Library/Frameworks/Python.framework/Versions/*/bin/python3 "$HOME"/.pyenv/versions/*/bin/python3 /usr/bin/python3; do [ -x "$p" ] && CANDIDATES+=("$p"); done
MODDIRS=()
for d in /usr/local/lib/SoapySDR/modules0.8 /opt/local/lib/SoapySDR/modules0.8 /opt/homebrew/lib/SoapySDR/modules0.8 "$HOME/.homebrew/lib/SoapySDR/modules0.8"; do [ -d "$d" ] && MODDIRS+=("$d"); done
UHD_TEST='import SoapySDR
mods = list(SoapySDR.listModules())
found = [dict(d) for d in SoapySDR.Device.enumerate("driver=uhd")]
b2 = [d for d in found if "b200" in " ".join(map(str, d.values())).lower()]
print("RESULT", SoapySDR.getAPIVersion(), len(mods), len(found), b2[0].get("serial", "-") if len(b2) == 1 else "-")'
PY=""; SDRPATH=""; PLUGIN=""; SERIAL=""; SEEN=" "
for c in ${CANDIDATES[@]+"${CANDIDATES[@]}"}; do
  [ -n "$c" ] || continue
  real="$(command -v "$c" 2>/dev/null || true)"; [ -n "$real" ] || continue
  case "$SEEN" in *" $real "*) continue;; esac; SEEN="$SEEN$real "
  ver="$("$real" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)" || { echo "  $real: starter ikke"; continue; }
  for extra in "" /usr/local/lib/python$ver/site-packages /opt/homebrew/lib/python$ver/site-packages "$HOME/.homebrew/lib/python$ver/site-packages" /opt/local/lib/python$ver/site-packages; do
    [ -z "$extra" ] || [ -d "$extra" ] || continue
    imp="$(env ${extra:+PYTHONPATH="$extra"} "$real" -c 'import SoapySDR;print("ok", SoapySDR.getAPIVersion(), SoapySDR.__file__)' 2>&1 | tail -1)"
    case "$imp" in ok*) ;; *) echo "  nei  $real py$ver ${extra:+[+$extra]} $imp"; continue;; esac
    for plug in "" ${MODDIRS[@]+"${MODDIRS[@]}"}; do
      out="$(env ${extra:+PYTHONPATH="$extra"} ${plug:+SOAPY_SDR_PLUGIN_PATH="$plug"} "$real" -c "$UHD_TEST" 2>&1 | grep '^RESULT' | tail -1)"
      read -r tag api nmods nfound serial <<< "${out:-RESULT ? ? ? -}"
      echo "  $([ "$serial" != - ] && echo 'OK  ' || echo 'nei ') $real py$ver ${extra:+[+$extra]} ${plug:+[plugins $plug]} api=$api moduler=$nmods uhd-enheter=$nfound b200=$serial  (${imp#ok })"
      if [ "$serial" != - ] && [ -n "$serial" ]; then PY="$real"; SDRPATH="$extra"; PLUGIN="$plug"; SERIAL="$serial"; break 3; fi
    done
  done
done
if [ -z "$PY" ]; then echo "FEIL: ingen kombinasjon av Python/SoapySDR/modul ser en B200"; finish "RESULT=no_b200_stack"; exit 1; fi
[ -n "$SDRPATH" ] && export PYTHONPATH="$SDRPATH"
[ -n "$PLUGIN" ] && export SOAPY_SDR_PLUGIN_PATH="$PLUGIN"
deps() { "$1" -c 'import importlib.util as u;print(" ".join(m for m in ("numpy","simplesoapy","simplespectral") if u.find_spec(m) is None))'; }
MISSING="$(deps "$PY")"
if [ -n "$MISSING" ]; then
  VENV="$OUTDIR/.venv-$(printf '%s' "$PY" | tr -c 'A-Za-z0-9' '_')"
  if [ ! -x "$VENV/bin/python" ] && [ "$MODE" = setup ]; then
    echo "== setup: lager $VENV fra $PY (--system-site-packages), installerer numpy scipy simplesoapy simplespectral"
    "$PY" -m venv --system-site-packages "$VENV" || { finish "RESULT=venv_failed"; exit 1; }
    "$VENV/bin/python" -m pip install -q --disable-pip-version-check numpy scipy simplesoapy simplespectral || { finish "RESULT=pip_failed"; exit 1; }
  fi
  if [ -x "$VENV/bin/python" ]; then PY="$VENV/bin/python"; MISSING="$(deps "$PY")"; fi
fi
if [ -n "$MISSING" ]; then echo "FEIL: $PY mangler: $MISSING (send en setup-forespørsel)"; finish "RESULT=missing_packages:$MISSING;PY=$PY;SERIAL=$SERIAL"; exit 1; fi
echo "python: $PY ($("$PY" --version 2>&1)) PYTHONPATH=${PYTHONPATH:-} SOAPY_SDR_PLUGIN_PATH=${SOAPY_SDR_PLUGIN_PATH:-}"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
DEV="driver=uhd,type=b200,serial=$SERIAL,recv_frame_size=16360,num_recv_frames=1024"
echo "device: $DEV   UHD_IMAGES_DIR=${UHD_IMAGES_DIR:-<unset>}"
if [ "$MODE" != scan ]; then echo "== $MODE ok"; finish "RESULT=${MODE}_ok;PY=$PY;SERIAL=$SERIAL"; exit 0; fi

# ---- 3. scan: soapy_power from this checkout (/bin/sh drops DYLD_*, so the wrapper sets them)
WRAP="$OUTDIR/.soapy_power-$STAMP"
{
  echo '#!/bin/sh'
  for v in DYLD_LIBRARY_PATH DYLD_FALLBACK_LIBRARY_PATH; do if [ -n "${!v:-}" ]; then printf 'export %s=%q\n' "$v" "${!v}"; fi; done
  printf 'export PYTHONPATH=%q\n' "$PYTHONPATH"
  if [ -n "${SOAPY_SDR_PLUGIN_PATH:-}" ]; then printf 'export SOAPY_SDR_PLUGIN_PATH=%q\n' "$SOAPY_SDR_PLUGIN_PATH"; fi
  printf 'exec %q -m soapypower "$@"\n' "$PY"
} > "$WRAP"
chmod +x "$WRAP"
if [ "$PROCEDURE" = baseline ]; then
  SESSION="$OUTDIR/baseline-$STAMP"; mkdir -p "$SESSION"
  FAILED=""
  for MHZ in 110 150 190 230 270; do
    OUT="$SESSION/baseline-${MHZ}MHz.jsonl"
    for TRY in 1 2; do
      rm -f "$OUT"
      if "$WRAP" -d "$DEV" -A "$ANTENNA" -C 0 -f "${MHZ}M" -r 56M -w 56M --master-clock-rate 56M --force-rate \
           -g "$GAIN_DB" -b 8192 -t 0.005 -u 200 --reopen-stream --tune-delay 0.02 --settle-samples 32768 \
           --lock-sensor lo_locked --require-timestamps --partial-policy reject --no-pyfftw --precision single \
           --max-threads 4 --max-queue-size 4 -F json -O "$OUT" --skip-driver-teardown; then echo "== $MHZ MHz ok"; break; fi
      echo "== $MHZ MHz attempt $TRY failed"; [ "$TRY" = 2 ] && FAILED="$FAILED $MHZ"
    done
  done
  {
    printf '{"kind":"baseline_captures","version":1,"input":"%s","antenna":"%s","gain_db":%s,"device":"%s",' "$INPUT" "$ANTENNA" "$GAIN_DB" "$DEV"
    printf '"settings":"-r 56M -b 8192 -t 0.005 -u 200 --reopen-stream --tune-delay 0.02 --settle-samples 32768","failed_mhz":"%s","files":{' "${FAILED# }"
    SEP=""; for F in "$SESSION"/baseline-*MHz.jsonl; do [ -f "$F" ] || continue
      printf '%s"%s":"%s"' "$SEP" "$(basename "$F")" "$(shasum -a 256 "$F" | awk '{print $1}')"; SEP=","; done
    printf '}}\n'
  } > "$SESSION/manifest.json"
  RESULT=done; [ -n "$FAILED" ] && RESULT="partial:$FAILED"
  finish "SESSION=$SESSION
RESULT=$RESULT"
  echo "== ferdig: $SESSION ($RESULT)"
  [ -z "$FAILED" ]; exit $?
fi
SUITE="$OUTDIR/suite-$STAMP.json"
SESSION="$OUTDIR/okt-$STAMP"
"$PY" -m soapypower.experiments plan "$DEV" -O "$SUITE" --scan-seconds "$SCAN_SECONDS" --gain "$GAIN_DB" --antenna "$ANTENNA" --input "$INPUT" || { finish "RESULT=plan_failed"; exit 1; }
"$PY" -m soapypower.experiments run "$SUITE" "$SESSION" --soapy-power "$WRAP"
RUN=$?
echo "== run exit $RUN"
ANALYZE=skipped
if [ $RUN -eq 0 ]; then "$PY" -m soapypower.experiments analyze "$SESSION"; ANALYZE=$?; echo "== analyze exit $ANALYZE"; fi
finish "SESSION=$SESSION
RUN_EXIT=$RUN
ANALYZE_EXIT=$ANALYZE
RESULT=$([ $RUN -eq 0 ] && echo done || echo run_failed)"
echo "== ferdig: $SESSION   logg: $LOG"
[ $RUN -eq 0 ]
