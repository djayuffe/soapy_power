# sat-full superscan, 30 min, auto gain 76 dB — 2026-11-22 20:42–21:12 UTC (fell back to 4 MS/s)

`queue.sh scan --seconds 1800 --superscan 1 --superscan-plan sat-full --superscan-rate 8 --superscan-gain auto --superscan-dither 300`

* The 8 MS/s pilot logged one *recovered* settle overflow and the runner counted it as a failure → fallback to 4 MS/s
  (22 hops with the 300 kHz dither, 16384 bins = 244 Hz, revisit 13.5 s). Fixed: the pilot now falls back only on a failed
  measurement or > 3 settle overflows in 30 s.
* Gain sweep 30–76 dB clean, input-referred noise −186.0 dB → **76 dB** (max |I|,|Q| 0.05).
* 2918 records, 134 sweeps, 1805 s, one overflow resumed (2 segments), 0 read timeouts.

## Result: nothing satellite-like
* 0 Doppler tracks, 0 confirmed, 0 frequency-only candidates; 203 passes ≥ 10° with an active downlink not heard — including a
  near-zenith rideshare train (HCT-SAT2, UM5Sat-EOsat, SKYBEE-A01, ConnectaIoT-3, LUR-1 at 87–88°) and Sedna-1/BRO-11 at 85°.
* Controls: off-curve 0/834, stack 0/632. The highest stack value (OSCAR 7, 145.925 MHz, z 9.2) is rejected by the new
  fixed-frequency test: its curve moved only 587 Hz (< 6 bins) and the stack was no sharper along the curve than at a fixed frequency
  (−0.04 dB) → a terrestrial 2 m line, which the previous stack rule would have reported as an identification.
* 68 persistent lines (51 RF, 3 LO spur, 14 one LO only); 433.926 / 434.072 MHz ISM bursts; no 400–403 MHz activity in this window
  (the Kinéis burst of the 19:17 run was not repeated).

Raw JSONL is not committed.
