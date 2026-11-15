#!/bin/bash
# B200 (UHD via SoapySDR) 100-300 MHz experiment session on macOS, receive only.
#   bash tools/macos_runner/run_session.sh            (or through runner.sh / launchd)
# Environment: MODE=probe|setup|scan (default scan), SCAN_SECONDS (5-600, default 30),
# GAIN_DB (0-70, default 20), ANTENNA (RX2 | TX/RX, default RX2),
# INPUT (antenna | terminated, default antenna; what the operator connected to the port).
# Under the runner, REQUEST_FILE (or the newest queue/taken request) supplies INPUT.
#   probe: report every Python candidate (SoapySDR import result) and the B200 serial
#   setup: create sessions/.venv (system site-packages) with numpy scipy simplesoapy simplespectral
set -u -o pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${SPR_REPO:-$(cd "$HERE/../.." && pwd)}"
OUTDIR="${SPR_SESSIONS:-$REPO/sessions}"
mkdir -p "$OUTDIR"
if [ -z "${INPUT:-}" ]; then
  REQ="${REQUEST_FILE:-}"
  if [ -z "$REQ" ] && [ -n "${SPR_SESSIONS:-}" ]; then REQ="$(ls -t "$OUTDIR"/queue/taken/*.json 2>/dev/null | head -1)"; fi
  if [ -n "$REQ" ] && [ -f "$REQ" ]; then INPUT="$(plutil -extract input raw -o - "$REQ" 2>/dev/null || true)"; fi
fi
INPUT="${INPUT:-antenna}"
MODE="${MODE:-scan}"; [ "${PROBE_ONLY:-0}" = 1 ] && MODE=probe
SCAN_SECONDS="${SCAN_SECONDS:-30}"; GAIN_DB="${GAIN_DB:-20}"; ANTENNA="${ANTENNA:-RX2}"
case "$MODE" in probe|setup|scan) ;; *) echo "FEIL: MODE må være probe, setup eller scan"; exit 2;; esac
if ! [[ "$SCAN_SECONDS" =~ ^[0-9]+$ ]] || [ "$SCAN_SECONDS" -lt 5 ] || [ "$SCAN_SECONDS" -gt 600 ]; then echo "FEIL: SCAN_SECONDS må være 5-600"; exit 2; fi
if ! [[ "$GAIN_DB" =~ ^[0-9]+(\.[0-9]+)?$ ]] || ! awk -v g="$GAIN_DB" 'BEGIN{exit !(g>=0 && g<=70)}'; then echo "FEIL: GAIN_DB må være 0-70"; exit 2; fi
case "$ANTENNA" in RX2|TX/RX) ;; *) echo "FEIL: ANTENNA må være RX2 eller TX/RX"; exit 2;; esac
case "$INPUT" in antenna|terminated) ;; *) echo "FEIL: INPUT må være antenna eller terminated"; exit 2;; esac
for v in DYLD_LIBRARY_PATH DYLD_FALLBACK_LIBRARY_PATH; do s="SPR_$v"; if [ -n "${!s:-}" ]; then export "$v=${!s}"; fi; done
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$OUTDIR/run-$STAMP.log"
LAST="$OUTDIR/last-run.env"
exec > >(tee "$LOG") 2>&1
finish() { { echo "STAMP=$STAMP"; echo "LOG=$LOG"; echo "INPUT=$INPUT"; echo "$1"; } > "$LAST"; }
echo "== soapy_power $MODE $STAMP  repo=$REPO  $(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null) $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null)"
echo "   scan_seconds=$SCAN_SECONDS gain_db=$GAIN_DB antenna=$ANTENNA input=$INPUT  PATH=$PATH"

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
