# soapy_power audit — 2026-10-22

Upstream: `https://github.com/xmikos/soapy_power`, branch `master`, commit
`46e1265`. `git pull --ff-only` reported already up to date. The checkout was
clean before this audit. Changes are local and uncommitted.

## Coverage and repairs

Reviewed every Python module, package metadata, README, manifest and PKGBUILD.
The Arch recipe and license did not require changes. This is a source and
simulated-device audit; it cannot certify all possible hardware or drivers.

| Area | Finding and repair |
| --- | --- |
| Welch windows | SimpleSpectral 1.0.0 makes symmetric windows for odd FFT lengths. Generate DFT-periodic windows for every length. |
| Tukey window | Dependency reverses alpha endpoints and calls nonexistent `numpy.hann` at alpha=0. Implement the correct endpoints and taper locally. |
| Numerical averaging | Use the number of complete segments as weight, sum in float64 and convert to dB only after averaging. |
| FFT workspace | Replace whole-buffer Welch materialization with bounded batches, retaining the dependency's optional FFT backends. |
| Parallelism | Replace the executor's private bounded queue with public Future callbacks and semaphore backpressure. Use named workers. Add `--fft-threads`, default 1, to avoid nested FFTW oversubscription. |
| PSD errors | Retrieve every worker Future's result so multi-update failures cannot disappear. Remove mutation of private Future result storage. |
| Crop | Do not use a `[0:-0]` slice when a small crop rounds to zero. Require at least two retained bins. |
| Frequency plan | Reject invalid ranges and zero-width hops; align the first retained FFT bin for odd and even lengths. Normalize API sweep overlap before both planning and cropping. |
| Acquisition buffers | Enforce the configured cap in whole device blocks, reject caps too small for an FFT and distribute capped reads to reduce excess integration. |
| Timing | Use monotonic elapsed timers and timezone-aware UTC timestamps. Preserve the first read's start time across a multi-buffer acquisition. |
| Interrupted acquisition | Report samples actually read, including early shutdown. Handle shutdown before the first sweep without uninitialized variables. |
| Lifecycle | Explicitly drain PSD and writer workers; clean up startup/acquisition failures and allow reuse. Importing the library no longer installs process signal handlers. |
| Output failures | Preserve earlier asynchronous write failures even when later marker writes succeed. Propagate errors rather than logging and continuing silently. |
| Output ownership | Drain before closing; preserve caller streams and descriptors. Support supplied text streams. Open CLI output in binary mode. |
| Binary v2 | Serialize explicit little-endian float32 regardless of input dtype. Use `frombuffer`, handle short reads and reject truncated/misaligned/unsupported records. |
| CLI validation | Require complete numeric tokens, finite values, ascending one/two-element frequency specifications and valid limits/window parameters. Help/version work without SoapySDR. |
| Packaging/docs | Declare Python >=3.6 and optional SciPy/pyFFTW extras. Explain accuracy, units, integration rounding and memory/performance choices. |

## Numerical basis

For window `w`, segment length `N`, rate `fs` and `K` complete segments:

`PSD[k] = sum_j(abs(FFT(w * detrend(x_j)))[k]**2) / (K * fs * sum(w**2))`.

This is a two-sided density for complex IQ input. Frequency-bin width is
`fs/N`. Integrating linear density over the frequency bins estimates signal
power. Log output is `10*log10(PSD)` and zero maps to negative infinity.
This does not supply device calibration to dBm.

The independent NumPy tests and SciPy comparisons follow the documented Welch
normalization and periodic window convention:
https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.welch.html

## Validation

Run from this checkout:

```sh
python3 -m unittest discover -s tests -v
```

- Python 3.9 / NumPy 1.26.2 / NumPy FFT: 24 tests, 23 passed, optional SciPy
  reference test skipped.
- Python 3.9 / NumPy 2.0.2 / SciPy 1.13.1: all 24 tests passed.
- SciPy reference comparison covers seven window families, Tukey endpoint
  cases, both odd/even FFT lengths and constant detrending.
- Tests cover tone frequency and integrated power, seeded noise, unequal segment
  weighting, zero power, DC interpolation, crop rounding, batched/strided input,
  binary conversion/truncation/short reads, writer failures/ownership,
  backpressure, strict CLI input, simulated sweeps, interruption and cleanup.
- Small illustrative benchmark (1,048,576 complex64 samples, 512 bins, SciPy
  backend, one trial): original Welch 0.0136 s / 32.01 MiB peak traced allocations;
  revised 0.0131 s / 28.01 MiB. Timing is not a robust speed comparison and traced
  allocations are not total process RSS. The batching benefit grows for larger
  acquisitions and overlap; no universal performance claim is made.

## Limits and remaining verification

- The host lacks the SoapySDR Python binding. Actual device enumeration, tuning,
  gain, streaming, overflow handling and sustained throughput remain unverified.
- pyFFTW and Windows behavior were inspected but not exercised on those runtimes.
- Python 3.6 is the declared feature minimum; this audit ran on Python 3.9.
- Welch overlap restarts at acquisition-buffer boundaries. Segments spanning
  separate reads are not reconstructed. Buffer size can therefore affect the
  number of periodograms; the implemented average correctly weights those used.
- Acquisition rounds upward to device blocks and complete FFT-sized buffers.
  The sample count records this rounding. Time limits remain checked per sweep.
- Buffer caps do not bound total RSS. Queued sample copies and backend workspaces
  consume additional memory; unlimited queues intentionally remain available.
- Binary v2 records produced by old writers with float64 payloads have no dtype
  tag and cannot be reliably auto-repaired. New output consistently uses float32.
- No hardware-specific optimum window, FFT length, queue or thread count can be
  chosen universally. Existing tuning controls remain, with their tradeoffs
  documented; FFTW transform threading is now optional and explicit.
