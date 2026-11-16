#!/usr/bin/env python3
"""Compare bladeRF survey runs (scan.jsonl.gz): throughput, headroom, LO/DC residual, gain-referenced floor.

Per hop: residual sample mean |mean| (from the recorded headroom metrics), origin-bin excess over the
250 kHz-1 MHz ring (the DC/LO spike), and the 20th-percentile PSD within +/-7 MHz minus nominal gain.
Per center the median over sweeps is compared, so runs of different length compare fairly.
Relative and uncalibrated; the origin is excluded from fused spectra either way.
"""
import argparse
import gzip
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

BANDS = [(42e6, 237.5e6), (237.5e6, 600e6), (600e6, 1200e6), (1200e6, 2000e6), (2000e6, 2800e6), (2800e6, 3800e6)]


def load(path):
    rows = dict(center=[], t=[], gain=[], mean=[], maxc=[], spike=[], floor=[])
    with gzip.open(path, 'rt') as handle:
        for line in handle:
            record = json.loads(line)
            meta = record['metadata']
            if meta['continuity'] != 'verified' or meta['measurement_status'] != 'complete':
                raise ValueError('unverified hop in ' + str(path))
            grid = meta['frequency_grid']
            power = np.asarray(record['power'], float)
            origin = -grid['first_bin_index']
            step = grid['step_hz']
            offsets = (np.arange(grid['bin_count']) + grid['first_bin_index']) * step
            ring = (np.abs(offsets) >= 250e3) & (np.abs(offsets) <= 1e6)
            central = (np.abs(offsets) <= 7e6) & (np.abs(offsets) >= 250e3)
            q = meta['bladerf_quality']
            gain = meta.get('bladerf_gain_nominal_db', sum(meta['context']['gains'].values()) - 6)
            rows['center'].append(meta['tuned_center_hz'])
            rows['t'].append(datetime.fromisoformat(record['time_start']).timestamp())
            rows['gain'].append(gain)
            rows['mean'].append(abs(complex(q['mean_i'], q['mean_q'])))
            rows['maxc'].append(q['max_component'])
            rows['spike'].append(power[origin] - np.median(power[ring]))
            rows['floor'].append(np.quantile(power[central], .2) - gain)
    return {k: np.asarray(v, float) for k, v in rows.items()}


def per_center(data, key):
    centers = np.unique(data['center'])
    return centers, np.array([np.median(data[key][data['center'] == c]) for c in centers])


def summary(data):
    span = data['t'].max() - data['t'].min()
    return dict(hops=int(data['center'].size), seconds=float(span), hops_per_second=float(data['center'].size / span),
                max_component=float(data['maxc'].max()), abs_mean_median=float(np.median(data['mean'])),
                dc_spike_db_median=float(np.median(data['spike'])), dc_spike_db_p90=float(np.quantile(data['spike'], .9)),
                gain_referenced_floor_db_median=float(np.median(data['floor'])))


def compare(a, b):
    """b relative to a, per center (medians over sweeps)."""
    out = {}
    for key in ('mean', 'spike', 'floor', 'gain'):
        ca, va = per_center(a, key)
        cb, vb = per_center(b, key)
        common, ia, ib = np.intersect1d(ca, cb, return_indices=True)
        out[key] = (common, va[ia], vb[ib])
    bands = []
    for lo, hi in BANDS:
        common, ma, mb = out['mean']
        sel = (common >= lo) & (common < hi)
        _, sa, sb = out['spike']
        _, fa, fb = out['floor']
        _, ga, gb = out['gain']
        bands.append(dict(start_mhz=lo / 1e6, stop_mhz=hi / 1e6, centers=int(sel.sum()),
                          abs_mean_a=float(np.median(ma[sel])), abs_mean_b=float(np.median(mb[sel])),
                          abs_mean_change_db=float(np.median(20 * np.log10(np.maximum(mb[sel], 1e-9) / np.maximum(ma[sel], 1e-9)))),
                          dc_spike_a_db=float(np.median(sa[sel])), dc_spike_b_db=float(np.median(sb[sel])),
                          floor_change_db=float(np.median(fb[sel] - fa[sel])),
                          gain_a_db=float(np.mean(ga[sel])), gain_b_db=float(np.mean(gb[sel]))))
    common, ma, mb = out['mean']
    change = 20 * np.log10(np.maximum(mb, 1e-9) / np.maximum(ma, 1e-9))
    return dict(centers=int(common.size), abs_mean_change_db_median=float(np.median(change)),
                centers_better_3db=int((change < -3).sum()), centers_worse_3db=int((change > 3).sum()), bands=bands)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('reference', type=Path, help='scan.jsonl.gz or its directory')
    parser.add_argument('candidate', type=Path)
    parser.add_argument('-O', '--output', type=Path)
    options = parser.parse_args(argv)
    paths = [p / 'scan.jsonl.gz' if p.is_dir() else p for p in (options.reference, options.candidate)]
    a, b = load(paths[0]), load(paths[1])
    report = dict(kind='bladerf_survey_comparison', reference=str(paths[0]), candidate=str(paths[1]),
                  reference_summary=summary(a), candidate_summary=summary(b), per_center=compare(a, b),
                  calibrated=False)
    text = json.dumps(report, indent=1)
    if options.output:
        options.output.write_text(text)
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
