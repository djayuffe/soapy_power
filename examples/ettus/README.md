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
