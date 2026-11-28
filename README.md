# soapy_power

Receive spectrum measurements with SoapySDR, inspect receiver telemetry, and generate reproducible reports. This fork adds guarded acquisition, calibration tools, artifact analysis, a recovery supervisor and a local browser workspace.

Supported SoapySDR devices include RTL-SDR, bladeRF, Ettus/USRP, Airspy, SDRplay, HackRF and LimeSDR. Features depend on the device and driver. The **macOS GUI runner currently exposes Ettus B200/B200mini and bladeRF + XB-200 workflows**; it does not expose every CLI device or calibration command.

## Install

Install SoapySDR, its Python bindings and the driver for your receiver separately, then install this checkout in the Python environment that can import those bindings:

```sh
python -m pip install .
# Optional FFT backends:
python -m pip install '.[fft]'
# or: python -m pip install '.[fftw]'   # not on Intel Macs: the pyFFTW 0.15.1 x86_64 wheel crashes on import
```

Core CLI metadata requires Python 3.6+. The optional GUI requires **Python 3.9+**. Automated reports have additional dependencies documented in [development](DEVELOPMENT.md) and the runner scripts. The source GUI's saved-result viewer uses the Python standard library and bundled browser assets; viewing results does not open an SDR.

## Open the local workspace

From the checkout:

```sh
python -m soapypower.gui --repo /absolute/path/to/soapy_power
# Installed entry point:
soapy_power_gui --repo /absolute/path/to/soapy_power
# Separate data location; leave browser opening to the user:
soapy_power_gui --repo /absolute/path/to/soapy_power \
  --sessions /absolute/path/to/sessions --no-browser
```

With the runner installed, it can also start the workspace in the background (no SDR access; log in `sessions/gui.log`):

```sh
bash tools/macos_runner/queue.sh scan --gui start --wait 60   # --gui stop ends it; --gui-port N for another port
```

Open **http://127.0.0.1:8765**. The server binds only to loopback; `--port` selects another port. On macOS, install the existing launchd runner with `tools/macos_runner/install.sh` to enable queued acquisition. Read that script's setup instructions before installation. Without a loaded runner, the GUI remains a saved-result viewer.

The workspace provides scan configuration, receiver probing, latest spectrum and sampled IF waterfall, headroom/temperature/LO-lock/continuity evidence, session history, report links and queued reanalysis. Defaults are starting points, not universal optimum settings. Select the input that actually matches the connected antenna or termination.

### Scan controls

| Control | Behavior |
| --- | --- |
| **Pause** | Close the supervised RX child, validate its complete records, then hold the session with the receiver stream closed. |
| **Resume** | Reopen acquisition in a new partition, using the remaining accepted-time budget and current supervisor gain. |
| **Retry attempt** | Preserve the current accepted prefix and immediately reopen the supervised receiver. It does not reset credited time. |
| **Abort** | End acquisition, retain qualified data and let the runner generate an available partial report. |
| **Repeat scan** | Queue a fresh session using the original supported request settings. Confirm the physical input still matches. |
| **Generate report** | Reanalyze a finished superscan/hopscan session without acquiring new RF data. |

Pause, resume and active retry require a **new supervised satellite scan** advertising control protocol v1. They are not offered for older supervisors, setup/gain pilots, unsupervised hopscan or full-range/bladeRF workflows. Abort also works with the earlier stop-aware supervisor. Repeat scan requires recoverable typed request settings and an idle runner. Unsupported/custom requests require configuring a new scan explicitly.

Requests remain pending until the supervisor acknowledges them. Abort takes priority over pending pause/resume/retry. The runner stays reserved during a pause; it does not start another hardware job. Manual control boundaries retain separate attempt files and do not spend the automatic retry budget. Do not run an independent capture concurrently outside the runner.

See [the GUI guide](docs/GUI.md) for exact state transitions, supported workflows, telemetry interpretation and troubleshooting.

## Command-line acquisition

```sh
soapy_power --help
# Example receive capture; choose gain and RF connection for your receiver:
soapy_power -d driver=rtlsdr -f 100M:110M -r 2M -b 8192 \
  -g 20 -e 30 -F json -O scan.jsonl
```

Consult [the original CLI reference](README.rst) and [device guide](SDR_DEVICE_GUIDE.md) for other drivers and options. Hardware availability, usable tuning range, sustained sample rate, gain stages, sensors and calibration support vary by receiver.

## Reports and measurement meaning

- Saved manifests distinguish requested duration, qualified sequential scan time, summed RF dwell and excluded outages. A live span estimate is provisional.
- Latest-frame PSD is a max-pooled display preview. The IF waterfall samples visits while the GUI is open; its RF center can change between rows. Missing time and masked bins remain blank. Use the original reports for historical RF waterfalls and complete analysis.
- Changing gain, rate or receiver context creates separate measurement partitions. Pause/retry preserves these boundaries and raw evidence.
- Antenna scans and room-temperature terminations alone do not establish absolute dBm, noise figure, analog linearity, sensitivity or phase calibration. PSD does not contain recoverable waveform phase; phase diagnostics require retained IQ and an appropriate reference.
- LO/DC/spur diagnostics compare evidence and controls. Candidate classifications are not proof of signal origin, and this UI does not silently delete suspicious lines.

## Calibration, operation and algorithms

| Topic | Documentation |
| --- | --- |
| Satellite-band survey, Doppler tracking and identification | [Superscan](docs/SUPERSCAN.md), [GUI](docs/GUI.md) |
| Capability discovery and guided calibration | [Automatic calibration](AUTO_CALIBRATION.md), [measurement steps](CALIBRATION_STEPS.md) |
| RF limits, gain and device architecture | [Device guide](SDR_DEVICE_GUIDE.md), [Ettus](ETTUS.md), [bladeRF](BLADERF.md) |
| Terminations, hot/cold and measured references | [Calibration](CALIBRATION.md), [noise audit](NOISE_CALIBRATION_AUDIT.md) |
| Response, equalization and passive references | [Characterization](RECEIVER_CHARACTERIZATION.md), [equalization](EQUALIZATION.md), [flattening](FLATTENING.md), [passive references](PASSIVE_REFERENCES.md) |
| LO/DC, images and controlled spur experiments | [Spurs](SPURS.md), [experiments](EXPERIMENTS.md) |
| Mathematical contracts and evidence integrity | [Radio theory](RADIO_THEORY.md), [remediation](REMEDIATION.md) |
| Operations, timing and development | [Operations](OPERATIONS.md), [runtime audit](RUNTIME_AUDIT.md), [development](DEVELOPMENT.md) |
| Worked receiver examples | [Ettus](examples/ettus/README.md), [bladeRF](examples/bladerf/README.md), [RTL-SDR](examples/rtlsdr/README.md) |

## Validation

```sh
python -m pip install pytest matplotlib sgp4 reportlab pypdf   # analysis/report tests skip without them
python -m pytest -q tests
# Focused GUI/control and recovery fault injection; no SDR is opened:
python -m pytest -q tests/test_gui.py tests/test_superscan_recovery.py tests/test_scan_controls.py tests/test_scan_supervisor_controls.py
```

`python tools/validate.py` repeats the suite once per installed FFT backend (NumPy, SciPy, pyFFTW), as CI does. HTTP tests need permission to bind a loopback socket. Runner, supervisor and report-publication tests are POSIX-only and skip on Windows. Recovery tests use synthetic records and fake receiver processes. They validate software behavior; they do not substitute for RF calibration or hardware qualification.

MIT licensed. Original project: [xmikos/soapy_power](https://github.com/xmikos/soapy_power). Fork: [djayuffe/soapy_power](https://github.com/djayuffe/soapy_power).

### GUI and runner reliability

The receiver console uses bounded request workers and display buffers, streamed report downloads, fresh-state controls and typed queue validation. Runner and receiver leases prevent cooperating jobs from overlapping after a process failure. Read the [GUI/runner audit](docs/GUI_AUDIT.md) for findings, runtime bounds and recovery limits. Update legacy launch-agent installations with `bash tools/macos_runner/install.sh --no-probe` while idle.

The receiver console includes an [expert measurement workbench](docs/GUI_WORKBENCH.md) for custom acquisition, calibration, telemetry, phase/noise and non-destructive spur diagnostics. CLI-derived controls, plan preflight, partitioned pause/retry and automatic high-resolution reports share the local runner.

[SatNOGS integration](docs/SATNOGS_GUI.md) adds pass planning, transmitter-based scan preparation and reference-aware live spectrum/waterfall predictions to the receiver console.

The SatNOGS panel includes a server-synchronized UTC prediction clock, animated
azimuth/elevation sky plot and an offline flat world map with ground tracks,
observer position and an approximate visibility footprint. Live tracking and
1–300× pass playback share one clock; recorded RF timestamps remain unchanged.
See [satellite tracking controls and limits](docs/SATNOGS_GUI.md#shared-utc-and-flat-world-map).

Satellite operations also provide pass search/pagination, downlink-specific
Doppler/readout selection, target-duration controls and explicit connection
recovery. Reviewed target plans bind both catalog and observer geometry before
RX. See [SatNOGS selection and review](docs/SATNOGS_GUI.md#selection-reconnect-and-review-workflow).

Automatic reports now include all-record telemetry exports, feature inventories,
telemetry plots and measured-dwell waterfall previews. Full-range sessions support
GUI report regeneration without reception. See [automatic report and waterfall
behavior](docs/AUTO_REPORT_AUDIT.md).

Automatic 3D reports add acquisition-time satellite azimuth/elevation trajectories,
predicted downlink overlays and CSV exports from verified archived references.
See [orbital context and limits](docs/AUTO_REPORT_AUDIT.md#orbital-azimuthelevation-in-3d-reports).
