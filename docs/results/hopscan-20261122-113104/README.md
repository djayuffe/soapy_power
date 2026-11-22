# 10 min hop scan 140-150 MHz, B200mini (antenna), queue.sh scan --seconds 600 --hopscan 1
Device string: driver=uhd,type=b200,serial=30B2A32,num_recv_frames=512. -r 2M -b 8192 -g 20 -o 25 -t 0.5,
--fft-threads 1 --max-threads 1, --deadline-policy finish-hop (replaces -u 100 so the run is time-bounded and the last hop completes).

- exit 0, 603 s, 994 records = 142 complete sweeps x 7 hops, 0 remainder
- all records measurement_status=complete, continuity=verified, acquired 1007616 samples each
- measurement-phase overflows: 0; settle-phase overflow warnings: 1 (tolerated, settle samples are discarded)
- host I/O: max read 75 ms, max RX scheduling gap 31 ms, GC pause max 0 ms, no read errors
- last sweep: strongest bin 148.247 MHz at -125.3 dB (relative), median -134.7 dB
Raw jsonl stays in sessions/ (gitignored).

## Analysis

`analysis/hopscan_analysis.pdf` (10 pages), `hopscan_summary.json`, `hopscan_candidates.csv`.
Regenerate: `python3 tools/analyze_hopscan.py scan_140_150.jsonl OUTDIR --tag hopscan`.
Note: the 148.247 MHz peak seen earlier is the receiver DC-artifact skirt at the 148.25 MHz hop centre, not a signal.
