# B200mini receive-only survey examples

These settings were exercised on one B200mini over USB 3. They are starting
points, not universal USRP defaults or an absolute power calibration. Install
SoapySDR Python bindings, SoapyUHD, matching UHD images, and this checkout with
SciPy (`pip install -e '.[fft]'`). Set `UHD_IMAGES_DIR` and
`SOAPY_SDR_PLUGIN_PATH` if your images/module are installed outside normal paths.
Discover the actual serial and RX capabilities first:

```sh
soapy_power --detect
soapy_power -d 'driver=uhd,type=b200,serial=YOUR_SERIAL' --info
```

## Capture

Select your receiver explicitly. The USB frame settings below were used in the
hardware tests; larger allocations can fail on some hosts. `SOAPY_POWER` can
point to a virtual-environment executable. The script defaults to RX2 and fixed
20 dB gain; override `SDR_ANTENNA` / `SDR_GAIN_DB` for your measured setup.

```sh
export SDR_DEVICE='driver=uhd,type=b200,serial=YOUR_SERIAL,recv_frame_size=16360,num_recv_frames=1024'
./examples/ettus/scan.sh 100M:200M 600 fm-vhf-10min.jsonl
./examples/ettus/scan.sh 400M:500M 600 uhf-10min.jsonl
./examples/ettus/scan.sh 44M:1000M 600 wide-survey-10min.jsonl
```

Run each command separately. Output names should be new: the capture CLI can
replace an existing file. Each uses 56 MS/s, 8192-point Hann FFT (6.84 kHz bin
spacing; 10.25 kHz ENBW), 25% frequency overlap, 5 ms requested integration,
20 ms minimum settling, LO-lock and strict timestamp/partial checks. Fixed gain
is not proof of analog linearity. `--reopen-stream` tunes while the stream is
closed; `--deadline-policy finish-hop` completes the final hop before stopping.
Signals still interrupt, and the last sweep may be incomplete. Short signals
between visits may be missed. The raw bins can extend beyond requested endpoints.

Other ranges need valid planned center frequencies. In particular, these
examples do not promise an exact full-device-endpoint scan for every overlap.

## Heatmap

Install the optional plotting dependency in the same Python environment:

```sh
python -m pip install matplotlib
python examples/ettus/heatmap.py fm-vhf-10min.jsonl \
  --start-mhz 100 --stop-mhz 200 -O fm-vhf-heatmap
python examples/ettus/heatmap.py uhf-10min.jsonl \
  --start-mhz 400 --stop-mhz 500 -O uhf-heatmap
```

The command writes PNG, NPZ (matrix/axes), and JSON (summary), requiring new
output paths. It checks record geometry, complete acquisitions, verified
continuity, logarithmic raw units, and ordered timezone-aware host timestamps.
It accepts uncalibrated logarithmic JSONL only, and streams the input file.
Memory scales with display time cells × frequency cells; choose `--time-bin`
and `--bins` accordingly for long sessions. Python 3.7+ is needed by this plotting
example for `datetime.fromisoformat`; the receiver package's declared minimum
is unchanged.

The default display takes maximum PSD per one-second / frequency cell, omitting
12% of each hop at both edges. Genuine hop overlap maintains coverage. This is
peak display aggregation, not mean PSD, integrated signal power, dBm or noise
figure. The native bins and captures remain in the original JSONL. Each hop is
assigned by host start time; rows do not imply simultaneous whole-band reception.
Missing cells remain blank, and percentile color limits can saturate strong peaks.
DC/LO spurs, images, antenna response and compression can still affect results.
Validate these with known physical references before relying on absolute power.

## Smoother 2.4 GHz display with redundant overlapping hops

With an antenna alone, gain calibration cannot be inferred from a busy Wi-Fi
spectrum or an assumed flat noise floor. A more conservative relative display
uses extra overlap, a restricted central IF band and a smooth weight, retaining
all original spectra:

```sh
SDR_OVERLAP_PERCENT=87.5 ./examples/ettus/scan.sh 2400M:2500M 120 wifi-refined.jsonl
python examples/ettus/heatmap.py wifi-refined.jsonl \
  --start-mhz 2400 --stop-mhz 2500 --edge-fraction 0.25 \
  --dc-mask-width-hz 500000 --mode weighted-mean -O wifi-refined-mean
python examples/ettus/heatmap.py wifi-refined.jsonl \
  --start-mhz 2400 --stop-mhz 2500 --mode peak -O wifi-refined-raw-peaks
```

At 56 MS/s, 87.5% overlap gives a 7 MHz hop. The central half of each native
spectrum is retained in the mean view (±14 MHz); the central 500 kHz **full
width** is excluded around each hop LO. Neighboring hops supply real RF samples
in that interval from offsets away from their own DC and filter edges. Those
samples are acquired at different times: a short signal occurring only in a
masked acquisition can be absent from the reduced view. Inspect raw peaks and
original evidence as well. Coverage is not reconstructed or interpolated when
no unmasked observation exists.

The weighted mode averages linear PSD, using the normalized quartic IF taper
`(1 - (offset / retained_half_width)^2)^2` and actual integration duration.
Calculation uses log-sum-exp to avoid exponentiation overflow/underflow; it is
not an average of dB values. The output NPZ uses `mean_density_db` for this mode
and `peak_density_db` for peak mode. The JSON records masks, mode, edge exclusion
and observed coverage. It does not contain a calibration profile. This changes
the sampling/aggregation, without subtracting a guessed baseline or boosting a
frequency-dependent curve. It cannot certify analog passband flatness, SNR,
linearity or absolute dBm. Additional hops reduce the revisit speed.

Weighted fusion requires an observable fixed-gain context and rejects AGC or
changes to receiver identity/path, gain, rate, bandwidth, clock, correction,
format or tuning-argument context. It must not merge arbitrary receiver states
into an apparently leveled spectrum. Peak mode remains available for raw review.

## Five-minute full-range LO/edge comparison

For the manufacturer's published RF span, use a new output filename:

```sh
SDR_OVERLAP_PERCENT=87.5244140625 ./examples/ettus/scan.sh 70M:6000M 300 full-specified.jsonl
```

The hardware experiment used the wider **driver-accepted** 42–6008 MHz span;
42–70 MHz and 6000–6008 MHz are outside the manufacturer's published 70 MHz–6 GHz
operating range. Streaming there does not establish specified sensitivity or
accuracy. To reproduce the experimental acquisition geometry explicitly:

```sh
SDR_OVERLAP_PERCENT=87.5244140625 ./examples/ettus/scan.sh 42M:6008M 300 full-extended.jsonl
python examples/ettus/heatmap.py full-extended.jsonl --start-mhz 42 --stop-mhz 6008 \
  --bins 8192 --time-bin 60 --edge-fraction .12 --mode peak -O full-broad-peaks
python examples/ettus/heatmap.py full-extended.jsonl --start-mhz 42 --stop-mhz 6008 \
  --bins 8192 --time-bin 60 --edge-fraction .25 --dc-mask-width-hz 500000 \
  --mode peak -O full-centre-reduced-peaks
python examples/ettus/heatmap.py full-extended.jsonl --start-mhz 42 --stop-mhz 6008 \
  --bins 8192 --time-bin 60 --edge-fraction .25 --dc-mask-width-hz 500000 \
  --mode weighted-mean -O full-centre-reduced-mean
```

The exact overlap retains 1022 of 8192 planning bins per hop: 6.986328125 MHz
spacing and 854 hops for 42–6008 MHz. Last planned center is 6004.831 MHz, inside
the driver's bound. A literal 87.5% plan would put the last center beyond that
bound. With 70–6000 MHz, the chosen grid uses 849 hops and centers inside that
published span. Frequency plans still require live device validation.

Use sufficiently long time cells: the measured dense full-range sweep interval
was 35.85 s, versus 7.64 s for the earlier fast survey. The example's minute cells
are not simultaneous observations and do not guarantee interception of short
bursts. Both comparison peak views use the same new data and estimator; the
mean is provided separately. No real RF-fixed line is automatically labelled a
birdie or deleted. The center mask is for the tested default zero-offset tuning;
fusion/masking with unverified tuning arguments is rejected because physical LO
and displayed baseband origin need not coincide.

See [the measured comparison](results/full-range-clean-5min.png),
[the LO detail](results/full-range-lo-detail.png),
[the compact result record](results/full-range-clean-5min.json), and the
[measurement roadmap](../../MEASUREMENT_ROADMAP.md) for terminology, missing
measurements and the calibration ladder. Raw data and individual IQ samples are
not part of the repository example. The JSONL files record uncalibrated PSD;
they are not continuous complex-IQ recordings.

### Offline RF Explorer for the full-band 30-minute recording

`build_explorer.py` packages the validated high-resolution B200 display bundle
into a self-contained HTML file. It does not acquire data or increase FFT
resolution. Requires Pillow for bundle validation; no browser libraries or
network connection are needed to use the resulting HTML.

```sh
python examples/ettus/build_explorer.py \
  /path/to/hires-viewer-meta.json /path/to/hires-textures.json \
  -o /path/to/heatmap-explorer.html
node --test tests/test_heatmap_analysis.js
python -m unittest discover -s tests -p test_explorer_bundle.py
```

Open the HTML in a browser. The existing weighted-mean, reduced-IF peak,
broad-IF peak and peak-difference views now accompany:

- A threshold activity profile and contiguous candidate-region table. The
  analysis always uses reduced-IF peak display cells, independently of the
  color view. Set an absolute PSD threshold, minimum observed time cells and
  minimum activity fraction. Missing cells are excluded from the denominator.
- Clickable regions, back through up to 40 zooms, keyboard cell selection,
  Ctrl + wheel zoom, and view bookmarks stored in the URL fragment. A bookmark needs the same
  local HTML file; it is not a public sharing link.
- Frequency-analysis CSV, selected-frequency time-trace CSV and a 3200-pixel
  wide PNG report with heatmap, activity profile, spectrum and time trace.
  The earlier heatmap PNG and selected-spectrum CSV exports remain available.

Activity means the fraction of observed 30-second display cells whose peak
exceeds the threshold, strictly `>`. It is not transmitter duty cycle, packet
occupancy, a signal detector or evidence that a displayed feature is genuine
RF. The full-band revisit interval is about 36 seconds and each frequency is
only briefly sampled. Contiguous display cells do not establish emission
bandwidth or transmitter identity. The table sorts qualifying regions by their
highest peak and displays the first 50; CSV exports every frequency cell.
Minimum observation filtering applies to the profile and candidate table;
summary activity includes all observed cells. Analysis rounds PSD to 0.01 dB,
matching browser readouts. Values near the threshold can change classification
within that quantization. All power remains uncalibrated.

The packager currently accepts the **42–6008 MHz, 16,384 × 60, 30-second** bundle
format from the full-range B200 recording. Other geometries and encodings are
rejected because this example contains acquisition-specific annotations. It is
not a general JSONL renderer. Metadata supplies dates, source count, units,
FFT spacing and encoding details; textures are lossless RGB/RGBA PNGs at the
matrix dimensions, with each uint16 value encoded in the red/green bytes.
`65535` denotes missing density/time/offset values; density is `(q/100)-250`,
host-read bounds are `q/10` seconds, peak offsets are `(q/100)-32` MHz. Counts
are integers, not independent average counts. Planned center overlays describe
the hop plan, not physical LO readback. The underlying numerical NPZ remains
the preferred input for precise offline analysis.

### Phase analysis of the terminated full-band bundle

The `phase_report.py` example reprocesses the 27 archived complex-IQ captures
and scalar full-band matrices from the termination experiment. It acquires no
new data. Run it from an environment with this repository installed, NumPy and
Matplotlib:

```sh
python examples/ettus/phase_report.py /path/to/termination-bundle
python -m unittest discover -s tests -p test_phase_diagnostics.py
```

The bundle must include `terminated-baseline-data.npz`,
`sweep-provenance.json` and the matching `iq-FREQUENCYM-gGAIN-dcSTATE.npz` and
JSON captures at 70, 100, 400, 915, 1800, 2440, 3500, 5800 and 6000 MHz, for gain
20/DC off, gain 20/DC on and gain 60/DC on. This example describes that measured
B200 bundle; it is not a general IQ-file renderer. `-o DIRECTORY` selects a
separate output directory. The general numerical entry point is
`soapypower.phase_diagnostics.analyze_phase(samples, sample_rate, ...)`.

Outputs include an expanded full-band graph, phase overview, spectral
coherence/phase matrices, IQ-density clouds, frequency stability regions,
per-capture phase CSV and numerical NPZ arrays. Raw source files are preserved.

Analysis removes the global mean of the analyzed prefix, then computes angular
moments, covariance-ellipse orientation, I/Q cross spectra and complementary
mirror-pair spectra. The I/Q convention is `E[conj(FFT(I))*FFT(Q)]`, matching
SciPy `csd`; a positive complex tone gives −90° at its positive IF bin. Mirror
phase uses `E[Z(+f)*Z(-f)]` and rotates by twice a rotation of the local I/Q
coordinate system. Neither is an absolute RF transfer phase.

Phase cells below squared coherence 0.2 are masked. Ellipse orientation is
shown only when `abs(E[z*z])/E[abs(z)**2] >= 0.05`, modulo 180 degrees. Angular
histograms omit radius ≤0.05 complex RMS. These are descriptive display gates,
not confidence/significance thresholds; 255 overlapping frames are correlated.
Gray means unsupported phase, not zero phase. Sparse delivered I/Q levels can
produce comb-like phase histograms; observed level counts do not establish ADC
bit depth or effective number of bits.

I/Q coherence can arise from unequal positive/negative sideband power even for
proper complex noise. Complementary coherence is a separate statistic; neither
alone identifies hardware IQ imbalance or proves an image-rejection ratio.
Separate retunes have no validated common phase reference, so no RF-frequency
phase unwrapping, group delay, absolute phase, oscillator phase noise in dBc/Hz
or calibrated complex correction is produced. Those need suitable coherent
reference measurements and reference-plane/path characterization.

### Use phase to qualify lines and inspect them coherently

The noise-audit CLI now accepts the opt-in `--coherent-features` flag. Install
optional SciPy support (`pip install '.[fft]'` from the repo) when using it:

```sh
soapy_power_noise_audit iq.npy --sample-rate 56e6 --fft-size 8192 \
  --segments 255 --coherent-features -O phase-qualified.json
```

This adds training/held-out stationary-line candidates to the diagnostic JSON.
It applies no notch or source-file correction. Programmatic access is through
`soapypower.coherent_features.coherent_features(iq, sample_rate)`; it also returns
spectra and residual-phase traces. The CLI retains a compact scalar/candidate
report. Existing noise-audit behavior is unchanged unless the flag is supplied.

Candidate selection, phase unwrapping and weighted linear frequency/phase fit
use only the first half of the analyzed IQ prefix. Validation uses the disjoint
second half with no refit. Short Hann frames overlap only within each partition.
Default gates select training peaks 8 dB above a 129-bin local median, then
require phase-lock value at least 0.7 in both partitions and at least 8 dB
held-out matched-frequency PSD excess above a nearby long-periodogram median.
These are descriptive gates, not false-alarm probabilities or significance
thresholds. Failed candidates may be valid modulated/intermittent RF. All PSD
observations and source IQ are retained.

The validated ~920 MHz feature in this experiment supports a longer coherent
analysis: 106.81 Hz FFT spacing and 160.22 Hz Hann ENBW in the held-out 9.36 ms,
versus 6835.94 Hz spacing and 10253.91 Hz ENBW for 8192-point frames. The finer
FFT grid comes from the longer window; phase qualification tests whether the
stationary-line model remains useful. The long-window PSD is one periodogram,
with different variance. Narrower ENBW raises a tone's density peak without
increasing its power. This is a local analysis of sparse IQ snapshots, not a
64-fold resolution improvement of the entire five-minute sweep.

Six targeted retunes at 910/913/915/917/923/925 MHz found the ~920 MHz feature
phase-qualified in five captures. Its IF moved approximately +7 to −5 MHz while
RF stayed fixed, ruling against a fixed-IF/LO-following feature in those captures.
It also qualified in the earlier terminated capture. This does not identify an
external transmitter or distinguish RF ingress, termination coupling and an
internally generated RF-fixed source. No automatic notch was applied.

Reproduce the plots from those specific archived bundles:

```sh
python examples/ettus/coherent_phase_report.py /path/to/antenna-bundle \
  /path/to/termination-bundle --retunes /path/to/retune-920mhz
python -m unittest discover -s tests -p 'test_coherent*.py'
```

The example expects the same nine-center antenna captures and the six retunes
named in this experiment. It performs no hardware acquisition. Native sample
clock/frequency accuracy, absolute phase calibration and source identification
remain separate measurements. A trial subtraction of the first-half sample
mean worsened held-out sample-mean power in seven of nine snapshots; that trial
was not applied to the source or scanning pipeline.

## Full receive operation

`full_operation.py` combines native-IQ preflight, independent phase holdouts,
RF retunes, native DC on/off and 0/20/40 dB gain observations with a guarded
five-minute full-driver-range receive survey. It defaults to RX2, 28 MS/s,
8192-bin Hann, a 500 kHz displayed-origin exclusion and usable IF +/-12 MHz.
It creates a measured receiver-control seed profile and keeps accepted IQ,
quality/RF/timestamp metadata, commands and compressed JSONL in a fresh output
folder. Configure matching Soapy/UHD Python bindings, module and images first.

```sh
python examples/ettus/full_operation.py \
  --device 'driver=uhd,type=b200,serial=YOUR_SERIAL,recv_frame_size=16360,num_recv_frames=1024' \
  --output /new/output/ettus-operation --seconds 300
```

The reported combined RF/DSP tuning range depends on the configured rate; the
example retains that observed range alongside the nominal 70-6000 MHz region.
Outside-nominal observations are not an extension of the manufacturer's
calibrated specifications. The gain/DC observations use the attached antenna:
they are not absolute gain, NF or vector calibration. Known physical references
remain necessary for the corresponding calibration APIs.

With `--reopen-stream`, streaming now stops immediately after the unique IQ
samples are acquired. RF readbacks, FFT completion, quality statistics, evidence
publication and output run with USB reception stopped; the next hop starts a
fresh stream after tuning. This avoids accumulating unwanted samples during
host processing. A completed five-minute 28 MS/s session exercised this path;
short smoke tests alone do not certify sustained 56 MS/s operation.

Offline record readers accept `.jsonl.gz` as well as plain JSONL, including the
heatmap, context extraction and fractional channel-integration tools. The
provenance combiner and other analysis commands are documented in
[receiver quality examples](../receiver_quality/README.md).

See [telemetry policies and reference models](TELEMETRY.md) for typed sensors,
warmup/validity gates, native filter phase, and the isolated SoapyUHD extension.

### Superscan evidence and publication

The macOS superscan runner keeps raw-IQ headroom checks, RX LO-lock checks,
verified timestamps, RF state and fresh native telemetry enabled during the main
capture. The 8 MS/s `sat-full --dither 300` plan uses 14 centers to guarantee
at least two trusted LO views throughout the requested bands, including DC
exclusion holes. More centers increase revisit time; pilot the plan on the host.

Before acquisition the runner refreshes the shared satellite cache, then freezes
it with the observer position into `SESSION/reference`. A failed refresh retains
the previous complete cache. A failed snapshot allows RF capture but disables
reference-based attribution. To make a snapshot explicitly:

```sh
python tools/satdb.py snapshot sessions/satdb SESSION/reference --qth sessions/qth.json
```

The default orbital-element age gate is 14 days at both ends of the capture;
`--max-tle-age-days` configures the snapshot policy. This is an engineering
eligibility limit, not a guarantee of orbit accuracy. Reanalysis prefers frozen
references. Legacy shared-cache analysis is labelled retrospective. PSD Doppler
matches are candidates; no decoded identity, analog linearity or absolute-power
calibration is implied. Guided and stacked searches use source-hop midpoints,
not a common sweep timestamp.

Analysis outputs are built in a private generation and published through an
atomic symlink replacement after all stages and input hashes pass. A failed
render returns nonzero and retains the previous complete report. Resolve the
output symlink once before reading a group of files; `bundle-manifest.json`
records input/output SHA-256 hashes. Old generations remain available for audit.
A first migration of a real output directory archives it intact and has a brief
name-availability gap. Keep raw JSONL outside the analysis destination.

### Unattended recovery and full partitioned analysis

The superscan runner holds a macOS `caffeinate -i -s` assertion through setup,
calibration, capture, recovery and reporting. It changes no permanent power
settings. Forced sleep, lid closure or physical disconnect can still interrupt
USB; holding an assertion does not certify a cable or USB controller.

Automatic gain selection now requires a configurable 12 dB amplitude reserve
on both peak and p99.9 components in the pilot. With no qualified point, the
runner refuses automatic selection. This reserve is an engineering margin;
a short pilot cannot guarantee future RF burst levels or analog linearity.

`tools/superscan_run.py` supervises the main receive command. It preserves raw
`attempt-NNN.jsonl` and logs, excludes a crash-truncated final fragment from
`capture-NNN.jsonl`, and records hashes, quality checks and durations in
`captures.json`. A complete malformed or unqualified row is fatal. Recognized
USB failures receive bounded retries; unrelated native crashes remain fatal.
A file-growth watchdog terminates a stalled process group. Cancellation also
terminates its child process group. Headroom rejection requires retained,
hash-verified IQ before a bounded 6 dB gain backoff; the default gain floor is
20 dB. Every gain context receives its own capture and report.

The scan budget credits validated record durations and normal within-attempt
retune gaps up to five seconds. Larger gaps and all inter-attempt downtime are
excluded. Summed RF dwell is reported separately. The accepted prefix can exceed
the requested time by the final hop duration; setup is outside that budget.

Generate a complete report from any number of legacy or supervised sessions:

```sh
python -m pip install numpy scipy matplotlib sgp4 pypdf reportlab
python tools/analyze_scan_set.py OUTPUT \
  sessions/superscan-YYYYMMDD-HHMMSS \
  sessions/superscan-YYYYMMDD-HHMMSS
```

The atomically published bundle includes a combined multipage PDF, every-record
telemetry CSV, native-resolution per-context analyses and interactive waterfalls,
JSON inventories, input/output hashes, and phase diagnostics for retained rejected
IQ when available. Waveform phase cannot be recovered from PSD. Absolute levels
and detection counts across different gains/rates must not be pooled as calibrated
measurements or unique transmitter totals. Never concatenate different gain/rate
contexts into one raw superscan file.
