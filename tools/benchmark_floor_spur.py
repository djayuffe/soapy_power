#!/usr/bin/env python3
"""Before/after benchmark: legacy floor/peak/temperature heuristics against the new estimators.

Synthetic truth only (soapypower.spur_twin); relative levels. Prints JSON, optional --markdown.
"""
import argparse
import json
import math
import sys
import numpy as np
from soapypower import floor_estimation as fe
from soapypower import spur_twin as st


def floor_bias(trials=200, bins=4000):
    rows = []
    for dof in (2, 10, 64):
        rng = np.random.default_rng(dof)
        k = dof / 2
        old, new, mh_old, mh_new = [], [], [], []
        for _ in range(trials):
            x = rng.gamma(k, 1 / k, bins)
            y = rng.gamma(k, 1 / k, bins)
            old.append(10 * math.log10(np.median(x)))
            new.append(fe.floor_estimate(x, dof=dof)['mean_db'])
            mh_old.append(10 * math.log10(np.minimum(x, y).mean()))
            mh_new.append(10 * math.log10(np.minimum(x, y).mean()) - fe.min_hold_to_mean_db(2, dof))
        rows.append({'dof': dof, 'median_bias_db_legacy': float(np.mean(old)), 'median_bias_db_new': float(np.mean(new)),
                     'new_sd_db': float(np.std(new)),
                     'minhold2_bias_db_legacy': float(np.mean(mh_old)), 'minhold2_bias_db_new': float(np.mean(mh_new))})
    return rows


def detection(trials=40, bins=20000, dof=8, strengths=(4.0, 8.0, 12.0)):
    rng = np.random.default_rng(3)
    k = dof / 2
    out = {m: {'false_positives': 0, 'noise_bins': 0, 'hits': {s: 0 for s in strengths}, 'lines': 0}
           for m in ('fixed_6db', 'bh_0.01', 'bonferroni_0.01')}
    for _ in range(trials):
        x = rng.gamma(k, 1 / k, bins)
        pos = rng.choice(bins, size=len(strengths) * 5, replace=False)
        truth = {}
        for i, p in enumerate(pos):
            s = strengths[i % len(strengths)]
            x[p] *= 10 ** (s / 10)
            truth[int(p)] = s
        floor = fe.floor_estimate(x, dof=dof)['mean_linear']
        legacy_floor = float(np.median(x))
        for name, kwargs, fl in (('fixed_6db', dict(method='fixed', fixed_db=6.0), legacy_floor),
                                 ('bh_0.01', dict(method='bh', alpha=0.01), floor),
                                 ('bonferroni_0.01', dict(method='bonferroni', alpha=0.01), floor)):
            sel = set(fe.detect_peaks(x, fl, dof, **kwargs)['index'].tolist())
            out[name]['false_positives'] += len(sel - set(truth))
            out[name]['noise_bins'] += bins - len(truth)
            out[name]['lines'] += len(truth)
            for p, s in truth.items():
                out[name]['hits'][s] += int(p in sel)
    for v in out.values():
        v['false_positive_rate'] = v['false_positives'] / v['noise_bins']
        v['detect_rate_by_db'] = {str(s): v['hits'][s] / (v['lines'] / len(strengths)) for s in strengths}
        del v['hits']
    return out


def classification(trials=60):
    rng = np.random.default_rng(11)
    agg = {'legacy': {'tp': 0, 'fp': 0, 'missed': 0, 'kind_errors': 0}, 'new': {'tp': 0, 'fp': 0, 'missed': 0, 'kind_errors': 0}}
    per = {}
    for name, sc in st.scenarios().items():
        per[name] = {'legacy': dict.fromkeys(agg['legacy'], 0), 'new': dict.fromkeys(agg['new'], 0)}
        for t in range(trials):
            seed = int(rng.integers(1 << 30))
            twin = st.Twin(seed=seed)
            n = st.analyze(twin, sc['los'], tones=sc['tones'])
            o = st.legacy_analyze(st.Twin(seed=seed), sc['los'], tones=sc['tones'])
            for label, res in (('new', n), ('legacy', o)):
                sc_ = st.score(res['peaks'], sc['expected'], twin.step)
                for k, v in sc_.items():
                    per[name][label][k] += v
                    agg[label][k] += v
    return {'total': agg, 'per_scenario': per, 'trials_per_scenario': trials}


def temperature(trials=30, profiles=('monotone', 'oscillating')):
    rng = np.random.default_rng(5)
    rows = []
    for profile, truth in [(pf, tr) for pf in profiles for tr in (0.0, 0.024, -0.1)]:
        ols_err, kal_err, kal_sd, kal_conf = [], [], [], []
        for _ in range(trials):
            n_centers, passes = 40, 8
            centers = np.arange(n_centers)
            offs = rng.normal(0, 3, n_centers)
            t, temp, y, g = [], [], [], []
            drift = 0.0
            clock = 0.0
            for p in range(passes):
                for c in centers:
                    clock += 0.5
                    temp_now = 46.3 + (0.9 * clock / (passes * n_centers * 0.5) if profile == 'monotone' else 0.5 * math.sin(2 * math.pi * clock / 55.0)) + rng.normal(0, 0.02)
                    drift += rng.normal(0, 0.004)  # non-thermal drift, correlated with the run
                    t.append(clock); temp.append(temp_now); g.append(int(c))
                    y.append(offs[c] + truth * (temp_now - 46.3) + drift + rng.normal(0, 0.03))
            t, temp, y, g = map(np.array, (t, temp, y, g))
            dt, dy = [], []
            for c in centers:
                m = g == c
                dt.extend(temp[m] - temp[m].mean()); dy.extend(y[m] - y[m].mean())
            dt, dy = np.array(dt), np.array(dy)
            ols = float((dt * dy).sum() / (dt ** 2).sum())
            k = fe.floor_temperature_kalman(t, temp, y, g)
            kal_conf.append(k['confounded'])
            ols_err.append(ols - truth); kal_err.append(k['slope_db_per_c'] - truth); kal_sd.append(k['slope_sd_db'])
        rows.append({'temperature_profile': profile, 'true_slope_db_per_c': truth, 'ols_rms_error': float(np.sqrt(np.mean(np.square(ols_err)))),
                     'kalman_rms_error': float(np.sqrt(np.mean(np.square(kal_err)))),
                     'kalman_reported_sd': float(np.mean(kal_sd)), 'flagged_confounded_share': float(np.mean(kal_conf))})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--quick', action='store_true')
    ap.add_argument('--output')
    args = ap.parse_args(argv)
    q = args.quick
    result = {'floor_bias': floor_bias(40 if q else 200),
              'detection': detection(10 if q else 40),
              'classification': classification(15 if q else 60),
              'floor_vs_temperature': temperature(8 if q else 30)}
    text = json.dumps(result, indent=1)
    if args.output:
        open(args.output, 'w').write(text)
    print(text)


if __name__ == '__main__':
    sys.exit(main())
