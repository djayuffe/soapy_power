# Automated 100–300 MHz experiments, instrument control and model validation

Evidence levels (see [MEASUREMENT_ROADMAP.md](MEASUREMENT_ROADMAP.md)):

- `soapy_power_experiment dry-run` and the unit tests are **software verification**
  against a simulated rig with known truth. Their reports say `data_source: simulated`.
- A `run` session on a real receiver is **hardware-observed, relative and
  uncalibrated** (`data_source: acquired`). It yields no dBm, noise figure,
  linearity or traceability. `calibrated` is always `false` here.
- First hardware session (2026-11-15, B200mini 30B2A32, macOS, Homebrew UHD + SoapyUHD,
  antenna on RX2, 20 dB, 4 × 30 s, no generator): all required checks passed.
  Repeatability σ 0.12 dB; IF response fully identified (1 component, 424/424 slots,
  redundancy 12); independent holdout refit agreed within 0.058 dB RMS (max 0.20 dB);
  median level drift between sessions −0.08 dB. The response ripples about ±0.5 dB
  inside ±14 MHz and rises to +1.5–1.9 dB at ±20 MHz. With an antenna, most cells sit at
  the receiver's own floor, so this `G` is the IF shape of the displayed floor (noise
  shaping and gain together), which is what flattens a survey; it is not a pure gain
  response. Applying it reduced the apparent dispersion only 3.08 → 3.05 dB: that
  dispersion is dominated by time variation of real signals between hop visits.
  Settling and image rejection were not measured (no generator).
- Telemetry session (2026-11-17, same B200mini, antenna on RX2, 20 dB, 4 × 60 s, now on
  Homebrew UHD 4.10 with the telemetry SoapyUHD module and the superseded SoapySDR 0.8.1
  keg): all required checks passed under the revised holdout rule (below). Repeatability
  σ 0.077 dB; holdout refit 0.051 dB RMS; the IF response agrees with the 2026-11-15
  session (UHD 3.15) within 0.126 dB RMS gauge-free. Telemetry on all 5741 records: RFIC
  temperature 46.3–47.2 °C in steps of 0.292 °C, no read while streaming, one processing
  chain fingerprint, IQ tracking on throughout, analog LO within 0.15 Hz of the spectrum
  origin. The first reading after every device open is 47.19 °C, one step above the rest
  of the trial; the trial summary therefore uses the median of the first and last tenth.
  Floor versus temperature within center: +0.024 ± 0.031 dB/°C (r = 0.01, diagnostic, an
  antenna is not a controlled input). The same three LO-harmonic laws as on 2026-11-15
  were attributed (LTE 800 3rd, GSM 900 5th, DVB-T 3rd).
- Terminated session (2026-11-16, same receiver state, a bare 50 Ω resistor on RX2,
  4 × 60 s): all required checks passed; repeatability σ 0.084 dB; holdout refit
  0.066 dB RMS; the IF correction now reduced dispersion by 0.17 dB (0.03 dB with
  the antenna). Floor −135.3 dB(input power/Hz), flat within 0.42 dB (p5–p95) over
  100–300 MHz, no steady internal features. The receiver's own IF shape is a smooth
  bowl, about −0.4 dB at ±3 MHz rising to +2.2 dB at ±21 MHz. The antenna-derived
  response differs by 0.27 dB RMS: its ripple came from the environment.
  `compare` classifies every steady antenna feature as external (DAB 219–230 MHz up to
  +24 dB, 200.05 MHz +14.6 dB, 237–269 MHz +9.5 dB, FM 105.65 MHz +13 dB). DAB
  vanishes with the termination, so port leakage is at least ~24 dB down. The
  termination still showed intermittent content above 237 MHz in 137 cells (p90 up to
  ~6 dB over the typical spread). An earlier version of this file called it local to
  the setup; that was wrong. It is not intermittent in time and not generated in the
  host: it follows the LO, not the RF. Per hop it appears with probability ≈1 at the
  same baseband offsets, and the offsets move by three or five times the LO step. The
  receiver's switching mixer responds at odd LO harmonics, so strong signals far above
  300 MHz leak into the B200's 3rd and 5th harmonic responses (see *Harmonic mixing
  responses* below). `spurious_responses` attributes 18 811 excess observations to
  `IF = 3·LO − f` with f in 762–821 MHz (LTE 700/800 downlink), 515 to
  `IF = f − 3·LO` with f in 482–488 MHz (DVB-T), and 472 to `IF = f − 5·LO` with f in
  811–818 MHz (LTE 800). Masking those 8 660 observations (52 hops) removes all 137
  intermittent cells, and the terminated floor stays −135.3 dB with 0.47 dB flatness.
  These signals still reach the mixer with the resistor on RX2; the coupling path
  (unshielded resistor, board or USB cable) is not identified.
  With the antenna the same analysis finds LTE 800 (3rd, 20 759 observations),
  GSM 900 (5th, 3 417, 933–940 MHz) and DVB-T (3rd, 2 822, around 484 and 563 MHz).
  After masking, `compare` keeps 19 of 20 external features and reports no internal or
  internal-intermittent features (20 before). The broad 237–269 MHz hump is real
  external RF, not a mixing response: all 12 LOs that cover 250 MHz see the same
  −127 to −128 dB there, against −135.3 dB terminated, and a harmonic response cannot
  sit at a fixed RF across that many LOs. A low-pass filter at or below 300 MHz in
  front of RX2 removes the harmonic responses physically. Fixed-tune baseline captures
  (`soapy_power_calibration baseline`, 50 Ω/50 Ω, temperature assumed 295 K, not
  measured) agree with the suite floor within 0.5 dB at 110–230 MHz; the 270 MHz tune
  averages 5–10 dB higher because its linear mean includes the 3rd-harmonic LTE
  response (3 × 270 = 810 MHz). Continuous 100 ms blocks at 56 MS/s overflowed intermittently,
  so baseline captures use 5 ms blocks with the stream reopened. One load cannot
  separate gain from noise figure: absolute scale still needs a known tone, two noise
  temperatures, or a known noise figure.

## Model being validated

Per RF cell `c` seen by hop `h` at baseband offset `f`: `D[c,h] = S[c] + G[f] + e` (dB).
`S` is the unknown received level (a nuisance parameter), `G` the relative IF response
of one fixed receiver state. Overlapping hops identify `G` up to one constant **per
connected component** of the cell/IF-slot graph. A perfectly periodic hop grid splits
the IF axis into unlinked cosets. In the synthetic test one 7 MHz grid leaves 35
components and only 4 of 140 slots identified relative to the anchor. A second grid
shifted by half a hop links all 140 slots (max error vs truth < 0.1 dB). The suite
therefore always scans two interleaved grids, A and B. Slots outside the anchor's
component are reported as `null`, never as numbers.

The fit is an exact weighted least-squares solve (rows eliminated analytically,
column Schur complement solved with a minimum-norm solve) inside a Huber IRLS loop with
a robust residual scale. Baseband offsets come from each record's
`metadata.frequency_grid.rf_origin_hz` and `frequency_sign`, so converter-LO and
IQ-conjugation mappings are honoured. Records without a frequency grid fall back to the
coverage midpoint, which is half a bin off for even FFTs; the report lists which was used.
Interpolated `estimated_bins` (DC) are never treated as measurements.

Validation never reuses the fitted data:

- **IF response:** fitted on the `fit` trials and refitted independently on the
  `holdout` trials, taken later. It passes when the fit is fully identified, the
  gauge-free RMS disagreement is at most 0.25 dB, and applying the fit to the holdout
  does not raise the IF-slot dispersion of quiet cells beyond the response's own
  estimation noise plus two bootstrap standard deviations of the paired change. Quiet
  cells are the lowest quarter by dispersion in the *fit* trials, so their selection does
  not depend on the holdout. A flat receiver passes with no improvement. A changed
  receiver fails. The all-cell dispersion is reported with its bootstrap SD but does not
  decide: with an antenna it is dominated by signals that change between hop visits. On
  2026-11-17 it was 5.05 dB with a 0.094 dB SD. The rule before then allowed only
  0.0005 dB of increase there and failed that session on noise (−0.011 dB), although the
  quiet cells improved from 0.953 to 0.912 dB.
- **Settling:** the model `|e(t)| = A·exp(−t/τ)` is fitted on even-indexed delays, and
  the chosen delay (with 1.25× margin) is verified on the odd-indexed delays. It fails
  if the model says the reference (longest delay) itself has not settled to 1/10 of
  the tolerance.
- **Image rejection** (informational): estimated per hop from the settling tone and
  its mirror at `2·origin − tone`, with the local floor subtracted. Floor-limited hops
  give only a lower bound. An RF-fixed signal at one hop's image frequency biases that
  hop only, so the median is robust.

## Workflow

```sh
soapy_power_experiment dry-run /tmp/dry            # whole pipeline vs simulated rig, ~7 s
soapy_power --detect
soapy_power_experiment plan 'driver=uhd,type=b200,serial=SERIAL' -O suite.json
soapy_power_experiment run suite.json session-001  # antenna only: grid A/B fit + holdout
soapy_power_experiment analyze session-001         # exit 0 only if all required checks pass

# With a SCPI generator (adds the settling sweep and image estimate):
soapy_power_experiment plan 'driver=uhd,type=b200,serial=SERIAL' -O suite-tone.json \
    --tone-dbm -40 --tone-mhz 200 --max-receiver-input-dbm -20 --path-loss-db 1.0
export SCPI_SOURCE_CONFIG=$PWD/scpi_source.json
soapy_power_experiment run suite-tone.json session-002 --source-hook tools/scpi_source_hook.py
```

Trials:

- `grid-{a,b}-{fit,holdout}`: 100–300 MHz. Grid A has 29 hops of 6.986 MHz (1022 of 8192
  bins retained at 87.5244140625 % overlap). Grid B starts half a hop lower and has
  30 hops. The acquisition policy is the same as `examples/ettus/scan.sh`.
- `settle-<N>ms`: tone ON. Each scans the tone ±(28 MHz + 2 hops) with
  `--settle-samples 0` and tune delay 0–200 ms. The reference is the longest delay. Only
  the survey's 7 MHz retune steps with stream reopen are characterised.

Hop centres reproduce `SoapyPower.freq_plan` exactly; the test compares the two directly.
Every generated argument vector is parsed by the real CLI parser in the tests.

`ledger.json` records, per attempt: argv, timing, stderr tail, record count, SHA-256 and
the source acknowledgment (including the hook's readback and config hash). Failed and
interrupted attempts are kept, and a failed attempt is retried under a new file name.
When retries run out, the session aborts. The source is returned to OFF on every exit
path, and the ledger records the final source state. `analyze` refuses incomplete
ledgers and any file whose SHA-256 changed. It writes `analysis.json` and
`if_response.json`, with `validated` true only when the holdout check passed.

`plan --tone-dbm` refuses to run without a declared receiver input ceiling and path
loss, and refuses tones above that ceiling.

`calibration-config` writes a validated tone-method config for
`soapy_power_calibration auto` (9 RF points 100–300 MHz × ±5/±10 MHz, interior
holdouts). Levels, ceiling, path loss, certificate and reference plane are mandatory
arguments. `physical_setup_confirmed` stays `false` until you edit it.

## Terminated input: internal versus external features

With a 50 Ω termination on the receive port, every RF cell sees the same thermal
input. The additive model then holds without a changing environment. Whatever still
rises above the floor is generated in the receiver or host.

```sh
soapy_power_experiment plan 'driver=uhd,type=b200,serial=SERIAL' -O suite-term.json --input terminated
soapy_power_experiment run suite-term.json session-term
soapy_power_experiment analyze session-term        # adds an informational terminated_floor check
soapy_power_experiment compare session-term session-antenna
```

`--input` (antenna | terminated) is the operator's declaration of what is connected.
The software cannot check it, but it is stored in the suite, the analysis and
`cell_levels.json`. A terminated suite refuses a settling tone.

`analyze` writes `cell_levels.json`: the IF-corrected median, 10th and 90th percentile
per 100 kHz cell, the holdout median, and the receiver context signature.

`compare` requires two passed sessions with the same data source and receiver context,
declared terminated and antenna. It reports:

- **External:** cells where the antenna session exceeds the terminated level by more
  than 3 dB. These signals arrive at the port.
- **Internal:** features above the terminated floor itself, generated in the receiver
  or host. `antenna_minus_terminated_db` near 0 means the antenna feature at that
  frequency is this internal one.
- **IF response agreement:** how closely the two sessions' IF responses match. With
  the antenna mostly at the receiver floor, they should agree.

## Telemetry per hop

Suites planned since 2026-11-17 carry a telemetry policy (`TELEMETRY_POLICY`; `plan --no-telemetry`
turns it off, `--require-temperature` makes a missing or stale RFIC temperature reject a hop). `run`
writes it to `telemetry-policy.json` in the session, records its SHA-256 in the ledger and passes
`--telemetry-policy` to every scan. Each record then carries `soapypower.telemetry` snapshots: RFIC
temperature (read at most once per second, only between hops while the stream is closed),
LO/reference lock, RSSI, processing-chain fingerprint and IQ-tracking state when the native SoapyUHD
extension is present, and the controller's analog-LO mapping.

`analyze` adds an informational `telemetry` check:

- temperature range, and drift per trial (°C/min, linear fit over the trial);
- `floor_vs_temperature`: each hop's median cell level regressed on its temperature within the same
  center (both demeaned per center, so RF content cancels). With a terminated input this measures
  the receiver's gain/noise drift; with an antenna it is a diagnostic only. It is never applied as
  a correction;
- distinct chain fingerprints and IQ-tracking states (a change means the processing chain differed
  between hops);
- the largest analog-LO offset from the spectrum origin, and how many records place the LO outside
  the DC mask (the LO leakage would then be fused as signal).

In a dry run with a known 0.04 °C/s ramp and −0.30 dB/°C, the check recovers −0.30 ± 0.02 dB/°C
and flags a 2 MHz LO offset on every record.

## Harmonic mixing responses

A direct-conversion receiver also responds near odd LO harmonics. A source at `f`
appears at baseband offset `IF` when `f = n·LO − s·IF` for order `n` (1, 3, 5, 7) and
sign `s = ±1`; `n = 1, s = −1` is the RF-fixed law (an ordinary signal), `n = 1, s = +1`
the IQ image. In a stepped survey each law draws a different line in the (LO, IF)
plane, so a source that is seen from several LOs identifies its law, even though one
hop alone cannot tell it apart from an RF signal.

`analyze` runs `spurious_responses` on every session (informational, never gating):

1. Residual of each observation against its cell level; observations more than 6 dB
   above it are *excess*.
2. `mixing_hypotheses`: for each law, the implied source frequency of every excess
   observation is binned (0.5 MHz). An observation's support under a law is the number
   of distinct LOs that land in the same source bin. Each observation goes to the law
   with the most support (at least 3 LOs); the RF-fixed law wins ties, so ordinary
   signals are never reattributed. A law needs at least 50 observations.
3. The image law is accepted only if its source bands contain an in-band parent at
   least 20 dB above the floor (`image_parent_ok`); otherwise it is retested without it.
4. Observations explained by an identified law are masked, the cell levels recomputed,
   and the loop repeats (up to 4 rounds) until no new source band appears.

The report lists each law with its source bands and attributed observation count;
`cell_levels.json` adds `mixing_identified` and the masked median/p10/p90, and
`terminated_floor` and `compare` repeat their results under `after_mixing_mask`.
The source frequencies are inferred from the law, not measured: confirm them with a
filter or a wideband scan of the source band.

## macOS hardware runner (launchd)

`tools/macos_runner/` runs sessions on a Mac whose shell an assistant or a remote job
cannot reach directly, for example when the SDR is on the Mac's USB.

```sh
bash tools/macos_runner/install.sh                      # once: launchd agent + probe
tools/macos_runner/queue.sh probe --wait 120            # Python/SoapySDR/module matrix + B200 serial
tools/macos_runner/queue.sh setup --wait 170            # sessions/.venv-* with numpy scipy simplesoapy simplespectral
tools/macos_runner/queue.sh scan --input terminated --wait 600
tools/macos_runner/queue.sh scan --input terminated --procedure baseline --wait 600   # fixed-tune captures
bash tools/macos_runner/uninstall.sh
```

- **Requests:** XML property lists in `sessions/queue/` with only `mode`,
  `scan_seconds`, `gain_db`, `antenna` and `input`, validated by the runner. The
  runner copied to `~/Library/Application Support/` executes nothing from a request.
  It starts `tools/macos_runner/run_session.sh` from the checkout, which runs the
  checkout's Python anyway. Status, logs and sessions are written to `sessions/`
  (git-ignored).
- **Baseline captures:** `--procedure baseline` records fixed-tune captures at 110,
  150, 190, 230 and 270 MHz (200 × 5 ms with the stream reopened per run, the
  survey's policy; continuous 100 ms blocks overflowed at 56 MS/s on macOS) with a
  SHA-256 manifest. These are for `soapy_power_calibration baseline` and `compare`
  ([CALIBRATION_STEPS.md](CALIBRATION_STEPS.md) §6), which need a single tuned grid
  per file.
- **Stack discovery:** `run_session.sh` tries each Python interpreter (plus Homebrew,
  `/usr/local` and MacPorts `site-packages`) with each SoapySDR module directory until
  one enumerates exactly one B200. After `brew upgrade` moved SoapySDR to 0.9 (Python
  3.15), the 0.8-ABI modules only load against the superseded `Cellar/soapysdr/0.8*`
  keg. The runner therefore also tries that keg's `site-packages`, with its `lib` and the
  UHD keg libraries on `DYLD_LIBRARY_PATH` (exported in a subshell, never through
  `env`). A SoapyUHD module in `sessions/b200-stack/modules` is tried first, with
  `UHD_IMAGES_DIR=sessions/b200-stack/images`. `brew cleanup` would remove the keg. It drops an inherited `PYTHONPATH`, since another
  Python version's site-packages breaks imports. `setup` adds missing packages to a
  dedicated venv (`--system-site-packages`) and never to a system Python.
- **Environment:** `install.sh` snapshots PATH and UHD/SoapySDR variables into the
  agent. `DYLD_*` variables are passed as `SPR_DYLD_*`, because SIP strips them when
  `/bin/bash` starts.

## Instrument control

`soapypower/instruments.py` talks SCPI over raw TCP (port 5025) with no extra
dependencies.

- `max_dbm`, `min_hz` and `max_hz` are mandatory and are checked before any RF is enabled.
- Sequence: `*CLS` on connect, RF OFF, frequency, level, `*OPC?`, read both back
  (level also against `max_dbm`), error queue, RF ON, `OUTP?` back, error queue.
- Any failure turns RF OFF. If that cannot be verified either, the error reports both
  causes.
- Only `on` and `off` are supported. An RF-off generator port is not a characterised
  termination or noise-source cold state, so `cold`, `hot` and `terminated` are
  refused; those need a dedicated hook.
- Command strings can be overridden per instrument (`commands` in the config).

`tools/scpi_source_hook.py` implements the `SourceController` hook protocol used by both
`soapy_power_experiment run` and `soapy_power_calibration auto`:

- The controller hashes the hook executable. The hook returns the SHA-256 of its JSON
  config, and the controller now keeps the hook's extra reply fields (`reply`) with
  each event.
- A wrong instrument identity closes the link without sending further commands.
- Run the hook with the interpreter that has soapy_power installed, via its shebang or a
  one-line `exec /path/to/python tools/scpi_source_hook.py` wrapper.

## Pass rules

| Check | Method | Required |
|---|---|---|
| `repeatability` | even/odd pass halves, robust σ of cell differences (includes RF environment) | σ ≤ 0.5 dB |
| `if_fit` | robust exact additive fit; component count | one component |
| `if_holdout` | independent refit on later acquisitions | RMS ≤ 0.25 dB, quiet-cell dispersion not inflated |
| `telemetry` | per-record temperature, chain, LO mapping; floor vs temperature within center | informational |
| `settling` | exponential fit, held-out delay verification | tolerance 0.1 dB |
| `image_rejection` | tone vs mirror per hop, floor subtracted | informational |
| `terminated_floor` | floor, flatness, steady and intermittent features (terminated input) | informational |
| `spurious_responses` | LO-harmonic mixing-law attribution and masking of excess observations | informational |
| context | fixed gain, no AGC, constant receiver context; no tune args with DC mask | input refused otherwise |

Not covered: absolute power, noise figure, compression/IIP3/SFDR, IF-fixed spur
attribution on real hardware (RF-fixed and LO-harmonic laws are attributed), measured
harmonic-response rejection, and large-retune settling. Strong broadcast
signals in 100–300 MHz (FM, DAB) can drive the receiver nonlinear; then `G` depends on
`S` and the additive model is violated. Use lower gain, or a terminated input for the
IF-response trials.

## Bias-free floor, FDR peaks and mixing-order classification (2026-11-17)

`soapypower/floor_estimation.py`, `soapypower/spur_twin.py`, `tools/benchmark_floor_spur.py`.
Relative levels only; nothing here is calibrated.

* **Floor.** A bin averaged over K segments is Gamma(K): its dB-median sits 10log10(median/mean) below the true
  mean level (-1.59 dB at dof 2, -0.29 dB at 10, -0.05 dB at 64) and a 2-sweep min-hold -3.0/-1.2/-0.45 dB.
  `floor_estimate` returns the mean level (median corrected for the dof, dof estimated from the data unless
  given, spur bins excluded) with a distribution-free CI.
* **Peaks.** `detect_peaks(method='bh'|'bonferroni'|'fixed')`: Benjamini-Hochberg or family-wise control instead of
  "N dB above floor". Cost: weak lines (4-8 dB at dof 8) are found less often; see the benchmark.
* **Mixing order.** `classify_lo_slope`: IF vs LO slope -n (RF-fixed n=1), +1 (image), odd n (LO harmonic), >= 3 LO
  settings. `drive_order`: level vs drive slope gives the intermodulation order. `spur_twin.analyze` is the
  pipeline; `scenarios()` is the T1-T5 matrix.
* **Floor vs temperature.** `floor_temperature_kalman` (random-walk offset + constant slope, RTS smoother, drift by
  marginal likelihood) is reported next to the OLS value as `floor_vs_temperature_kalman`. A temperature that only
  rises is indistinguishable from drift (`confounded: true`): then neither estimate is trustworthy.
* Reproduce: `PYTHONPATH=. python tools/benchmark_floor_spur.py`.

| measure (synthetic) | before (legacy) | after |
|---|---|---|
| median floor bias, dof 2 / 10 / 64 | -1.59 / -0.29 / -0.045 dB | +0.002 / +0.002 / 0.000 dB |
| 2-sweep min-hold bias, dof 2 / 10 / 64 | -3.01 / -1.22 / -0.45 dB | +0.002 / +0.003 / 0.000 dB |
| false alarms per noise bin (dof 8) | 2.6e-4 (fixed 6 dB) | 1.0e-5 (BH 0.01), ~0 (Bonferroni) |
| detection of +8 dB / +12 dB lines | 80 % / 97 % | 60 % / 96 % (BH) |
| T1-T5, 360 true peaks | 120 mislabelled (spur and image read as signal) | 0 mislabelled, 0 false, 0 missed |
| floor-vs-T slope error, oscillating T | 0.019-0.022 dB/C (OLS) | 0.010-0.015 dB/C (Kalman) |
| floor-vs-T slope, monotone T | 0.076-0.089 dB/C error, no warning | 0.061-0.068 dB/C error, flagged confounded 100 % |

Real data, B200 session okt-20261117-193257 (5741 temperatures, 46.3-47.2 C, antenna input, 1914 records used): within-center OLS
+0.024 dB/C; Kalman -0.018 dB/C, sd 0.057, plausible range -0.037..-0.016, not confounded (corr(T,t)=0.33). The two agree within
the Kalman uncertainty and neither excludes zero: the earlier "+0.024 +/- 0.031" was too tight. Treat the floor-vs-temperature
slope as unresolved for this session; a terminated-input run with a temperature swing is needed.
