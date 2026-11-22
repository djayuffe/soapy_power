# Superscan 137–138 / 145.8–146.0 / 435–438 MHz, 15 min, B200mini 30B2A32 — 2026-11-22 12:12 UTC

Queued with `tools/macos_runner/queue.sh scan --seconds 900 --gain 20 --superscan 1`; the runner ran the scan and then
`tools/analyze_superscan.py` itself (`result.txt`: report exit 0 in 9 s on the Mac). `analysis/` here is regenerated from the
same `superscan.jsonl` with the committed tool version (layout only differs).

```
soapy_power -d driver=uhd,type=b200,serial=30B2A32,num_recv_frames=512 -A RX2 -C 0 -r 2M -b 8192 -g 20 -t 0.5 \
  --centers 136.8M,138.2M,145.5M,435.85M,436.6M,437.35M -e 900 --deadline-policy finish-hop \
  --fft-window blackman --dc-mask-width 5000 --tune-delay 0.05 --fft-threads 1 --max-threads 1 --partial-policy reject -F json
```

Hop centres keep the DC spike ≥ 100 kHz outside every band; each band bin is taken from the hop that sees it closest to
its centre (≤ 850 kHz offset). All bands are revisited every sweep.

* 1413 records, 236 complete sweeps × 6 hops, all `complete` / `verified`; 0 measurement and 0 settle overflows;
  revisit 3.82 s per band, dwell 0.50 s, retune 0.10 s (0.20 s on the 145.5→435.85 and 437.35→136.8 MHz jumps);
  max host read latency 92 ms (no overflow with num_recv_frames=512).
* Floor σ 0.28 dB per bin in every band (receiver-limited, gain 20 dB).
* No Doppler track, no drifting track, no persistent line ≥ 0.5 dB, no narrowband intermittent emission in any band.
* 2 wideband transients, both only in the 136.80 MHz hop (137.0–137.5 MHz), one sweep each, at 31 s and 195 s
  (9–10 % of bins > 3σ).

Not decided by this data: whether a satellite passed during the window (no orbit data used), and the sensitivity of the
antenna + gain-20 chain to a real pass. The Doppler detector itself is verified on synthetic passes
(`tests/test_analyze_superscan.py`: f0 within 150–250 Hz, TCA within 6 s, peak rate within 12 %).
