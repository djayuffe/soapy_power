#!/usr/bin/env python3
"""Compare the full-range (70 MHz-6 GHz) features-on / features-off sessions. Relative levels only."""
import argparse
import json
import sys
from pathlib import Path
import numpy as np

CELL_HZ = 1e6


def load(path, usable=0.35, dc_hz=0.5e6, start_hz=70e6, stop_hz=6e9):
    cells, vals, hops, times, temps = [], [], set(), [], 0
    tseries, rseries = [], []
    records = 0
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            if r.get('power') is None:
                continue
            records += 1
            p = np.asarray(r['power'], dtype=float)
            f0, w = float(r['first_bin_center_hz']), float(r['bin_width_hz'])
            freq = f0 + w * np.arange(p.size)
            center = 0.5 * (r['coverage_start_hz'] + r['coverage_stop_hz'])
            half = 0.5 * (r['coverage_stop_hz'] - r['coverage_start_hz'])
            keep = (np.abs(freq - center) <= usable * 2 * half) & (np.abs(freq - center) >= dc_hz) & np.isfinite(p) & (freq >= start_hz) & (freq < stop_hz)
            idx = np.floor(freq[keep] / CELL_HZ).astype(np.int64)
            for c in np.unique(idx):
                cells.append(c)
                vals.append(float(np.mean(p[keep][idx == c])))
            hops.add(round(center))
            times.append(r['time_start'])
            rseries.append((r['time_start'], round(center)))
            meta = r.get('metadata') or {}
            rs = (meta.get('rf_state') or {}).get('after') or {}
            if (rs.get('validity') or {}).get('temperature_c') is not None:
                temps += 1
                tseries.append((r['time_start'], float(rs['validity']['temperature_c'])))
    return {'records': records, 'hops': len(hops), 'cells': np.array(cells), 'vals': np.array(vals),
            'time_first': times[0] if times else None, 'time_last': times[-1] if times else None, 'temperature_records': temps, 'tseries': tseries, 'rseries': rseries}


def levels(d):
    out = {}
    order = np.argsort(d['cells'], kind='stable')
    c, v = d['cells'][order], d['vals'][order]
    for u in np.unique(c):
        out[int(u)] = float(np.median(v[c == u]))
    return out


def counts_of(d, cells):
    u, n = np.unique(d['cells'], return_counts=True)
    m = dict(zip(u.tolist(), n.tolist()))
    return [m.get(int(c), 0) for c in cells]


def _series(rows, t0, step=25):
    from datetime import datetime
    if not rows:
        return []
    a = datetime.fromisoformat(t0)
    return [[round((datetime.fromisoformat(t) - a).total_seconds(), 2), round(v, 3)] for t, v in rows[::step]]


def _hops(rows, t0, step=20):
    from datetime import datetime
    a = datetime.fromisoformat(t0)
    return [[round((datetime.fromisoformat(t) - a).total_seconds(), 2), c / 1e6] for t, c in rows[::step]]


def summarize(name, d):
    lv = levels(d)
    cells = np.array(sorted(lv))
    db = np.array([lv[c] for c in cells])
    from datetime import datetime
    secs = None
    if d['time_first']:
        a, b = (datetime.fromisoformat(d['time_first']), datetime.fromisoformat(d['time_last']))
        secs = (b - a).total_seconds()
    passes = d['records'] / max(d['hops'], 1)
    return {'variant': name, 'records': d['records'], 'hops': d['hops'], 'passes': passes, 'seconds': secs,
            'records_per_s': d['records'] / secs if secs else None, 'cells_1MHz': int(cells.size),
            'span_mhz': [float(cells.min()), float(cells.max())] if cells.size else None,
            'cell_level_median_db': float(np.median(db)) if db.size else None,
            'cell_level_p5_p95_db': [float(np.percentile(db, 5)), float(np.percentile(db, 95))] if db.size else None,
            'temperature_records': d['temperature_records'],
            'cell_mhz': [int(c) for c in cells], 'cell_level_db': [round(float(x), 3) for x in db],
            'cell_observations': [int(n) for n in counts_of(d, cells)],
            'temperature_series': _series(d['tseries'], d['time_first']), 'hop_series': _hops(d['rseries'], d['time_first'])}, lv


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('session')
    ap.add_argument('--input', default='antenna')
    a = ap.parse_args(argv)
    sess = Path(a.session)
    out = {'session': str(sess), 'input': a.input, 'scope': 'relative uncalibrated levels; 1 MHz cells, hop edges and DC excluded',
           'variants': {}}
    lvs = {}
    for name in ('features-on', 'features-off'):
        p = sess / (name + '.jsonl')
        if not p.exists():
            out['variants'][name] = {'status': 'missing'}
            continue
        summary, lv = summarize(name, load(p))
        out['variants'][name] = summary
        lvs[name] = lv
    if len(lvs) == 2:
        common = sorted(set(lvs['features-on']) & set(lvs['features-off']))
        diff = np.array([lvs['features-on'][c] - lvs['features-off'][c] for c in common])
        mad = float(np.median(np.abs(diff - np.median(diff))) * 1.4826)
        out['agreement'] = {'common_cells': len(common), 'median_diff_db': float(np.median(diff)), 'robust_sd_db': mad,
                            'p95_abs_diff_db': float(np.percentile(np.abs(diff - np.median(diff)), 95)),
                            'cells_over_3db': int((np.abs(diff - np.median(diff)) > 3).sum()),
                            'note': 'antenna input: both sessions see changing external signals, so large cell differences are expected'
                                    if a.input == 'antenna' else 'terminated input: differences are receiver effects'}
    json.dump(out, sys.stdout, indent=1)
    print()


if __name__ == '__main__':
    main()
