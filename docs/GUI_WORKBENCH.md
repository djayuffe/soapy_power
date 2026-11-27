# Measurement workbench

Open **Workbench** in the receiver console. The existing **New scan** workflows
remain available with their qualified-time supervisor. Expert acquisition uses
an explicitly selected receiver and an active-process budget; it does not
pretend tuning, startup or missing guards are qualified RF time.

The generated catalog contains **44 operations and 357 option bindings**,
including options managed by the service. It is generated from the real CLI
parsers, not a separately maintained list of flags.

| Area | Available operations |
| --- | --- |
| Acquisition | Custom range or centers, gain stages/AGC, antennas/channels, FFT/window/overlap/crop, coverage, DC/IQ correction, calibration/profile, clocks, tuning/stream/device arguments, buffers/threads/workspace, lock/timestamp/settling/headroom and telemetry |
| Calibration | Validate/build profiles, extract context, integrate/compare/baseline, characterize, equalize, smooth/validate response, fit overlaps, compare gains, tone/interval IQ audits, offline assistance and guided acquisition |
| Analysis | Regenerate high-resolution scan reports, rebin/fuse density, relative noise references, common IQ, phase diagnostics, held-out spur atlas and profile import |
| Telemetry | Hardware inventory, thermal/settling fits, filter model, RF signature and thermal correction |
| Spur inference | Synthetic demo (explicitly synthetic), explicit capture-manifest inference, LO experiment design and measured scan-record adapter |
| Experiments | Plan/run/analyze suites, create calibration configurations, compare antenna/termination sessions and synthetic dry run |
| Noise | Complex-IQ noise audit, including optional coherent-feature diagnostics |

## Configure and review

1. Choose an operation and native runtime. Ettus uses the existing B200 stack;
   bladeRF uses the existing patched stack/profile; generic/RTL selects an
   importable configured SoapySDR stack without requiring a B200. Runtime
   selection does not manufacture hardware capabilities.
2. Set required inputs and the optional controls you need. Blank fields retain
   CLI defaults. Search finds controls inside collapsible groups. Integration
   time, total integration time and repeats are alternatives; so are total gain,
   gain stages and AGC. The actual parser rejects conflicting combinations.
3. Input paths are relative to `sessions/`. Refresh the artifact list, or enter
   an exact existing path. Lists accept space-separated values or a JSON array
   (use a JSON array for paths containing spaces). The JSON input editor saves
   new files under `sessions/inputs/` and never replaces an existing file.
4. **Validate and preview** runs the real CLI parser in an isolated CPU-only
   process. Acquisition also runs its DSP/profile argument validator before
   device initialization. Physical capability/readback checks still happen at
   acquisition time.
5. Review the typed plan and **Queue reviewed plan**. No browser-supplied shell
   command is executed. The runner revalidates the request, serializes work and
   reserves the receiver for hardware operations.

The guarded B200 preset is a starting point: 100–200 MHz, 2 MS/s, 8192 bins,
Blackman window, 75% overlap, 10 kHz DC mask, coverage planning, settling delay,
RF-state snapshots and headroom rejection. Mask-aware coverage now retains the requested overlap views instead of replacing that hop grid. Gain is a conservative starting
setting, not an optimization or calibration result. Inspect actual readbacks
and guard outcomes; a suitable rate/gain/antenna/lock sensor is device-specific.

## Files and controls

Each job gets a unique `expert-YYYYMMDD-HHMMSS-xxxxxxxx/` directory containing
its plan, status, worker log and fresh outputs. Acquisition sessions are visible in the
main console; the workbench monitor exposes artifacts and controls. The saved
job identifier can reopen the monitor after a browser restart.

JSON is the default acquisition format and supports live spectrum/waterfall,
guard reports and measured spur analysis. Other native CLI output formats
remain selectable and retain their own raw partitions; they do not acquire
JSON-only previews or qualification claims. Output paths, descriptors,
endless execution, CLI help/version and overwriting are service-managed.
The web acquisition budget is finite (5–3600 seconds). These restrictions are
explicit service contracts, not hidden versions of the CLI switches.

**Pause** stops the current RX child and preserves that attempt. **Resume**
starts a fresh partition with the remaining active-process budget. **Retry**
also starts a fresh partition; it does not concatenate or relabel gain
contexts. A control must be acknowledged before another is accepted.
**Abort** is sticky and preserves all files. Offline analyses support abort,
but cannot pause midway through a numerical fit. Repeating a completed job
requires a newly reviewed plan. A runner crash is reconciled as interrupted,
not silently replayed.

Native RX children inherit the receiver lease, including experiment trials.
A surviving RX child therefore prevents a second runner from opening the
receiver. CPU-only work bypasses receiver discovery. Experimental suite
reanalysis uses a bounded copy (maximum 2 GiB), leaving the source suite intact;
symlinked suites are rejected for this operation.

## Spur prevention and inference

Acquisition controls expose existing DC masks, hardware/manual DC and IQ
corrections, windowing/detrending, coverage/overlap/explicit LO centers,
headroom/profile backoff, lock/settling and telemetry policies. These are
separate mechanisms; enabling every switch is not automatically beneficial.
For example, interpolating DC estimates unmeasured power and requires a boxcar
window, while measured coverage with a mask deliberately retains the hole.

The optional **Measured spur audit**, also available as the standalone
`scan-spurs` operation for older JSONL scans, reads every source row and hashes
the sources. It uses deterministic context sampling with bounded memory and
computational budgets. It keeps matched local RF neighborhoods, independent LO
views, units, gain, rate, exact IF grids and DSP settings separate. It analyzes
contiguous common measured support without interpolating masked/estimated
bins. Fewer than three matched LO views are reported as insufficient evidence.
Known nonzero analog/digital tuning splits and transformed axes require an
explicit capture manifest instead of an assumed LO mapping.

Inference uses the existing Gamma/mixture floor, estimated degrees of freedom,
BH false discovery control, robust LO slopes and Bayesian signal/image/harmonic
hypotheses (orders 1, 3, 5, 7). Sequential signal variability can imitate LO
tracks. An attribution is not independent proof of its physical cause.
The adapter reports selection limits and skipped evidence and **never creates
automatic notches, deletes lines or overwrites raw data**. The explicit-manifest
spur analyzer additionally exposes its native alpha/order/profile settings;
the held-out atlas requires genuine associated train/validation observations.

## Automatic reports and phase

The standalone `scan-report` operation can regenerate a report into a new job without changing its source. JSON acquisitions automatically produce per-attempt waterfall PNGs at 3960 ×
2200 pixels, an HTML report and a guard inventory with SHA-256 hashes. The
preview samples rows across the whole attempt and uses maximum-preserving
bin pooling solely for display. Blocks crossing masks remain masked. Columns
are labeled as display-bin groups and the separate tuning trace gives the
row-dependent RF center; the image does not imply a common RF grid or measured
elapsed-time coverage. Qualified guard counts are not credited RF seconds.
Report failures remain distinct from capture failures and retain raw outputs.

Phase analysis requires a retained complex-IQ NPY/NPZ input in the native
analysis format. PSD/JSONL power cannot recover phase. The GUI exposes common-IQ,
phase, tone/interval and coherent-feature analyses without synthesizing missing
IQ. Absolute power calibration still requires appropriate physical references;
a room-temperature load alone does not establish gain or an absolute response.

Guided source-controlled calibration and active experiments require an existing
executable source hook under `sessions/hooks/`, implementing the native explicit
JSON acknowledgement protocol. The GUI does not guess source state, fabricate
reference values or bypass the protocol. Passive baseline configurations remain
available under their native requirements.

## Development and validation

Regenerate bindings after parser changes:

```sh
python tools/gui/build_catalog.py
```

Tests compare the generated catalog against every real leaf parser and exercise
queue/runner execution, preflight rejection, path confinement, immutable input
artifacts, sticky abort, acknowledged controls, partitioned retries, measured
spur support/context handling and high-resolution reporting. They use temporary
files, synthetic evidence and fake RX processes. Hardware readback, RF
performance and physical calibration need separate measurements on each device.
