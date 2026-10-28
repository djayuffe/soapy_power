# Report review and implemented measurement contracts — 2026-10-28

Reviewed the user-supplied *SoapyPower SDR/FFT Remediation and Verification
Report* against commit `8395c2f`. Its recommendations are review input, not
instructions to change correct mathematics or a claim of hardware verification.
The Welch normalization is retained. Detrending, whole-bin integration and
float64 RF limits are treated as explicit transformations/contracts rather than
mislabelled arithmetic errors.

## Findings and disposition

| Report ID | Implementation and practical limit |
| --- | --- |
| DSP-001 | Documented mean-plus-residual invariant; TwoSum addition residual, transported by old segment weight. Decimal aggregation reference with adversarial ordering. Products, FFTs and division still round; reduction order is not bitwise deterministic. |
| DSP-002 | Commit tail and input mode only after successful submission. Fault injection proves failed submit preserves the tail and can be retried. |
| DSP-003 | Live path requires complex IQ. Integer API input is rejected. Explicit native interleaved conversion needs supplied offset/full-scale. Legacy real floating input warns; explicit `input_format='real'` is available. |
| DSP-004 | Conservative application-workspace preflight before window allocation and batch sizing per worker. This is not a hard RSS cap: queued input, concurrent states, backend workspaces/caches, allocator overhead and driver buffers are separate. |
| DSP-005 | Calibrated processing checks detrending against profile; v1 profiles imply no detrending. Capture workflows reject detrending. |
| DSP-006 | Estimated DC indices carried in metadata/JSON/v3. Integration refuses estimates unless explicitly included. Calibrated interpolation requires explicit opt-in. |
| DSP-007 | Cache ENBW after coherent-gain conditioning and finite checks. |
| DSP-008 | Integer FFT-bin indices and separate RF origin/step retained in metadata. Distinct float64 output frequencies remain mandatory; unrepresentable grids fail before processing. |
| RF-001 | Distinguish terminal flag, remaining terminal-packet samples and whether the terminal packet was consumed. Whole-burst processing is unknown when burst start/history is unknown; never infer it from END_BURST alone. |
| RF-002 | Actual/requested/target counts, completion fraction and partial reason recorded. Calibrated default rejects partial. Raw metadata-capable outputs may emit explicitly labelled partial. Legacy output requires explicit legacy policy for partial emission. |
| RF-003 | Packet/anchor counts, maximum timing error and unknown/inferred/anchored/verified confidence. Gaps fail. Optional verified-timestamp requirement accommodates timestamp-capable hardware without excluding RTL-SDR by default. |
| RF-004 | Time and discarded-sample settling, optional named RX lock sensor and timeout. A configured policy is not empirical proof of analog settling. |
| RF-005 | Check actual tuning readback for uncovered gaps and end coverage under the existing scan-grid convention. Fail rather than claim planned coverage. No automatic hardware-specific retune adaptation. |
| RF-006 | Exact `bins*repeats` target by default, independent of MTU. Explicit block-rounding compatibility mode retained. Unused packet samples are counted. |
| RF-007 | Metadata adds completion, count, burst, timing, settling and continuity state. JSON v2 and binary v3 carry it; legacy formats cannot carry the full contract. |
| RF-008 | Host UTC read bounds remain distinct from integer hardware clock timestamps; split-packet offsets preserved before rounding at output. Hardware time is not labelled UTC. |
| CON-001 | Live DSP/writer submit is nonblocking with explicit bounded-queue overload failure. The bounded chunk queue absorbs limited jitter; no dedicated acquisition-ring/thread has been introduced. |
| CON-002 | Cooperative cancellation checks between FFT batches on failed acquisition cleanup. One running FFT/driver call remains non-preemptible; no hard wall-time promise. |
| OUT-001 | JSON v2 explicit bin-center/support geometry; fractional support integration with actual intersected masked/estimated bandwidth. It assumes piecewise-constant density within a bin. |
| OUT-002 | Boundary selection in relative rational index space rather than repeated absolute floating-bin construction. Version-1 center selection remains explicitly available. |

The report's proposed universal throughput margin, full RSS bound, exact
reproducibility and physical-metrology gates have **not** all been established.
Passing local software tests does not justify claiming that every proposed gate
or every device is certified. Further physical/environmental validation remains
necessary. The dedicated-ring architecture is an optional future throughput
improvement; current overload behavior is explicit failure, not silent loss.

## Compatibility and output changes

- JSON records now use version **2**. Legacy `start_hz` is still the first bin
  center and `stop_hz = start_hz + count*step_hz`. Explicit
  `first_bin_center_hz`, `bin_width_hz`, `coverage_start_hz` and
  `coverage_stop_hz` remove ambiguity. Versions 1 and 2 are readable offline.
- Integration defaults to fractional bin support for v2 and legacy center
  selection for v1. `--method fractional|legacy-centers` chooses explicitly.
  `--include-estimated` acknowledges estimated DC power; `--omit-masked` reports
  incomplete measured bandwidth. Neither option reconstructs missing RF data.
- Acquisition target is exact unless `--acquisition-rounding block` is used.
  `--partial-policy reject|emit|legacy` is explicit. Default is reject with
  calibration and emit for raw metadata-capable output; a partial legacy CSV/v2
  binary record is rejected unless legacy policy is explicitly selected.
- `--workspace-budget BYTES` limits the application FFT workspace per worker.
  Live queue capacity defaults to 32 pending chunks plus workers, is adjustable
  with `--max-queue-size`, and cannot be unbounded. Queue-full is a measurement
  failure. Small buffers/high rates may need a measured queue-size tradeoff.
- `--settle-samples`, `--lock-sensor`, `--lock-timeout`,
  `--timestamp-tolerance-samples` and `--require-timestamps` select explicit
  settling/timing policy. Default timing tolerance includes a 2 ns floor.
- All advertised readable device settings are snapshotted. Old calibration
  contexts missing newly exposed settings can fail matching; recapture and
  rebuild instead of editing fields to bypass the check.

### Binary v3

Select `-F soapy_power_bin_v3` for float64 powers and full JSON-v2 record metadata.
The existing `soapy_power_bin` format is unchanged v2/float32. Reader API:
`SoapyPowerBinV3Format().read(stream)` returns a validated record or None at clean
EOF. It rejects truncated, corrupt, oversized or malformed records.

Wire layout, little endian:

1. Seven-byte magic `SDRFF3\0`.
2. Two uint64 lengths: metadata bytes and payload bytes.
3. 32-byte SHA-256 of packed lengths + metadata + payload.
4. Strict UTF-8 JSON record header, excluding `power`, plus
   `binary_version: 3` and `power_dtype: "float64-le"`.
5. Contiguous little-endian float64 power values. JSON masks/zero indices retain
   the distinction between NaN exclusion and negative-infinite log zero.

Default reader/writer bounds are 16 MiB metadata and 256 MiB payload. SHA-256
here detects corruption, not authenticity. It does not make the entire file
atomic: earlier valid records can precede a truncated final record. Close and
check acquisition success before publishing measurement files.

## Calibration and uncertainty scope

[CALIBRATION.md](CALIBRATION.md) adds the progression from terminated/antenna
baselines to measured tone, hot/cold and independently known-NF grids. A 75 ohm
termination alone never creates an absolute profile. Noise methods explicitly
require confirmation of source/receiver assumptions. Captures must be complete,
fixed-gain, coordinate-checked and settings-matched; manifests scale across RF
and baseband with exact input hashes.

Version-2 calibration profiles preserve the v1 response representation and add
an explicit standard dB component budget with declared independence/correlation.
Missing terms remain unknown; live measurement statistics, empirical
interpolation residuals, source impedance noise waves and environmental validity
are not automatically estimated. The version-2 schema deliberately does not
pretend the report's proposed uncertainty/environment programme has already
been physically performed. A full propagated live-spectrum confidence interval
is outside the implemented scalar calibration-budget model.

## Verification

Run `python tools/validate.py`. New tests include a 70-decimal-digit direct DFT
for small odd/even transforms, an 80-digit weighted-mean reference, rejected
submission/overload, native IQ conversion, exact target/terminal tails,
settling/lock failures, timing confidence/gaps, actual coverage gaps, fractional
edge/ULP cases, estimates/masks, float64 v3 corruption/short writes, covariance
validation and synthetic calibration workflows. Existing backend, Parseval,
window, framing and hardware-boundary tests remain.

Physical RTL-SDR or other SDR hardware, reference-source accuracy, live lock
behavior, temperature drift, RF input match and sustained stream throughput
were not measured here. No real calibration coefficients are supplied.

Primary API sources checked during review:
[SoapySDR flags](https://pothosware.github.io/SoapySDR/doxygen/latest/Constants_8h.html),
[NumPy FFT](https://numpy.org/doc/stable/reference/routines.fft.html),
[pyFFTW interfaces](https://pyfftw.readthedocs.io/en/latest/source/pyfftw/interfaces/interfaces.html),
[BIPM uncertainty guidance](https://www.bipm.org/en/committees/jc/jcgm/publications).
The pyFFTW implementation already has a global planner/destructor lock; a second
application lock serializing every FFT is not added
([implementation](https://github.com/pyFFTW/pyFFTW/blob/master/pyfftw/pyfftw.pyx)).
