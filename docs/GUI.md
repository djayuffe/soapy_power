# Local receiver workspace

Start the optional browser GUI with Python 3.9 or newer:

```sh
python -m soapypower.gui --repo /path/to/soapy_power --sessions /path/to/soapy_power/sessions
# Installed package entry point:
soapy_power_gui --repo /path/to/soapy_power
```

On macOS the installed runner can start it detached from any shell or remote session: `bash tools/macos_runner/queue.sh scan --gui start --wait 60` (and `--gui stop`; `--gui-port N` for another port). It runs the repository's GUI with the runner's Python, writes `sessions/gui.pid` and `sessions/gui.log`, reports an already running server instead of starting a second one, and never opens the SDR.

The server binds only to `127.0.0.1:8765`. Use `--port` to change the port or `--no-browser` to avoid opening a browser. Viewing saved sessions needs no native SDR bindings and does not probe a device. The frontend uses bundled assets, with no network service or frontend build dependency.

Acquisition and report generation use the existing macOS launchd runner. Install it using `tools/macos_runner/install.sh` as described in the runner documentation. If it is unavailable, the GUI remains a report viewer. Choose **Probe receiver** to explicitly queue a hardware probe. Do not run another capture process concurrently outside the runner.

The configuration dialog supports Ettus satellite, hopscan, full-range, baseline and experiment-suite workflows, and the bladeRF + XB-200 survey profile. Its settings map to the existing queue interface. RTL-SDR acquisition is not exposed by this runner. Some workflows run multiple captures: the dialog explains duration semantics. Antenna and termination selections document the actual physical connection; they do not switch physical loads.

Saved session history links to original reports and can queue reanalysis for superscan/hopscan sessions. Receiver quality shows the latest frame's delivered-IQ headroom, RFIC temperature, LO lock, continuity and RF validity. Unknown evidence remains unknown. These values do not establish calibrated absolute power, noise figure, or phase calibration.

The spectrum is a max-pooled relative PSD preview of the latest saved LO visit. Masked or missing samples remain gaps. The IF waterfall collects up to 60 visits while the page is open, preserves their actual acquisition time intervals and leaves unsampled time blank. Different rows may have different RF centers. It is not a full RF waterfall or a reconstruction of an entire historical scan; open the generated report for that analysis. The history resets when sample rate, bin count or gain changes. RF bin spacing is displayed separately from the FFT window's effective noise bandwidth.

Confirmed credited time and records come from saved manifests/reports. During acquisition a separate live span estimate may be shown; it is not yet qualified and may contain outages. Abort is offered only for supervisors that advertise stop-request support. An abort request terminates the child capture, validates and preserves its accepted prefix, and lets the runner generate a partial report. Aborting does not imply the requested duration was completed. Other workflow versions must finish through their existing controls.

The satellite workflow behind the scan dialog (band plans, rate pilot, gain selection, supervision, analysis and identification rules) is described in [SUPERSCAN.md](SUPERSCAN.md).

Keep the GUI local. Its actions use typed validation, a per-process request token and local Host/Origin checks; report HTML runs in a sandbox. No transmit or firmware-writing controls are provided. Close the server with Ctrl-C; this does not terminate independently queued runner jobs.

## Pause, resume, abort and retry

Controls are driven by the selected session's advertised capabilities. Updating this checkout does not retrofit controls into a supervisor already running. New supervised satellite captures advertise `control_version: 1`; older stop-aware supervisors offer Abort only. Pilots/calibration before the supervisor starts, and other workflows, do not offer live pause/retry. Abort is a capture operation; closing the GUI server is not an abort.

| State | Available operation | Result |
| --- | --- | --- |
| Running, control v1 | Pause | Terminate and reap the RX child, validate its accepted prefix, acknowledge the request and enter Paused. |
| Paused, control v1 | Resume | Reopen RX in a new attempt, preserving current gain and remaining accepted-time budget. |
| Running or Paused, control v1 | Retry attempt | Preserve accepted records and reopen RX in a new attempt. A paused session becomes Running. |
| Running or Paused, stop-aware | Abort | End capture with `cancelled` state / exit 130, preserve accepted records and run available partial reporting. |
| Finished, idle runner, supported original request | Repeat scan | Queue a fresh session using the original requested settings and duration. No previous evidence is overwritten. |
| Finished superscan/hopscan, idle runner | Generate report | Rebuild analysis from the saved data, without acquiring RF. |

Pause closes the acquisition stream rather than suspending a process with live USB buffers. The supervisor stays alive and reserves the runner. Time spent paused is recorded separately and does not count toward qualified scan time. Resume starts a new partition; it cannot guarantee phase continuity or a coherent RF timeline across the interruption. Automatic gain backoff already applied by the supervisor is preserved on resume/active retry. Repeat scan instead uses the original **requested** configuration and reruns the normal pilots/gain selection where configured.

Pause, Resume and Retry requests have unique IDs and are acknowledged in `captures.json`. The GUI shows a pending message and disables conflicting requests until acknowledgment. Expand Control history to inspect acknowledged operations and their accepted-time totals. Abort takes priority even when another request is pending. The supervisor checks requests while acquiring, while paused and during retry backoff; terminating a nonresponsive child can require the bounded five-second termination grace period. Prefix validation and reporting may take longer with large files. Do not infer that a click has completed the transition until the acknowledged state appears.

The accepted prefix of every interrupted attempt remains separate, with original raw/log files preserved. Complete but malformed or unqualified records still fail validation. Manual boundaries do not spend the automatic attempt budget, do not lower gain automatically, and do not credit unsampled gaps. A trailing interrupted JSON fragment is retained in the raw file and excluded from the accepted partition.

For new runner sessions, `request.plist` records the typed acquisition request. For older sessions, Repeat scan can recover a matching archived runner request through its status record. Custom plan arguments, overflow experiments, unsupported options, missing requests and unsafe paths are rejected rather than approximated. Repeat uses the full requested duration, not the previous remaining duration. Check the confirmation summary and physical RF connection before starting it. The backend refuses new queued work while the runner is busy.

## Troubleshooting

- **Read-only view:** verify the runner installation and selected `--repo` / `--sessions` paths. Hardware probing is explicit.
- **No Pause or Retry attempt:** the selected capture does not advertise the new protocol, or it has finished. Start a new supervised satellite scan to use these controls.
- **Waiting for acknowledgement:** the supervisor is closing/validating an attempt, or may no longer be alive. Inspect the acquisition log and runner status; the GUI never kills an arbitrary PID to force a transition.
- **No Repeat scan:** original supported settings are unavailable. Use New scan to configure an explicit request.
- **Report failed after Abort:** there may be no qualified records to report. Inspect the retained log and evidence; cancellation does not turn an empty capture into a valid measurement.
- **Browser kept open across a server restart:** reload for updated assets. Existing frontend versions may have an expired request token; the current frontend can refresh a changed token before retrying a request rejected with HTTP 403.

## Developer/API contract

All POST endpoints require the local Host, a matching Origin when provided, `Content-Type: application/json` and the per-process `X-CSRF-Token` from the workspace page. `/api/pause`, `/api/resume`, `/api/retry`, `/api/abort` and the compatibility alias `/api/stop` accept exactly `{"session":"superscan-YYYYMMDD-HHMMSS"}`. Only `/api/retry` can queue a fresh acquisition for a finished session; pause/resume/abort never enqueue hardware work. Validation errors use HTTP 400; pending/busy conflicts use 409. A successful control response means the request is durably written, not that the hardware transition has already finished.

The supervisor reads an atomically published `control-request.json` with version 1, a 32-character lowercase hexadecimal ID and action `pause`, `resume` or `retry`. `captures.json.control_ack` identifies the last acknowledged command, and `controls` records acknowledgments and credited time, including the final abort event. Abort events have a null request ID; `control_ack` continues to identify the last protocol command. Abort uses the existing sticky `stop-request.json`, which takes priority. These files are local operator control, not an API for unrelated programs to modify during a scan.
