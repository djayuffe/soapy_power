#!/usr/bin/env python3
"""Identification and signal-format analysis for analyze_superscan.py (imported, not run directly).

identify(A, satdb_dir, qth) adds to A:
  A['id'] = dict(available, reason, passes=[...], ids=[...], not_heard=[...], db_meta)
  every observed track / event gets 'format' (observed spectral/temporal class) and, when matched, 'id'.
Matching: observed f(t) vs predicted f_tx*(1 - range_rate/c) of every in-band transmitter of every
satellite above the horizon, allowing a constant transmitter offset (see satdb.match_track)."""
import json, math
from pathlib import Path
import numpy as np
import satdb as sd


def _profile(B, rows, cols_center, half_bins):
    """Average (linear) of rows of B aligned on per-row centre column; returns dB profile over +-half_bins."""
    acc = np.zeros(2 * half_bins + 1); cnt = np.zeros(2 * half_bins + 1)
    for r, c in zip(rows, cols_center):
        a, b = c - half_bins, c + half_bins + 1
        seg = np.full(2 * half_bins + 1, np.nan)
        lo, hi = max(a, 0), min(b, B.shape[1]); seg[lo - a:hi - a] = B[r, lo:hi]
        ok = np.isfinite(seg); acc[ok] += 10 ** (seg[ok] / 10); cnt[ok] += 1
    with np.errstate(invalid='ignore', divide='ignore'):
        return 10 * np.log10(acc / cnt)


def measure_format(S, rows, cols, sig, step, duty):
    half = int(40e3 / step)
    prof = _profile(S['B'], rows, cols, half)
    n = max(1, len(rows)); noise = sig / math.sqrt(n) * 1.3
    thr = max(3 * noise, 0.15)
    pk = np.nanmax(prof[half - 3:half + 4]) if np.isfinite(prof[half - 3:half + 4]).any() else np.nan
    if not np.isfinite(pk) or pk < thr:
        return dict(bw_hz=float(step), peak_db=float(pk) if np.isfinite(pk) else None, peaks=1, peak_sep_hz=None, duty=duty,
                    cls=sd.observed_format(step, duty, 1, None, step), profile=prof)
    lvl = max(thr, pk - 10.0)
    above = np.nan_to_num(prof, nan=-9) > lvl
    a = b = half
    while a > 0 and above[a - 1]:
        a -= 1
    while b < len(prof) - 1 and above[b + 1]:
        b += 1
    bw = (b - a + 1) * step
    seg = np.nan_to_num(prof[a:b + 1], nan=-9)
    loc = [i for i in range(1, len(seg) - 1) if seg[i] >= seg[i - 1] and seg[i] >= seg[i + 1] and seg[i] > pk - 4.0]
    merged = []
    for i in loc:                                   # merge maxima closer than 4 bins
        if merged and i - merged[-1] < 4:
            if seg[i] > seg[merged[-1]]:
                merged[-1] = i
        else:
            merged.append(i)
    sep = float(np.mean(np.diff(merged)) * step) if len(merged) >= 2 else None
    return dict(bw_hz=float(bw), peak_db=float(pk), peaks=len(merged), peak_sep_hz=sep, duty=duty,
                cls=sd.observed_format(bw, duty, len(merged), sep, step), profile=prof)


GUIDE_HZ = 6e3        # search half-width around a predicted Doppler curve (TLE age + transmitter offset)
CONTROL_HZ = 45e3     # same search shifted off every curve: false-alarm control


def _band_cache(Bd):
    c = Bd.get('_gcache')
    if c is None:
        S = Bd['S']; D = S['D']; m0 = np.nanmedian(D); s0 = 1.4826 * np.nanmedian(np.abs(D - m0))
        c = Bd['_gcache'] = dict(thr_base=(m0, s0), t_row=np.nanmedian(S['T'], 1), f=S['f'], fsrc=S['fsrc'])
    return c


def guided(A, p, tx, shift=0.0, k_sigma=5.0):
    """Search the band residual along one predicted Doppler curve, sweep by sweep (bursty beacons included).
    Returns hits [(sweep, t_s, f_obs, offset_hz, level_db)] or None when the curve is not inside a band."""
    f_tx = tx['f_hz'] + shift
    for Bd in A['bands']:
        b = Bd['band']
        if not (b['lo_hz'] <= f_tx <= b['hi_hz']):
            continue
        S = Bd['S']; D = S['D']; c = _band_cache(Bd); m0, s0 = c['thr_base']; thr = m0 + k_sigma * s0
        t = c['t_row']; ok = np.isfinite(t)
        el = np.full(len(t), -90.0); el[ok] = np.interp(t[ok], p['t'], p['el'])
        rows = np.flatnonzero(ok & (el > 0))
        if not len(rows):
            return []
        fp = f_tx * (1 - np.interp(t[rows], p['t'], p['rr']) / sd.C)
        lo = np.searchsorted(c['f'], fp - GUIDE_HZ); hi = np.searchsorted(c['f'], fp + GUIDE_HZ, side='right'); out = []
        for r, a, b_, f_ in zip(rows, lo, hi, fp):
            if b_ - a < 3:
                continue
            row = D[r, a:b_]
            if not np.isfinite(row).any():
                continue
            j = int(np.nanargmax(row))
            if row[j] > thr:
                fo = float(c['fsrc'][a + j]); out.append((int(r), float(t[r] - A['t_ref']), fo, fo - float(f_), float(row[j])))
        return out
    return None


def consistent_hits(hits, tol=1.5e3):
    """Largest group of hits whose offsets agree within tol (a real transmitter keeps its offset)."""
    if not hits:
        return []
    offs = np.array([h[3] for h in hits]); best = []
    for o in offs:
        g = [h for h in hits if abs(h[3] - o) <= tol]
        if len(g) > len(best):
            best = g
    return best


def identify(A, satdb_dir=None, qth_path=None):
    step = A['info']['step_hz']; period = A['timing']['sweep_period_s']; t_ref = A['t_ref']
    # observed format for every non-trivial track and event (works without a database)
    for Bd in A['bands']:
        S = Bd['S']
        for tr in Bd['tracks']:
            t, fx = tr['points']; rows = [int(np.argmin(np.abs(S['T'][:, 0] - t_ref - x))) for x in t]
            cols = [int(np.argmin(np.abs(S['fsrc'] - f))) for f in fx]
            span = max(t.max() - t.min(), period); duty = min(1.0, len(t) / (span / period + 1))
            tr['format'] = {k: v for k, v in measure_format(S, rows, cols, Bd['sig'], step, duty).items() if k != 'profile'}
        for e in Bd['events']:
            r0 = int(np.argmin(np.abs(S['T'][:, 0] - t_ref - e['t_start_s']))); r1 = int(np.argmin(np.abs(S['T'][:, 0] - t_ref - e['t_end_s'])))
            rows = list(range(r0, r1 + 1)); c = int(np.argmin(np.abs(S['fsrc'] - e['f_mhz'] * 1e6)))
            e['format'] = {k: v for k, v in measure_format(S, rows, [c] * len(rows), Bd['sig'], step, 1.0).items() if k != 'profile'}
    res = dict(available=False, reason='', passes=[], ids=[], not_heard=[], db_meta=None)
    A['id'] = res
    try:
        import sgp4  # noqa: F401
    except Exception:
        res['reason'] = 'sgp4 not installed (queue.sh --satdb-refresh 1 installs it)'; return res
    if not satdb_dir or not Path(satdb_dir, 'tle.json').exists():
        res['reason'] = 'no satellite database (queue.sh --satdb-refresh 1)'; return res
    if not qth_path or not Path(qth_path).exists():
        res['reason'] = 'no observer position (sessions/qth.json)'; return res
    qth = json.loads(Path(qth_path).read_text()); db = sd.load(satdb_dir)
    meta = json.loads(Path(satdb_dir, 'meta.json').read_text()) if Path(satdb_dir, 'meta.json').exists() else {}
    res['db_meta'] = dict(fetched_utc=meta.get('fetched_utc'), satellites_with_downlinks=len(db), tle_count=meta.get('tle_count'))
    bands = [Bd['band'] for Bd in A['bands']]
    t0, t1 = t_ref, t_ref + A['timing']['span_s']
    preds = sd.predict(db, qth, t0, t1, bands)
    res['available'] = True; res['qth_source'] = qth.get('source', '')
    used = set()
    for Bd in A['bands']:
        for tr in Bd['tracks']:
            t, fx = tr['points']
            if len(t) < 6:
                continue
            c = sd.match_track(t + t_ref, fx, preds)
            if not c:
                continue
            best = c[0]; conf = sd.confidence(best, tr['rms_hz'], tr['shift_hz'])
            if conf == 'frequency-only':
                continue                            # a track is only attributed when its Doppler shape was tested
            tr['id'] = dict(norad=best['norad'], name=best['name'], transmitter=best['tx']['description'], mode=best['tx']['mode'], baud=best['tx']['baud'],
                            f_tx_hz=best['tx']['f_hz'], offset_hz=best['offset_hz'], rms_hz=best['rms_hz'], confidence=conf, alternatives=len(c) - 1,
                            format_check=sd.format_consistency(best['tx'], tr['format']['bw_hz'], step))
            used.add(best['norad'])
            res['ids'].append(dict(kind='track', band=Bd['band']['name'], f_mhz=tr['f_median_hz'] / 1e6, t_start_s=tr['t_start_s'], t_end_s=tr['t_end_s'],
                                   observed_format=tr['format']['cls'], observed_bw_hz=tr['format']['bw_hz'], **tr['id']))
        for e in Bd['events']:
            tm = t_ref + (e['t_start_s'] + e['t_end_s']) / 2; hits = []
            for p in preds:
                if np.interp(tm, p['t'], p['el']) < -2 or str(p['sat_status']).lower() in ('dead', 're-entered', 'decayed'):
                    continue
                for tx in p['transmitters']:
                    d = e['f_mhz'] * 1e6 - float(sd.doppler(p, tx['f_hz'], tm))
                    if tx['alive'] and abs(d) < 6e3:
                        hits.append((abs(d), p, tx, d))
            if hits:
                hits.sort(key=lambda h: h[0]); _, p, tx, d = hits[0]
                e['id'] = dict(norad=p['norad'], name=p['name'], transmitter=tx['description'], mode=tx['mode'], baud=tx['baud'], f_tx_hz=tx['f_hz'], offset_hz=d,
                               confidence='frequency-only', alternatives=len(hits) - 1, format_check=sd.format_consistency(tx, e['format']['bw_hz'], step))
                used.add(p['norad'])
                res['ids'].append(dict(kind='event', band=Bd['band']['name'], f_mhz=e['f_mhz'], t_start_s=e['t_start_s'], t_end_s=e['t_end_s'],
                                       observed_format=e['format']['cls'], observed_bw_hz=e['format']['bw_hz'], rms_hz=None, **e['id']))
    # guided search along every predicted curve not already matched by a track/event, with an off-curve control
    res['guided'] = []; ctrl_hits = ctrl_n = 0
    for p in preds:
        if p['norad'] in used:
            continue
        for tx in p['transmitters']:
            if not tx['alive']:
                continue
            hits = guided(A, p, tx)
            if hits is None:
                continue
            g = consistent_hits(hits)
            for sh in (CONTROL_HZ, -CONTROL_HZ):
                c = consistent_hits(guided(A, p, tx, shift=sh) or []); ctrl_n += 1; ctrl_hits += len(c) >= 2
            if len(g) >= 2:
                used.add(p['norad']); off = float(np.median([h[3] for h in g])); lv = float(max(h[4] for h in g))
                rows = [h[0] for h in g]; Bd = next(B for B in A['bands'] if B['band']['lo_hz'] <= tx['f_hz'] <= B['band']['hi_hz'])
                cols = [int(np.argmin(np.abs(Bd['S']['fsrc'] - h[2]))) for h in g]
                fmt = {k: v for k, v in measure_format(Bd['S'], rows, cols, Bd['sig'], step, len(g) / max(1, len(hits) + 1)).items() if k != 'profile'}
                same = [q['name'] for q in preds if q['norad'] != p['norad'] and any(abs(x['f_hz'] - tx['f_hz']) < 2 * GUIDE_HZ for x in q['transmitters'])]
                res['ids'].append(dict(kind='guided', band=Bd['band']['name'], f_mhz=g[0][2] / 1e6, t_start_s=min(h[1] for h in g), t_end_s=max(h[1] for h in g),
                                       observed_format=fmt['cls'], observed_bw_hz=fmt['bw_hz'], norad=p['norad'], name=p['name'], transmitter=tx['description'],
                                       mode=tx['mode'], baud=tx['baud'], f_tx_hz=tx['f_hz'], offset_hz=off, rms_hz=float(np.std([h[3] for h in g])),
                                       confidence=f"guided ({len(g)} sweeps)" + (' ambiguous: ' + ', '.join(same[:3]) if same else ''), alternatives=len(same),
                                       format_check=sd.format_consistency(tx, fmt['bw_hz'], step), peak_db=lv))
                res['guided'].append(dict(norad=p['norad'], name=p['name'], f_mhz=tx['f_hz'] / 1e6, sweeps=len(g), offset_hz=off, peak_db=lv))
                break
    res['control'] = dict(searches=ctrl_n, false_detections=ctrl_hits, shift_hz=CONTROL_HZ)
    for p in preds:
        alive = [x for x in p['transmitters'] if x['alive'] and x['status'] in ('active', '')]
        row = dict(norad=p['norad'], name=p['name'], sat_status=p['sat_status'], max_el=p['max_el'], t_max_el_s=p['t_max_el'] - t_ref,
                   t_rise_s=p['t_rise'] - t_ref, t_set_s=p['t_set'] - t_ref, heard=p['norad'] in used,
                   transmitters=[dict(f_mhz=x['f_hz'] / 1e6, mode=x['mode'], baud=x['baud'], alive=x['alive'], status=x['status'], description=x['description']) for x in p['transmitters']],
                   curve_t_s=(p['t'] - t_ref).tolist(), curve_el=np.nan_to_num(p['el'], nan=-90).tolist(), curve_rr=np.nan_to_num(p['rr']).tolist())
        res['passes'].append(row)
        if not row['heard'] and alive and p['max_el'] >= 10:
            res['not_heard'].append(row)
    return res


def predicted_curves(A, band, min_el=0.0):
    """(label, t_s array, f_hz array) of every alive in-band transmitter of a visible satellite."""
    out = []
    for p in A.get('id', {}).get('passes', []):
        t = np.array(p['curve_t_s']); el = np.array(p['curve_el']); rr = np.array(p['curve_rr'])
        m = (el > min_el) & (t >= 0) & (t <= A['timing']['span_s'])
        if m.sum() < 2:
            continue
        for x in p['transmitters']:
            f = x['f_mhz'] * 1e6
            if band['lo_hz'] - 20e3 <= f <= band['hi_hz'] + 20e3:
                out.append((f"{p['name']} {x['mode']}", t[m], f * (1 - rr[m] / sd.C), p['heard']))
    return out
