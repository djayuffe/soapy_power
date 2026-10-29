# Implementation and execution audit — v1.12.1

This audit reviews the shipped SDR/FFT, calibration, characterization,
equalization, response fitting, passive diagnostics, record/binary I/O,
threading and offline CLI implementations. It fixes concrete integration and
execution errors and adds regression coverage. Passing software checks is not
a claim that hardware, physical source models or every possible execution is
flawless.

## Findings fixed

| Finding | Correction | Regression evidence |
| --- | --- | --- |
| Raw metadata-backed captures could miss a fixed-gain/rate/tuning change during a hop, then be reused as apparently fixed-state reference data. | Verify reported sample rate and tuning for every hop; verify full fixed-state contexts after raw as well as calibrated acquisition. Preserve intentionally requested AGC display mode. | Inject gain, sample-rate and tuning changes during raw acquisition and verify no hop is published; requested AGC remains usable. |
| Complete measured-reference workflows did not uniformly require known gain-register readbacks. | Share the known fixed-reference context validator across power profiles, complete reference captures and passive workflows. Raw baseline diagnostics can still describe an unknown-gain receiver without certifying a reference. | Reject complete reference capture with missing gain readback, retain its diagnostic baseline path. |
| Async PSD entry points could borrow a mutable complex input buffer. A caller could overwrite queued data before computation. | Snapshot queued IQ in both precisions and independent/contiguous modes. Dtype conversions already owning data avoid a redundant copy. Live acquisition uses that ownership contract. | Block worker, submit IQ, overwrite producer buffer, then verify original power. |
| Async writers borrowed direct spectrum arrays and metadata dictionaries. | Snapshot immediate arrays and non-callable metadata before queue submission. PSD futures and metadata callables remain deferred intentionally. | Queue behind a blocker, mutate all producer data, verify original serialized record. |
| Some older calibration commands could overwrite manifests, measurements or captures using --force. | One evidence inventory covers every file-producing calibration subcommand, including nested characterization noise/CW captures. Source evidence is protected even with --force. | Context, CSV build, manifest builder and nested characterization overwrite attempts leave source bytes unchanged. |
| Input files could change while captures were being averaged or reports were being serialized/flushed. | Capture reads verify source identity/version. CLI campaigns retain source snapshots and recheck after output flush, immediately before publication. Equalization uses the same final publication check. | Append during capture read, mutate after analysis, mutate during fsync, replace files and retarget symlinks; reject without replacing destination. |
| A malformed power-profile processing contract could pass loading and fail only during later acquisition. | Validate processing contracts in the profile constructor as well as at use. | Reject unknown modes/keys/containers at load; retain valid constant-detrend profiles. |
| Nested context lists did not use the same type and numerical comparison rules as dictionaries. | Recursively compare sequences, preserve list/tuple distinction and reject bool-versus-number confusion. | Nested coefficient tolerance/type/length cases. |
| Invalid numerical containers could escape as TypeError instead of validation errors. | Normalize array-conversion failures across profile, smoothing, relative-response, equalization and joint-uncertainty inputs. | Malformed numerical dictionaries are rejected as ValueError. |
| Relative-fit covariance calculations could overflow for large finite standard errors even when standard deviations were representable. | Normalize SVD precision weights and propagate parameter standard deviations through row norms without squaring physical error scales. | Complete overlap graph with 1e200 dB standard errors produces the independently known finite conditional standard deviation. |
| A checksum-valid v3 payload could conflate NaN missing data with logarithmic zero, or disagree with the record passed to its writer. | Check payload shape, values and NaN/-infinity classifications against validated records before writing and after reading. | Valid-checksum NaN-as-zero rejection; mismatched write publishes no bytes; valid zero roundtrip retained. |

## Reviewed paths retained

- Welch/periodic-window normalization, linear segment weighting, continuous
  overlap framing, IQ orientation, DC/spur masks, RF/baseband mapping and
  fractional-bin integration retain their existing mathematical contracts.
- Stream overflow, timestamp discontinuity, end-burst, queue-pressure,
  cancellation and partial-record handling retain their explicit failure/status
  behavior. Tests use simulated device boundaries and real FFT implementations.
- Fixed-gain context, clock/reference gates, manual correction readbacks,
  measured power grids, hot/cold and known-NF workflows remain distinct.
- Multi-temperature OLS/WLS, scalar receiver/LNA separation, CW checks and
  conditional Monte Carlo continue to report physical/model failures rather
  than silently clipping negative noise temperatures or deleting observations.
- Attenuation-only normalization remains global across file hops. Explicit
  usable-band masks, constrained response smoothing and independent holdout
  validation retain their gates and incomplete-uncertainty status where needed.
- Passive overlap fitting still checks graph identifiability, repeated
  stability, channel width, source SNR, known gain readbacks and separate
  validation. Its profiles cannot become absolute dBm/NF calibrations.
- Selected-tone drift and active/quiet interval reports remain diagnostics,
  with no protocol identity, automatic PPM setter or inferred noise figure.
- Standard JSON remains strict, no-clobber publication remains atomic, and
  binary size/checksum validation remains enabled.

## File and concurrency contracts

Async callers may reuse immediate sample/spectrum/metadata buffers after the
submission call returns. For writer futures and metadata callables, the caller
must supply stable values when they resolve; deferred evaluation is required
for live PSD completion metadata. No snapshot can make a buffer safe if another
thread is concurrently writing it during the snapshot itself.

Evidence snapshots compare file identity, size, modification and change times,
following the original source path so symlink retargets are detected. SHA-256
values remain provenance identifiers. These checks detect ordinary concurrent
writes/replacements; they are not an immutable filesystem transaction or a
security guarantee against forged filesystem metadata. Publish capture files
before analyzing them and retain source artifacts. Any final input version
change detected before publication aborts and preserves an existing output.

The shared protection applies to CLI-produced artifacts. Library users building
larger multi-file transactions must preserve their own evidence snapshots;
individual capture reads protect themselves. No hardware-setting operation,
radio transmission or automatic physical recalibration was added by this audit.

## Verification

The expanded suite contains 219 tests, executed with NumPy, SciPy and FFTW on
Python 3.12. Backend-specific availability checks intentionally skip where the
selected worker disables that backend. The Python 3.9/NumPy fallback also passes
with optional backend checks skipped.

The 10000-case seeded fractional-integration check passes against exact rational
float-input intervals (maximum observed error 0 ULP in this environment).
Compile, wheel/sdist build, package metadata and clean installed-wheel offline
workflow checks are part of release validation. Tests and the audit documentation
are included in the source distribution.

No physical RTL-SDR, bladeRF, USRP, LNA, calibrated source or public-band campaign
was measured during this audit. ADC/analog linearity, RF losses/mismatch,
temperature drift, source correctness, external timing and a complete metrology
uncertainty budget still require actual measurements. These limits remain
explicit rather than being hidden behind a passing software test count.
