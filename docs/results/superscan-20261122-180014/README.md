# sat-full superscan, 30 min, 4 MS/s, auto gain 76 dB — 2026-11-22 17:02–17:32 UTC

`queue.sh scan --seconds 1800 --superscan 1 --superscan-plan sat-full --superscan-rate 8 --superscan-gain auto`

* Rate pilot: 8 MS/s failed to open (`usb rx6 submit failed: LIBUSB_ERROR_OTHER`, num_recv_frames 2048) → automatic fallback to 4 MS/s
  (11 hops, 16384 bins, analog bandwidth 5 MHz, revisit 6.8 s vs 12.3 s at 2 MS/s). Runner now caps the ring at 1024 frames and enlarges frames at 8 MS/s.
* Gain sweep 30–76 dB: input-referred noise still improving at 76 dB (−186.0 vs −183.5 at 70); max |I|,|Q| 0.064 → **76 dB**.
* 2894 records, 264 sweeps, 0 measurement / 0 settle overflows, 1 segment; report 29 s on the Mac.

## Signals
* Two ~6–8 kHz wide signals at 137.66 and 137.80 MHz, ~700–1000 s, frequency falling ~3–4 kHz (LEO-like); 6 Doppler-like tracks.
  Channels match Orbcomm; the only Orbcomm in the database pass list (FM116, 12°) does not follow the curve, and orbit-only matching
  is not used on wide signals (the peak wanders inside the band). Unidentified.
* Doppler-corrected stack: carrier at 400.656 MHz, z 8.3 over 45 sweeps, matching the Kinéis constellation's downlink (KINEIS-1B/-1E/-2B/-4B share
  the frequency; which one is ambiguous). Controls: per-sweep search 0/748, stack 0/490 false.
* Spurs / steady lines at round frequencies: 400.000, 401.600, 440.000 MHz; APRS 144.800 MHz and 434 MHz ISM bursts (terrestrial).
* 264 satellites with in-band downlinks above the horizon; 96 ≥ 10° with an active downlink not heard.
