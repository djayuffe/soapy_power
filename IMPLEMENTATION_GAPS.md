# Campaign findings integrated into the program

The 2026-11-17 integration promotes reusable algorithms and acquisition guards
from the Ettus B200mini, bladeRF x40/XB-200 and RTL-SDR campaigns. It preserves
raw evidence and separates software tests, historical hardware observations and
physical calibration. It does not turn the old measurements into a universal
receiver calibration.

| Finding / gap | Main-program implementation | Validation / remaining boundary |
|---|---|---|
| Headroom was checked after centering or only in scripts | `acquisition_quality`, `power.SoapyPower.psd`, CLI `--headroom` | Uncentered max/P99.9/rail limits; rare-peak and DC-offset regressions. Digital safety does not prove analog linearity. |
| Unsafe/partial IQ could disappear before assertion/retry | `RejectionEvidence` and packet-level prefix retention | Original complex samples, SHA-256, atomic NPZ/metadata/index; transport-prefix, nonfinite, disk-failure, no-overwrite tests. Settling samples are explicitly outside the capture. |
| Gain/VGA backoff was receiver-specific script logic | `ScanProfile`, observed-stage guards, bounded retry/cache | Exact identity/rate and declared state; explicit descending native ladders or bladeRF1 stages. No invented gain calibration or uncontrolled retry. |
| Gain changes could carry stale DC corrections | Exact actual-stage/RF-domain anchor selection and zero reset | Tests include tiny tolerated gain-readback differences, gain backoff, anchor validation, XB boundary and profile reuse. |
| bladeRF DC fit depended on hidden LMS state | State-bound profile import; optional isolated SoapybladeRF patch | Missing/changed `lms_dc_state_at_open` fails; register snapshot remains explicitly state-at-open. |
| RTL fractional native gain readback was wrong | Observed gain guard plus version-specific source patch | Runtime rejects corrupted readback; the patch rounds after multiplying by ten and reads native state. No system driver replacement is performed. |
| RTL serial was absent from hardware info | Unique enumeration identity with open-index cross-check | Never substitutes a merely requested serial; ambiguous or inconsistent identity fails. Reconnects/transport failures require a new valid stream. |
| RTL `HAS_TIME` looked like hardware continuity | Existing driver-counter classification retained | Hardware times null; driver sample-counter times retained; strict hardware timestamp requests reject RTL. |
| Completed USB samples were confused with RF validity | `rf_state` snapshots and post-acquisition lock check | RF/BB changes or lost requested lock fail. Sensor absence stays unknown; snapshots do not prove continuous lock or phase accuracy. |
| Origin masks and zero taper edges left exact grid holes | `coverage.masked_frequency_plan`, CLI `--ensure-coverage` | RTL 625 Hz grid reproduces the old hole and verifies all new cells have positive off-mask support. Actual requested/observed centers are checked. |
| Report comparisons used ad hoc rebinning | `spectral_comparison.rebin_density` | Exact area overlap in linear density; conservation, boundary, missing/zero/partial support and extreme-density tests. No RF-hole interpolation. |
| Overlap fusion lacked reusable per-cell provenance | `fuse_observations`, offline `fuse` | Sparse contributing hop IDs/weights, support counts, first/last times, declared units/reference convention, optional stale exclusion, separate maximum contributor. |
| Terminated references were script-only and repeatedly decompressed | `RelativeNoiseReference`, offline noise commands | Full observed context, exact grid/gain/DSP match, explicit accepted headroom and load/reference-plane metadata; one immutable in-memory cache. Negative excess preserved. |
| Common-IQ comparisons used unequal native bandwidths/durations | `common_iq`, offline `common-iq` / `phase` | Equal common-band filter/duration and native unfiltered headroom; delay/tone-amplitude tests; equal FFT/Hann ENBW and held-out partitions. Independent receivers are not RF phase aligned. |
| LO/birdie decisions lacked reusable held-out recurrence checks | `spur_atlas`, offline `spur-atlas` | RF-fixed/IF-fixed hypotheses, independent retunes and clock/rate domains; unresolved outcomes retained. Produces no automatic exclusion or destructive notch. |
| Direct-conversion LO harmonics, IQ images and intermodulation were handled ad hoc | `spur_model`, `spur_inference`, `spur_pipeline`, `spur_design`, `spur_visuals`, `soapy_power_spurs` | Synthetic T1-T5 (zero missed required features, zero kind errors), bias-corrected and Gamma-mixture floor, FDR peaks, Bayesian track posterior, CRLB, robust/sparse/coherent methods, D-optimal plan and before/after figures. No automatic notch or exclusion. The controlled 50-ohm/generator/filter hardware phase is specified but not run. |
| Measured campaign profiles were not portable | `import_campaign_profile`, offline `import-profile` | Converts documented bladeRF/RTL formats with fresh observed context and input hash. Dense point support is not silently extrapolated to a different hop grid. |
| Offline evidence could change while outputs were being written | Atomic analysis CLI and `EvidenceSnapshot` | Input replacement/mutation blocks publication; output cannot overwrite an input. |
| Relative/absolute response and uncertainty were conflated | Existing passive-response, calibrated-response/equalization and uncertainty APIs retained | Redundant overlap graphs, stable-source conditions, held-out validation and regularized measured inverse remain separate from terminated-noise ratios. |

## Physical experiments still required

The existing guided calibration/characterization APIs can collect known-source,
source-on/off, hot/cold and independent holdout data when the appropriate physical
references are connected. This software change does not supply those references.
The remaining physical gaps are absolute power/response, NF/ENR and path/mismatch,
analog compression, clock/frequency accuracy, reference-driven settling/drift,
vector RF phase/delay/image rejection, and calibrated signal-detection/false-alarm
or probability-of-intercept performance. A single unknown 50-ohm termination,
especially through an unknown cable/adapter, does not identify these quantities.
No external terminated bladeRF baseline exists in this experiment.

A general automatic birdie-removal classifier, learned settling schedule, adaptive
scanner optimizer and coarse-LO/polyphase retuning optimization still require
backend/reference experiments and explicit preservation criteria. The new
runtime guards, provenance combiner and diagnostic spur hypotheses provide
reusable primitives; they do not claim those unvalidated policies are complete.

No new physical SDR capture or firmware change is claimed by this integration.
Software regression tests and replay of retained campaign data are recorded in
the validation deliverables. See `examples/receiver_quality/README.md` for the
schemas, CLI commands, budgets and publication semantics.

## Full-operation follow-up

The sustained antenna run exposed a USB-lifecycle gap that a short smoke test
had missed: reopened streams remained active during post-acquisition CPU/output
work. Reopen mode now closes the stream immediately after collecting the unique
IQ samples, before RF readbacks, FFT completion and quality/evidence work. Tests
verify this ordering and repeated-hop restart behavior. The corrected 28 MS/s
run completed 300 seconds and 4,449 verified hops; the earlier overflow run is
retained separately. This is session evidence, not a universal throughput claim.

Compressed JSONL readers now stream `.gz` inputs through the same numerical and
record checks. Spur-atlas identifiability metadata is converted to a Python
boolean so successful diagnostics also publish as strict JSON. The operation
exercised CLI context/integration, rebin/fusion, common IQ, held-out phase, noise
reference identity and mismatched-reference refusal, native audit, historical
profile import and retune recurrence. No current antenna-only data were relabelled
as absolute power/NF/vector calibration.

## Telemetry implementation follow-up

Typed observations, closed-stream temperature polling, explicit freshness/error
semantics, startup thermal waits, thermal/reference/GPIO gates, profile-bound
policies and actual RF/BB LO mapping are implemented in the main scanner. The
optional isolated native SoapyUHD patch exposes hidden IQ tracking, typed filters
and native format scale. Reference-trained scalar thermal and RF-step settling
models now have independent holdout gates, bounded domains and context-bound
application; a partial complex digital filter model supports phase/delay analysis.

The 300-second antenna validation completed 4,406 verified accepted hops at
28 MS/s, eight full sweeps plus a partial sweep. IQ tracking remained enabled,
the processing chain fingerprint stayed constant and 293 temperature reads all
occurred with RX stopped. The final warmup path was separately tested using the
independently rebuilt module. See examples/ettus/TELEMETRY.md for schemas and
reference requirements. Synthetic model tests establish software behavior only;
known-source thermal/settling accuracy, absolute power/NF, external-clock error
and end-to-end coherent RF phase remain physical reference experiments.
