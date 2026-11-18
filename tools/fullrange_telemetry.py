#!/usr/bin/env python3
"""Summarize all telemetry from a fullrange features-on session.

Reads SESSION/features-on.jsonl (and waterfall.json for pass-to-pass drift) and
writes SESSION/telemetry.json. Relative, uncalibrated levels; the RFIC sensor is
coarsely quantised, so its fast comb is quantisation, not resolved thermal
dynamics. usage: fullrange_telemetry.py SESSION [--out PATH]"""
import argparse
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np


def num(x):
    return np.array([np.nan if v is None else v for v in x], float)


def load_waterfall(sess):
    path = sess / 'waterfall.json'
    if not path.exists():
        return None, None
    w = json.loads(path.read_text())
    if 'features-on' not in w:
        return None, None
    f = w['start_mhz'] + w['cell_mhz'] * (np.arange(len(w['features-on']['grid'][0])) + .5)
    grid = num([x for row in w['features-on']['grid'] for x in row]).reshape(
        len(w['features-on']['grid']), -1)
    return f, grid


def ols(x, y):
    k = np.isfinite(x) & np.isfinite(y)
    x, y = x[k], y[k]
    if x.size < 3:
        return None, None, None
    slope, intercept = np.polyfit(x, y, 1)
    pred = slope * x + intercept
    se = math.sqrt(np.sum((y - pred) ** 2) / (x.size - 2) / np.sum((x - x.mean()) ** 2))
    return float(slope), float(se), float(np.corrcoef(x, y)[0, 1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('session')
    ap.add_argument('--out', default=None)
    a = ap.parse_args()
    sess = Path(a.session)
    out = Path(a.out) if a.out else sess / 'telemetry.json'

    f, grid = load_waterfall(sess)
    times, temps, passes = [], [], []
    chains, failures, fresh, streaming = set(), 0, 0, 0
    missing = 0
    t0 = prev = None
    p = -1
    with open(sess / 'features-on.jsonl') as fh:
        for line in fh:
            r = json.loads(line)
            meta = r.get('metadata') or {}
            ts = r.get('time_start')
            if not ts:
                continue
            t = datetime.fromisoformat(ts)
            t0 = t0 or t
            center = meta.get('tuned_center_hz')
            if center is not None:
                if prev is None or center < prev - 1e6:
                    p += 1
                prev = center
            after = (meta.get('rf_state') or {}).get('after') or {}
            chain = (after.get('chain') or {}).get('sha256')
            if chain:
                chains.add(chain)
            validity = after.get('validity') or {}
            if validity.get('failures'):
                failures += 1
            telemetry = after.get('telemetry') or {}
            temp_state = telemetry.get('rx:temp') or {}
            fresh += bool(temp_state.get('fresh'))
            streaming += bool(temp_state.get('polled_while_streaming'))
            temp = validity.get('temperature_c')
            if temp is None:
                missing += 1
                continue
            times.append((t - t0).total_seconds())
            temps.append(float(temp))
            passes.append(p)

    times, temps, passes = np.array(times), np.array(temps), np.array(passes)
    uniq = np.unique(temps)
    steps = np.diff(uniq) if uniq.size > 1 else np.array([])

    per_pass = []
    for k in range(p + 1):
        sel = passes == k
        if not sel.any():
            continue
        tt, cc = times[sel], temps[sel]
        slope = float(np.polyfit(tt, cc, 1)[0] * 60) if tt.size > 2 else None
        drift = None
        if grid is not None and k < grid.shape[0]:
            drift = float(np.nanmedian(grid[k] - np.nanmedian(grid, axis=0)))
        per_pass.append({
            'pass': int(k),
            'start_s': round(float(tt.min()), 2),
            'end_s': round(float(tt.max()), 2),
            'mean_c': round(float(cc.mean()), 4),
            'min_c': round(float(cc.min()), 4),
            'max_c': round(float(cc.max()), 4),
            'slope_c_per_min': None if slope is None else round(slope, 5),
            'drift_db': None if drift is None else round(drift, 4),
        })

    drift_summary = {'passes': 0, 'range_db': None, 'ols_slope_db_per_c': None,
                     'ols_se_db': None, 'correlation': None,
                     'note': 'diagnostic only: antenna input; drift is whole-band median after per-cell frequency removal'}
    if grid is not None:
        med_cell = np.nanmedian(grid, axis=0)
        drift = np.nanmedian(grid - med_cell, axis=1)
        pt = np.array([row['mean_c'] for row in per_pass if row['drift_db'] is not None])
        dr = np.array([row['drift_db'] for row in per_pass if row['drift_db'] is not None])
        slope, se, r = ols(pt, dr)
        drift_summary.update({
            'passes': int(len(dr)),
            'range_db': round(float(np.nanmax(dr) - np.nanmin(dr)), 4),
            'ols_slope_db_per_c': None if slope is None else round(slope, 6),
            'ols_se_db': None if se is None else round(se, 6),
            'correlation': None if r is None else round(r, 4),
        })

    band = {}
    if grid is not None:
        band = (f >= 150) & (f <= 160)
    on_band = np.nanmedian(grid[:, band], axis=1) if band.any() else np.array([])
    off_band = np.array([])
    wpath = sess / 'waterfall.json'
    if band.any() and wpath.exists():
        w = json.loads(wpath.read_text())
        if 'features-off' in w:
            off = num([x for row in w['features-off']['grid'] for x in row]).reshape(
                len(w['features-off']['grid']), -1)
            off_band = np.nanmedian(off[:, band], axis=1)

    means = [row['mean_c'] for row in per_pass]
    summary = {
        'session': str(sess),
        'kind': 'fullrange_telemetry',
        'records': int(times.size),
        'passes': int(p + 1),
        'seconds': round(float(times.max()), 2),
        'temperature': {
            'readings': int(temps.size),
            'missing': int(missing),
            'min_c': round(float(temps.min()), 4),
            'max_c': round(float(temps.max()), 4),
            'median_c': round(float(np.median(temps)), 4),
            'span_c': round(float(temps.max() - temps.min()), 4),
            'first_c': round(float(temps[0]), 4),
            'last_c': round(float(temps[-1]), 4),
            'quantisation_levels': int(uniq.size),
            'step_c': None if steps.size == 0 else round(float(np.median(steps)), 4),
            'pass_mean_range_c': round(float(max(means) - min(means)), 4),
            'fresh_readings': int(fresh),
            'polled_while_streaming': int(streaming),
            'validity_failures': int(failures),
            'sensor_note': f'RFIC sensor is quantised to {uniq.size} levels in this run '
                           f'(~{float(np.median(steps)):.3f} C step); the fast comb is '
                           'quantisation/dither, not resolved thermal dynamics.'
                           if steps.size else 'RFIC sensor reported a single level in this run.',
        },
        'chain': {'fingerprints': sorted(chains), 'stable': len(chains) == 1},
        'drift': drift_summary,
        'pass_sigma_db': {
            'median': round(float(np.nanmedian(np.nanstd(grid, axis=0))), 4),
            'p95': round(float(np.nanpercentile(np.nanstd(grid, axis=0), 95)), 4),
            'max': round(float(np.nanmax(np.nanstd(grid, axis=0))), 4),
        } if grid is not None else None,
        'band_150_160': {
            'on_per_pass_min_db': round(float(np.nanmin(on_band)), 3) if on_band.size else None,
            'on_per_pass_max_db': round(float(np.nanmax(on_band)), 3) if on_band.size else None,
            'on_per_pass_range_db': round(float(np.nanmax(on_band) - np.nanmin(on_band)), 3) if on_band.size else None,
            'off_per_pass_median_db': round(float(np.nanmedian(off_band)), 3) if off_band.size else None,
            'note': 'cause unresolved; controlled 50 ohm or alternating ON/OFF source run required',
        },
        'per_pass': per_pass,
    }
    out.write_text(json.dumps(summary, indent=1) + '\n')
    print('wrote', out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
