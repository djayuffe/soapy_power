# Satellite superscan

The superscan is a B200/B200mini receive-only survey of satellite downlink bands. It hops the receiver
across every band once per sweep, so each band is revisited every few seconds. It then tracks
Doppler-shifted carriers and compares what it saw with predicted passes from the SatNOGS database and
SGP4 orbits. Everything is computed from the power spectral density (PSD): nothing is demodulated or
decoded, levels are relative, and a satellite "identification" is a match between the observed frequency
and the Doppler curve predicted for that satellite.

The workflow runs on macOS through the launchd runner (`tools/macos_runner`). You can start it from the
command line or from the local GUI ([GUI.md](GUI.md)). The analysis and report tools run on any POSIX host
(macOS or Linux) that has the optional analysis dependencies.

## Quick start

```sh
# one-time: install the runner (see the script header) and write sessions/qth.json
tools/macos_runner/install.sh
printf '%s\n' '{"lat_deg": 59.91, "lon_deg": 10.75, "alt_m": 20, "source": "map"}' > sessions/qth.json

# satellite database (SatNOGS transmitters + satellites + TLEs) and sgp4 in the runner venv
bash tools/macos_runner/queue.sh scan --satdb-refresh 1 --wait 600

# 30-minute scan of the default band set at 8 MS/s, automatic gain, two LO views per frequency
bash tools/macos_runner/queue.sh scan --seconds 1800 --superscan 1 --superscan-plan sat-full \
  --superscan-rate 8 --superscan-gain auto --superscan-dither 300
```

The command prints a request id. The run creates `sessions/superscan-YYYYMMDD-HHMMSS/`. When it
finishes, `analysis/index.html` and `analysis/full-analysis.pdf` contain the report.

To rebuild the report from saved data without opening the SDR, run:

```sh
bash tools/macos_runner/queue.sh scan --reanalyze superscan-YYYYMMDD-HHMMSS
```

## Request options

| `queue.sh` flag | Request key | Values (default) | Meaning |
| --- | --- | --- | --- |
| `--superscan 1` | `superscan` | 1 | Run the superscan workflow (B200 only). |
| `--seconds N` | `scan_seconds` | 5–3600 (30) | Credited scan time requested from the supervisor. |
| `--superscan-plan` | `superscan_plan` | `sat`, `sat-full`, `sat-s` (`sat-full`) | Band preset, see below. |
| `--superscan-rate` | `superscan_rate` | 2, 4, 8 MS/s (8) | Planned sample rate; the pilot may lower it. |
| `--superscan-gain` | `superscan_gain` | `auto` or 0–76 dB (`auto`) | Fixed gain, or automatic selection. |
| `--superscan-dither` | `superscan_dither` | 0–9999 kHz (0) | Second hop grid shifted by this amount. |
| `--antenna` | `antenna` | `RX2`, `TX/RX` (`RX2`) | Receive port. |
| `--input` | `input` | `antenna`, `terminated` (`antenna`) | What is connected; recorded with the data. |
| `--satdb-refresh 1` | `satdb_refresh` | 1 | Refresh the satellite cache only. |
| `--reanalyze NAME` | `reanalyze` | session name | Re-run the report without hardware. |

`--gain` is the gain for the other workflows (0–70 dB). A superscan uses `--superscan-gain`.

### Band presets

| Preset | Bands (MHz) | Hops without / with 300 kHz dither at 2, 4, 8 MS/s |
| --- | --- | --- |
| `sat` | 137–138, 145.8–146.0, 435–438 | 6/15, 4/11, 3/9 |
| `sat-full` | 137–138, 144–146, 399.9–403, 430–440 | 20/44, 11/26, 5/14 |
| `sat-s` | 2200–2290, 2400–2450 | 185/372, 87/176, 42/86 |

`tools/satplan.py` places the hops so that the outer 0.425 × rate of each hop is flat and every band
frequency stays at least 100 kHz from a hop centre (the DC/LO-leak skirt). With `--dither`, the planner
adds a second grid and repairs any remaining interval so that every band frequency is seen from two
different LOs. An RF signal stays at the same frequency from both LOs. A receiver spur stays at the same
offset from each LO, so it moves in RF from one view to the other (see "Line origin" below). More hops
means a slower revisit. At 8 MS/s with 14 hops, each band is revisited about every 9 s.

## What a run does

1. **Plan and reference.** `satplan.py` writes `bands.json`. The shared satellite cache
   (`sessions/satdb`) is refreshed when it is older than 24 h. The cache is then frozen together with
   `sessions/qth.json` into `SESSION/reference`. This immutable snapshot (`satdb.py snapshot`) is what
   every later analysis uses, so a reanalysis next month still uses the orbital elements from capture
   time. If the snapshot fails, RF capture continues, but attribution is disabled.
2. **Rate pilot.** A 30 s capture runs at 50 dB at the planned rate. The rate is halved (8 → 4 → 2)
   and the band plan is rebuilt if a measurement fails or if there are more than 3 overflows during
   settling. The device string sets the USB ring:
   - `num_recv_frames = 256 × rate`, capped at 1024. macOS libusb rejects 2048.
   - `recv_frame_size = 16360` at 8 MS/s, so the ring holds about 0.5 s.
   - The analog bandwidth is 1.25 × the rate.
3. **Gain selection (`auto`).** One guarded sweep runs per gain: 30, 40, 50, 60, 70 and 76 dB.
   - The raw-IQ headroom guard watches each sweep.
   - A USB overflow or read timeout is measured again, up to 3 tries. The first real headroom rejection
     ends the sweep.
   - `gaincal.py` then picks the smallest gain whose input-referred noise is within 1 dB of the best one.
     That gain must also keep max |I|,|Q| < 0.5 and p99.9 < 0.2 of full scale with a 12 dB burst reserve
     (both multiplied by 3.98).
   - If no gain qualifies, the scan is not started.
   - The result is `gaincal.json` and `gaincal.png`.
4. **Supervised acquisition** (`superscan_run.py`) repeats attempts until the requested time has been
   credited. Each attempt is a `soapy_power` child process.
   - **What is kept:** the raw output (`attempt-NNN.jsonl` and `.log`) is never modified. Only validated
     complete records are copied to `capture-NNN.jsonl`. A record must be complete, timestamp-verified,
     headroom-accepted and RF-state-valid, with the expected gain and an unchanged receiver context.
   - **What is credited:** record durations plus retune gaps up to 5 s. Setup, outages, pauses and
     larger gaps are not credited.
   - **Transport faults** (overflow, read timeout, libusb/UHD USB errors, device absent) are retried with
     backoff. The supervisor stops after 5 consecutive attempts without new records, or after 20
     automatic attempts.
   - **Stalls:** a file-growth watchdog kills a child process group that stops writing for 60 s.
   - **Headroom:** a rejection must leave hash-verified retained IQ. The gain is then lowered by 6 dB,
     not below 20 dB, and continues in a new capture partition. Gains are never mixed in one file.
   - **Other failures** are fatal.
   - **Bookkeeping:** `captures.json` records every attempt, its failure class, credited time and the
     operator controls.
   - **Operator controls** (pause, resume, retry, abort) come from the GUI. They do not spend the
     automatic retry budget.
   - **Sleep:** on macOS the supervisor holds `caffeinate -i -s` while it runs.
5. **Report.** `analyze_scan_set.py` analyses every capture partition separately and publishes
   `SESSION/analysis` as an atomic bundle.

## Session folder

| Path | Content |
| --- | --- |
| `request.plist` | The typed request (used by Repeat scan in the GUI). |
| `bands.json`, `telemetry-policy.json` | Band plan and sensor policy used for the capture. |
| `reference/` | Frozen satellite database and observer position (bundle, see below). |
| `pilot-*M.jsonl/.log`, `gaincal/`, `gaincal.json`, `gaincal.png` | Rate pilot and gain calibration evidence. |
| `attempt-NNN.jsonl/.log` | Raw child output, unchanged. |
| `capture-NNN.jsonl` | Validated records of each attempt (one gain context each). |
| `rejected-main-N/` | Retained IQ and metadata of rejected acquisitions (hash-indexed). |
| `captures.json` | Supervisor manifest: status, attempts, credited/dwell/paused seconds, controls. |
| `scan.log`, `result.txt` | Supervisor log and one-line step summary. |
| `analysis/` → generation | Published report bundle (`index.html`, `full-analysis.pdf`, `summary.json`, `telemetry.csv/png`, `part-NN/`). |
| `analysis.log`, `analysis-failure.txt` | Report log, and the traceback if the last report failed. |

Each `analysis/part-NN/` is one capture partition analysed by `analyze_superscan.py`:

- `superscan_analysis.pdf`, with one page per band plus Doppler, identification, passes and timing pages
- PNGs of each page
- an interactive `superscan_waterfall_*.html` per band
- `superscan_{tracks,lines,events,passes}.csv`
- `superscan_summary.json`

## Analysis

`analyze_superscan.py` builds one waterfall per band:

1. **Levelling and stitching.** Each hop record is levelled by its own noise floor: the 30th-percentile
   filter of the 20th time-percentile. Each band bin is then taken from the hop that sees it closest to
   that hop's centre, outside the DC skirt.
2. **Tracks.** Peaks above 5σ (3-bin power average) are linked from sweep to sweep. The linking gate is
   3 bins plus the largest Doppler rate a LEO satellite can produce over the sweep interval, and a track
   may miss up to 5 sweeps. Each track is fitted with a constant, a line and a Doppler S-curve
   f₀ − A·tanh((t − tca)/τ). Wide signals are re-centred on their spectral centroid and merged.
3. **Classification.** A track is **LEO-Doppler-like** when all of these hold:
   - it falls in frequency by more than 2.5 bins;
   - the total shift and the peak rate stay within what a LEO pass can produce at that frequency
     (|Δf| ≤ 2.2·f·8 km/s/c, rate ≤ 1.15·f·v²/(c·300 km));
   - the fit explains more than 60 % of the variance;
   - the residual is at most 1.5 bins.

   Other moving tracks are **drift**. A drift that most long tracks of the same hop share is
   **common-mode drift** (receiver LO). The rest are **stable**.
4. **Persistent lines** are run-mean excesses of at least 0.5 dB that are present in at least 25 % of
   sweeps and are not the footprint of a moving track. **Events** are intermittent emissions: connected
   regions more than 8σ above the floor, shorter than half the run. **Wideband transients** are sweeps
   in which more than 2 % of a hop's bins are above 3σ.
5. **Line origin.** With two LO views (dither), each line is classified as follows:
   - **RF**: seen at the same RF frequency from both LOs.
   - **LO spur**: both views cover it but only one shows it, or it appears at the same LO offset in
     several hops.
   - **Not decided**: covered by one LO view only.

   Lines at exact 10 MHz multiples are also flagged as a possible reference harmonic (the B200's 40 MHz
   clock).

## Identification

The reference snapshot supplies SatNOGS transmitters (downlink, mode, baud, status), satellites and
TLEs. Orbital elements older than 14 days at either end of the capture are excluded; this is an
engineering limit, not an accuracy guarantee. Passes are predicted with SGP4 (TEME → ECEF via GMST,
WGS84 observer), and the Doppler is f_rx = f_tx·(1 − ṙ/c). The checks below run in this order.

| Method | Reported as | Requirement |
| --- | --- | --- |
| Track match | strong / probable | An observed track follows a predicted in-band curve within 4 / 8 kHz of the listed downlink. A constant transmitter offset is allowed, but the predicted shift must exceed 1 kHz and the observed shift must follow it. |
| Orbit-only | orbit match | A narrow (≤ 7 bins), clean (rms < 200 Hz) LEO track that no listed downlink explains, attributed only when exactly one satellite's orbit fits clearly best. Wide signals are never attributed by orbit. |
| Guided search | guided (N sweeps) | Searches ±6 kHz around a predicted curve, sweep by sweep, with persistent lines masked. It needs ≥ 3 consistent hits that follow the curve. **The prediction must move at least 2 bins across the hits, and the offsets must scatter less than 0.6× the observed frequencies.** Otherwise a fixed ground station on the satellite's simplex frequency fits just as well, and the result is downgraded to frequency-only. One signal is attributed to one satellite. |
| Doppler-corrected stack | stacked z | Rows are aligned on the predicted curve and averaged (top 10 % trimmed). A detection needs z ≥ 7, a Doppler excursion of at least 6 bins, and must be sharper along the curve than at any fixed frequency the curve crosses. |
| Frequency coincidence | frequency-only | An event near a predicted downlink whose bandwidth does not contradict the transmitter's mode/baud. Events inside a broadband transient are not offered. **This is not an identification.** |

**False-alarm controls.** Every guided search and stack is repeated ±45 kHz off the curve. The report
gives the number of control detections on noise. A control that crosses an already detected signal is
counted separately. **Format** compares the observed −10 dB width with the width expected from the
transmitter's mode and baud, down to the resolution limit (7 bins).

Satellites that passed above 10° with an active in-band downlink and were not heard are listed. Their
absence is evidence about this receiver and antenna, not about the satellite.

## Reports and publication

All reports and references are published through `tools/report_bundle.py`:

- **Building:** the output is built in a private generation directory, `bundle-manifest.json` records the
  SHA-256 hashes of the inputs and outputs, and then `DEST` is switched to it with one atomic symlink
  replacement.
- **Failures:** if a render fails, or an input changes during the build, the previous report is kept and
  the command exits nonzero. Readers that need a consistent set of files resolve the symlink once.
- **Old generations** are kept for audit. To prune or check a bundle:

```sh
python tools/report_bundle.py verify SESSION/analysis          # recompute and compare all hashes
python tools/report_bundle.py prune SESSION/analysis --keep 2  # keep the published + newest previous
```

`analyze_scan_set.py OUTPUT SESSION [SESSION ...]` builds one combined report from any number of
sessions. Gain and rate contexts stay in separate parts, and detection counts must not be added across
parts as unique transmitters.

## Observer position

`sessions/qth.json` (`lat_deg`, `lon_deg`, `alt_m`, optional `source`, `min_elevation_deg`) stays inside
the sessions folder, which git ignores. Published summaries record only the `source` text. Reference
provenance lists file names without local paths, and the hash of `qth.json` is withheld, because a short
structured file's digest would let someone confirm a guessed location. Do not commit `sessions/` or a
`reference/` folder.

## Limits

- A PSD match is evidence for a candidate, not a decoded identity. Orbit predictions depend on TLE age.
  Satellites on shared frequencies (for example Kinéis, Orbcomm) are reported with their alternatives.
- Levels are uncalibrated dB(input power/Hz). Gain changes, pauses and retries produce separate
  partitions, and nothing is pooled across them as calibrated power.
- A 12 dB pilot reserve is an engineering margin. It cannot guarantee future burst levels or analog
  linearity.
- Sustained 8 MS/s depends on the host USB controller. The pilot and the supervisor handle overflows,
  but they do not certify a cable or a controller.

## Tools

| Tool | Purpose |
| --- | --- |
| `tools/satplan.py` | Hop plan for a preset or a custom `bands.json` (`--rate`, `--dither`). |
| `tools/satdb.py` | `refresh` the SatNOGS/CelesTrak cache, `snapshot` a frozen reference, `passes` for a window. |
| `tools/gaincal.py` | Select the automatic gain from the guarded gain sweep. |
| `tools/superscan_run.py` | Supervise attempts, credit time, recover, and answer operator controls. |
| `tools/analyze_superscan.py` | Analyse one capture partition (waterfalls, tracks, lines, identification). |
| `tools/superscan_id.py` | Identification module used by the analysis. |
| `tools/analyze_scan_set.py` | Combined report over sessions and partitions, telemetry and retained IQ. |
| `tools/report_bundle.py` | Atomic publication, `verify` and `prune` of report/reference bundles. |

Install the analysis dependencies with
`python -m pip install numpy scipy matplotlib sgp4 reportlab pypdf`. The runner installs them into its
own virtual environment.
