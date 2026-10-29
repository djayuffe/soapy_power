# Measurement and maintenance routines

See the [complete calibration procedure](CALIBRATION_STEPS.md) and
[hardware-family characteristics](SDR_DEVICE_GUIDE.md) for preparation, validation
and conditions requiring new reference measurements.

These procedures complement [RADIO_THEORY.md](RADIO_THEORY.md), which defines
normalization, frequency orientation, DC handling and the calibration model.
Keep raw captures and reference measurements: a passing software test does not
measure a receiver's analog accuracy.

## Bring up a receiver

Install the SoapySDR Python bindings and the hardware's Soapy driver using the
same Python environment as soapy_power. Install this checkout with
`python -m pip install .`. Optional FFT backends are installed with
`python -m pip install '.[fft,fftw]'`. Confirm device discovery and capabilities:

```sh
soapy_power --detect
soapy_power --info
```

Select the intended device with `-d 'driver=...,serial=...'` and channel with
`-C`. Use the supported sample rate, bandwidth, antenna and gain elements shown
by the driver. Record driver/firmware versions, physical connections, clock
reference and external converters. Allow the radio and reference source to
stabilize according to their specifications.

Start with a short, fixed-gain, single-center JSON acquisition. For example,
the following requests 2 MS/s, 4096 bins and four independent acquisitions at
100 MHz; change these settings to supported values for the intended hardware:

```sh
soapy_power -f 100M -r 2M -b 4096 -g 20 -n 64 -u 4 -F json -O raw.jsonl
soapy_power_calibration integrate raw.jsonl
```

Acquisition `-O` opens/truncates its destination. Choose a new capture path for
each run. Offline context/profile commands refuse existing output paths unless
`--force` is explicit. They publish a complete, fsynced JSON file atomically;
the default no-clobber publication requires a filesystem supporting hard links.
`--force` uses atomic replacement. This does not guarantee survival of a power
failure before directory metadata reaches storage.

The uncalibrated unit is delivered-IQ mean-square per Hz, not dBm or dBFS.
Compare measured bin spacing with actual sample rate divided by FFT length.
Confirm a known tone's RF label and frequency direction by moving the tone or
receiver tuning. Test both positive and negative raw-baseband offsets. With a
high-side external mixer use `--lnb-lo` and `--lnb-high-side`; use
`--iq-conjugate` only when the stream orientation requires it.

## Choose frontend and DC behavior

| Receiver/path behavior | Procedure |
| --- | --- |
| Zero IF, LO/DC spur near baseband zero | Measure its width with the chosen window and gain. If needed, select `--dc-mask-width HZ`; bins are explicitly missing. |
| Low IF/DDC with a displaced spur | Measure its raw-IQ offset and select `--dc-offset HZ --dc-mask-width HZ`. Raw coordinates precede conjugation and external mixing. |
| Direct sampling or external converter | Verify the physical Nyquist zone, anti-alias filtering and LO side using known tones. CF32 cannot determine these automatically. |
| Driver provides DC/IQ correction | Keep `default` initially. Compare measured spur/image performance with supported explicit `--dc-correction on/off` and `--iq-correction on/off` settings. |
| Driver provides offset tuning | Supply measured/supported `--tune-args KEY=VALUE` settings and verify tuning readback and orientation. |

`-D constant` removes each segment's mean and can remove a wanted near-DC
signal. It differs from excluding a known contaminated region. `--remove-dc`
is an interpolated estimate restricted to boxcar, not a measurement of DC.
Choose a window for leakage and resolution; ENBW and bin spacing differ.
Do not infer that a driver correction flag guarantees a measured image rejection
ratio or removes all offset. Avoid changing correction modes during a capture.

## Construct a physical power calibration

1. Fix the complete acquisition configuration, especially gain elements, sample
   rate, bandwidth, antenna, frontend correction policy, clocking and external
   chain. Do not use AGC. Capture uncalibrated JSON and extract its actual context:

   ```sh
   soapy_power_calibration context raw.jsonl -O context.json
   ```

2. Specify the reference plane, source, source calibration date/uncertainty,
   cable/attenuator losses and measurement environment. Define acceptance limits
   for frequency error, compression, repeatability, residual response and image
   rejection appropriate to the application before evaluating the data.

3. Measure a rectangular grid of RF frequencies and raw-baseband offsets.
   At each pair tune the receiver so the injected tone appears at that offset;
   include the chosen reference baseband offset in every RF row. Cover every
   retained, unmasked bin that the intended scans will use. Use an off-DC
   reference offset if DC is excluded. Boundary points need not be exactly at
   Nyquist when those bins are cropped or masked. There is no extrapolation.

4. For each point obtain **net tone power** by integrating the entire window
   main lobe and relevant leakage in linear raw-IQ density times bin width.
   Select a band inside the retained spectrum without masks or other signals.
   Measure source-off noise in the same band and configuration. Subtract noise
   in linear power where the procedure calls for it; quantify the resulting
   uncertainty and reject nonpositive or noise-dominated net powers. Establish
   adequate SNR without compression by repeating at several known input levels.
   Do not insert a peak bin's PSD or a dB average as integrated `raw_power`.

5. Save the actual reference-plane input and net raw powers in a UTF-8 CSV with
   this exact header (each row contains measured numbers):

   ```csv
   rf_hz,baseband_hz,input_dbm,raw_power
   ```

   `input_dbm` includes the measured source/path corrections to the stated plane;
   `raw_power` is integrated raw IQ mean-square, not density. The software cannot
   verify a source certificate, choose a noise subtraction method or infer
   physical coefficients from the receiver's nominal gain setting.

6. Build and inspect the profile. This example assumes **100 kHz is an actually
   measured reference offset** in `measurements.csv`:

   ```sh
   soapy_power_calibration build --context context.json --measurements measurements.csv \
     --reference 'Source identification, certificate and measurement procedure' \
     --reference-plane 'Receiver input connector' --reference-baseband-hz 100000 \
     -O calibration.json
   soapy_power_calibration validate calibration.json
   ```

   Supply `--measured-at` and `--uncertainty-db` when established by the actual
   procedure. Repeated grid cells require `--max-spread-db` with your acceptance
   limit. Accepted repeats average normalized **linear power response**, then
   invert it to a dB correction. This is not an average of dB offsets. The profile
   preserves the measurement CSV's SHA-256; archive that exact CSV with it.

7. Verify against additional frequencies, offsets and levels that were not used
   to fit the grid. Check interpolation error, continuity between hops, repeated
   runs and the full intended retained band. Densify the measured grid when the
   residual response exceeds the declared acceptance limit. Run with exactly
   the configuration in the profile:

   ```sh
   soapy_power -f 100M -r 2M -b 4096 -g 20 -n 64 -u 4 -F json \
     --calibration calibration.json -O calibrated.jsonl
   soapy_power_calibration integrate calibrated.jsonl
   ```

   The command is only applicable if that RF band, baseband coverage and all
   hardware settings match your actual profile. Context is checked before and
   after tuning and after acquisition. Readback cannot reveal every internal
   driver/firmware or temperature change, so physical verification remains
   necessary. A reported uncertainty is supplied provenance, not an automatically
   propagated confidence interval.

## Integrate and interpret records

```sh
soapy_power_calibration integrate calibrated.jsonl --start-hz 99900000 --stop-hz 100100000
```

For JSON v2, band bounds select intersections with bin support in `[start, stop)`
and must lie inside **each record's** measured support. Version-1 records retain
legacy center selection unless `--method fractional` is explicit. The routine integrates densities in linear power over actual intersected bin widths. It emits one result per hop,
not a stitched or simultaneous wideband measurement. Do not sum overlapping
hops or acquisitions as independent frequency bins.

Excluded bins cause an error by default. `--omit-masked` explicitly reports only
the measured bandwidth with `complete: false` and excluded bandwidth; this is
not full-band power. JSON null logarithms of true zero are distinguished from
excluded bins. If the integrated linear result overflows or underflows float64,
the output reports that condition and retains a representable dB result instead
of fabricating zero or infinity.

## Routine checks and recovery

Before a measurement campaign, verify tone frequency/orientation, source-off
noise, a known reference level, expected spur/image behavior, and clipping or
compression margins. Check representative band edges and every intended gain
configuration. Record physical temperature and external path state where they
matter to the uncertainty budget.

After changing radio/firmware/driver, clock, gain, bandwidth, frontend settings,
external filters/converters/cables, or physical environment, recheck the
reference and repeat calibration where its assumptions no longer hold. Never
edit context fields just to bypass a mismatch. Preserve the original profile
and measurements and build a separately identified replacement.

For a long run, first test the intended rate, FFT size, output destination and
runtime. Watch overflow, discontinuity, device and writer errors. A hop affected
by a detected stream gap is rejected; earlier complete output records may still
exist. Treat a nonzero exit as a failed acquisition and inspect which complete
records precede it. A process interrupted during output can leave a partial last
JSON line or binary record: preserve the file and reject the incomplete record.
Stop/reconnect the device, resolve throughput or configuration problems and
start a separately named acquisition. Do not concatenate IQ across a restart.

Performance depends on the host and backend. Benchmark the intended workload
with bounded `--max-threads`, `--fft-threads` and queue sizes; multiple workers
and multiple FFT threads can oversubscribe CPUs. Single precision is optional;
validate its error against double precision for the required dynamic range.
A larger FFT changes resolution, memory and processing latency, not the
receiver's analog selectivity. Deadlines cannot interrupt a blocking driver
call or promise real-time completion.

See [CALIBRATION.md](CALIBRATION.md) for terminated baseline/antenna comparisons
and manifest-driven measured calibration; [REMEDIATION.md](REMEDIATION.md) lists
the current integrity, timing, exact-target and versioned-output contracts.
