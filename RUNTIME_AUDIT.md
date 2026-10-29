# Timing, execution order and numerical audit — v1.12.2

This audit covers the source paths for acquisition/setup/retuning/settling,
packet continuity, continuous FFT framing, worker queues and completion,
ordered output, calibration/characterization, passive diagnostics, equalization,
record integration and offline evidence publication. It extends the earlier
[execution audit](EXECUTION_AUDIT.md); it is a finite software review and
regression exercise, not proof that all hardware or possible executions are flawless.

## Defects reproduced and corrected

| Defect | Correction | Regression |
| --- | --- | --- |
| Minimum tune delay started before synchronous frequency/gain/correction programming and stream reactivation. Slow control operations could consume the entire delay before the frontend had reached its final state. | Start the settling clock after all those operations return. Settling still requires configured discarded samples and requested lock gates. | Advance a fake monotonic clock inside gain programming and verify the settling origin is the later time. |
| A background output failure could remain unnoticed during the next long acquisition, until its next output submission. | Check the writer's recorded exception under its lock at acquisition cancellation/packet boundaries; propagate the original error and use existing cleanup. | Run an actual failed writer task and verify acquisition raises before another packet read. |
| Finite large complex64 input could overflow inside FFT accumulation or detrending before the otherwise representable float64 density was calculated. | Near the working dtype's limit, normalize each affected segment by an exact power of two before detrending/FFT, then restore that exponent after FFT normalization in float64. Accumulate detrending means in complex128 before rounding to the working dtype. Ordinary-range inputs retain the existing FFT scaling path. | Large complex64 constant tone gives the analytic finite two-sided PSD; detrending the constant gives zero. Mixed-scale segments and discarded extreme tails preserve the valid small signal. These are synthetic numerical stress cases, not plausible calibrated SDR amplitudes. |
| Tone SNR computed a linear power ratio before taking its logarithm. Two finite powers could yield an infinite ratio, then nonfinite provenance or misleading acceptance behavior. | Use a difference of logarithms in tone-profile building, independent holdouts and characterization CW acceptance. | A synthetic 600-decade power ratio remains finite through profile construction and holdout validation. |

## Timing and flow contracts

1. Device construction establishes the selected RX path, clock configuration,
   gain mode/settings, frequency correction and hardware correction policy.
   Actual accepted sample rate drives DSP, rather than assuming the request was exact.
2. Setup starts the stream, determines device block size, allocates bounded
   buffers, creates the PSD workers and creates one ordered writer worker.
   Exact requested sample targets and explicitly block-rounded targets remain distinct.
3. A hop clears previous packet/framing state, tunes if needed, reapplies gain
   and hardware corrections, and reactivates a reset stream. The minimum settling
   timer then starts. Lock sensors, minimum delay and discarded sample count are
   separate conditions. Settling discards packet tails rather than carrying them
   into the measurement.
4. Each device read checks errors, overflow, abrupt termination, available
   timestamps and reported sample count. Packet tails preserve samples needed
   by the current acquisition. No FFT overlap carries across retunes or losses.
5. Contiguous chunks retain incomplete FFT tails. Complete segments are processed
   with periodic-window energy normalization and segment-weighted linear averaging.
   Async entry points snapshot input arrays. Submission failures preserve framing
   state; bounded live queues report pressure rather than accepting unlimited work.
6. A hop verifies actual rate/tuning and fixed-state context again before sealing
   its PSD future. Completion callbacks resolve results without occupying another
   worker waiting for the same pool. The single writer preserves submission order.
7. Output and DSP failures propagate to the caller. Cleanup stops the stream,
   closes DSP workers, drains the writer and releases output wrappers. A failed
   sweep can leave earlier complete records in a streaming output; streaming
   acquisition is not an atomic whole-file transaction. Offline generated JSON
   artifacts use separate atomic publication and source-evidence checks.

## Four different meanings of time

| Quantity | Meaning and limit |
| --- | --- |
| Sample duration | Acquired sample count / actual sample rate; FFT overlap does not add elapsed sample time. Retained complete Welch segments may omit a final tail. |
| Host read bounds | UTC readings around acquisition calls, not precise first/last RF sample arrival times. Host clock adjustment and buffering remain relevant. |
| Hardware sample timestamps | Device-defined epoch and rate, independently checked for continuity where exposed. Nanoseconds do not imply UTC or cross-device synchronization. |
| Run deadline and settling | Monotonic host time. The run deadline stops starting further work at checked boundaries; minimum settling delay begins after control operations. |

This is **soft real-time** software. A driver read/control call already in
progress, FFT backend call, OS scheduling pause or blocking output write is not
preempted by the deadline. Shutdown drains resources and may take longer than
the acquisition time limit. There is no bounded worst-case execution latency or
hard real-time guarantee. A stalled output sink cannot be made interruptible by
checking for exceptions; only failures already reported by its worker are caught.

For latency-sensitive capture, use a suitably small supported maximum buffer,
measured settling policy, adequate CPU/transport capacity and a reliable sink.
Live queues are bounded but worker count and buffer size still determine total
queued memory and latency. Record actual overrun/error behavior under the intended
rate and host load. More threads are not automatically faster. A termination,
known tone and hardware timestamp test are required to establish the physical
capture behavior of a particular receiver/driver combination.

## Numerical and corner-case checks retained

- Complex two-sided Welch normalization, periodic windows, ENBW, odd/even FFT
  geometry, linear weighting and independent direct-DFT references.
- Chunk boundaries, overlap extremes, short packets, end-burst tails, partial
  deadlines, overflow, timestamp gaps, cancellation, queue rejection and shutdown.
- Snapshot ownership, worker failures, ordered writes, binary short I/O,
  checksums and missing/zero/estimated-bin semantics.
- Fixed-gain/context guards, interpolation domain restrictions, known-reference
  requirements, source-on/off subtraction, thermal/LNA models, relative-response
  fitting and independent holdout checks.
- Fractional-bin integration against exact rational representations of supplied
  floating-point interval widths. The approximation remains piecewise constant
  over FFT bin support, not reconstruction of a sub-bin waveform.
- Offline CLI input/output separation, concurrent evidence changes and atomic
  publication. File metadata checks address ordinary concurrent mutation, not a
  hostile filesystem forging identity/time metadata.

See [DEVELOPMENT.md](DEVELOPMENT.md) for reproduction commands and
[AUDIT.md](AUDIT.md) for the actual validation results. No physical SDR, sustained
hardware throughput, RF linearity or absolute measurement uncertainty was certified
by these software checks.
