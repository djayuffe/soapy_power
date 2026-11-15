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
