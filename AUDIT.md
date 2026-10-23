# Radio, IQ and numerical audit — 2026-10-23

Base: `c46cbb5` on the user's fork `djayuffe/soapy_power`. This audit supersedes
the 2026-10-22 report. The repository was clean before this second audit.
Changes are local, for review; this audit does not certify hardware performance
or claim exact arithmetic with finite-precision samples and FFTs.

## Findings and implemented changes

| Area | Finding | Resolution |
| --- | --- | --- |
| Welch continuity | Overlap restarted on every acquisition buffer, making PSD depend on read sizes. | A continuous IQ framer carries fewer than N samples between reads, preserving segment alignment. Independent-record API calls remain independent and cannot be mixed with streaming in one state. |
| Integration duration | Multiple capped buffers could significantly overshoot the requested sample count. | Round the total integration once to device blocks and shorten the final acquisition chunk. |
| IQ sample loss | SimpleSoapy's bulk reader hides overflows and concatenates discontinuous samples. | Read its public packet API directly, retain short-packet remainders and abort on reported overflow, abrupt-end flags or detected timestamp gaps. |
| Hardware timestamps | Converting epoch nanoseconds to float loses individual sample timing. | Subtract integer epochs first, then compare elapsed sample time within a quantization tolerance. |
| RF frequency labels | Requested tuning can differ from actual device tuning. Narrow odd-FFT ranges could miss a requested edge. | Use actual reported tuning in output and shared integer crop geometry in planning and PSD. Account for the asymmetric Nyquist endpoint. |
| External conversion | Only additive low-side LO mapping was supported. | Optional `--lnb-high-side` maps RF = LO - IF, including the mirrored Nyquist endpoint and ascending RF output. Existing RF = LO + IF and negative LO offsets remain supported. |
| IQ orientation | IQ convention reversal was not explicit. | Optional `--iq-conjugate` reverses baseband orientation without blind signal/image estimation. |
| Computational precision | Single-precision FFT/power arithmetic could lose weak components, even with a float64 accumulator. | Double-precision computation by default; optional `--precision single`. Power is squared in float64 in either mode. Compensated summation reduces long-average rounding loss. |
| Window interpretation | FFT spacing could be mistaken for resolution/noise bandwidth. | Expose coherent gain and ENBW, and log bin spacing and ENBW separately. |
| Async memory | Completed Future lists grew with every input chunk; result jobs occupied PSD workers while waiting. | Retain only pending Futures plus the first error. Seal states and resolve result Futures via callbacks. |
| Resource defaults | CPU-count workers, large automatic queues and 100 MiB buffers could retain excessive IQ. | Auto PSD workers are capped at four; auto pending capacity is one per worker. Auto acquisition chunks target 100 ms and are capped at 100 MiB, subject to device-block granularity. FFT batches remain bounded. Explicit controls remain available. |
| Deadlines | Session duration was checked only after an entire sweep. | Check deadline and interrupt between packets and hops. Partial hops are emitted only if at least one complete FFT exists. Drain queued work at shutdown. |
| Executor lifecycle | A submission after nonblocking shutdown could wait for queue capacity first. | Reject stopped executors before waiting and wake blocked submitters periodically to observe shutdown. |
| Output correctness | Linear output was labeled dB/Hz; mismatched arrays could be truncated silently by zip. | Correct scale-dependent text units and validate real, matching arrays on a finite ascending uniform frequency grid. |
| Large RF offsets | Estimating bin spacing from the first two large RF numbers amplified float cancellation over long records. | Use the full endpoint span divided by N-1 to estimate the serialized step. |
| Binary geometry | Payload length and frequency span could disagree; arbitrary payload sizes were accepted. | Validate geometry, payload alignment, float32 overflow and a configurable default 256 MiB read limit. Preserve v2 wire layout. |
| CLI side effects | Parsing `-O` opened/truncated files before validating settings or checking hardware. | Open output paths lazily during stream setup. Reject nonfinite/duplicate gains, invalid window parameters and negative file descriptors early. |
| Capabilities/errors | `--info` ignored the selected channel and missing bindings produced tracebacks. | Apply the selected channel and report missing dependencies and device errors directly. |
| Packaging | README loaded relative to the process directory; the Arch recipe fetched the unchanged original release. | Resolve README relative to setup.py, include audit/tests in source distributions and point development metadata/recipe at the user's fork. |

## Mathematical contract

For each uninterrupted measurement, with N samples per segment, overlap O,
hop H=N-O, sampling rate fs and periodic window w, the implemented two-sided
Welch density is:

`PSD[k] = sum_j |FFT(w * detrend(x[j*H:j*H+N]))[k]|^2 / (K * fs * sum(w^2))`.

Only complete segments are used. There is no zero-padding of incomplete tails,
and no factor-of-two correction for complex IQ. Continuous framing produces the
same estimator as concatenating the uninterrupted input first. Retuning starts
a new measurement and clears the Welch tail.

For the default orientation, RF = LO + center_IF + baseband_frequency. For high
side conversion, RF = LO - center_IF - baseband_frequency. IQ conjugation is an
independent, explicit input-convention choice. Frequencies are sorted ascending
with power reordered identically. For even FFT lengths there is only one
Nyquist endpoint; mirroring swaps which endpoint is included.

Bin spacing is fs/N; ENBW is fs*sum(w^2)/sum(w)^2. Periodic Hann ENBW is 1.5
bins, boxcar ENBW is one bin. The full-spectrum integral obeys Parseval as the
window-weighted mean-square of each segment. Cropping, detrending and replacing
the DC bin modify the signal or its reported power; apply energy identities
before those operations. Average linear power first, then apply 10*log10.
Zero density maps to negative infinity. Overlapped segments are correlated;
segment count is not an independent-average count or a confidence interval.

Primary references used for the convention and API checks:

- [SciPy Welch documentation](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.welch.html)
- [SciPy spectral analysis](https://docs.scipy.org/doc/scipy/tutorial/signal.html#spectral-analysis)
- [SoapySDR stream flags](https://pothosware.github.io/SoapySDR/doxygen/latest/Constants_8h.html)
- [SoapySDR device/stream API](https://pothosware.github.io/SoapySDR/doxygen/latest/classSoapySDR_1_1Device.html)

## Verification

Run `python -m unittest discover -s tests -v` from the checkout.

- Python 3.9, NumPy 2.0.2, SciPy 1.13.1: all 53 tests pass.
- Python 3.9, NumPy 1.26.2, NumPy FFT: 52 pass; the optional SciPy comparison
  is skipped (53 tests total).
- Whole-record SciPy comparisons cover every supported window family, Tukey
  endpoints, odd/even FFT sizes and constant detrending, using a double-precision
  reference and tight tolerances.
- Independent explicit NumPy periodograms verify continuous chunk equivalence
  for N=2,3,7,63,64,127, overlap=0%,10%,50%,90%,99%, empty chunks and chunks
  shorter than a segment. Other checks cover strided input and FFT batching.
- Physical invariants cover positive/negative complex tones, real cosines,
  noncoherent tones, IQ image ratios, conjugation, seeded circular white noise,
  Parseval with every window family, coherent gain and ENBW.
- Simulated acquisition covers short packets, retained remainders, overflow,
  abrupt termination, timeouts, integer epoch timestamps, timestamp gaps,
  per-packet deadlines, partial measurements, cleanup, reuse, actual tuning,
  odd/even narrow ranges and contiguous high-side RF sweep coverage.
- I/O and lifecycle checks cover binary geometry/range limits, partial/truncated
  reads, large RF offsets, units, output ownership, deferred truncation, worker
  errors, bounded submission and nonblocking shutdown.
- Python source compilation and Arch recipe shell syntax are checked. The Arch
  package itself is not built on this macOS host.

## Radio limits and deliberate feature boundaries

- No real SDR was exercised: this host lacks the SoapySDR Python binding.
  Driver tuning/settling, CF32 normalization, actual device clocks, gain accuracy,
  RF filtering, sustained acquisition, overload/clipping and AGC require tests
  with the intended radio and a reference source.
- pyFFTW testing could not be added: the isolated dependency installation was
  not executed because automatic approval review hit a usage limit. FFTW thread
  controls remain available; no FFTW performance claim is made.
- Windows and Python versions other than the tested 3.9 runtime were not
  exercised. Python >=3.6 remains the declared language/API minimum.
- Host UTC timestamps describe read-call start/finish, not a calibrated RF
  acquisition epoch. Hardware time is used for gap detection when provided.
  Without it, only driver-reported discontinuities can be detected.
- Deadline checks cannot interrupt a blocking driver call or FFT/backpressure.
  Queued writes and computations drain before returning. They are soft deadlines.
- An overflow aborts the affected measurement rather than producing a falsely
  continuous result. Previously completed output records can still be valid.
- PSD units are input power/Hz or their logarithm. Values are not calibrated
  dBm, dBm/Hz or dBFS. Those need IQ scale, impedance/full-scale convention,
  frequency/gain calibration and a physical reference.
- No automatic IQ/image equalization, noise subtraction, calibration curve,
  deconvolution, resampling or adjacent-hop blending is guessed from the signal.
  Such processing can erase legitimate signals or create biased power estimates.
  Overlap-only sweeps retain duplicate frequencies; crop sweeps tile their
  retained bands. The analog passband must be measured to choose a useful crop.
- Double precision cannot recover ADC information already lost to quantization
  or clipping. Single FFT mode is a throughput tradeoff; backends may promote
  precision. Extreme inputs outside representable power range are rejected.
- Binary v2 retains float32 powers and has no embedded measurement/calibration
  metadata. Save acquisition settings alongside binary data or use text for
  float64 values. Old untagged float64-v2 records cannot be reliably repaired.
- Buffer caps are per acquisition array, not a whole-process RSS bound. Queued
  copies, retained overlap, FFT workspaces and output queues consume memory.
  Explicit unlimited queues/buffers remain available and can grow without bound.
- A universal best FFT length/window/thread count cannot be established without
  hardware and workload benchmarks. No unsupported speedup claim is made.
