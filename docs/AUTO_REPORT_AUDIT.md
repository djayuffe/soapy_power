# Automatic reports and waterfall audit

Automatic full-range, supervised scan-set and Workbench capture reports include
a shared **feature-telemetry** report by default. It contains every complete
record's metadata in JSONL, all scalar fields in JSON/CSV, constants/categories
in a table, and varying numeric fields in PNG/PDF pages. Plots use bounded
representative points (1024 per field) without connecting gaps or partitions.
Inventory statistics use all complete records, independently of display sampling.
Arrays longer than 32 entries and nesting beyond 16 levels remain in the complete
metadata export rather than generating thousands of curves. Categories keep 32
named values plus an explicit other-record count. Missing/non-finite data remain
unknown. PSD cannot reconstruct waveform phase; retained IQ analysis remains a
separate evidence-qualified workflow.

## Corrected paths

- Full-range waterfall and comparison aggregation now obey both masked and
  estimated-bin declarations. Density is averaged in linear power before dB
  conversion; mixed units require separate reports. Unterminated fragments are
  excluded from completed-record analysis and retained in the original capture.
- Partial/narrow waterfall rows remain visible. 3D plots share data-derived level
  and time axes, skip empty bands and avoid fixed receiver/antenna labels.
  Surfaces are explicitly sampled sequential mosaics, not continuous reception.
- Full-range automatic reports include measured-dwell capture previews alongside
  the 3D overviews. Measured timestamp axes leave revisit gaps blank; invalid or
  absent timestamp geometry uses an explicit row-index display.
- Workbench report generation publishes capture, spur and telemetry output as
  one atomic bundle. A failed generation cannot expose a partial new report.
- GUI **Generate report** now supports fullrange sessions as well as hopscan and
  superscan. The request is dispatched before SDR imports/discovery and uses the
  report publication lock. Failures are visible separately from capture state;
  previous successful reports remain available.
- Supervised report guard plots use recorded maximum/p99.9 limits instead of
  fixed reference values. Missing limits are not replaced with defaults.

The HTML index links all telemetry, waterfalls and evidence outputs. Merged
full-range and supervised PDFs include the varying-telemetry pages. Scientific
signal identification and retained-IQ/phase analysis continue through the native
supervised pipeline; unavailable evidence is never manufactured by enabling a
report feature. Optional rotating GIF generation remains opt-in (`--gif`) because
it creates a large animation and adds no measurement evidence.

## Regenerate without reception

Use **Generate report** for a saved session, or:

```sh
python tools/report_auto.py sessions/fullrange-YYYYMMDD-HHMMSS
python tools/analyze_scan_set.py sessions/superscan-YYYYMMDD-HHMMSS/analysis sessions/superscan-YYYYMMDD-HHMMSS
```

The first command atomically publishes `report/index.html`, `report/report.pdf`,
`report/feature-telemetry/` and `report/capture-previews/`. The second publishes
partitioned native-resolution waterfalls, signal/pass analysis, qualified retained
IQ diagnostics, telemetry and the merged PDF under `analysis/`.

## Orbital azimuth/elevation in 3D reports

`report_auto.py` now adds acquisition-time satellite context to the 3D pipeline.
A session's frozen `reference/` is verified and always takes precedence. For a
legacy capture with no frozen reference, an archived bundle can be supplied
explicitly with `--reference PATH`. The current satellite cache is never silently
substituted. A broken frozen reference fails publication; missing references or
recording-time anchors get an explicit unavailable status and explanatory page.

Outputs include `orbital-context.json`, `orbital-az-el.csv`, and 3D azimuth / time /
elevation plus polar sky-path pages in PNG/PDF. Predicted downlink trajectories
are shown as dashed paths at the 3D waterfall's base plane, with az/el labels.
Their base-plane height is a visual anchor, not measured signal strength. The
same predictions are included when GIF generation is requested. These are SGP4
predictions, not measured antenna pointing or confirmation of satellite identity.
Existing raw pointing/phase fields remain available in the telemetry inventory.

Orbit propagation uses each variant's timezone-qualified acquisition origin and
end, the frozen observer, transmitter eligibility and the native TLE-age policy.
No current-time orbital state is used for a historical scan. Propagation is bounded
to 4096 candidates, 64 exported tracks, four transmitters per track, and roughly
721 samples per track; plots label at most eight paths. Limits and reference
scope are exported. Below-mask intervals and azimuth wrap crossings remain gaps
in the relevant Cartesian trajectory plots.

```sh
python tools/report_auto.py sessions/fullrange-YYYYMMDD-HHMMSS --reference /path/to/archived/reference
```

CLI parsing rejects unknown options and missing suite files. PDF generation is
required by default; use `--no-pdf` explicitly to omit the merged PDF while retaining the per-page artifacts.
HTML links reflect available artifacts. Subprocess stages have bounded timeouts,
feature availability is recorded in `summary.json`, and all reference/code inputs
participate in atomic publication integrity checks. Narrow, one-pass and partial
waterfalls retain measured samples; 3D axes include capture duration and peak
levels rather than clipping to the first pass or a high percentile. Full-range
reports also include bounded measured spur diagnostics, retaining both masked
and estimated-bin exclusions without creating automatic notches.

Future full-range runner sessions snapshot cached SatNOGS/observer references before
RX. A missing cache or observer records an explicit unavailable marker and does
not prevent RF reception. Reports honor that marker without substituting another
reference; an explicitly broken frozen bundle still fails integrity checks.

Measured spur analysis now checks the actual common IF interval, including
off-center windows produced by mask splitting. Peak-linking geometry is cached
per capture and nearest-peak matching is vectorized. Automatic full-range spur
analysis runs in a child with a 120-second deadline; failures refuse publication
and leave the previous report intact.
