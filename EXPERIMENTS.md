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
  does not raise the apparent RF dispersion beyond the response's own estimation noise.
  A flat receiver passes with no improvement. A changed receiver fails.
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
  one enumerates exactly one B200. It drops an inherited `PYTHONPATH`, since another
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
| `if_holdout` | independent refit on later acquisitions | RMS ≤ 0.25 dB, dispersion not inflated |
| `settling` | exponential fit, held-out delay verification | tolerance 0.1 dB |
| `image_rejection` | tone vs mirror per hop, floor subtracted | informational |
| context | fixed gain, no AGC, constant receiver context; no tune args with DC mask | input refused otherwise |

Not covered: absolute power, noise figure, compression/IIP3/SFDR, RF-fixed versus
IF-fixed spur attribution on real hardware, and large-retune settling. Strong broadcast
signals in 100–300 MHz (FM, DAB) can drive the receiver nonlinear; then `G` depends on
`S` and the additive model is violated. Use lower gain, or a terminated input for the
IF-response trials.
