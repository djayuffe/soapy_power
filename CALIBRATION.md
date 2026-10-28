# Practical SDR calibration: start simple and scale out

Use [OPERATIONS.md](OPERATIONS.md) for basic acquisition, [RADIO_THEORY.md](RADIO_THEORY.md)
for the estimator and [REMEDIATION.md](REMEDIATION.md) for integrity/version changes.
All new workflow commands work offline on JSON captures and never operate a
source, switch RF connections or turn a bias tee on automatically.

## Choose the simplest method that answers the question

| Equipment | What the software can establish | What remains unknown |
| --- | --- | --- |
| Receiver and a room-temperature RF terminator | Repeatable raw baseline, spurs, change after connecting an antenna, ideal thermal/mismatch calculation | Absolute gain and receiver noise cannot be separated from this one observation. |
| A termination and independently measured receiver noise figure | Conditional absolute scale from the specified temperature/NF model | Any error or frequency/impedance dependence of the supplied NF transfers into the calibration. |
| Known CW source and suitable attenuation | Absolute tone-power scale, measured across RF and baseband, with source-off noise subtraction | Source/path accuracy, linearity and interpolation still need verification. |
| Characterized hot/cold noise source or calibrated ENR head | Gain scale and equivalent receiver noise temperature/NF from two levels | Source match, image/alias response, path losses, stationarity and statistical uncertainty. |

A known tone with measured attenuation is usually the most direct next step
after the simple terminated baseline. A generic advertised RTL-SDR noise figure
is not a per-unit calibration. A cheap uncharacterized noise generator can show
relative response, but does not supply a known ENR for absolute calibration.

## RTL-SDR and a 75 ohm terminator

Use a shielded RF termination specified for the frequency, not an open connector
or an antenna assumed to be at room temperature. A passive termination is a
source of available thermal noise `kT W/Hz`. At 290 K this is about
`-173.975 dBm/Hz`, before receiver-added noise. The exact SI Boltzmann constant
is `1.380649e-23 J/K` ([NIST](https://www.nist.gov/si-redefinition/kelvin/kelvin-boltzmann-constant)).

A 75 ohm load connected to an *ideal purely resistive* 50 ohm input transfers
`4*75*50/(75+50)^2 = 0.96` of available noise power, a 0.1773 dB mismatch loss.
Real dongle input impedance varies with frequency and tuner state. This number
is a circuit-model calculation, not a measured correction for an RTL-SDR. The
receiver's own noise can also change with source impedance. Prefer a termination
matched to a characterized input for absolute work; do not subtract a universal
0.1773 dB from all antenna measurements.

Before attaching a passive termination, disable the bias tee through the actual
supported driver setting and verify it is off. DC power heats the termination
and changes both its temperature and the measurement. RTL-SDR Blog V3/V4 bias
tees are software-selectable; do not assume an adapter blocks DC
([manufacturer guide](https://www.rtl-sdr.com/v4/)). Use a suitable DC block if
needed and account for its RF loss/match.

Check `soapy_power --detect` and `soapy_power --info`. Install a driver appropriate
to the actual dongle; V4 requires compatible drivers. Disable tuner AGC with a
fixed `-g`/`-G` setting and disable **digital AGC** separately. The SoapyRTLSDR
implementation advertises `digital_agc`, `direct_samp`, `iq_swap` and, depending
on the build, `biastee`. Only request settings advertised by your installed
version ([driver implementation](https://github.com/pothosware/SoapyRTLSDR/blob/master/Settings.cpp)).
All advertised setting readbacks are now included in acquisition context;
enabled digital/IF AGC or test mode is rejected by calibration.

For a driver advertising the settings below, an example raw capture is:

```sh
soapy_power -d driver=rtlsdr -f 100M -r 2M -b 4096 -g 20.7 -n 256 -u 4 \
  --device-settings digital_agc=false,biastee=false --settle-samples 32768 \
  --partial-policy reject -l -F json -O terminated.jsonl
soapy_power_calibration baseline terminated.jsonl \
  --temperature-k 293.15 --source-ohms 75 --receiver-ohms 50 \
  --start-hz 99900000 --stop-hz 100100000
```

Change frequency, supported rate/gain and temperature to actual values. Avoid DC,
filter edges and identifiable internal spurs in the selected integration band.
Repeat the identical capture configuration with the antenna attached, using a
new output filename:

```sh
soapy_power_calibration compare terminated.jsonl antenna.jsonl \
  --start-hz 99900000 --stop-hz 100100000
```

This reports relative integrated power and signed excess raw power. A negative
excess is retained, not clipped into a fictitious signal. Antenna mismatch,
external noise/interference and receiver noise changes all contribute; this is
not an absolute antenna temperature or a gain calibration.

## Build a measured grid from captures

Copy an appropriate template from [examples/calibration](examples/calibration).
Its frequencies and offsets are example geometry, while physical source values
are deliberately null. Fill measured values, source identification and actual
capture paths. Paths are relative to the manifest file. Do not use a template
as a physical calibration without performing the measurements.

Each cell describes an actual RF frequency `rf_hz` and **raw IQ baseband** offset
`baseband_hz`. Tune so that the cell's RF falls at that offset; for an ordinary
noninverted receiver without an external LO, `tune = RF - baseband`. Capture
`on/off`, `hot/cold`, or `cold` files at that same tune and configuration. The
workflow checks coordinates against actual tuning metadata, including external
LO side and IQ conjugation. Every cell needs an explicit narrow integration band
`[start_hz, stop_hz)` containing `rf_hz`, without masked or estimated bins.

Use fixed gains, no detrending or DC interpolation, and `--partial-policy reject`.
Each file must contain repeated acquisitions of one tuned grid, not a sweeping
scan. The default minimum is 32 complete Welch segments; this is a sample-amount
gate, **not** an independent-average count or confidence bound. Capture multiple
runs to check stability. Source switching and reconnection are manual: allow
settling, retain the same settings, and verify no overload/compression.

The grid must have at least two RF frequencies and two baseband offsets and be
rectangular. Choose offsets spanning the retained usable passband. Include the
`reference_baseband_hz` explicitly. All intended calibrated bins must lie inside
the measured RF/baseband domain. A narrow band gives a local response estimate;
verify it is sufficiently flat, sample additional offsets and validate at
independent frequencies/levels. The builder does not infer response in a notch,
clipped band, image or alias.

```sh
soapy_power_calibration from-captures my-tone-manifest.json -O calibration.json
soapy_power_calibration validate calibration.json
```

Use `--calibration calibration.json` with the matching receiver configuration.
The profile records the method, manifest hash, capture hashes and per-cell
results. Old manually measured CSV profiles remain supported through `build`.
Capture averaging is weighted by complete segment count in **linear density**,
then integrated using bin-support intersections; it does not average dB values.

### Tone method

Start with [tone.json](examples/calibration/tone.json). Supply `input_dbm` at the
source plane and `path_loss_db` to the stated receiver input reference plane.
The effective injected level is their difference. The on/off captures are
integrated over the same band and source-off power is subtracted in linear
units. The net result must be positive and satisfy `min_snr_db` (default 10 dB,
net signal divided by off-state band noise). Ensure the band includes the tone's
window leakage; widening it must not materially change the net tone result.

Repeat cells at several input levels and compare correction offsets to detect
compression. Repeated grid cells require `max_spread_db`, an explicit acceptance
limit. Accepted cells average linear response normalized to known source power.
Do not replace a measured source level with the signal generator's nominal label
unless its accuracy and path loss meet the stated measurement requirements.

### Hot/cold or ENR method

Start with [hot-cold.json](examples/calibration/hot-cold.json). Provide effective
source temperatures or measured cold physical temperature and calibrated ENR at
each RF point. With ENR, the source-plane hot temperature is
`Th = Tc + 290*10^(ENR_dB/10)`; cold temperature is not silently set to 290 K.
For a matched passive path with loss `L >= 1` at physical temperature `Tp`,
`T_receiver = T_source/L + Tp*(1-1/L)`.

For mean raw densities `qh` and `qc`, the implementation uses
`G_raw = (qh-qc)/(k*(Th-Tc))` and
`Te = (Th-Tc)*qc/(qh-qc) - Tc`.
It reports `NF = 10*log10(1+Te/290)` and the gain-scale correction. It rejects
nonpositive differences, negative inferred `Te`, and Y below `min_y_db`
(default 0.5 dB). Small Y amplifies errors: a room-temperature load and a slightly
warmer load often provide inadequate contrast on a noisy dongle.

Set `assumptions_confirmed: true` only after checking equal source impedance in
both states, stable receiver gain/noise, linear operation and unwanted image or
alias-band response. Broadband noise can measure a different effective response
from a single-sideband CW signal if image noise enters the receiver; verify or
suppress it before treating this profile as a tone-power calibration. These are
standard limitations of noise-source methods
([Keysight](https://www.keysight.com/us/en/assets/7018-06829/application-notes/5952-3706.pdf),
[Analog Devices](https://www.analog.com/en/resources/technical-articles/system-noisefigure-analysis-for-modern-radio-receivers.html)).

### Terminator plus independently known noise figure

Start with [known-nf.json](examples/calibration/known-nf.json). Supply an actual
applicable `noise_figure_db` for each cell. The workflow computes
`Te = 290*(10^(NF/10)-1)` and derives scale from `qc = G_raw*k*(Tc+Te)`.
This is **conditional on the supplied NF**, not a measurement of NF from one load.
Do not insert a generic 5 dB or other guessed RTL-SDR specification.

Both noise methods require explicit source and receiver resistance values.
Unequal values are rejected unless `accept_ideal_mismatch: true`; accepting this
records a purely resistive approximation, not a full noise-wave/mismatch
correction. The profile documents this condition. Receiver-added noise remains
in subsequent calibrated PSDs as input-referred receiver noise; calibration does
not automatically subtract it from antenna or tone measurements.

## Uncertainty and ongoing verification

An optional manifest `uncertainty` object creates a version-2 profile. It contains
`components` with all five named standard dB terms (`reference_source_db`,
`repeatability_db`, `interpolation_db`, `drift_db`, `mismatch_db`), each nonnegative
or null if unknown; `coverage_factor`; and either `independent: true` or a
positive-semidefinite 5x5 `correlation` matrix in that order. A complete declared
budget is combined with covariance, and expanded by the supplied factor. A null
term keeps total uncertainty unknown. A coverage factor of two alone does not
establish 95% coverage. This is a declared calibration budget, not automatically
measured uncertainty of the live spectrum. Measurement statistics and physical
validity are not invented ([BIPM GUM](https://www.bipm.org/en/committees/jc/jcgm/publications)).

Validate independent RF/baseband points, multiple input levels, warmup and drift.
Revalidate after changing gain, sample rate, filters, antenna path, clock,
firmware, driver, correction policy or environment. Keep certificates, raw
captures, manifest, hashes and acceptance results. A matching JSON context is
necessary but cannot establish unreported analog state or physical accuracy.

See [BLADERF.md](BLADERF.md) for board/channel selection, manual RX correction
controls and the distinction from native Nuand calibration tables.

## Device guides and noise diagnostics

See [Ettus/USRP](ETTUS.md), [bladeRF](BLADERF.md), and the
[colored calibration workflow and offline IQ audit](NOISE_CALIBRATION_AUDIT.md).
Noise-shape diagnostics do not replace measured power references.

## Multi-temperature receiver and LNA characterization

The separate [receiver/LNA workflow](RECEIVER_CHARACTERIZATION.md) fits measured
source-temperature levels, checks CW/noise linearity and optionally propagates
joint correlated uncertainty. Its report is distinct from a power profile.

## Optional relative PSD equalization

See [EQUALIZATION.md](EQUALIZATION.md) for bounded measured-response shaping,
its distinction from absolute power/NF calibration, and corrections to the
room-correction report. This does not invert a terminated noise curve.

## Validated response flattening

[FLATTENING.md](FLATTENING.md) adds whole-file attenuation-only equalization,
explicit usable-band selection, constrained uncertainty-weighted smoothing and
independent tone holdout validation, with templates and colored workflow.

## Passive reference diagnostics

[PASSIVE_REFERENCES.md](PASSIVE_REFERENCES.md) adds relative IF overlap fitting,
fixed-gain-state comparison, selected-tone drift and active/quiet IQ intervals.
Public signals do not independently establish input dBm, receiver NF or phase.
