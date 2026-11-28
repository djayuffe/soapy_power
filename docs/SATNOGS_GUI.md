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
is marked as clipped. The pass search samples 30-second intervals; bracketed elevation-mask crossings are refined to 0.5-second intervals. Clipped window edges remain explicit; numerical refinement is not a TLE accuracy guarantee.
The catalog search includes satellite name, NORAD number, frequency and mode.
Inactive/dead transmitters and invalid or mismatched TLE catalog numbers are excluded.
Expired TLEs remain visible as stale counts and cannot prepare a target scan.

**Prepare scan** opens a populated Workbench plan for the selected **Target duration** (1, 5, 10 or 30 minutes). It does not start reception.
The frequency envelope includes the nominal downlink range, Doppler allowance
and a mode-based width estimate for a single-frequency entry. Review device,
antenna, gain, duration and geometry, then use **Validate and preview** and
**Queue reviewed plan**. The initial receiver preset is B200/UHD; other receiver
capabilities must be selected explicitly and are checked by the native acquisition.

Target scans preserve the NORAD/transmitter ID, catalog content hash and a hash of the reviewed WGS84 observer geometry.
Immediately before acquisition, the worker checks that the cache still matches
the reviewed target, freezes database/observer files into `reference/`, verifies
the frozen hash, and writes `satellite-target.json`. A changed cache, changed observer or failed
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
5000 catalog entries, 256 pass segments and an 8 MiB worker response. Narrow the
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

## Shared UTC and flat world map

The header clock is the shared **orbital prediction clock**. Live UTC is anchored
against `/api/health` every 30 seconds, with half the observed round-trip time
shown as transport uncertainty. This is not an absolute UTC error bound: the
server host must itself have a correct system clock. A monotonic browser timer
advances the last anchor, so changing the browser wall clock does not jump an
orbit. Synchronization older than 90 seconds is explicitly marked unsynchronized.

Live UTC, playback, speed and scrubbing drive both the polar plot and flat
world map. Playback stops at the selected segment end even when the plot is off
screen. A hidden tab pauses simulation elapsed time; live UTC catches up when
shown again. Return to **Live UTC** to use refreshed geometry. A new RF range or
observer clears the old geometry immediately. Playback retains its observer,
catalog and geometry snapshot across background updates.

The equirectangular world map is bundled locally with public-domain Natural
Earth 1:110m land polygons; no map tiles or observer coordinates are sent to a
map service. Earth-fixed SGP4 positions are converted to WGS84 latitude,
longitude and altitude. A cyan segment shows the selected local pass; a muted
cyan path shows one orbital revolution from the prediction start (up to 24 h).
Longitude crossings split at the map edges instead of drawing a line across
the world. The observer is an amber square. The dashed footprint uses a
spherical Earth and the observer's elevation mask and is approximate; local
azimuth/elevation uses the WGS84 observer geometry.

Global position tracks span 30 seconds before to 150 seconds after each
prediction anchor, at 15-second intervals. They include below-horizon positions
and remain available without an observer. Up to **512 eligible satellites** in
the selected RF range are drawn, prioritizing satellites with local passes;
the limit is disclosed. Clicking a map dot selects its live geometry; **Live satellite** also provides
keyboard selection of global tracks and returns the shared clock to live UTC. The pass
selector and **View pass** buttons provide keyboard-accessible local pass
selection. Current positions are never extrapolated outside the track; expired
geometry is reported rather than held out as realtime. During distant pass
playback, unrelated short rolling tracks may be unavailable.

Compute remains one CPU-only worker, with a 30-second deadline and an **8 MiB**
UTF-8 response budget. These views neither operate an antenna rotator nor tune,
start, pause or stop reception. RF telemetry and saved spectrum annotations
continue to use acquisition timestamps and frozen references, independently of
the orbital simulation clock.


## Selection, reconnect and review workflow

Use **Live satellite** for current geometry. The catalog's **Track live** action
pins a NORAD number into the next prediction request, giving that satellite
priority in the bounded propagation/map set. It does not remove the RF-range or
TLE-age policy. A stale catalog entry is labeled and cannot prepare or select a
live target. Narrow the RF range if the displayed catalog itself is truncated.

Choose **Downlink reference** to inspect an individual transmitter's predicted
receive frequency and Doppler. The selected downlink's **Prepare selected
downlink** action uses the target duration and opens Workbench for review; it
does not schedule an upcoming pass or operate the receiver. Device, gain and
frequency coverage still require a reviewed Workbench plan. AOS/LOS countdowns
refer to the displayed sampled segment, including explicit clipped boundaries.

Pass search accepts a name or NORAD number. The table displays 24 segments per
page, with previous/next controls and a matching count. The full bounded set
remains available to the sky view and its pass selector. Viewer RF range,
look-ahead window, overlay toggles and target duration are remembered locally
when browser storage is available. Playback and observer drafts are not saved
in browser storage.

The prediction connection badge is separate from RF receiver state. Failed
requests show the age of the last response, retry with a bounded backoff and
can be restarted with **Reconnect predictions**. Returning to a visible tab or
restoring connectivity refreshes the predictions. Changing RF range, selected
recording or observer aborts the old request; stale errors and responses cannot
replace a newer context. Geometry outside its time interval is never held out
as a current satellite location.

Clicking a world-map satellite during playback returns all orbital views to
live UTC and selects current geometry. Playback's observer mask, transmitter
catalog and pass geometry remain bound to its snapshot. Selecting a live
satellite with a displayed local pass retains AOS/LOS and playback controls.

Scan preparation now binds observer geometry as well as catalog content. A
queued target prepared before this binding was introduced must be prepared and
reviewed again. Both the current observer and frozen observer are checked before
RX; changing unrelated QTH metadata does not change the geometry identity.
Concurrent observer-form saves are serialized in the browser; edits made while
a save is pending remain marked as unsaved.

Corrupt frozen recording references suppress frame annotations while current
live planning remains usable. Malformed transmitter rows are excluded without
discarding other valid entries. Identical in-flight prediction requests are
coalesced into one CPU job; all cache files participate in cache invalidation.

The crossing refiner bisects a verified above/below-mask bracket. It retains the
coarse boundary if finite geometry does not bracket a crossing, rather than
inventing an event. Short one-sample segments can receive playable geometry when
both crossings are bracketed. No event is refined across the requested window
boundary. The displayed peak remains a sampled maximum.
