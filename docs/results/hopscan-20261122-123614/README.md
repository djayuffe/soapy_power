# Hop scan 140–150 MHz, 10 min, B200mini 30B2A32 — 2026-11-22 11:36 UTC

Queued with `tools/macos_runner/queue.sh scan --seconds 600 --gain 20 --hopscan 1`; the runner ran the scan and then
`tools/analyze_hopscan.py` (PDF + waterfalls + viewer) itself (`result.txt`: report exit 0 in 11 s on the Mac).
The analysis in `analysis/` was regenerated from the same `scan_140_150.jsonl` with the final tool version (intermittent-emission detection added).

```
soapy_power -d driver=uhd,type=b200,serial=30B2A32,num_recv_frames=512 -A RX2 -C 0 -f 140M:150M -r 2M -b 8192 -g 20 -o 25 -t 0.5 \
  -e 600 --deadline-policy finish-hop --fft-window blackman --dc-mask-width 5000 --tune-delay 0.05 \
  --fft-threads 1 --max-threads 1 --partial-policy reject -F json -O scan_140_150.jsonl
```

* 993 records, 141 complete sweeps × 7 hops, statuses ['complete'], continuity ['verified']; 0 measurement overflows, 0 settle overflows.
* Floor -134.9 dB (uncalibrated dB(input power/Hz)), drift sd 15 mdB; bin sd 0.279 dB vs Welch theory 0.285 dB.
* 16 persistent lines ≥ 0.5 dB (same frequencies as the 10:31 UTC run): 148.1270 (+1.98), 146.5618 (+1.60), 146.6614 (+1.13), 148.0659 (+0.97), 147.4551 (+0.85), 149.3481 (+0.85) …
* 4 intermittent emissions:
  * 143.2500 MHz, peak +11.5 dB, 225–272 s after start, ≥0.7 kHz, hop 2 offset -500 kHz
  * 143.2500 MHz, peak +6.9 dB, 69–78 s after start, ≥0.5 kHz, hop 2 offset -500 kHz
  * 142.1780 MHz, peak +3.0 dB, 301–306 s after start, ≥7.1 kHz, hop 1 offset -72 kHz
  * 145.7002 MHz, peak +1.7 dB, 222–231 s after start, ≥0.2 kHz, hop 3 offset +450 kHz

Not decided by this data: whether the persistent lines are real carriers or receiver spurs (retune the LO grid by a few hundred kHz),
and absolute levels. 143.2500 MHz sits exactly 500.0 kHz below its hop LO; it is intermittent, so not a static spur, but the same retune check applies.

Files: `analysis/hopscan_waterfall.png` (hero), `hopscan_waterfall_zoom.png`, `hopscan_waterfall_raw.png`, `hopscan_waterfall.html` (interactive, full resolution),
`hopscan_analysis.pdf`, `hopscan_summary.json`, `hopscan_candidates.csv`, `hopscan_events.csv`. Regenerate: `python3 tools/analyze_hopscan.py scan_140_150.jsonl OUTDIR --tag hopscan`.
