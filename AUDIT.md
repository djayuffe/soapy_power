# SDR architecture, integrity and calibration audit — 2026-10-28

This audit extends the work based on `1683b77` and the calibration/integrity
review of `8395c2f` on `djayuffe/soapy_power`. See [RADIO_THEORY.md](RADIO_THEORY.md) for the full measurement
model, architecture matrix, calibration procedure and remaining physical limits.
Maintenance procedures are in [OPERATIONS.md](OPERATIONS.md) and repeatable
software checks in [DEVELOPMENT.md](DEVELOPMENT.md).

## Scope and implemented corrections

Reviewed signal representation, Welch scaling, window normalization, frequency
mapping, read framing, DC/LO contamination, driver corrections, gain control,
calibration, metadata and behavior across hardware architectures. Existing
continuous Welch, inversion, crop, timing, queue and binary fixes are retained.

| Finding | Change |
| --- | --- |
| DC was treated as a central one-bin artifact despite window leakage and shifted IF/DDC spurs. | Add explicit raw-baseband DC/spur exclusion, correctly mapped through IQ conjugation, external inversion, Nyquist wrap and crop. Excluded bins are NaN, not fabricated measurements. |
| Central interpolation could be selected with Hann despite leaving its DC sidelobes. | Restrict `--remove-dc` to boxcar with at least three bins; retain segment mean removal as a distinct option. Reject conflicting exclusion/interpolation settings. |
| Automatic frontend correction was inaccessible and driver/model assumptions could be inappropriate. | Add capability-checked `--dc-correction` and `--iq-correction`, both defaulting to preserving driver policy. Reapply explicit selections after tuning and check readback. |
| The wrapper did not reliably disable pre-existing AGC in fixed-gain mode. | Explicitly select supported gain mode, apply fixed gains afterward, reapply after retuning and reject unsupported requested AGC. Validate that the requested channel was not silently replaced. |
| Modern frequency correction could be ignored by the wrapper's legacy CORR-only implementation. | Use the supported modern correction API, with legacy CORR fallback; reject unsupported nonzero corrections. |
| Offset-tuned and multi-component receivers need explicit frequency arguments. | Add `--tune-args` forwarding to the standard driver API without guessing an architecture. Output still uses actual total tuning readback. |
| Absolute scaling was not implemented; nominal receiver gain is not a power calibration. | Add measured reference-plane calibration profiles, fixed-gain and actual-context validation, explicit units and profile identity/provenance. No guessed coefficients or automatic gain-to-dBm conversion. |
| RF-only correction misses passband shape and RF-dependent baseband response. | Require a measured RF-by-raw-baseband residual grid normalized at a reference offset. Interpolate in dB, including RF/baseband interaction, with no extrapolation on either axis. |
| Conjugation/inversion can apply a response correction at the wrong offset. | Associate each output bin with its original raw-IQ baseband coordinate before calibration. Test all sign/conjugation combinations. |
| A single context check before tuning can reject a valid per-LO frontend configuration or miss later changes. | Validate static context before stream setup and complete context after retuning and explicit corrections. Context includes actual gain elements, rates, bandwidth, antenna, clock sources, hardware info/serial, frontend modes, correction, settings and conversion choices. |
| Legacy output cannot carry all units and acquisition context. | Add standard JSON-per-hop output with actual context, DSP parameters, ENBW, segment count, calibration provenance and explicit missing/zero bins. Correct unit labels and include metadata comments in FFTW text output. |
| Finite hardware bursts could be concatenated without a timestamp flag. | Honor END_BURST, finish the current valid partial measurement and stop the sweep; do not concatenate another burst. Existing overflow/abrupt-end/gap checks remain. |
| Library FFTW calls inherited a CPU-count global thread default despite bounded PSD workers. | Pass FFT thread count per PSD instance, default one, including the library API. Do not mutate the dependency's global thread setting. |
| Single FFT mode normalized against unrounded rather than actually used window coefficients. | Compute energy/ENBW from the actual single-precision coefficients. |
| Expected calibration/device failures produced full tracebacks in normal CLI use. | Give concise measurement errors; preserve tracebacks with `--debug`. |

## Implementation and maintenance completion

- Recheck calibration context after acquisition, so a detected in-read gain or
  mode change cannot publish calibrated power. Excluded bins no longer require
  response-grid coverage; every retained measured bin still does.
- Scale FFT components before power accumulation and retain a compensated,
  segment-weighted running mean. Avoid overflowing intermediate sums where the
  tested final density is representable. Invalid/out-of-range IQ still fails.
- Retry partial binary writes and reject nonprogress or invalid payload limits.
- Reject duplicate JSON fields and nonfinite/overflowing JSON numbers. Return
  detached calibration snapshots so callers cannot mutate a validated identity.
- Add offline context extraction, measured-grid profile construction/validation
  and stable linear band integration with explicit missing-band treatment.
  Profiles include the measurement CSV hash; repeats need an explicit spread
  acceptance limit and average normalized linear responses.
- Publish context/profile artifacts atomically without overwriting by default;
  explicit force uses replacement. No physical coefficients are fabricated.
- Add a subprocess-per-backend validation command, a Linux/macOS/Windows CI
  matrix, an isolated build backend, an installed offline entry point and
  practical receiver/calibration/revalidation/release procedures.
- Fix isolated build version loading and README rendering found by actual
  sdist/wheel validation.

## Mathematical contract

The estimator remains a two-sided density on the delivered IQ sample rate:

`S[k] = sum_j |FFT(w * detrend(x_j))[k]|^2 / (K * fs * sum(w^2))`.

Linear averaging precedes log conversion. Complete segments are framed across
uninterrupted reads, never across retunes or bursts. Calibration and DC exclusion
are output operations and do not silently modify unrelated FFT bins. Default
DC/IQ correction policy remains the driver's policy; no blind image estimator
or universal DC notch is applied.

Calibration is based on a measured C0(F) at reference raw-baseband offset b_ref,
plus a measured residual response grid deltaC(F,b). Every RF row is normalized
so deltaC(F,b_ref)=0. A bin's corrected density is:

`dBm/Hz = 10*log10(S_raw) + C0(F) + deltaC(F,b)`.

Linear calibrated output is mW/Hz. RF interpolation and the RF/baseband residual
interpolation are in dB, with RF/baseband extrapolation rejected. Actual context
must match. The profile declares a reference plane, reference source/provenance
and optional stated uncertainty. It does not certify linearity or propagate all
uncertainties into a confidence interval.

`--remove-dc` remains an explicit estimate. DC exclusion preserves the fact that
power in an excluded region is unknown. JSON uses null for a nonfinite value,
with separate `masked_bins` and `zero_power_bins` to distinguish missing data
from a zero-density logarithm. Legacy CSV/binary retain their layouts and need
external settings/profile records to identify units and provenance.

## Verification — 2026-10-28 update

Current baseline is `8395c2f`; this update incorporates the attached report
review and the practical calibration workflow work. See
[REMEDIATION.md](REMEDIATION.md) for per-finding implementation and remaining
limits, and [CALIBRATION.md](CALIBRATION.md) for physical reference procedures.

Run `python tools/validate.py`. The suite now contains **131 tests**, including
independent 70-digit direct-DFT and 80-digit aggregation references, fault
injection and synthetic calibration manifests.

| Environment | Result |
| --- | --- |
| Python 3.12.10 / NumPy 2.5.4 / SciPy 1.18.1 / pyFFTW 0.15.1 | FFTW: 131 pass. NumPy and SciPy: 130 pass, one FFTW-only test skipped per run. |
| Python 3.9.6 / NumPy 1.26.2 / no optional FFT backends | NumPy: 129 pass, two optional-reference/backend tests skipped. |

A separate seeded test of 100,000 fractional integration bands against exact
rational float-input widths passed (flat linear-density case, maximum observed
error 0 ULP). This does not prove an 8-ULP bound for every spectrum or log input.
The existing stochastic/window/backend tests remain; real SimpleSpectral and
installed backends are exercised, not an FFT compatibility stub.

Source archive/wheel builds, archive contents, `twine check`, installed CLI help
and offline workflows are checked. The configured remote CI matrix is separate
from locally observed results. No physical receiver/reference was exercised.

## bladeRF support — version 1.7.1

Added normalized manual RX DC/IQ controls, readback/quantization checks,
correction identity in context, per-retune reapplication and capability caching
to avoid repeated AGC support probes during context readback. The measured
power-calibration workflow remains serial/channel/gain specific. See
[BLADERF.md](BLADERF.md) and its measured-tone manifest template.

The suite contains 136 tests: Python 3.12 checks NumPy, SciPy and FFTW separately
(the FFTW-only test skips on the other two backends). Python 3.9 checks the
NumPy-only environment with two optional tests skipped. New bladeRF tests use
simulated driver controls and a real DSP/writer acquisition pipeline. No physical
bladeRF, native gain-table import or automated IQ coefficient search is claimed.

## Limits of this audit

- No SDR was physically exercised; the SoapySDR binding and an intended radio
  are not available here. Actual analog behavior, driver settling, live timing,
  sustained throughput and metrology still require hardware/reference tests.
- Synthetic calibration coefficients test equations and safeguards. No physical
  calibration coefficients are supplied or inferred for the user's radio.
- ADC normalization, overload, clock error, anti-alias filtering, Nyquist-zone
  selection and RF-path effects cannot be inferred universally from CF32 data.
  The analyzer assumes a meaningful delivered stream and driver tuning mapping.
- Hardware/firmware, temperature, external chain state and a driver's calibration
  tables can change without a useful readback. Context matching is necessary,
  not sufficient, to establish continuing validity. A missing serial cannot
  identify an individual unit. Profile provenance and physical revalidation matter.
- A finite response grid approximates a measured response; it is not arbitrary
  deconvolution. It cannot recover clipping, aliases, a lost band, deep-notch
  information or noise-dominated signals. Calibration does not remove receiver
  noise or establish noise figure, dBFS or ADC full-scale voltage.
- DC algorithms can affect wanted near-DC signals. Missing bins cannot be
  summed as though they were measured. Reference measurements must cover the
  retained RF/baseband domain, including the actual tune/crop choices.
- Overlap correlates periodograms; no universal independent-average count,
  logarithmic bias correction or uncertainty interval is manufactured.
- Only one RX channel's power is analyzed; no phase-coherent cross-channel or
  multi-radio synchronization claim is made.
- Deadline checks cannot interrupt an in-progress driver call or queued work.
  Host UTC bounds are not hardware RF timestamps. Untimestamped unreported
  sample loss remains undetectable.
- Binary v2 remains float32 and lacks units/context fields. JSON/text preserve
  float64 powers. The opt-in v3 binary format preserves float64 and metadata
  with checksums. Explicit exclusion introduces NaN into legacy arrays; clients
  using it must support missing values.
- Queue/FFT batches are bounded, but backend workspaces and the dependency's
  FFTW interface plan cache affect total RSS. No universal speed optimum is
  asserted. Linux, Windows, unlisted Python/dependency versions and actual device
  drivers remain outside the exercised environments.

Primary sources and architecture reasoning are linked in
[RADIO_THEORY.md](RADIO_THEORY.md). The existing hardware-free mathematical
contracts are verified; physical calibration accuracy remains empirical.

## Version 1.8.0: USRP controls and IQ noise audit

Added capability-checked clock/time/master-clock controls, motherboard reference
lock gating distinct from RX LO lock, USRP serial/frontend mapping identity, and
explicit manual-correction readback tolerances. Added independent offline IQ
noise diagnostics with linear PSD averaging, bounded frame work, overlap-aware
amplitude statistics, DC/variation warnings and protected JSON output.
See [ETTUS.md](ETTUS.md) and [NOISE_CALIBRATION_AUDIT.md](NOISE_CALIBRATION_AUDIT.md)
for device-specific limits, equations and colored Mermaid workflow.
The suite contains 148 tests; no physical USRP validation is claimed.

## Version 1.9.0: receiver/cascade noise characterization

Reviewed the multi-temperature/LNA report and corrected its stale claim that
fractional integration and hot/cold calibration were missing. Added a separate
measured-cell receiver characterization workflow, centered/scaled SVD fits,
conditional covariance, scalar Friis extraction, CW/contrast/residual gates and
optional correlated-input Monte Carlo. Native stream format/full-scale and
hardware-time capability are now included in context when exposed.
See [RECEIVER_CHARACTERIZATION.md](RECEIVER_CHARACTERIZATION.md) for equations,
input schema, colored workflow, conditional uncertainty and physical limits.

Validation: 161 tests across NumPy, SciPy and FFTW on Python 3.12, plus the
Python 3.9 NumPy fallback. Backend-specific checks skip when unavailable.
Wheel/sdist build, metadata validation and installed offline capture-to-report
smoke checks pass. Legacy native-format binding failure is explicitly unknown.
No physical receiver/LNA calibration or vector mismatch validation is claimed.
