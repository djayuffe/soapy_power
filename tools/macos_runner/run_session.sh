#!/bin/bash
# B200 (UHD via SoapySDR) 100-300 MHz experiment session on macOS, receive only.
#   bash tools/macos_runner/run_session.sh            (or through runner.sh / launchd)
# Environment: MODE=probe|setup|scan (default scan), SCAN_SECONDS (5-600, default 30),
# GAIN_DB (0-70, default 20), ANTENNA (RX2 | TX/RX, default RX2),
# INPUT (antenna | terminated, default antenna; what the operator connected to the port),
# PROCEDURE (suite | baseline | fullrange, default suite):
#   fullrange: B200 70 MHz-6 GHz, SCAN_SECONDS per variant, features-on (rf-state, headroom, coverage, telemetry policy)
#             then features-off, compared by tools/fullrange_compare.py
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
if ! [[ "$SCAN_SECONDS" =~ ^[0-9]+$ ]] || [ "$SCAN_SECONDS" -lt 5 ] || [ "$SCAN_SECONDS" -gt 3600 ]; then echo "FEIL: SCAN_SECONDS må være 5-3600"; exit 2; fi
if ! [[ "$GAIN_DB" =~ ^[0-9]+(\.[0-9]+)?$ ]] || ! awk -v g="$GAIN_DB" 'BEGIN{exit !(g>=0 && g<=70)}'; then echo "FEIL: GAIN_DB må være 0-70"; exit 2; fi
case "$ANTENNA" in RX2|TX/RX) ;; *) echo "FEIL: ANTENNA må være RX2 eller TX/RX"; exit 2;; esac
case "$INPUT" in antenna|terminated) ;; *) echo "FEIL: INPUT må være antenna eller terminated"; exit 2;; esac
case "$PROCEDURE" in suite|baseline|fullrange) ;; *) echo "FEIL: PROCEDURE må være suite, baseline eller fullrange"; exit 2;; esac
PLAN_ARGS="${PLAN_ARGS:-$(req_field plan_args)}"   # extra `experiments plan` flags, e.g. --no-telemetry
case "$PLAN_ARGS" in *[!A-Za-z0-9\ .=_-]*) echo "FEIL: plan_args har ulovlige tegn"; exit 2;; esac
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

# ---- Homebrew 0.8-ABI stack. `brew upgrade` moves /opt/homebrew/opt/soapysdr to 0.9 (Python 3.15 bindings,
# modules0.9) but keeps the superseded 0.8 keg until `brew cleanup`. Modules built for 0.8 (SoapyUHD,
# SoapyBladeRF) name libSoapySDR.0.8 by its opt path; dyld resolves it by leaf name through
# DYLD_LIBRARY_PATH, and the keg's own site-packages supply matching Python bindings. DYLD_* is only ever
# exported from bash, never passed through env(1) or /bin/sh arguments: SIP strips it there.
KEG08=()
for k in $(ls -d /opt/homebrew/Cellar/soapysdr/0.8* "$HOME"/.homebrew/Cellar/soapysdr/0.8* 2>/dev/null | sort -r); do
  [ -f "$k/lib/libSoapySDR.0.8.dylib" ] && KEG08+=("$k")
done
UHDLIBS=""
for d in $(ls -d /opt/homebrew/Cellar/uhd/*/lib "$HOME"/.homebrew/Cellar/uhd/*/lib 2>/dev/null | sort -r); do UHDLIBS="$UHDLIBS${UHDLIBS:+:}$d"; done
echo "   soapysdr 0.8 keg: ${KEG08[*]+${KEG08[*]}}  uhd keg libs: ${UHDLIBS:-none}"
keg_site() { [ -f "$1/lib/python$2/site-packages/SoapySDR.py" ] && echo "$1/lib/python$2/site-packages"; }
# in_env SITE DYLD PLUGIN IMAGES -- command...: run with exactly that SoapySDR environment (subshell)
in_env() {
  ( site="$1"; dyl="$2"; plug="$3"; img="$4"; shift 5
    unset PYTHONPATH SOAPY_SDR_PLUGIN_PATH
    if [ -n "$site" ]; then export PYTHONPATH="$site"; fi
    if [ -n "$dyl" ]; then export DYLD_LIBRARY_PATH="$dyl${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"; fi
    if [ -n "$plug" ]; then export SOAPY_SDR_PLUGIN_PATH="$plug"; fi
    if [ -n "$img" ]; then export UHD_IMAGES_DIR="$img"; fi
    "$@" )
}

if [ "$DEVICE_KIND" = bladerf ]; then
  # ---- bladeRF1 + XB-200 (receive only): patched SoapyBladeRF module + its libbladeRF from sessions/
  STACK="$OUTDIR/bladerf-stack"; PROF="$OUTDIR/bladerf-profile"
  for f in "$STACK/modules/libbladeRFSupport.so" "$STACK/lib/libbladeRF.2.dylib" "$PROF/receiver-profile-final.json" \
           "$PROF/validated-full-grid-profile.json" "$PROF/schedule-selection.json"; do
    [ -f "$f" ] || { echo "FEIL: mangler $f (se tools/bladerf/README.md)"; finish "RESULT=bladerf_stack_missing"; exit 1; }
  done
  unset PYTHONPATH
  PY=""; BSITE=""; BDYLD=""
  BTEST='import SoapySDR, numpy, simplesoapy, simplespectral
print("RESULT", SoapySDR.getAPIVersion(), len(SoapySDR.Device.enumerate("driver=bladerf")))'
  for v in "$OUTDIR"/.venv*/bin/python /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3; do
    [ -x "$v" ] || continue
    ver="$("$v" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)" || continue
    for k in "" ${KEG08[@]+"${KEG08[@]}"}; do
      site=""; dyl="$STACK/lib"
      if [ -n "$k" ]; then site="$(keg_site "$k" "$ver")"; [ -n "$site" ] || continue; dyl="$STACK/lib:$k/lib"; fi
      diag="$(in_env "$site" "$dyl" "$STACK/modules" "" -- "$v" -c "$BTEST" 2>&1)"
      out="$(printf '%s\n' "$diag" | grep '^RESULT' | tail -1)"
      read -r tag api nfound <<< "${out:-RESULT - 0}"
      echo "  $([ "${nfound:-0}" -gt 0 ] 2>/dev/null && echo 'OK  ' || echo 'nei ') $v py$ver ${site:+[+$site]} api=$api bladerf=$nfound"
      [ "${nfound:-0}" -gt 0 ] 2>/dev/null || printf '%s\n' "$diag" | grep -iE 'error|not loaded|dlopen|no module|reason' | head -3 | cut -c1-240 | sed 's/^/        /'
      if [ "${nfound:-0}" -gt 0 ] 2>/dev/null; then PY="$v"; BSITE="$site"; BDYLD="$dyl"; break 2; fi
    done
  done
  [ -n "$PY" ] || { echo "FEIL: ingen Python/SoapySDR 0.8-kombinasjon laster bladeRF-modulen og ser enheten"; finish "RESULT=no_python"; exit 1; }
  export SOAPY_SDR_PLUGIN_PATH="$STACK/modules"
  export DYLD_LIBRARY_PATH="$BDYLD${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
  export PYTHONPATH="$REPO:$REPO/tools/bladerf${BSITE:+:$BSITE}"
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
# sessions/b200-stack: a SoapyUHD module (e.g. patches/soapyuhd-native-telemetry.patch built against the
# Homebrew UHD 4.x + SoapySDR 0.8 kegs) and the FPGA/firmware images of that UHD version (UHD_IMAGES_DIR).
B200STACK="$OUTDIR/b200-stack"
MODDIRS=()
for d in "$B200STACK/modules" /usr/local/lib/SoapySDR/modules0.8 /opt/local/lib/SoapySDR/modules0.8 /opt/homebrew/lib/SoapySDR/modules0.8 "$HOME/.homebrew/lib/SoapySDR/modules0.8"; do [ -d "$d" ] && MODDIRS+=("$d"); done
images_for() { if [ "$1" = "$B200STACK/modules" ] && [ -d "$B200STACK/images" ]; then echo "$B200STACK/images"; fi; }
UHD_TEST='import SoapySDR
mods = list(SoapySDR.listModules())
found = [dict(d) for d in SoapySDR.Device.enumerate("driver=uhd")]
b2 = [d for d in found if "b200" in " ".join(map(str, d.values())).lower()]
print("RESULT", SoapySDR.getAPIVersion(), len(mods), len(found), b2[0].get("serial", "-") if len(b2) == 1 else "-")'
PY=""; SDRPATH=""; PLUGIN=""; SDRDYLD=""; IMAGES=""; SERIAL=""; SEEN=" "
for c in ${CANDIDATES[@]+"${CANDIDATES[@]}"}; do
  [ -n "$c" ] || continue
  real="$(command -v "$c" 2>/dev/null || true)"; [ -n "$real" ] || continue
  case "$SEEN" in *" $real "*) continue;; esac; SEEN="$SEEN$real "
  ver="$("$real" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)" || { echo "  $real: starter ikke"; continue; }
  SITES=("|")
  for d in /usr/local/lib/python$ver/site-packages /opt/homebrew/lib/python$ver/site-packages "$HOME/.homebrew/lib/python$ver/site-packages" /opt/local/lib/python$ver/site-packages; do [ -d "$d" ] && SITES+=("$d|"); done
  for k in ${KEG08[@]+"${KEG08[@]}"}; do ks="$(keg_site "$k" "$ver")"; [ -n "$ks" ] && SITES+=("$ks|$k/lib${UHDLIBS:+:$UHDLIBS}"); done
  for pair in "${SITES[@]}"; do
    extra="${pair%%|*}"; dyl="${pair#*|}"
    imp="$(in_env "$extra" "$dyl" "" "" -- "$real" -c 'import SoapySDR;print("ok", SoapySDR.getAPIVersion(), SoapySDR.__file__)' 2>&1 | tail -1)"
    case "$imp" in ok*) ;; *) echo "  nei  $real py$ver ${extra:+[+$extra]} $imp"; continue;; esac
    for plug in "" ${MODDIRS[@]+"${MODDIRS[@]}"}; do
      img="$(images_for "$plug")"
      out="$(in_env "$extra" "$dyl" "$plug" "$img" -- "$real" -c "$UHD_TEST" 2>&1 | grep '^RESULT' | tail -1)"
      read -r tag api nmods nfound serial <<< "${out:-RESULT ? ? ? -}"
      echo "  $([ "$serial" != - ] && echo 'OK  ' || echo 'nei ') $real py$ver ${extra:+[+$extra]} ${dyl:+[dyld keg]} ${plug:+[plugins $plug]} api=$api moduler=$nmods uhd-enheter=$nfound b200=$serial  (${imp#ok })"
      if [ "$serial" != - ] && [ -n "$serial" ]; then PY="$real"; SDRPATH="$extra"; SDRDYLD="$dyl"; PLUGIN="$plug"; IMAGES="$img"; SERIAL="$serial"; break 3; fi
    done
  done
done
if [ -z "$PY" ]; then echo "FEIL: ingen kombinasjon av Python/SoapySDR/modul ser en B200"; finish "RESULT=no_b200_stack"; exit 1; fi
[ -n "$SDRPATH" ] && export PYTHONPATH="$SDRPATH"
[ -n "$PLUGIN" ] && export SOAPY_SDR_PLUGIN_PATH="$PLUGIN"
[ -n "$SDRDYLD" ] && export DYLD_LIBRARY_PATH="$SDRDYLD${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
[ -n "$IMAGES" ] && export UHD_IMAGES_DIR="$IMAGES"
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
echo "python: $PY ($("$PY" --version 2>&1)) PYTHONPATH=${PYTHONPATH:-} SOAPY_SDR_PLUGIN_PATH=${SOAPY_SDR_PLUGIN_PATH:-} DYLD_LIBRARY_PATH=${DYLD_LIBRARY_PATH:-}"
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
  if [ -n "${UHD_IMAGES_DIR:-}" ]; then printf 'export UHD_IMAGES_DIR=%q\n' "$UHD_IMAGES_DIR"; fi
  printf 'exec %q -m soapypower "$@"\n' "$PY"
} > "$WRAP"
chmod +x "$WRAP"
# ---- 3b. overflow_probe (request key, integer 1): A/B of the 140-150 MHz hop scan that overflowed with the
# default UHD USB ring (no frame args) against the same scan with num_recv_frames set in the device string.
if [ "${OVERFLOW_PROBE:-$(req_field overflow_probe)}" = 1 ] && [ "$DEVICE_KIND" = b200 ]; then
  SESSION="$OUTDIR/overflow-$STAMP"; mkdir -p "$SESSION"; : > "$SESSION/result.txt"
  for FR in default 64 512; do
    case "$FR" in default) DEVP="driver=uhd,type=b200,serial=$SERIAL";; *) DEVP="driver=uhd,type=b200,serial=$SERIAL,num_recv_frames=$FR";; esac
    OUT="$SESSION/scan-$FR.jsonl"; ERR="$SESSION/scan-$FR.log"; T0=$(date +%s)
    "$WRAP" -d "$DEVP" -A "$ANTENNA" -C 0 -f 140M:150M -r 2M -b 8192 -g "$GAIN_DB" -o 25 -t 0.5 -e "$SCAN_SECONDS" \
      --fft-window blackman --dc-mask-width 5000 --tune-delay 0.05 --partial-policy reject -F json -O "$OUT" > "$ERR" 2>&1
    RC=$?; REC=$(wc -l < "$OUT" 2>/dev/null | tr -d ' '); OVF=$(grep -c 'SDR overflow' "$ERR")
    echo "frames=$FR exit=$RC records=${REC:-0} overflow_errors=$OVF seconds=$(( $(date +%s) - T0 )) of=$SCAN_SECONDS" | tee -a "$SESSION/result.txt"
    sleep 3
  done
  finish "SESSION=$SESSION
RESULT=overflow_probe_done"
  exit 0
fi

# ---- report helper: analysis of a hop-scan / superscan session (never changes a scan result; needs numpy+scipy+matplotlib)
report() {  # report <analyzer.py> <scan.jsonl> <session dir> <tag> [extra args...]
  local an="$1" src="$2" ses="$3" tag="$4"; shift 4
  RPY=""; for cand in "$PY" /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    MPLBACKEND=Agg "$cand" -c 'import numpy, scipy.ndimage, matplotlib.pyplot' >/dev/null 2>&1 && { RPY="$cand"; break; }; done
  ARC=skipped; local t1; t1=$(date +%s)
  if [ -n "$RPY" ] && [ -s "$src" ]; then
    MPLBACKEND=Agg "$RPY" "$REPO/tools/$an" "$src" "$ses/analysis" --tag "$tag" "$@" > "$ses/analysis.log" 2>&1; ARC=$?
  fi
  echo "${tag}_report exit=$ARC via=${RPY:-none} files=$(ls "$ses/analysis" 2>/dev/null | wc -l | tr -d ' ') seconds=$(( $(date +%s) - t1 ))" | tee -a "$ses/result.txt"
}

# ---- satellite database (SatNOGS transmitters + TLEs) for superscan identification; no SDR use.
satdb_refresh() {  # satdb_refresh [max_age_hours]: refresh $OUTDIR/satdb when missing or older than max_age_hours
  local age=${1:-0} meta="$OUTDIR/satdb/meta.json"
  if [ "$age" -gt 0 ] && [ -f "$meta" ] && [ $(( $(date +%s) - $(stat -f %m "$meta" 2>/dev/null || echo 0) )) -lt $(( age * 3600 )) ]; then
    echo "satdb fresh (< ${age} h)"; return 0; fi
  "$PY" -c 'import sgp4' >/dev/null 2>&1 || "$PY" -m pip install --quiet sgp4 >/dev/null 2>&1
  "$PY" "$REPO/tools/satdb.py" refresh "$OUTDIR/satdb" > "$OUTDIR/satdb-refresh.log" 2>&1; local rc=$?
  echo "satdb refresh exit=$rc sgp4=$("$PY" -c 'import sgp4; print(sgp4.__version__)' 2>/dev/null || echo missing) tle=$(grep -o '"tle_count": [0-9]*' "$OUTDIR/satdb/meta.json" 2>/dev/null | grep -o '[0-9]*$')"
  return $rc
}
if [ "$(req_field satdb_refresh)" = 1 ]; then
  satdb_refresh 0 | tee -a "$LOG"; SRC=${PIPESTATUS[0]}
  finish "SESSION=$OUTDIR/satdb
RESULT=satdb_refresh_$SRC"
  exit "$SRC"
fi

# ---- 3a. reanalyze (request key, string session name): rerun the report of an existing session, no SDR use.
REAN="$(req_field reanalyze)"
if [ -n "$REAN" ]; then
  case "$REAN" in
    hopscan-*)   SESSION="$OUTDIR/$REAN"; report analyze_hopscan.py "$SESSION/scan_140_150.jsonl" "$SESSION" hopscan;;
    superscan-*) SESSION="$OUTDIR/$REAN"; satdb_refresh 24 >> "$LOG" 2>&1
                 report analyze_superscan.py "$SESSION/superscan.jsonl" "$SESSION" superscan --bands "$SESSION/bands.json" --qth "$OUTDIR/qth.json" --satdb "$OUTDIR/satdb";;
    *) ARC=bad_name;;
  esac
  finish "SESSION=$OUTDIR/$REAN
RESULT=reanalyze_$ARC"
  [ "$ARC" = 0 ] && exit 0 || exit 1
fi

# ---- 3d. superscan (request key, integer 1): interleaved satellite-band scan, all bands revisited every sweep.
#   superscan_plan  sat | sat-full | sat-s (default sat-full)     tools/satplan.py: DC >= 100 kHz outside every band
#   superscan_rate  8 | 4 | 2 MS/s (default 8)                    sat-full: 5 / 11 / 20 hops, bins 4096 x rate (244 Hz)
#   superscan_gain  auto (default) | dB
# Rate pilot: 30 s at the planned rate; any SDR overflow halves the rate (8 -> 4 -> 2) and re-plans. Analog bandwidth
# -w = 1.25 x rate keeps out-of-band energy off the ADC at high gain; UHD ring num_recv_frames = 256 x rate, capped at
# 1024 (2048 fails on macOS with "usb rx6 submit failed: LIBUSB_ERROR_OTHER", seen 2026-11-22); at 8 MS/s the frames
# are enlarged to 16360 B instead so the ring still holds 0.5 s.
# Gain auto: one sweep per gain 30..76 dB with the raw-IQ headroom guard (stops at the first overload); gaincal.py picks
# the smallest gain within 1 dB of the best input-referred noise with max |I|,|Q| < 0.5 and p99.9 < 0.2 of full scale.
# Main scan resumes after an overflow (up to 5 segments) until SCAN_SECONDS of scanning are done.
# After the scan: refresh the satellite database if older than 24 h, then the report identifies tracks.
if [ "${SUPERSCAN:-$(req_field superscan)}" = 1 ] && [ "$DEVICE_KIND" = b200 ]; then
  SESSION="$OUTDIR/superscan-$STAMP"; mkdir -p "$SESSION"
  OUT="$SESSION/superscan.jsonl"; ERR="$SESSION/scan.log"; : > "$SESSION/result.txt"
  PLAN="$(req_field superscan_plan)"; [ -n "$PLAN" ] || PLAN=sat-full
  RATE="$(req_field superscan_rate)"; case "$RATE" in 2|4|8) ;; *) RATE=8;; esac
  ss_plan() {
    CENTERS=$("$PY" "$REPO/tools/satplan.py" "$PLAN" "$SESSION/bands.json" --rate "$RATE" 2>>"$ERR") || return 1
    BINS=$(( 4096 * RATE )); NRF=$(( 256 * RATE )); [ "$NRF" -le 1024 ] || NRF=1024   # macOS libusb refuses 2048 transfers
    BWHZ=$(( 1250000 * RATE )); WS=$(( RATE * 40000000 + 33554432 ))
    DEVSTR="driver=uhd,type=b200,serial=$SERIAL,num_recv_frames=$NRF"
    [ "$RATE" -lt 8 ] || DEVSTR="$DEVSTR,recv_frame_size=16360"   # 1024 x 16360 B = 0.5 s of ring at 8 MS/s (full-range stack setting)
    echo "superscan plan=$PLAN rate=${RATE}M bins=$BINS bw=$BWHZ centers=$CENTERS" | tee -a "$LOG" "$SESSION/result.txt"
  }
  ss_run() {  # ss_run <gain> <outfile> <extra args...>
    local g="$1" o="$2"; shift 2
    "$WRAP" -d "$DEVSTR" -A "$ANTENNA" -C 0 -r "${RATE}M" -b "$BINS" -w "$BWHZ" -g "$g" -t 0.5 --centers "$CENTERS" \
      --fft-window blackman --dc-mask-width 5000 --tune-delay 0.05 --fft-threads 1 --max-threads 1 --partial-policy reject \
      -F json -O "$o" "$@"
  }
  ss_plan || { echo "superscan plan '$PLAN' failed" | tee -a "$SESSION/result.txt"; finish "SESSION=$SESSION
RESULT=superscan_plan_failed"; exit 2; }
  while [ "$RATE" -gt 2 ]; do                       # rate pilot
    ss_run 50 "$SESSION/pilot-${RATE}M.jsonl" -e 30 --deadline-policy finish-hop > "$SESSION/pilot-${RATE}M.log" 2>&1; PRC=$?
    POV=$(grep -c 'SDR overflow' "$SESSION/pilot-${RATE}M.log")
    echo "superscan pilot rate=${RATE}M exit=$PRC overflow_lines=$POV records=$(wc -l < "$SESSION/pilot-${RATE}M.jsonl" 2>/dev/null | tr -d ' ')" | tee -a "$LOG" "$SESSION/result.txt"
    [ "$PRC" = 0 ] && [ "$POV" = 0 ] && break
    RATE=$(( RATE / 2 )); ss_plan || break
  done
  SGAIN="$(req_field superscan_gain)"; [ -n "$SGAIN" ] || SGAIN=auto
  if [ "$SGAIN" = auto ]; then
    CAL="$SESSION/gaincal"; mkdir -p "$CAL"; TC=$(date +%s)
    for G in 30 40 50 60 70 76; do
      ss_run "$G" "$CAL/gain_$G.jsonl" -u 1 --headroom --headroom-max 1.0 --headroom-p999 1.0 --headroom-workspace "$WS" \
        --rejection-evidence "$CAL/rejected-$G" > "$CAL/gain_$G.log" 2>&1
      GRC=$?; echo "$GRC" > "$CAL/gain_$G.rc"; echo "gaincal gain=$G exit=$GRC" | tee -a "$LOG"
      [ "$GRC" = 0 ] || break
    done
    GSEL=$("$PY" "$REPO/tools/gaincal.py" "$SESSION/gaincal.json" "$CAL" --bands "$SESSION/bands.json" --fallback "$GAIN_DB" --png "$SESSION/gaincal.png" 2>>"$ERR")
    [ -n "$GSEL" ] && GAIN_DB="$GSEL"
    echo "superscan gain auto -> $GAIN_DB dB ($(( $(date +%s) - TC )) s calibration)" | tee -a "$LOG" "$SESSION/result.txt"
  else
    GAIN_DB="$SGAIN"; echo "superscan gain fixed $GAIN_DB dB" | tee -a "$LOG" "$SESSION/result.txt"
  fi
  T0=$(date +%s); : > "$OUT"; SEG=0; RC=1
  while [ "$SEG" -lt 5 ]; do
    LEFT=$(( SCAN_SECONDS - ( $(date +%s) - T0 ) )); [ "$LEFT" -ge 20 ] || break
    SEG=$(( SEG + 1 ))
    ss_run "$GAIN_DB" "$SESSION/segment-$SEG.jsonl" -e "$LEFT" --deadline-policy finish-hop >> "$ERR" 2>&1; RC=$?
    cat "$SESSION/segment-$SEG.jsonl" >> "$OUT" 2>/dev/null; rm -f "$SESSION/segment-$SEG.jsonl"
    echo "superscan segment=$SEG exit=$RC left_before=${LEFT}s" | tee -a "$LOG" "$SESSION/result.txt"
    [ "$RC" = 0 ] && break
    grep -q 'SDR overflow' "$ERR" || break           # resume only after an overflow, never after other failures
  done
  REC=$(wc -l < "$OUT" 2>/dev/null | tr -d ' ')
  echo "superscan plan=$PLAN rate=${RATE}M gain=$GAIN_DB exit=$RC segments=$SEG records=${REC:-0} settle_overflow_warnings=$(grep -c 'during settling' "$ERR") measurement_overflow=$(grep -c 'Measurement failed: SDR overflow' "$ERR") seconds=$(( $(date +%s) - T0 )) of=$SCAN_SECONDS" | tee -a "$SESSION/result.txt"
  satdb_refresh 24 | tee -a "$SESSION/result.txt"
  report analyze_superscan.py "$OUT" "$SESSION" superscan --bands "$SESSION/bands.json" --qth "$OUTDIR/qth.json" --satdb "$OUTDIR/satdb"
  finish "SESSION=$SESSION
RESULT=superscan_exit_$RC;REPORT=$ARC"
  exit "$RC"
fi

# ---- 3c. hopscan (request key, integer 1): the 140-150 MHz single-thread hop scan for SCAN_SECONDS with the UHD
# ring enlarged in the device string (device-open arg), finishing the hop in flight instead of cutting it.
if [ "${HOPSCAN:-$(req_field hopscan)}" = 1 ] && [ "$DEVICE_KIND" = b200 ]; then
  SESSION="$OUTDIR/hopscan-$STAMP"; mkdir -p "$SESSION"
  OUT="$SESSION/scan_140_150.jsonl"; ERR="$SESSION/scan.log"; T0=$(date +%s)
  "$WRAP" -d "driver=uhd,type=b200,serial=$SERIAL,num_recv_frames=512" -A "$ANTENNA" -C 0 -f 140M:150M -r 2M -b 8192 -g "$GAIN_DB" -o 25 -t 0.5 \
    -e "$SCAN_SECONDS" --deadline-policy finish-hop --fft-window blackman --dc-mask-width 5000 --tune-delay 0.05 \
    --fft-threads 1 --max-threads 1 --partial-policy reject -F json -O "$OUT" > "$ERR" 2>&1
  RC=$?; REC=$(wc -l < "$OUT" 2>/dev/null | tr -d ' ')
  echo "hopscan exit=$RC records=${REC:-0} settle_overflow_warnings=$(grep -c 'during settling' "$ERR") measurement_overflow=$(grep -c 'Measurement failed: SDR overflow' "$ERR") seconds=$(( $(date +%s) - T0 )) of=$SCAN_SECONDS" | tee "$SESSION/result.txt"
  report analyze_hopscan.py "$OUT" "$SESSION" hopscan
  finish "SESSION=$SESSION
RESULT=hopscan_exit_$RC;REPORT=$ARC"
  exit "$RC"
fi

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
if [ "$PROCEDURE" = fullrange ]; then
  SESSION="$OUTDIR/fullrange-$STAMP"; mkdir -p "$SESSION"
  printf '%s\n' '{"version":1,"temperature_interval_s":1.0,"temperature_max_age_s":3.0}' > "$SESSION/telemetry-policy.json"
  FAILED=""
  VARIANTS="features-on features-off"; case " $PLAN_ARGS " in *" --only-on "*) VARIANTS=features-on;; *" --only-off "*) VARIANTS=features-off;; esac
  for VARIANT in $VARIANTS; do
    OUT="$SESSION/$VARIANT.jsonl"
    EXTRA=""; [ "$VARIANT" = features-on ] && EXTRA="--rf-state --headroom --headroom-retries 2 --rejection-evidence $SESSION/rejected-iq-$STAMP --ensure-coverage --telemetry-policy $SESSION/telemetry-policy.json"
    # shellcheck disable=SC2046
    "$PY" -c 'import sys;from soapypower.experiments import scan_argv;print("\0".join(scan_argv(sys.argv[1],70e6,6e9,float(sys.argv[2]),sys.argv[3],antenna=sys.argv[4],gain_db=float(sys.argv[5]),executable=sys.argv[6])))' \
      "$DEV" "$SCAN_SECONDS" "$OUT" "$ANTENNA" "$GAIN_DB" "$WRAP" > "$SESSION/$VARIANT.argv0" || { FAILED="$FAILED $VARIANT"; continue; }
    ARGS=(); while IFS= read -r -d '' a; do ARGS+=("$a"); done < "$SESSION/$VARIANT.argv0"
    "${ARGS[@]}" $EXTRA; RC=$?
    echo "== $VARIANT exit $RC"; [ $RC -eq 0 ] || FAILED="$FAILED $VARIANT"
  done
  "$PY" "$REPO/tools/fullrange_compare.py" "$SESSION" --input "$INPUT" > "$SESSION/compare.json" 2> "$SESSION/compare.err"; echo "== compare exit $?"
  # autogenerate the 3D waterfalls / intermittency / merged PDF into $SESSION/report (never fails the run; needs matplotlib)
  # (uses the first python that can import numpy + matplotlib: the SDR python often lacks them)
  if [ -z "${NO_REPORT:-}" ]; then
    RPY=""; for cand in "$PY" "$HOME/.venv/bin/python" "$HOME/.pyenv/shims/python3" /opt/homebrew/bin/python3 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /usr/local/bin/python3 python3; do
      "$cand" -c 'import numpy, matplotlib.pyplot' >/dev/null 2>&1 && { RPY="$cand"; break; }
    done
    if [ -n "$RPY" ]; then "$RPY" "$REPO/tools/report_auto.py" "$SESSION" > "$SESSION/report.log" 2>&1; echo "== report exit $? via $RPY ($(tail -n 1 "$SESSION/report.log" 2>/dev/null))"
    else echo "== report hoppet over: ingen python med numpy+matplotlib (pip install matplotlib pypdf packaging)"; fi
  fi
  RESULT=done; [ -n "$FAILED" ] && RESULT="partial:$FAILED"
  finish "SESSION=$SESSION
RESULT=$RESULT"
  [ -z "$FAILED" ]; exit $?
fi
SUITE="$OUTDIR/suite-$STAMP.json"
SESSION="$OUTDIR/okt-$STAMP"
"$PY" -m soapypower.experiments plan "$DEV" -O "$SUITE" --scan-seconds "$SCAN_SECONDS" --gain "$GAIN_DB" --antenna "$ANTENNA" --input "$INPUT" $PLAN_ARGS || { finish "RESULT=plan_failed"; exit 1; }
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
