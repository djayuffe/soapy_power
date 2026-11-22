# sat-full superscan with automatic gain, 45 min, B200mini 30B2A32 — 2026-11-22 15:52–16:40 UTC

`queue.sh scan --seconds 2700 --superscan 1 --superscan-plan sat-full --superscan-gain auto`; gain calibration, scan, satellite database and report all ran on the Mac runner.

Bands 137–138, 144–146, 399.9–403, 430–440 MHz; 20 hop centres from `tools/satplan.py` (DC ≥ 100 kHz outside every band), revisit 12.3 s.

## Gain
* Sweep 10–70 dB with the raw-IQ headroom guard: input-referred noise fell from -146.7 to -183.5 dB (digital − gain) and had not flattened at 70 dB; max |I|,|Q| at 70 dB 0.014 of full scale → **70 dB** chosen (the earlier 20 dB runs were ~28 dB less sensitive). The runner now also tests 76 dB.

## Scan
* 4395 records, 220 sweeps, statuses ['complete'], 0 measurement and 0 settle overflows; report 27 s on the Mac.
* Persistent lines: 400.000 MHz (+19 dB), 439.9999 MHz (+17 dB), 137.2001 MHz (+15 dB) — round frequencies, most likely receiver/reference spurs (retune check pending); 28 weaker lines in 144–146 MHz.
* 124 intermittent emissions: APRS bursts at 144.800 MHz (up to +13.6 dB), 433.9–434.1 MHz ISM bursts (up to +10.8 dB), a burst cluster across 137.0–137.9 MHz in the first ~400 s.

## Satellites
* 349 satellites with in-band downlinks above the horizon during the scan (SatNOGS + SGP4, observer position from the address register).
* Doppler-confirmed identifications: 0. Guided per-sweep search along every predicted curve: no satellite heard; off-curve control 0/1000 false.
* Frequency-only candidates (no Doppler test possible): ERMINAZ-2X, NOAA 6, Orbcomm FM112 — not identifications.
* 120 satellites passed ≥ 10° with an active downlink and were not heard.

Interactive per-band viewers (with predicted Doppler curves) are in the Mac session folder `sessions/superscan-20261122-165242/analysis/` (not committed, ~15 MB).
