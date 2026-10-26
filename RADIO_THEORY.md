# Radio architecture, DC treatment and power calibration

This analyzer measures the **delivered CF32 stream**. It does not assume an ADC
bit depth, ADC clock, full-scale voltage, impedance conversion or receiver
architecture from a product name. Hardware capabilities and readbacks decide
which controls can be used. A calibration file supplies measured power scaling;
receiver gain is never subtracted as a substitute for calibration.

## Architecture and frequency interpretation

| Design or operating mode | Relevant behavior | Application treatment |
| --- | --- | --- |
| Zero-IF / direct conversion | LO leakage, DC offset and analog I/Q mismatch may contaminate the center or reduce analog headroom. | Keep driver correction defaults unless explicitly selected. Mean removal, DC interpolation and DC exclusion are different operations. |
| Low-IF or offset-tuned receiver | DDC moves the signal and can move an upstream DC/LO artifact away from displayed zero. Image rejection and analog filtering depend on the receiver. | Forward explicit `--tune-args`, such as driver-supported `OFFSET`. Select the observed raw-IQ artifact location with `--dc-offset`; do not assume its sign from a model or tuning argument. |
| Superheterodyne | Mixer images, IF filters, inversion and synthesizer settling depend on the conversion chain. Analog I/Q correction may be irrelevant. | Use the driver's total tuned frequency and delivered sample rate. Do not enable correction methods that the driver does not advertise. |
| Direct RF sampling with a DDC | The delivered complex stream may have a much lower rate than the ADC; alias rejection and Nyquist-zone selection are upstream. | Normalize with the delivered sample rate. Do not substitute the master/ADC clock or invent an alias-zone mapping. |
| Raw real/direct-sampling mode | Positive and negative spectral copies can represent the same real signal; an untuned stream does not necessarily identify an RF alias branch. | Report the two-sided stream as delivered. Do not automatically double bins, infer RF from sample rate, or promise unambiguous RF labels without a meaningful driver tune/DDC mapping. |
| External LNB/upconverter/multiple mixers | Gain, noise, drift and inversion parity belong to the full external chain. | Default RF = LO + IF; `--lnb-high-side` uses RF = LO - IF. Choose the effective affine conversion for the selected branch and a reference plane covering the intended chain. |

For complex input I+jQ, positive digital frequency normally maps above the tuned
center. `--iq-conjugate` explicitly changes that convention. It is independent
of the external mixer's high/low-side selection. Frequencies and powers are
reordered together when producing ascending RF output. Even FFT lengths contain
one Nyquist endpoint, so mirroring changes which endpoint is retained.

Digital sample rate, clock source and frequency correction readbacks are not
measurements of clock accuracy. Aliases and analog clipping cannot be removed
by choosing more FFT bins or double precision. Phase-coherent multi-channel
measurements, cross spectra and beamforming are outside this single-channel
power analyzer's measurement model.

## DC controls are not interchangeable

Default behavior leaves driver DC and I/Q correction modes unchanged. Explicit
`--dc-correction on|off` and `--iq-correction on|off` require the selected RX
channel to advertise the relevant API. They are checked and reapplied after
retuning because drivers can restore per-LO frontend calibration. Manual fixed
gain mode is also reapplied after a tune; `--agc` must be explicitly supported.

- `--detrend constant` subtracts the mean of **each FFT segment** before applying
  its window. It affects real signals near zero frequency as well as offsets.
  It does not undo analog headroom already consumed by DC or clipping.
- `--remove-dc` replaces the central density with the mean of its neighboring
  bins. It is an estimate, not a measurement of missing signal power. It now
  requires boxcar and at least three bins: one-bin interpolation does not remove
  the window-spread DC lobe of Hann and similar windows.
- `--dc-mask-width HZ` leaves the samples and all other bins unchanged, marking
  excluded bin centers as NaN. `--dc-offset HZ` locates this band in **raw IQ
  before optional conjugation**. The exclusion follows conjugation, external
  mixer inversion and crop correctly. It wraps at the FFT's Nyquist boundary.
- Do not sum over missing bins and call the result full-band power. A sum over
  valid bins measures only the reported part of the band. Neither interpolation
  nor a calibration curve recovers a signal hidden by an unknown DC notch.

For example, a Hann window spreads a coherent constant offset into the central
three FFT bins. At 1024 samples/s and N=64, `--dc-mask-width 32` excludes their
centers at -16, 0 and +16 Hz. This is a mathematical example, not a prescription
for a hardware notch width. A real notch may be wider, frequency dependent or
located away from zero. Determine it with suitable reference measurements.

Automatic hardware correction can attenuate wanted near-DC signals. Superhet,
RFIC and FPGA implementations differ; no universal automatic correction policy
is imposed. Blindly estimating I/Q correction from arbitrary received traffic
can erase legitimate spectral components, so no such estimator is enabled.

## Density, power and window normalization

For periodic window w, segment length N, overlap O and step H=N-O:

`S[k] = sum_j |FFT(w * detrend(x[j*H:j*H+N]))[k]|^2 / (K * fs * sum(w^2))`.

The complex stream has a two-sided spectrum of width fs. A real waveform has
conjugate spectral copies; this analyzer still reports both and does not apply
a one-sided factor of two. The raw density integral is the average
window-weighted segment mean-square. A coherent unit complex tone integrates to
one input-power unit; a unit real cosine integrates to one half.

Bin spacing is fs/N. Window noise bandwidth is
`ENBW = fs * sum(w^2) / |sum(w)|^2`, and coherent gain is `sum(w)/N`.
Hann ENBW is 1.5 bins; boxcar ENBW is one. A peak density multiplied by ENBW
recovers the power of an isolated, exactly bin-centered coherent tone. An
off-bin tone has scalloping and leakage; integrate its appropriately resolved
spectrum instead of applying that peak formula indiscriminately.

Continuous framing preserves Welch alignment across reads. Segments do not
span retunes, reported gaps, abrupt termination or separate hardware bursts.
Overlap makes neighboring periodograms correlated. Segment count therefore
is not an independent-average count, and no universal confidence interval is
claimed. Averaging is in linear power; log conversion follows averaging.
That does not make the logarithmic estimate statistically unbiased.

## Calibration workflow

The calibration model maps **raw IQ mean-square** to input-referred milliwatts,
not ADC counts or a guessed voltage across 50 ohms. A suitable source/reference
measurement defines this relationship at a stated reference plane. A user's
profile supplies the evidence; loading a profile does not make the SDR a
certified power meter.

1. Select the actual device, channel, antenna, delivered rate, bandwidth,
   clock/reference configuration, all gain elements, frontend modes, external
   converter settings and RF path. Warm up the equipment and use levels within
   its linear range. Keep AGC off. Preserve those settings.
2. Acquire uncalibrated `-F json` measurements. Copy the complete
   `metadata.context` object into the profile. This records actual readbacks,
   including per-element gain, serial when exposed, clock sources, hardware info,
   frontend states, tuning arguments and requested device settings readbacks.
3. Inject a known tone at RF frequency F and a chosen raw-baseband reference
   offset b_ref away from contaminated/notched regions. Account for cable and
   external-chain loss at the selected reference plane. For default low-side
   IQ, tuning to F-b_ref puts a tone at b_ref; use the selected converter/IQ
   mapping for other configurations.
4. Obtain raw tone power by integrating linear density, with bin width fs/N.
   Include the window leakage appropriate to the measurement, and avoid an
   unqualified peak-only estimate. Use sufficiently high SNR for noise error to
   be negligible, or estimate and subtract background in linear units with an
   uncertainty analysis. The program does not guess or subtract receiver noise.
5. Compute `C0(F) = known_input_dBm - 10*log10(raw_tone_power)`.
   Repeat at ascending RF points, at the same b_ref and matching context.
6. At each RF point, move the tone across raw baseband by changing the tune.
   Measure `C(F,b)` and store the residual `C(F,b)-C0(F)`. This captures
   passband shape **including RF-dependent differences** rather than assuming
   that an RF-only scalar correction measures the whole passband.
7. Validate intermediate RF/baseband positions and independent signal levels.
   Record reference provenance, conditions and uncertainty. Use sufficiently
   dense points for the response; do not interpolate across an unresolved
   analog notch or a switched hardware path. Use separate profiles/acquisitions
   when actual gain or context changes across a band.

Profile version 1 is a JSON object with these fields:

| Field | Meaning |
| --- | --- |
| `version` | Integer 1. |
| `units` | `"dBm"`, the known input power reference. |
| `reference` | Nonempty description of the source/reference and conditions. |
| `reference_plane` | Nonempty physical port/plane to which power refers. |
| `context` | Complete acquisition context from actual JSON output. |
| `points` | Ascending pairs `[RF_frequency_hz, C0_dB]`, at least two. |
| `reference_baseband_hz` | b_ref used to measure C0. |
| `baseband_hz` | Ascending raw-IQ baseband axis, at least two, within +/-fs/2. |
| `baseband_correction_db` | Matrix with one row per RF point and one column per baseband point. Each row must interpolate to zero at b_ref. |
| `uncertainty_db` | Optional stated nonnegative reference/calibration uncertainty. It is recorded, not automatically propagated into a statistical interval. |
| `measured_at` | Optional recorded measurement date/time string. |
| `measurements_sha256` | Optional SHA-256 of the exact measurement CSV; supplied by the offline builder. |

See [OPERATIONS.md](OPERATIONS.md) for the offline construction commands and
reference/noise/linearity checks. Context is rechecked after acquisition as well
as after tuning; this cannot detect every unreported internal hardware change.

There are no fabricated default calibration coefficients. A flat baseband row
of zeros is an explicit assertion of flat response; measure and validate that
assumption instead of inserting zeros simply to satisfy the schema.

The program interpolates C0 linearly in dB over RF, and the residual matrix
bilinearly in RF and raw baseband. It refuses RF or baseband extrapolation and
checks the full context immediately after tuning. Static settings are checked
before stream setup; DC/IQ states are checked after the driver has tuned and
explicit correction requests have been reapplied. Gain/AGC mismatches stop the
measurement. The profile hash and reference are included in JSON metadata.

For a bin at RF F and corresponding raw-baseband b, after linear averaging:

`dBm/Hz = 10*log10(S_raw) + C0(F) + residual(F,b)`.

`mW/Hz = S_raw * 10^((C0(F)+residual(F,b))/10)`.

Conjugation and external inversion change the displayed coordinates; the
calibration lookup still uses the original raw-baseband coordinate. It does
not count a gain correction twice. Integrate milliwatts/Hz times bin width to
obtain milliwatts, and only then take a logarithm for band power. Never sum dBm
bins directly.

A response correction is not deconvolution, signal/noise separation or proof of
linearity. Finite windows still convolve the underlying spectrum. Calibration
cannot undo clipping, aliases, lost samples, an unmeasured band, saturation or
a noise-dominated deep notch. Hardware/firmware changes, external gains,
reference drift and temperature can invalidate a profile even if readbacks
match. A driver that exposes no serial cannot prove which individual unit is
connected. Physical identification and revalidation remain necessary.

## Output, compatibility and operational limits

Prefer `-F json` for scientific records. Each line includes density units, band
geometry, sample count, UTC read-call times, actual context, DSP parameters,
window ENBW, segment count, DC treatment and calibration provenance. JSON null
represents a nonfinite density: `masked_bins` identifies exclusions and
`zero_power_bins` identifies exact zeros (negative infinity in log output).
Standard JSON NaN/Infinity tokens are never emitted.

`rtl_power_fftw` labels units and includes DSP/calibration metadata comments.
CSV and binary v2 retain their existing layout and cannot describe the full
context; preserve settings and the profile with those files. Without a profile,
output remains input power/Hz or its logarithm. With a matched profile, it is
mW/Hz or dBm/Hz. Binary v2 retains float32 payload precision and explicit NaN
markers for exclusions. Text and JSON retain float64 values.

Hardware timestamps detect discontinuities when supplied; host UTC timestamps
remain read-call wall-clock bounds, not calibrated RF epochs. END_BURST ends
the measurement/sweep without joining another burst. END_ABRUPT, overflow and
detected gaps abort the affected measurement. An interrupt or duration limit
is checked between reads; an in-progress driver call or queued FFT/write can
still delay return. Completed output records preceding an error may remain valid.

Double precision does not improve ADC ENOB. FFT workers and per-transform
FFTW threads are separate controls; both CLI and library defaults use one
FFTW thread per transform. Batch and queue bounds do not bound total RSS:
backend workspaces and the dependency's FFTW interface plan cache also consume
memory. Performance choices require workload and hardware benchmarks.

## Primary references

- [SoapySDR device API: frequency chain, OFFSET and correction capabilities](https://pothosware.github.io/SoapySDR/doxygen/latest/classSoapySDR_1_1Device.html)
- [SoapySDR stream flags](https://pothosware.github.io/SoapySDR/doxygen/latest/Constants_8h.html)
- [UHD frontend calibration and correction](https://files.ettus.com/manual/page_calibration.html)
- [UHD power references, gain and calibration](https://files.ettus.com/manual/page_power.html)
- [SciPy Welch density, periodic windows and scaling](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.welch.html)
- [pyFFTW interfaces and threading](https://pyfftw.readthedocs.io/en/latest/source/pyfftw/interfaces/interfaces.html)
