# sat-full superscan, 8 MS/s, dithered, auto gain 76 dB — 2026-11-22 19:17 UTC (stopped after 522 s)

Same request as superscan-20261122-194411, with the fixed gain sweep.

* **Gain sweep 30–76 dB, all six gains clean** (no headroom rejection, max |I|,|Q| 0.07 at 76 dB): input-referred noise −186.2 dB
  → **76 dB**, ~12 dB more sensitive than the 50 dB run.
* Segment 1 overflowed at ~272 s and was resumed; segment 2 ended with `SDR read failed or timed out: -1` at 522 s, which the runner did
  not treat as a transport fault → run stopped at 830 records / 84 sweeps. Fixed: a read timeout now resumes like an overflow
  (up to 20 segments, 2 s pause before reopening).

## Signals
* **KINEIS-2E (62931), 400.6422 MHz, Doppler-confirmed (guided search, 3 consecutive sweeps 478–491 s; the predicted curve moved 0.9 kHz and the observed frequency followed it by 0.7 kHz, offsets −0.3…−0.1 kHz).**
  KINEIS-2A and KINEIS-5E share the downlink and are flagged as alternatives. Before this run's analysis fixes it was only a
  frequency-only event, and the burst also made one off-curve control fire (THEA +45 kHz crossed it) — now counted as
  "crossed a detected signal" (1), noise false alarms 0/426; stack 0/246.
* The 137.50 MHz events at 407–419 s coincide with a broadband transient (137.05–138 MHz, 31 % of bins > 3σ at 413 s): no longer
  offered as NOAA 6 candidates (format inconsistent anyway).
* Lines as in the 50 dB run; at 76 dB: 55 RF, 10 LO spur, 11 single LO; 399.9999 / 440.0000 MHz again RF at 10 MHz multiples.
* 78 passes ≥ 10° not heard (TRISAT-R 85°, ION SCV-007 57°, Eaglet 1 55° …).

Raw JSONL is not committed.
