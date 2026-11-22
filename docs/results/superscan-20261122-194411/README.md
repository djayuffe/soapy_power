# sat-full superscan, 30 min, 8 MS/s, dithered hop grid, gain 50 dB — 2026-11-22 18:45–19:15 UTC

`queue.sh scan --seconds 1800 --superscan 1 --superscan-plan sat-full --superscan-rate 8 --superscan-gain auto --superscan-dither 300`

* **8 MS/s works** with the 1024 × 16360-byte UHD ring: pilot clean, 10 hops (5 + 5 dithered by 300 kHz: 136.9/137.2, 143.9/144.2,
  399.8/400.1, 433.4/433.7, 436.7/437.0 MHz), 32768 bins = 244 Hz, analog bandwidth 10 MHz, **revisit 6.24 s per band**
  (4 MS/s: 6.8 s with 11 hops but no dither; 2 MS/s: 12.3 s).
* 2880 records, 289 sweeps, 1808 s; one USB overflow at ~9 min, resumed automatically (2 segments).
* **Gain only 50 dB:** the gain sweep hit a USB overflow at 60 dB and stopped there, so 70/76 dB were never measured.
  Input-referred noise at 50 dB is ~12–16 dB worse than at 76 dB → nothing satellite-like reached the detectors.
  Fixed in the runner (an overflow is re-measured, not taken as a headroom rejection) — see the 76 dB runs.

## Signals
* 0 LEO-Doppler tracks, 0 confirmed identifications, 0 frequency-only candidates. 159 passes ≥ 10° with an active downlink not heard
  (TRISAT-R 85°, KOSEN-1 72°, KINEIS-2A 66°, ESTCUBE 1 66° …).
* Controls: off-curve 0/796, stack 0/580 false.
* 77 persistent lines. **Line origin (new, from the dithered grid):** 60 seen from both LOs at different offsets → RF at the antenna;
  8 only from one LO at a fixed offset → LO-related spurs (137.5999 +400 kHz and 137.6500 +450 kHz from the 137.2 MHz LO, 400.4 and
  401.2 MHz from 400.1, 431.2 MHz from 433.4); 9 seen from one LO only (not decided).
* 399.9999 and 440.0000 MHz are RF (both LOs) at exact 10 MHz multiples → most likely the B200's own 40 MHz reference leaking into the
  front end, not a transmitter. 400.8 MHz (RF) completes a 400 kHz comb around 400 MHz.
* 2 m: ~57 steady RF lines (145.7005 +18 dB, 145.8226, 145.9185, 144.3826 …), local interference, all steady → not satellites.
* 433/434 MHz ISM bursts (434.0716 MHz), 2 m bursts (145.6756 +37.5 dB at 1767 s, 144.8023 APRS).

Raw JSONL (1.9 GB) is not committed.
