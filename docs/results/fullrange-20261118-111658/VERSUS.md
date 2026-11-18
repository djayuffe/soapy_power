# Ordinary vs all features ON

Detailed comparison from `fullrange-20261118-111658`: the plain scan (features
OFF) against the full scan-side feature bundle (features ON). Same B200mini
`30B2A32`, RX2 antenna, 20 dB, 70 MHz–6 GHz, 300 s per variant.

All levels are relative and uncalibrated. The input is an antenna, so both
variants contain external RF changes. Neither mode applies a spur notch,
exclusion or level correction.

## What "all features ON" means

- RF-state observation per hop (LO lock, temperature, validity, chain fingerprint).
- Headroom checks with 2 retries.
- Mask-aware coverage planning (`--ensure-coverage`).
- Rejection-evidence capture (IQ evidence index).
- Per-hop telemetry policy (1 s temperature interval, 3 s max age).

The ordinary scan has none of these. It is a plain hop-and-FFT scan.

## Plan and throughput

| Metric | Ordinary | All ON | Difference |
|---|---:|---:|---:|
| Records | 7,061 | 5,125 | −27.4 % |
| Hop centers per sweep | 849 | 215 | −74.7 % |
| Full sweeps in 300 s | 8.32 | 23.84 | +186.6 % |
| Seconds per sweep | 36.06 | 12.58 | −65.1 % |
| Record rate | 23.55 rec/s | 17.09 rec/s | −27.4 % |
| 1 MHz cells | 5,930 | 5,929 | −1 |
| Median looks per cell | 48 | 24 | −50 % |
| RFIC temperatures | 0 | 5,125 | — |
| Rejected IQ | n/a | 0 | — |

The record-rate number is misleading on its own: ordinary produces more records
per second, but all ON completes a full sweep **2.9× faster** because its
mask-aware plan uses 215 centers instead of 849. All ON gets half the looks per
cell in the same wall time.

The plans are not identical. ON uses ~27.98 MHz center spacing with a 56 MHz
capture; ordinary uses 6.986 MHz spacing. Both cover 70–6000 MHz. This is a
bundle-vs-plain comparison, not a per-feature ablation.

## Level agreement

Across 5,929 common 1 MHz cells:

| Metric | Value |
|---|---:|
| Median Δ (ON − OFF) | −0.043 dB |
| Median absolute Δ | 0.213 dB |
| Robust σ | 0.316 dB |
| p95 absolute Δ | 0.760 dB |
| Maximum absolute Δ | 18.4 dB |
| Cells with absolute Δ > 3 dB | 48 |
| Cells beyond 3 robust σ | 173 (2.9 %) |

The median offset is below 0.05 dB, so the bundle does not shift the level
scale. The tail is dominated by a few locations, not a uniform bias.

## Band summary

Medians of the per-cell ON − OFF difference:

| Band | Cells | Ordinary | All ON | Δ |
|---|---:|---:|---:|---:|
| FM broadcast | 20 | −130.6 | −131.5 | −0.55 |
| Airband | 19 | −132.2 | −132.8 | −0.73 |
| 150–160 MHz | 10 | −133.9 | −118.6 | +15.37 |
| DAB / VHF | 66 | −134.0 | −134.2 | −0.29 |
| TETRA / PMR | 90 | −132.9 | −133.3 | −0.40 |
| LTE 800 | 71 | −135.3 | −135.1 | −0.16 |
| GSM 900 | 80 | −135.1 | −135.0 | +0.04 |
| GNSS L1 | 30 | −135.3 | −135.4 | −0.20 |
| GSM 1800 | 170 | −134.0 | −134.0 | −0.24 |
| UMTS | 250 | −134.7 | −134.4 | +0.11 |
| ISM 2.4 GHz | 84 | −135.1 | −135.1 | −0.02 |
| LTE 2600 | 190 | −135.0 | −135.1 | −0.08 |
| 5G 3.5 GHz | 400 | −134.9 | −134.9 | −0.07 |
| WiFi 5 GHz | 700 | −135.3 | −135.3 | −0.05 |
| **70–6000 MHz** | **5,929** | **−135.0** | **−135.1** | **−0.04** |

The only large band-level effect is **150–160 MHz**, where the ON-window level
is 15.4 dB above the ordinary median. That is not a uniform feature effect; the
band is intermittent across ON sweeps (18.8 dB per-pass range) and stays near
the floor in all ordinary sweeps.

## Largest single-cell differences

| MHz | Ordinary | All ON | Δ | Note |
|---:|---:|---:|---:|---|
| 777 | −113.5 | −131.9 | −18.4 | sharp signal edge |
| 153 | −133.9 | −116.1 | +17.8 | 150–160 band |
| 261 | −134.2 | −118.1 | +16.2 | ~260 MHz feature |
| 157 | −134.0 | −118.2 | +15.9 | 150–160 band |
| 156 | −134.1 | −118.4 | +15.7 | 150–160 band |
| 154 | −133.9 | −118.3 | +15.6 | 150–160 band |
| 159 | −134.0 | −118.6 | +15.4 | 150–160 band |
| 158 | −134.0 | −118.7 | +15.4 | 150–160 band |

The 777 MHz cell is a sharp edge of a strong signal at 772–776 MHz; the two
plans sample the edge slightly differently. The 150–160 MHz and ~260 MHz cells
are the intermittent features visible in the ON waterfall. None of these is
evidence of a level bias from the feature bundle.

## Pass-to-pass stability

| Metric | Ordinary | All ON |
|---|---:|---:|
| Sweeps | 9 | 24 |
| Median σ (2 MHz cells) | 0.064 dB | 0.223 dB |
| p95 σ | 0.647 dB | 1.116 dB |

These are not like-for-like: ordinary has 48 looks per cell and 9 sweeps; all ON
has 24 looks per cell and 24 sweeps. The ON p95 is inflated by the intermittent
150–160 MHz and ~260 MHz structures (p95 falls from 1.116 to 0.917 dB when those
bands are excluded); the median is unchanged at 0.223 dB. Both modes are quiet
away from those structures.

## Telemetry and rejection evidence

All ON:

- 5,125 records, all with a fresh RFIC temperature reading.
- 0 missing, 0 validity failures, 0 polled while streaming.
- Temperature 41.345–42.807 °C, median 41.930 °C, span 1.462 °C.
- 8 quantisation levels, step 0.2924 °C; the fast comb is sensor
  dither/quantisation, not resolved thermal dynamics.
- One stable chain fingerprint.
- 0 rejected IQ; no acquisition was refused on this antenna.

Ordinary: no telemetry, no RF-state checks, no headroom retries, no rejection
evidence. Its behaviour is what a plain `soapy_power` hop scan does.

## Interpretation

1. **No measurable median level shift.** The feature bundle changes overhead and
   timing, not the level scale: median Δ −0.04 dB, robust σ 0.32 dB.
2. **Speed vs depth.** All ON sweeps 2.9× faster but sees each 1 MHz cell half as
   often. That is a deliberate coverage/telemetry trade-off, not a sensitivity
   gain.
3. **The tail is environmental.** 150–160 MHz and ~260 MHz are intermittent
   ON-window signals; 777 MHz is a sharp signal edge. None is attributed to the
   features, and none is removed.
4. **Zero rejects.** Headroom and rejection evidence had no overload event to
   record on this antenna. This run cannot show what they would do under
   overload.
5. **Not a per-feature ablation.** Only all-on vs all-off was measured, and the
   hop plans differ. Isolating RF state, headroom, coverage or telemetry needs a
   controlled 50 Ω input and one feature at a time.
6. **Unresolved.** The 150–160 MHz cause still needs a controlled 50 Ω or
   alternating ON/OFF source run.

## Figure

`versus_fullrange.png` summarises all of the above: mode cards, spectra,
ON−OFF histogram, side-by-side 70–1000 MHz waterfalls, difference against
frequency, band table, 150–160 MHz per sweep, pass-to-pass σ, looks per cell,
largest differences and interpretation.

## Reproduce

```sh
env -u PYTHONPATH sessions/.venv/bin/python tools/report_versus.py \
    sessions/fullrange-20261118-111658
```

Raw JSONL stays in `sessions/` and is not committed.
