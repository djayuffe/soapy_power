# Capability-driven assisted and automatic calibration

`soapy_power_calibration assist` checks a configuration and discovers the actual
receiver. `auto` runs the selected measurement, validates it and writes a profile
only after its acceptance checks pass. Use the [complete measurement procedure](CALIBRATION_STEPS.md)
and [hardware guide](SDR_DEVICE_GUIDE.md) to establish physical source levels,
input limits, reference planes, matching, native preparation and uncertainty.

Three workflows are selected from the supplied evidence:

| Configuration | Automated measurement | Published result |
| --- | --- | --- |
| `source: null`, with a termination model | Fixed-state terminated captures at every grid coordinate | Baseline report only; no absolute profile |
| `source.type: tone`, with at least three known source levels | On/off captures at every RF/baseband/level, linearity checks at each grid point, then fresh holdouts at every declared level | Absolute measured power profile if all checks pass |
| `source.type: hot-cold`, with characterized source temperatures | Hot/cold grid and fresh independent hot/cold checks | Conditional thermal power profile; independent physical linearity confirmation is required |

No load, antenna or public transmission supplies a known absolute receiver gain
by itself. Known-NF, LNA characterization, passive overlap and equalization remain
available through their existing explicit commands; this assistant does not
silently substitute one of those models for missing reference measurements.

## Start with a plan

Copy a template and edit it for your actual receiver and measurement:

```sh
cp examples/calibration/auto-baseline.json my-calibration.json
soapy_power_calibration assist my-calibration.json --offline -O offline-plan.json
```

Offline planning does not open an SDR and always reports `unverified_offline`.
It checks numerical geometry and identifies family guidance; it cannot verify
capabilities, actual gain or accepted rates. To discover the live receiver:

```sh
soapy_power --detect
soapy_power -d 'driver=rtlsdr,serial=REPLACE' --info
soapy_power_calibration assist my-calibration.json -O live-plan.json
```

Live `assist` opens the selected RX and applies its proposed fixed gain,
correction policy and advertised secondary-control changes, without streaming.
It reports actual context, supported automatic corrections, gain-stage names,
clock/time choices, lock sensor names, applied changes and blockers. Repeating
`assist` requires a new output filename or explicit `--force`.

The supplied RTL-SDR selector, rate and gain are example values, not defaults for
all SDRs. Choose the actual serial/channel/antenna and supported gain settings.
Select explicit clock/time sources and master clock only when appropriate to the
physical reference connection. Record any external converter LO and IQ orientation.
A live ready plan means software preconditions are satisfied, not that RF accuracy
has been independently certified.

## Configure the receiver and acceptance limits

The version-1 JSON configuration has these fields; unknown fields are rejected.

| Field | Meaning |
| --- | --- |
| `receiver.device` | Explicit Soapy selector, including the actual unit where possible |
| `receiver.sample_rate`, `bandwidth`, `channel`, `antenna` | Requested RX path/rate/filter configuration; actual accepted rate is used for geometry and FFT math |
| `receiver.gain` | Required finite overall gain, or a mapping of advertised stage names to values; AGC is disabled |
| `receiver.settings` | String-to-string driver settings; secondary AGC/test/bias enable requests conflict with this workflow |
| `receiver.dc_correction`, `iq_correction` | `auto` selects automatic RX correction when advertised, otherwise preserves driver default; `default`, `on`, `off` are explicit alternatives |
| `receiver.clock_source`, `time_source`, `master_clock_rate`, `corr_ppm` | Optional existing capability-checked controls; PPS/clock selection does not create coherent multi-board timing |
| `receiver.lnb_lo`, `lnb_high_side`, `iq_conjugate` | Existing raw-IQ/RF mapping controls; false/zero by default |
| `rf_hz`, `baseband_hz` | Sorted unique axes; absolute calibration requires a rectangular grid with at least two points on each axis |
| `reference_baseband_hz` | One of the measured baseband offsets |
| `holdouts` | At least two distinct in-domain RF/baseband coordinates for absolute workflows; captured separately after the candidate is frozen |
| `reference`, `reference_plane` | Actual source identification/certificate/procedure and the named physical connector |
| `physical_setup_confirmed` | Set true only after the physical checklist below is completed; templates deliberately set false |
| `acquisition.fft_bins`, `segments`, `fft_overlap` | Defaults 4096, 64, 0.5; at least 32 complete Welch frames; requested blocks are computed from the actual framing rule |
| `acquisition.integration_width_hz` | Integration band centered on each reference RF, containing tone leakage/drift without unrelated signals |
| `acquisition.tune_delay`, `settle_samples`, `lock_timeout` | Minimum post-control delay, discarded samples and lock/settle timeout; defaults 0.1 s, 32768 samples and 2 s are starting values to verify |
| `acquisition.lock_sensor`, `reference_lock_sensor` | Optional names that must be advertised at the RX or motherboard scope |
| `acquisition.require_timestamps` | Optional strict hardware continuity gate; false by default, because many USB drivers cannot satisfy it |
| `acceptance` | Optional positive limits: `min_snr_db` 10, `min_y_db` 0.5, `max_abs_error_db` 0.3, `max_ripple_db` 0.3, `max_cw_residual_db` 0.1, `cw_slope_tolerance` 0.01 |

All frequencies/rates in JSON are numerical Hz; no `M` suffix. Reference bands
must stay at least two FFT bins away from DC and inside the central 90% of the
sampled Nyquist bandwidth. Geometry is rechecked against the accepted rate and
hardware tuning range. No automatic detrending, DC interpolation, extrapolation
or spectrum flattening is applied to reference evidence.

The grid is limited to 256 cells, holdouts to 64 coordinates, source levels to
16, FFT size to 262144 and frames to 10000 per capture. These bound planned work;
choose smaller measurements first and expand after verifying repeatability.
Frames overlap and are not independent statistical observations.

## Physical checklist before enabling a run

1. Identify the actual unit, RX port/channel and architecture. Perform any
   appropriate manufacturer native preparation with this application disconnected.
2. Verify input electrical limits, a conservative linear input ceiling, source
   levels, cables/attenuators/DC blocks and their frequency-dependent loss.
3. Define the reference plane and actual input impedance. High-impedance ADC ports
   must not be treated as 50 Ω by assumption. A 75 Ω termination on a nominal
   50 Ω input supplies a conditional mismatch model, not an absolute calibration.
4. Verify no DC reaches an unsuitable passive termination. This workflow disables
   advertised `biastee`/`bias_tee`; an external LNA must have separate appropriate
   power/DC isolation. Check unadvertised or persistent hardware bias separately.
5. Establish thermal stability, source switching that preserves match, fixed
   RF/IF/digital gain and stable sample scaling, including any OS/remote path.
6. For hot/cold, independently confirm linear operation at both source states,
   correct source temperatures/ENR interpretation, match and image/alias response.
7. Fill the measured source values and set `physical_setup_confirmed: true`.
   A JSON flag records your confirmation; the software cannot inspect a cable,
   prove a source certificate or independently measure an unreported gain loop.

## Known-tone source

Use [auto-tone.json](examples/calibration/auto-tone.json). Fill its deliberately
null source fields with measurements:

```json
{
  "type": "tone",
  "levels_dbm": [-80, -70, -60],
  "max_receiver_input_dbm": -40,
  "path_loss_db": [[100000000, 10.2], [101000000, 10.4]]
}
```

These are syntax examples, not universal safe or calibrated values. Levels are
known **source-plane** dBm; loss is subtracted to obtain receiver-plane power.
`max_receiver_input_dbm` is your verified conservative operating ceiling, not the
damage limit from a datasheet. Every planned level is checked against it after
path loss. Path loss may be a nonnegative scalar or an RF table covering all grid
and holdout frequencies; interpolation is linear and extrapolation is rejected.

At every grid coordinate, capture on/off for each of 3–16 source levels spanning
at least 1 dB. Noise-subtracted power must satisfy SNR, slope and residual gates.
The middle listed level supplies that cell's profile measurement. Fresh holdouts
are then captured at **all** declared levels. Holdout powers are converted to
reference-plane dBm exactly once, and compared to the frozen response. A failed
cell stops the session; levels and outliers are not silently dropped or retuned.
Change the physical setup/gain and start a new session after investigating.

## Characterized hot/cold source

Use [auto-hot-cold.json](examples/calibration/auto-hot-cold.json). Fill cold/hot
source temperatures in kelvin, matched source/receiver impedances, passive path
loss and path temperature. Temperatures can be scalars or increasing RF/value
tables covering every requested point. Derive hot temperature from calibrated
ENR using its stated reference temperature and actual cold temperature; do not
assume an uncharacterized noise head has known ENR.

Set `source.linearity_confirmed: true` only after independent verification. The
assistant cannot infer analog linearity from hot/cold captures alone. Passive
path thermal noise is included by the existing temperature model. Y-factor and
nonnegative receiver-temperature gates apply at every grid point. Independent
hot/cold holdouts compare the inferred offsets with the frozen candidate and
apply offset-error/ripple limits. These checks remain conditional on the thermal,
match, alias/image and fixed-gain assumptions; they are not a complete uncertainty
budget or an independently measured noise-source calibration.

## Run in guided mode

```sh
soapy_power_calibration auto my-calibration.json --directory calibration-session-001
```

For tone/hot-cold guided runs, use a real terminal. Before each capture the assistant prints the requested
source state, RF and source-plane tone level. Set the generator/switch/termination,
wait for its physical settling, then type **READY**. Any other response cancels.
Receiver settling/discard/lock checks run after the source confirmation and tuning.
The assistant does not operate a generator in guided mode.

A baseline session needs no generator or switching: after the configured passive
termination and `physical_setup_confirmed` have been verified by the operator,
`auto` collects the entire baseline unattended, including without a terminal.
It records that the termination state comes from operator configuration, not an
electronic load sensor. An explicit source hook can still control a load switch.

`terminated` means connect the specified load at the reference plane; `on`/`off`
mean the known CW state and its impedance-preserving off state; `hot`/`cold` mean
the characterized noise-source states. The source must hold its confirmed state
for the whole capture. Source switching alone does not prove its output match.

## Fully automatic source control

Supply an executable that actually controls your source or RF switch:

```sh
soapy_power_calibration auto my-calibration.json \
  --directory calibration-session-002 --source-hook /absolute/path/to/source-controller \
  --hook-timeout 30
```

The executable is selected explicitly on the CLI, never loaded from configuration.
It runs without a shell. Each invocation receives one JSON object on stdin:

```json
{"version":1,"request_id":"unique-token","state":"on","rf_hz":100000000.0,"input_dbm":-70.0}
```

It must apply the state, wait for its own verified settling, and return that same
object with `"confirmed": true`. Echo all fields with their exact values/types;
request IDs prevent stale acknowledgments. `input_dbm` is null outside tone-on.
Use `terminated`, `on`, `off`, `hot` and `cold` as appropriate. A hook that merely
echoes requests does **not** establish physical state; do not use an echo stub
with hardware. Hook implementation is instrument-specific and must respect its
source limits, calibrated output/path and synchronous state-holding contract.

Nonzero exit, timeout, malformed/stale replies, an executable changed during the
run or replies larger than 64 KiB stop calibration. Source hooks have a timeout;
SDR/driver and blocking output operations retain the [soft real-time limits](RUNTIME_AUDIT.md).
The CLI handles Ctrl-C/SIGTERM as interruptions so waiting prompts and captures
can enter cleanup. Driver calls still must return before Python can handle a
signal. On a failed active/unknown state, the assistant attempts an explicit hook `off`
(or `cold`) command and records any cleanup failure. In guided mode it reports
that the operator must set the source safe. No implementation can guarantee a
physical source switched off after a communication or power failure.

## Device-family decisions

Actual advertised capabilities and readbacks take priority over model labels:

- RTL-SDR: disable advertised digital/IF AGC and test mode, verify bias off; retain
  tuner/mode guidance, including the V3/V4 HF distinction. [Manufacturer guide](https://www.rtl-sdr.com/v4/).
- Airspy R2/Mini: preserve tuner/gain/decimation state. HF+ is treated separately;
  its embedded digital gain prevents an assumed fixed-gain absolute auto profile.
  Baseline diagnostics remain available. [HF+ architecture](https://airspy.com/airspy-hf-discovery/).
- SDRplay: preserve port, IF/decimation/HDR/notch state and inspect actual
  gain-reduction/LNA controls. Do not assume all interfaces or input impedances match.
- HackRF: preserve stage allocation and distinguish One from Pro; source-level
  tests check the measured operating range without assuming model-wide headroom.
- bladeRF, Lime and AD936x/Pluto: enable only advertised automatic RX DC/IQ modes,
  preserve RFIC/path/filter state and require measured references. No universal
  manual coefficients or undocumented native calibration commands are generated.
- Ettus: keep actual daughterboard/subdevice/channel and clock context. Native
  UHD self-calibration and X420 utilities differ; they may involve TX leakage and
  are left to the applicable manufacturer procedure, not invoked by `auto`.
  [UHD calibration manual](https://files.ettus.com/manual/page_calibration.html).
- Direct-ADC, Hermes, audio-IQ, remote and unknown devices: provide architecture
  guidance and require observable fixed state; never infer impedance or RF volts
  from a CF32 stream. A detectable HF+ behind a remote selector retains its
  absolute-calibration restriction. Unknown remote/OS scaling remains an operator
  verification requirement, not something a family string can certify.

Readbacks, rate/domain checks and measured acceptance still decide whether a
session can publish. A driver advertising a feature does not guarantee a working
setter; failed control application/readback stops the workflow.

## Evidence and application

Every run uses a **new** directory. Existing sessions are never overwritten or
resumed automatically. The session contains raw JSONL captures, state events,
configuration hash, capability/context plan, completed measurement manifests,
linearity/holdout results and final status. Failed sessions retain evidence and
an error, but do not publish a usable profile. Capture/configuration mutation
before publication is rejected. Hooks and raw captures have provenance hashes.

Successful absolute sessions additionally contain `profile.json` with the measured
response, auto-workflow provenance and a hash of the independent validation.
Use `soapy_power_calibration validate calibration-session-001/profile.json`, then
apply with `--calibration` only to the same fixed context and covered RF/baseband
bins. The small example grid does not cover an entire wide FFT automatically.
Baseline sessions deliberately omit `profile.json`.

Keep the session directory with its profile. Complete the physical uncertainty
budget and recurring reference checks before relying on an absolute measurement.
Automated software acceptance is not factory calibration, RF certification,
phase equalization or proof of performance on an untested hardware unit.
