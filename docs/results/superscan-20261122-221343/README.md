# sat-full superscan, 30 min, 8 MS/s + 76 dB, dithered — 2026-11-22 21:15–21:45 UTC (the intended configuration)

`queue.sh scan --seconds 1800 --superscan 1 --superscan-plan sat-full --superscan-rate 8 --superscan-gain auto --superscan-dither 300`

* Pilot at 8 MS/s clean (0 measurement failures, 0 settle overflows) → 10 hops, 32768 bins = 244 Hz, 10 MHz analog bandwidth,
  **revisit 6.24 s per band**.
* Gain sweep 30–76 dB clean, input-referred noise −186.2 dB → **76 dB** (max |I|,|Q| 0.12).
* **2886 records, 289 sweeps, 1809 s in one segment: 0 measurement overflows, 0 read timeouts** (16 recovered settle overflows).

## Signals
* 0 LEO-Doppler tracks, 0 Doppler-confirmed identifications.
* 145.827 MHz bursts at 1736–1798 s (4 sweeps, FSK-like, 1.2 kHz tone spacing, ±1 kHz scatter) on the worldwide satellite APRS
  simplex frequency 145.825 MHz. GUARANISAT-2 (rising, < 14°) fits with offset −0.2 kHz and PARUS-T1 (71°, just after TCA) with
  +4.5 kHz — but GUARANISAT-2's predicted Doppler moved only ~230 Hz across the hits, so a ground station transmitting to the
  digipeater fits just as well. Reported as **frequency-only**, not as an identification (new rule: guided hits count as
  Doppler-confirmed only if the curve moves ≥ 2 bins and the offsets scatter < 0.6× the observed frequencies; one signal → one satellite).
* Controls: off-curve 0/908, stack 0/742.
* 77 persistent lines: 55 RF, 10 LO spur, 12 one LO only; 399.9999 / 440.0000 MHz RF at 10 MHz multiples (B200 reference);
  400.4 / 401.6 MHz LO spurs; 2 m: steady local RF lines (145.8226, 145.9185, 144.3826, 145.7266 MHz).
* ISM 433.926 / 434.072 MHz and 2 m terrestrial bursts (145.7005 +21 dB, 144.800 APRS). No 400–403 MHz activity in this window.
* 241 passes ≥ 10° with an active downlink not heard (SATGUS 89°, KINEIS-3C 88°, MACSAT 84° …).

## Across the four runs tonight
Only one Doppler-confirmed satellite: **KINEIS-2E, 400.6422 MHz** (19:17 run, 3 sweeps, predicted curve moved 0.9 kHz, observed followed by 0.7 kHz, offsets −0.3…−0.1 kHz).
At 76 dB the receiver is at its noise limit (−186 dB input-referred), and dozens of high passes stay silent — the limit is most likely the
antenna/feed line and the satellites' short duty cycles rather than the scan.

Raw JSONL (1.9 GB) is not committed.
