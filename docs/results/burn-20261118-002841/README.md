# burn-20261118-002841

Burn test of the B200mini receive chain: four consecutive 300 s trials over
100–300 MHz (grid A/B, fit/holdout), antenna on RX2, 20 dB, no source connected.
All three required acceptance checks passed.

**Evidence level: hardware-observed, relative and uncalibrated.** The input was an
antenna, not a controlled 50 Ω load, so the record includes external RF changes.
Nothing here is dBm, noise figure, an absolute floor, or traceable calibration.

- Session: `sessions/okt-20261118-002841` (gitignored, ~5.2 GB raw JSONL)
- Session ran 2026-11-18 00:28–00:51 local (2026-11-17 23:28:42–23:50:47 UTC)
- Software: `155e1ad` (`v1.6.1-62-g155e1ad`), SoapySDR 0.8.1, UHD 4.11
- Device: B200mini serial `30B2A32`, `driver=uhd,type=b200`, internal clock

## Result

| Required check | Result | Value | Limit |
|---|---|---|---|
| Repeatability | pass | σ 0.0317 dB, bias −0.0006 dB, p95 abs 0.191 dB over 231 passes / 23,138 cells | σ ≤ 0.5 dB |
| IF fit | pass | 1 identifiable component, 424/424 slots, median redundancy 12, residual MAD 0.366 dB | fully identified |
| IF holdout | pass | gauge-free RMS 0.0508 dB, p95 abs 0.121 dB, max abs 0.263 dB; improvement 0.086 dB | RMS ≤ 0.25 dB |

`analysis.json` reports `passed: true`. The holdout limit applies to the RMS
disagreement; the single largest slot differs by 0.263 dB.

Level drift between fit and holdout, including environmental change:

- median −0.037 dB, p95 absolute 1.218 dB over 23,138 cells.
- The quiet-cell check paired median increase is −0.176 dB, so the floor did not
  rise between partitions.

## Temperature and floor

- 28,996 records, all with a fresh RFIC temperature reading (no stale or missing).
- Temperature 46.316–48.070 °C, median 47.193 °C, span 1.754 °C. The sensor
  reports seven quantisation levels of ~0.292 °C in this run.
- Maximum absolute per-trial temperature slope 0.0625 °C/min; chain fingerprints
  stable; zero records with the analog LO outside the 500 kHz DC mask; maximum
  absolute analog LO offset 0.143 Hz.

Floor versus temperature is **diagnostic only**, because the antenna is not a
controlled source:

- OLS: −0.0554 ± 0.0190 dB/°C, r −0.017, explained fraction 0.0003.
- Kalman: +0.0297 ± 0.0224 dB/°C, not confounded, drift range 0.406 dB,
  mean NIS 1.003.
- Neither is applied as a correction.

The plot uses a simple relative floor proxy: median power over ±100 kHz around
130 MHz whenever that RF is within 5 MHz of the tuned centre. It is **not a
calibrated floor** and still contains residual IF shape and environmental change.

- Overall median −132.536 dB (relative); p5–p95 −133.669 to −131.933 dB
  (1.74 dB); full span 2.948 dB.
- Per-trial median: grid-a-fit −132.501, grid-a-holdout −132.547,
  grid-b-fit −132.820, grid-b-holdout −132.492 dB.
- Per-trial proxy slope: +0.027, −0.000, +0.002, +0.022 dB/min.

## Spurious responses

Mixing-law attribution of excess observations, with no automatic notch and no
exclusion from the reported metrics:

| Law | Attributed events | Strongest inferred sources |
|---|---|---|
| 3rd order, sign +1 | 171,839 | 814.0, 797.5, 811.5 MHz |
| 3rd order, sign −1 | 34,717 | 488.0, 488.5, 481.0 MHz |
| 5th order, sign −1 | 18,999 | 816.5, 817.0, 816.0 MHz |

15,196 observations in 47 hops were masked for this test only. Source
frequencies are inferred from the mixing law, not measured; the image hypothesis
was rejected in all four rounds for lack of an in-band parent at least 20 dB
above the floor. `gating: false`.

## Prior 600 s attempt

A 4 × 600 s attempt on the same hardware (`sessions/okt-20261118-000121`) failed:

- `grid-a-fit` completed 600 s with 14,889 records.
- `grid-b-fit` aborted twice with `SDR overflow: samples were dropped; refusing
  discontinuous IQ`, after 374 s and 393 s. The run stopped with exit 2.
- The raw files are retained, compressed as `.jsonl.zst` (5.75 GiB → 2.10 GiB).

The cause is unresolved. The filesystem was 99 % full (13 GiB free) at the time,
and Spotlight indexing covered `sessions/`; both were addressed before the 300 s
run, which completed without overflow. This is not evidence that the receiver is
stable at 600 s, nor that disk I/O caused the overflow.

## Limits

- Antenna input: external signals and interference are part of the series; a
  stable physical input is required to isolate receiver drift.
- Four 300 s trials only. This is not a multi-hour soak, and 600 s per-trial
  behaviour was not reproduced.
- Thermal slopes use a coarsely quantised sensor and one receiver state; they do
  not transfer to another gain, antenna, band or unit.
- Spurious identifications are inferred, not source-measured, and are not used to
  remove data.
- The 150–160 MHz behaviour remains unresolved; it needs a controlled 50 Ω input
  and/or alternating ON/OFF runs before any causal claim.
- Single device (`30B2A32`), single gain (20 dB), RX2, 100–300 MHz.

## Artifacts

- `analysis.json` — acceptance checks from `soapy_power_experiment analyze`
  (ledger SHA-256 `732d3d98…f2e0`).
- `ledger.json` — trials, attempts, record counts, per-trial timings.
- `if_response.json` — relative IF response and its standard error.
- `cell_levels.json` — per-cell levels used by the checks.
- `burn_series.json` — 28,996 temperature points, 1,448 floor-proxy points, and
  per-trial summaries.
- `burn.png` — temperature and 130 MHz floor proxy versus elapsed time.
- `make_burn_plot.py` — regenerates `burn.png` from `burn_series.json`.

## Reproduce

```sh
tools/macos_runner/queue.sh scan --seconds 300 --gain 20 --antenna RX2 \
    --input antenna --procedure suite --wait 1800
```

The suite, analysis and ledger are written by the runner; raw JSONL stays in
`sessions/` and is not committed.
