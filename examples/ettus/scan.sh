#!/bin/sh
# Receive-only B200mini USB 3 survey; configure the driver/image environment first.
set -eu
if [ "$#" -ne 3 ]; then
  echo "Usage: SDR_DEVICE='driver=uhd,type=b200,serial=YOUR_SERIAL' $0 RANGE SECONDS OUTPUT.jsonl" >&2
  exit 2
fi
: "${SDR_DEVICE:?Set SDR_DEVICE to the actual receiver selector}"
exec "${SOAPY_POWER:-soapy_power}" \
  -d "$SDR_DEVICE" -A "${SDR_ANTENNA:-RX2}" -C 0 \
  -f "$1" -e "$2" -O "$3" -F json \
  -r 56M -w 56M --master-clock-rate 56M --force-rate \
  -g "${SDR_GAIN_DB:-20}" -b 8192 -o 25 -t 0.005 \
  --tune-delay 0.02 --settle-samples 32768 --lock-sensor lo_locked \
  --reopen-stream --deadline-policy finish-hop --require-timestamps \
  --partial-policy reject --no-pyfftw --precision single \
  --max-threads 4 --max-queue-size 4
