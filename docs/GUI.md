# Local receiver workspace

Start the optional browser GUI with Python 3.9 or newer:

```sh
python -m soapypower.gui --repo /path/to/soapy_power --sessions /path/to/soapy_power/sessions
# Installed package entry point:
soapy_power_gui --repo /path/to/soapy_power
```

The server binds only to `127.0.0.1:8765`. Use `--port` to change the port or `--no-browser` to avoid opening a browser. Viewing saved sessions needs no native SDR bindings and does not probe a device. The frontend uses bundled assets, with no network service or frontend build dependency.

Acquisition and report generation use the existing macOS launchd runner. Install it using `tools/macos_runner/install.sh` as described in the runner documentation. If it is unavailable, the GUI remains a report viewer. Choose **Probe receiver** to explicitly queue a hardware probe. Do not run another capture process concurrently outside the runner.

The configuration dialog supports Ettus satellite, hopscan, full-range, baseline and experiment-suite workflows, and the bladeRF + XB-200 survey profile. Its settings map to the existing queue interface. RTL-SDR acquisition is not exposed by this runner. Some workflows run multiple captures: the dialog explains duration semantics. Antenna and termination selections document the actual physical connection; they do not switch physical loads.

Saved session history links to original reports and can queue reanalysis for superscan/hopscan sessions. Receiver quality shows the latest frame's delivered-IQ headroom, RFIC temperature, LO lock, continuity and RF validity. Unknown evidence remains unknown. These values do not establish calibrated absolute power, noise figure, or phase calibration.

The spectrum is a max-pooled relative PSD preview of the latest saved LO visit. Masked or missing samples remain gaps. The IF waterfall collects up to 60 visits while the page is open, preserves their actual acquisition time intervals and leaves unsampled time blank. Different rows may have different RF centers. It is not a full RF waterfall or a reconstruction of an entire historical scan; open the generated report for that analysis. The history resets when sample rate, bin count or gain changes. RF bin spacing is displayed separately from the FFT window's effective noise bandwidth.

Confirmed credited time and records come from saved manifests/reports. During acquisition a separate live span estimate may be shown; it is not yet qualified and may contain outages. Stop is offered only for supervisors that advertise stop-request support. A stop request terminates the child capture, validates and preserves its accepted prefix, and lets the runner generate a partial report. Stopping does not imply the requested duration was completed. Other workflow versions must finish through their existing controls.

Keep the GUI local. Its actions use typed validation, a per-process request token and local Host/Origin checks; report HTML runs in a sandbox. No transmit or firmware-writing controls are provided. Close the server with Ctrl-C; this does not terminate independently queued runner jobs.
