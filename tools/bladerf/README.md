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
| `soapybladerf-0.4.1-lms-state.patch` | SoapyBladeRF: reports the LMS DC calibration state at open |

## Stack (as measured)

- bladeRF x40 (serial 43697856…5403), firmware 2.6.0, FPGA hostedx40 0.16.0. FPGA 0.16
  needs firmware ≥ 2.6. With firmware 2.4 the stream lost samples (timestamp gaps).
- libbladeRF 2.6.1 built from Nuand source, SoapyBladeRF 0.4.1 with this patch, SoapySDR 0.8.
- 28 MS/s, 28 MHz bandwidth, stream arguments `buffers=128,buflen=65536,transfers=16`,
  `xb200=custom` (RXFILT ↔ RXFILT-ANT jumpered).

```sh
git clone https://github.com/pothosware/SoapyBladeRF && cd SoapyBladeRF
git checkout soapy-bladerf-0.4.1 && git apply -p1 /path/to/soapy_power/tools/bladerf/soapybladerf-0.4.1-lms-state.patch
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
`~/Documents`):

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

## Why dense DC

The 2026-11-16 15:24 UTC survey (v2 profile, 19 validated anchors) improved the residual DC by
10–15 dB below 1.2 GHz, compared with the 14:39 UTC survey. Above 1.2 GHz it made it
5–8 dB *worse*: at 2–3.8 GHz the median |mean| was 0.050–0.057 against 0.018–0.027.
At the 32 centers where both runs used the same DC value, the residual did not change
(< 1 dB across 45 minutes), so the regression comes from the anchor values, not drift. An
anchor is valid at its own frequency only. The origin bin is excluded from fused spectra
(±250 kHz), so this does not bias the survey spectrum. It does take ADC headroom and corrupt
IQ captures near the LO.

## Not covered

Absolute power, noise figure, I/Q image correction (needs a known reference), compression and
intermodulation. Quick retune (`bladerf_schedule_retune` with quick_tune), which libbladeRF
recommends with the XB-200, would cut the ~78 ms per hop. SoapyBladeRF does not expose it.
