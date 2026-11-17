# bladeRF1 (x40/x115) + XB-200 survey tools

Receive-only tools for soapy_power on an original bladeRF (LMS6002D) with the XB-200
transverter, 42–3800 MHz. They run the 2026-11-16 acquisition policy, add per-center DC
calibration, and compare survey runs. Results are relative and uncalibrated (no dBm, no noise
figure).

| File | Purpose |
|---|---|
| `bladerf_profile.py` | Gain and DC selection, dense DC table, 2×2 Jacobian DC solve (pure, unit-tested) |
| `bladerf_hw.py` | `Receiver`: verified hop, stage-gain, DC and LMS-state readback, live headroom guard |
| `bladerf_dense_dc.py` | Per-center DC calibration at survey gains, with independent verification |
| `bladerf_scan.py` | Timed survey (anchor DC, or `--dc-table` dense DC) plus IQ captures |
| `bladerf_compare.py` | Per-center comparison of two surveys: throughput, headroom, DC residual, floor |
| `../../patches/soapybladerf-lms-state.patch` | SoapyBladeRF 0.4.1: reports the LMS DC calibration state at open |

## Stack (as measured)

- bladeRF x40 (serial 43697856…5403), firmware 2.6.0, FPGA hostedx40 0.16.0. FPGA 0.16
  needs firmware ≥ 2.6. With firmware 2.4 the stream lost samples (timestamp gaps).
- libbladeRF 2.6.1 built from Nuand source, SoapyBladeRF 0.4.1 with this patch, SoapySDR 0.8.
- 28 MS/s, 28 MHz bandwidth, stream arguments `buffers=128,buflen=65536,transfers=16`,
  `xb200=custom` (RXFILT ↔ RXFILT-ANT jumpered).

```sh
git clone https://github.com/pothosware/SoapyBladeRF && cd SoapyBladeRF
git checkout soapy-bladerf-0.4.1 && git apply -p1 /path/to/soapy_power/patches/soapybladerf-lms-state.patch
cmake -B build -DLIBBLADERF_INCLUDE_DIR=… -DLIBBLADERF_LIBRARY=…/libbladeRF.2.dylib && cmake --build build
```

The patch adds `lms_dc_state_at_open` (lpf_tuning, tx/rx LPF I/Q, dc_ref, rxvga2a/b I/Q) to
`getHardwareInfo()`. Manual DC values are only valid for the LMS state they were measured
in. The tools fail closed when the state differs. The LMS calibration
(`bladerf_calibrate_dc` for lpf_tuning, rx_lpf and rxvga2) is volatile, so a power cycle
returns all ten values to 31 and the profile no longer applies.

## Gain and DC

- Gains are explicit stages: LNA 0/3/6 dB, RXVGA1 5–30 dB, RXVGA2 0–30 dB in 3 dB steps. The
  driver's nominal total is `lna + rxvga1 + rxvga2 − 6`. Per center the survey uses the lowest
  gain that passed headroom on the full grid. A live guard lowers RXVGA2, then RXVGA1, then the
  LNA when a hop's samples exceed |I|,|Q| ≥ 0.85, p99.9 ≥ 0.60 or any sample ≥ 0.95.
- Manual DC: sign plus 6 bits per channel, readback quantum 1/64. Below 237.5 MHz the XB-200
  mix path swaps I and Q, so the I control moves Q. The effect of the control scales with
  RXVGA2 gain: on Q it is about −0.02, −0.20, −0.41 and −0.79 normalized units per unit
  control at RXVGA2 0, 12, 18 and 24 dB. It is cross-coupled and varies within tens of MHz.

## Procedures (macOS runner)

Copy the stack and profile into the git-ignored `sessions/` (launchd jobs should not read
`~/Documents`). The module links `libSoapySDR.0.8`: after a Homebrew upgrade to SoapySDR 0.9
the runner finds the superseded `Cellar/soapysdr/0.8*` keg and loads that library and its
Python bindings (it stays usable until `brew cleanup` removes it):

```
sessions/bladerf-stack/modules/libbladeRFSupport.so   sessions/bladerf-stack/lib/libbladeRF.2.dylib
sessions/bladerf-profile/{receiver-profile-final.json, validated-full-grid-profile.json, schedule-selection.json}
```

```sh
tools/macos_runner/queue.sh probe --device bladerf --wait 60          # enumerate, FPGA/FW, LMS state vs profile
tools/macos_runner/queue.sh scan --device bladerf --seconds 300 --wait 900                       # dense DC + survey
tools/macos_runner/queue.sh scan --device bladerf --bladerf-job survey-anchor --seconds 300       # control: anchor DC
python3 tools/bladerf/bladerf_compare.py reference/scan.jsonl.gz candidate/scan.jsonl.gz -O comparison.json
```

`bladerf_dense_dc.py` measures each center in the survey plan (1074 centers) at the stage
gains the survey will use:

1. A base capture at the anchor DC. The headroom guard lowers gain exactly as the survey would.
2. One capture with the I control stepped and one with the Q control stepped. The step is
   4 codes at RXVGA2 ≥ 18 dB, otherwise 8.
3. A 2×2 Jacobian from the sample means and a search on the 1/64 lattice.
4. An independent verification capture. The new value is kept only if |mean| falls to ≤ 0.7×
   (or ≤ 0.002) and headroom still passes.

`bladerf_scan.py --dc-table` uses a table value only where the stage gains match. Otherwise
it uses the nearest-anchor rule: same gains, same XB domain, a validated anchor within
150 MHz. When nothing matches, DC is zero.

## Why dense DC, and what it achieved (2026-11-16, same antenna and LMS state)

The 15:24 UTC survey (v2 profile, 19 validated anchors) improved residual DC by 10–15 dB
below 1.2 GHz compared with the 14:39 UTC survey, but made it 5–8 dB *worse* above 1.2 GHz.
At the 32 centers where both runs used the same DC value, the residual stayed within 1 dB
over 45 minutes. So the anchor values caused the regression, not drift: an anchor is
right only at its own frequency.

| Run (UTC) | DC values | Hops in 300 s | Residual \|mean\| per center, median / p90 | Origin-bin spike, median per hop |
|---|---|---:|---:|---:|
| v2 15:24 | 19 anchors | 3852 | 0.0280 / 0.108 | 32.7 dB |
| v3 16:20 | dense, one verified step | 3733 | 0.0027 / 0.0120 | 9.1 dB |
| v4 16:34 | dense, second Newton step | 3748 | 0.0027 / 0.0095 | 8.9 dB |
| v5 16:42 | v4 table + live calibration after gain backoff | 3689 | 0.0026 / 0.0085 | 8.8 dB |

- The calibration took 330–345 s. 1014 of 1074 centers were accepted. The local Jacobian is well
  conditioned (median condition number 1.1, max 1.8).
- Median change from v2 to v5 per band: 42–237 MHz −9 dB, 237–600 MHz −27 dB,
  0.6–1.2 GHz −5 dB, 1.2–2 GHz −23 dB, 2–2.8 GHz −23 dB, 2.8–3.8 GHz −20 dB.
  976 centers improved by more than 3 dB and 14 got worse.
- Above 2 GHz the result is at the DAC limit. 89–92 % of centers lie within half a code on
  both controls, because RXVGA2 at 48–54 dB amplifies one 1/64 step to about 0.016.
- At 700–960 MHz, strong GSM/LTE signals push the guard to lower gains. Some centers there
  do not verify: the residual returns to 0.004–0.02 whatever the control setting. This
  includes the 910–917 MHz IQ captures, which stay at 0.004. The cause is probably
  signal-dependent DC (second-order intermodulation), which no static value can cancel.
  This is not verified.
- No side effects: the gain-referenced floor (20th percentile ±7 MHz) changed by
  −0.05 dB median, maximum |I|,|Q| ≤ 0.848 in every run, and there were no settle
  retries. Throughput was 12.3–12.5 against 12.8 hops/s (−3 to −4 %). The v5 figure
  includes 15 live calibrations.
- The 13 IQ captures off the grid now calibrate too. At 1.8, 2.44 and 3.5 GHz the residual
  went from 0.020/0.031/0.020 to 0.0007/0.0022/0.0049.

The origin bin (±250 kHz) is excluded from fused spectra either way. The gain is ADC
headroom, clean single-hop and IQ data near the LO, and a correct record of which
correction was applied (`bladerf_dc_source`: dense, dense-live, anchor or none).

## Not covered

Absolute power, noise figure, I/Q image correction (needs a known reference), compression and
intermodulation. Quick retune (`bladerf_schedule_retune` with quick_tune), which libbladeRF
recommends with the XB-200, would cut the ~78 ms per hop. SoapyBladeRF does not expose it.
