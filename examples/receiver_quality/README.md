# Safe live scanning and portable comparison algorithms

These controls promote the three-receiver campaign safeguards into the main
program. A receiver profile is a measured **control policy**, not a power/NF
calibration. Templates deliberately have an unusable serial until edited.
Check the hardware, native stage readbacks, actual rate, bandwidth and antenna
selection before creating a policy. Band entries are approximate starting
seeds; live headroom checks remain necessary at every hop.

## Scan with retained rejection evidence

For the tested RTL configuration, after verifying the connected receiver and
native gains, an example fixed-gain scan is:

```sh
python -m soapypower -d 'driver=rtlsdr,serial=YOUR_SERIAL' -f 400M:500M \
  -r 2.56M -b 4096 --repeats 16 --gain 19.7 --runs 1 --format json \
  --dc-mask-width 40k --ensure-coverage --usable-halfwidth 960k \
  --headroom --rejection-evidence /new/path/rejected-iq --rf-state
```

Check `python -m soapypower --help` for the argument names supported by the
installed version. Use a fresh evidence directory on every run. Optional
`--receiver-profile profile.json --headroom-retries 4` enables bounded profile
backoff. Without a profile, an unsafe hop is retained and rejected immediately;
the program does not invent an automatic gain strategy. Workspace defaults to
64 MiB for quality analysis independently of the DSP budget. A hop requiring
more raw/statistical workspace fails before measurement allocation.

The default limits are raw maximum component <0.85, raw P99.9 component <0.60,
and zero components at or above 0.95. IQ is not centered before these checks.
Headroom does not prove analog linearity. Rejected IQ and valid prefixes from
transport failures are saved as compressed complex NPZ before retry/failure,
with SHA-256, requested controls, context, failure stage and a manifest. A disk
or publication failure stops scanning. Settling samples are deliberately
excluded; an error before measurement can have a zero-sample evidence record.
Accepted IQ is used transiently and is not continuously recorded.

`--ensure-coverage` accounts for the excluded displayed origin and a one-bin
margin inside the usable IF edges. It uses additional physical observations,
including extra boundary hops; it never reconstructs missing RF samples.
Full requested output coverage requires completing the plan. A deadline can
end a partial sweep. The planner rejects an unreachable band or wrapping mask.
`--usable-halfwidth` is a conservative observed IF band, not an automatic filter
calibration. Legacy planning stays available without the flag.

`--rf-state` snapshots advertised RF/BB tuning components and sensors before
and after a hop. Quality mode enables those snapshots as well. Requested lock
sensors are checked after measurement in addition to settling. Before/after
lock is not proof of uninterrupted lock, phase accuracy or calibrated RF state.
RTL sample-counter timestamps remain explicitly unverified as hardware clocks.

## Import measured campaign profiles

Use a freshly observed full measurement `context`, not a handwritten requested
identity. An import metadata JSON contains `context`, `reference` describing the
measurement procedure, optional `point_tolerance_hz` (default 2), and for RTL an
independently observed descending native `gain_ladder` in dB.

```sh
python -m soapypower.analysis_cli import-profile campaign.json profile.json \
  --metadata observed-import-context.json
```

The importer accepts retained bladeRF band/DC-anchor profiles and RTL selected
or dense gain points. It records the campaign input hash. RTL points stay local:
a different hop grid needs a newly validated point domain or explicitly declared
approximate bands. No gain interpolation or arbitrary point extrapolation occurs.
Manual-DC profiles require a complete observed `control_context` (all main
context fields except identity, hardware_info and gains), including correction
modes, channel, clock, tuning arguments and DSP frequency conventions. The
bladeRF importer supplies this binding. For bladeRF1, serial, rate, bandwidth
and the exact `lms_dc_state_at_open` must match. Supply `--dc-correction off` when using manual DC anchors. A changed gain
or XB-200 mix/bypass domain resets manual DC to zero unless an independently
validated anchor matches the **actual** gain stages and local RF domain.

The source patches in `../../patches/` are for the exact plugin revisions from
the campaigns. They expose native RTL fractional gain readback and the bladeRF1
LMS state at device open. Apply/build them only in an isolated matching driver
source tree. The main scanner validates observed state rather than silently
patching installed drivers. A missing LMS state or mismatched gain fails closed.

## Offline analysis CLI

Install exposes `soapy_power_analysis`; `python -m soapypower.analysis_cli` works
from a checkout. Outputs are atomic, input evidence is checked before publication,
and existing outputs require `--overwrite`. Source files cannot be overwritten.
NPZ input keys are `frequency_edges_hz`, `density` in **linear density units**,
and optionally `supported_fraction`; IQ inputs use `iq` as a complex vector.

```sh
python -m soapypower.analysis_cli rebin source.npz common.npz --target-edges edges.json
python -m soapypower.analysis_cli fuse hops.json combined.npz --target-edges edges.json
python -m soapypower.analysis_cli noise-reference terminated.npz reference.npz --metadata reference.json
python -m soapypower.analysis_cli noise-compare antenna.npz comparison.npz --metadata antenna.json --reference reference.npz
python -m soapypower.analysis_cli common-iq iq.npz common-iq.npz --sample-rate 56000000
python -m soapypower.analysis_cli phase iq.npz phase.npz --sample-rate 56000000
python -m soapypower.analysis_cli spur-atlas track.json atlas.json --tolerance-hz 1000
```

Rebinning integrates exact cell overlap in linear units. NaN means unmeasured;
zero is measured zero. Support fractions and missing output cells survive.
Fractional input support represents uniform subcell coverage; use a fine grid
to retain the actual positions of small holes.

A fusion JSON contains `observations`, each with `path` (relative to manifest),
`id`, `units`, `reference_convention`, `start_s`, `stop_s`, optional positive
`weight`. State any gain normalization in `reference_convention`; all rows must
agree. Optional `reference_time_s` and `max_age_s` exclude stale observations.
The output includes sparse per-cell hop IDs/weights, support count and acquisition
time range, mean and largest contributing cell mean. That peak is not a
calibrated burst detector; sweep time is not simultaneous full-band reception.

Noise metadata contains the complete main-program fixed-gain `context` and a
`method` with `fft_bins`, `window`, `normalization`. Reference creation also needs
`reference` with `termination`, `reference_plane`, `temperature_c` (null if
unknown) and `headroom_accepted: true` established from raw evidence. Use NaN for
unsupported reference cells. Context, RF cell grid and method must match exactly;
there is no reference interpolation across gains or tuning. Archives load once
into an immutable array cache. Results are **noise ratio** and signed linear
excess, including negative excess: neither SNR, dBm, NF nor an equalizer.
Record cable/adapter placement and unknown RF rating rather than assuming a
perfect direct 50-ohm reference. There is no bladeRF terminated baseline in the
three-radio experiment, so one must not be synthesized.

Common IQ uses the same 2.56 MS/s target, 129-tap 900 kHz Kaiser-8.6 FIR,
64-sample delay compensation, 0.5 ms guarded start and 16 ms duration. Native
headroom remains unfiltered. Common phase uses identical 10 kHz FFT spacing and
15 kHz periodic-Hann ENBW, disjoint train/validation partitions and frozen phase
predictions. Source IQ stays unchanged; independent radios are not phase aligned.
SciPy is needed for these IQ/phase functions (`pip install '.[fft]'`).

A spur track is an explicitly associated observation list with `capture_id`,
`partition` (`train`/`validation`), `center_hz`, `baseband_hz`, `sample_rate_hz`
and `clock_state`. The diagnostic freezes RF-fixed and IF-fixed anchors on
training captures, checks independently retuned holdouts, and separates clock/
rate domains. It creates **no automatic exclusion or notch**. Association and
physical RF/reference experiments still require evidence; stationarity alone
cannot distinguish every internal spur from a real signal.
