# SatNOGS in the receiver console

The **SatNOGS** navigation button opens the satellite operations panel. The same
panel is linked from **Configure a scan**. It integrates the existing SatNOGS
catalog, SGP4 orbit prediction, observer geometry and immutable scan references.
This is receive-only planning and annotation. It does not decode frames, upload
observations to SatNOGS Network, confirm satellite identity or change raw DSP data.

## Data and observer

**Update SatNOGS data** downloads satellite, transmitter and TLE records from the
[official public API](https://docs.satnogs.org/projects/satnogs-db/en/stable/api.html).
API data are attributed to SatNOGS DB under CC BY-SA. CelesTrak remains the TLE
fallback if SatNOGS has no usable elements. A valid SatNOGS TLE response avoids
unnecessary fallback requests. Sources and fetch time remain in `satdb/meta.json`.

Updates use a separate CPU/file-only process and a cross-process lock. They do
not acquire the receiver, wait behind an RF capture, or run device discovery.
Only a complete generation is published atomically. Failed updates preserve the
previous generation. The worker records durable status in `satnogs-refresh.json`
and diagnostics in `satnogs-refresh.log`; interrupted workers are distinguished
from live updates. API URLs are fixed by the program, not supplied by the browser.

Set the WGS84 observer latitude, longitude, altitude and minimum elevation in the
local observer form. Existing observer metadata is preserved, and changes retain
a private backup. Coordinates remain on this computer and are never included in
remote satellite API requests. An observer is necessary for pass and Doppler
prediction; cached transmitter frequencies remain catalog information.

## Passes and scan preparation

Choose an RF range and a 30-minute, one-hour or two-hour look-ahead window.
The table lists upcoming visible pass segments, approximate rise/set times,
peak elevation and active downlinks/modes. Separate horizon crossings stay
separate; a pass already in progress or extending beyond the requested window
is marked as clipped. Timing is sampled at 30 seconds, not a precision event solver.
The catalog search includes satellite name, NORAD number, frequency and mode.
Inactive/dead transmitters and invalid or mismatched TLE catalog numbers are excluded.
Expired TLEs remain visible as stale counts and cannot prepare a target scan.

**Prepare 5 min** opens a populated Workbench plan. It does not start reception.
The frequency envelope includes the nominal downlink range, Doppler allowance
and a mode-based width estimate for a single-frequency entry. Review device,
antenna, gain, duration and geometry, then use **Validate and preview** and
**Queue reviewed plan**. The initial receiver preset is B200/UHD; other receiver
capabilities must be selected explicitly and are checked by the native acquisition.

Target scans preserve the NORAD/transmitter ID and catalog content hash.
Immediately before acquisition, the worker checks that the cache still matches
the reviewed target, freezes database/observer files into `reference/`, verifies
the frozen hash, and writes `satellite-target.json`. A changed cache or failed
snapshot stops the job before its RX subprocess starts. Editing frequency or
explicit centers in Workbench clears the attached target declaration.
The existing supervised satellite-band workflows retain their native reference
snapshot and report analysis; they are available alongside these target plans.

## Spectrum and waterfall annotations

Predicted downlinks appear as dashed cyan spectrum markers and small cyan ticks
on measured waterfall visits. The caption shows expected receive frequency,
Doppler shift, azimuth and elevation. Nothing is connected across missing visits
or drawn into masked waterfall bins. Markers never remove data or change the
anti-spur classifications, headroom guards or coverage planner.

Predictions use the midpoint of each acquisition interval. Historical frames use
their frozen SatNOGS/TLE/QTH reference, checked against its bundle hashes. A missing
or corrupted frozen reference is reported and never silently replaced with the
current cache. Legacy recordings without a snapshot are labeled as using the
current cache. Current-time pass planning uses the current cache and observer
independently of the selected recording.

SGP4 propagation is limited to the configured TLE age policy (14 days). Short
Doppler/angle curves are sampled every five seconds and interpolated without
extrapolation. Azimuth interpolation handles the 0°/360° wrap. An out-of-window
frame, different selected session, below-mask geometry or invalid reference
suppresses its markers. Receding range rate shifts receive frequency downward.
No TLE error bound or receiver frequency calibration is invented.

Orbital work runs in one background child with a 30-second execution limit,
a bounded four-context cache and at most one pending request. HTTP/RX control
remains responsive. Bounds are explicit: at most 4096 propagated satellites,
5000 catalog entries, 256 pass segments and a 4 MiB worker response. Narrow the
RF range if truncation is reported. These are reference predictions, not a
measurement of transmitted activity or proof that a detected line is a satellite.

## Validation

Tests cover split pass segments, inactive and stale elements, TLE identity,
frozen-versus-current references, corrupted snapshots, changed-target rejection,
observer backups, asynchronous worker caching, refresh failure preservation,
Doppler sign, azimuth wrapping and stale/session-mismatched marker suppression.
RF hardware is not opened by these tests. Physical reception and decoded identity
require separate evidence.

## Polar sky view and pass playback

The Gpredict-style **Sky view** displays azimuth clockwise from North (North up,
East right), with the horizon at the outer circle and zenith at the center.
Elevation rings show 0°, 30° and 60°, and the amber ring shows the observer's
minimum elevation. The selected pass is cyan; the start/end markers denote
AOS/LOS or the displayed window boundaries when the pass is clipped.

Select **View pass** in the pass table or choose the pass dropdown. **Play pass**,
**Pause playback**, **Restart pass**, the time slider and 1×–300× speed controls
animate the predicted position. **Live UTC** returns to the current clock.
All sky-view clocks and chart axes use UTC. The side panel shows azimuth,
elevation, slant range, signed range rate, Doppler for the first listed downlink,
and predicted pass timing/peak. The lower chart shows elevation and a time cursor.
Other predicted visible satellites can be toggled or selected by clicking a dot;
the dropdown provides the keyboard-accessible equivalent.

Playback holds a prediction snapshot so a background refresh does not move the
trajectory under the slider. A catalog, observer or RF-range change replaces that
snapshot. The simulation changes only the sky view; receiver settings and recorded
spectrum timestamps remain unchanged. Historical spectrum overlays still use
the recording's own frozen reference, independently of this current-observer sky view.

Displayed paths are refined with SGP4 at roughly five-second intervals, capped at
96 points per pass for long windows. Refinement applies only to the displayed
256-segment budget. These sampled paths are interpolated without extrapolation;
short single-sample segments have no playable geometry. Predictions are not an
antenna rotator command or confirmed reception. Animation is limited to about
12 frames/second and pauses its playback clock when the tab or map is hidden.
