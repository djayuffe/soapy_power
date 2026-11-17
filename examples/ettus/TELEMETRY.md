# Telemetry and reference-bound receiver models

Telemetry qualifies measurements without altering their IQ or PSD. Use the
normal scanner with `--rf-state`; add `--telemetry-policy telemetry-policy.json`
for explicit validity requirements. The supplied policy requires the optional
native adapter and a fresh RX temperature reading. It is a tested acquisition
policy, not a calibrated temperature-error specification.

```sh
soapy_power_telemetry inventory --device 'driver=uhd,type=b200,serial=YOUR_SERIAL' \
  --sample-rate 28000000 --bandwidth 28000000 --gain 20 --antenna RX2 \
  --master-clock-rate 56000000 -O inventory.json

soapy_power -d 'driver=uhd,type=b200,serial=YOUR_SERIAL' -A RX2 \
  -r 28M -w 28M --master-clock-rate 56M --force-rate -g 20 \
  -f 100M:200M -b 8192 --repeats 32 --reopen-stream \
  --tune-delay .02 --settle-samples 32768 --lock-sensor lo_locked \
  --require-timestamps --partial-policy reject --deadline-policy finish-hop \
  --no-pyfftw --precision single --max-threads 2 --max-queue-size 4 \
  --rf-state --telemetry-policy examples/ettus/telemetry-policy.json \
  --headroom --rejection-evidence new-rejected-directory -F json -O scan.jsonl -e 300
```

A receiver profile may contain `telemetry_policy`. The scanner enforces it and
rejects a different explicit override. Native chain fingerprints and gain/DC
profiles remain distinct from absolute power or coherent RF calibration.

## Polling, thermal validity and lock

Sensor schema is discovered once. Observations contain raw and typed values,
units, source, host read brackets, latency, streaming state, last good reading,
age, freshness and errors. A failed reading cannot reuse an earlier value as
fresh. Boolean labels such as `locked` are states, not physical units. Sensor
latch times are unavailable; host read time is not the physical measurement epoch.

Temperature defaults to one read per second, only with streaming stopped.
`--reopen-stream` provides those slots and is required for strict temperature
policies. Continuous receive can retain stale or deferred readings when no
strict temperature requirement is selected. Fast lock/RSSI readbacks surround
each hop. `reference_lock: selected` requires reference lock only for an external
or GPSDO clock; the separate RX LO lock gate is `--lock-sensor lo_locked`.

Use `temperature_sensor_key` to select a particular temperature sensor explicitly.
The default prefers `rx:temp`, then an unambiguous RX temperature sensor; multiple
ambiguous sensors do not get averaged into a fictitious temperature.

Optional policy fields include `temperature_range_c`, `reference_temperature_c`
with `max_reference_delta_c`, `max_latency_ms`, `expected_chain_sha256`,
`warmup_window_s`, `warmup_max_slope_c_per_min`, and `warmup_timeout_s`.
Warmup waits with RX stopped for a robust median-endpoint temperature slope;
it times out if stability is not established. Later instability rejects a hop.
Choose limits from your reference experiment; the sensor descriptor's default
0–0 range is not a usable thermal operating range. The B200mini RFIC temperature
is approximate, shared by RX/TX, and differs from ambient/load temperature.

Explicit fixture feedback is supported through `gpio` entries containing
`bank`, integer `mask`, integer `expected` and a nonempty physical mapping
`reference`. No GPIO bank or bit implies a switch/load state automatically.

## Optional native SoapyUHD extension

Stock SoapyUHD on the tested B200mini misses the daughterboard IQ-mode getter.
The included patch checks both property paths and adds read-only versioned JSON
settings for device/link metadata and the exposed RX chain. It does not change
tracking modes during discovery. Standard backends remain usable; unexposed
native metadata stays `unsupported`, not `false` or guessed.

Build with `build_telemetry_driver.py CLEAN_SOAPYUHD_CHECKOUT NEW_BUILD_DIR`.
The script requires revision `d6f7feaf016e1559ea479007196def1cc55a463c`, copies
sources, checks/applies the patch, compiles, runs CTest and installs under the
new directory. Use `--cmake-arg=-DSoapySDR_DIR=...` and
`--cmake-arg=-DUHD_DIR=...` to select matching SDKs. If Homebrew imported headers
refer to a different SDK, also pass
`--cmake-arg=-DSOAPY_UHD_SDK_INCLUDE_DIR=/matching/soapysdr/include` and matching
UHD include/library overrides. Select only the resulting module directory with
`SOAPY_SDR_PLUGIN_PATH`, and match its ABI to the Python bindings and runtime.
No system module or firmware is replaced.

The extension exposes actual rate, bandwidth, master clock, DC/IQ tracking,
native format/full scale, power-reference capability and typed filter stages.
Filters are validated and fingerprinted; configuration changes invalidate the
cache, with periodic refresh controlled by `refresh_hops`. RF, gain and other
controls remain in the full measurement context. USB link metadata is a declared
capability; host reads are not USB packet counts or measured link capacity.

## Reference-trained thermal compensation

`soapy_power_telemetry fit-thermal measurements.json model.json` fits a scalar
gain correction with explicit reference provenance, unique training/validation
captures, a temperature domain, correction bounds and independent improvement
checks. See `thermal-reference.synthetic.json` for the schema; every number in
that file is synthetic and unsuitable for your receiver. `settling-reference.synthetic.json` provides the separate settling schema.

Source identity includes `reference`, `reference_sha256`, `controlled_input:true`,
`sensor_key`, `context_sha256`, `response_scope`, and optional `max_sensor_age_s`.
Measurements require `partition`, `capture_sha256`, `temperature_c`, and
`correction_db`. Declare measurement/reference and temperature uncertainties.
Partition by independent capture/time/thermal cycles; never split one capture
into both partitions. A digest establishes file identity, not traceability of
a reference certificate. A controlled source and appropriate RF/gain/IF coverage
must be established experimentally.

For application, use a linear-density NPZ with `density`, `frequency_edges_hz`,
and JSON scalar `metadata` containing the complete `context`, `processing` and
`units`. `signature input.npz signature.json` derives its exact grid/receiver/DSP
signature, which must match the trained source. An observation JSON contains
`fresh:true`, `age_s`, `max_age_s`, `temperature_c`, `context_sha256`, `sensor_key`.

```sh
soapy_power_telemetry apply-thermal model.json input.npz observation.json corrected.npz
```

Application checks freshness against the model's bound as well as the observed
bound, forbids extrapolation and double application, preserves `raw_density`
and units, and reports conditional uncertainty. This offline operation is
explicit; acquisition never trains or applies a thermal correction to antenna
traffic automatically. Per-frequency vector/DC/IQ/frequency compensation and
absolute power/NF calibration require their corresponding measured reference
models in the existing calibration tools; a scalar thermal model establishes
none of those quantities.

## Reference-trained settling

`fit-settling measurements.json model.json` chooses the shortest tested delay
that meets amplitude/frequency error limits on training and independent holdout
captures, including at least three actual holdouts at that exact delay. Its
source `context_sha256` is the digest of
`{context: COMPLETE_MEASUREMENT_CONTEXT, previous_center_hz: VALUE, center_hz: VALUE}`.
The scanner accepts `--settling-model model.json`. Unmatched RF/gain/clock/step
states retain the configured `--tune-delay`; corrupted models fail before RX.
Lock flags alone do not establish RF settling. The synthetic example only
demonstrates schema and fitting, not a measured shorter delay.

## Filter phase, transport and anomaly diagnostics

`filter-model chain.json response.npz --frequency-grid offsets.json` computes
the DC-normalized complex response and relative linear-phase delay of exposed
active digital stages. SciPy is required for this optional command. This partial
model omits unexposed FPGA and analog response, so inversion is not authorized
and it is not calibrated RF phase. Keep actual RF/BB tuning coordinates with
IQ; `lo_mapping` locates a controller-reported LO hypothesis, including spectral
conjugation/high-side mapping. It never notches an unverified line.

Every accepted hop includes host read/error counters, read latency percentiles,
telemetry counters and latency, and RF state snapshots. Rejected/prefix IQ and
continuity errors remain evidence. RSSI/IQ disagreement is advisory and matched
to receiver/gain/tuning state: driver RSSI has an unknown integration window
and is not channel dBm or a proof of analog linearity. Existing headroom gates
and explicit measured gain ladders govern backoff. Phase diagnostics cannot
recover unknown phase across independent retunes or establish external timing.
