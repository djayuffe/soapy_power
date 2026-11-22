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
    """D with persistent lines and run-long stable tracks blanked (+-3 bins): a predicted curve crossing a
    steady carrier or spur must not count as hearing the satellite."""
    c = Bd.get('_gcache')
    if c is None:
        S = Bd['S']; D = S['D'].copy(); f = S['f']; span = float(np.nanmax(S['T']) - np.nanmin(S['T']))
        steady = [l['f_mhz'] * 1e6 for l in Bd['lines']]
        steady += [t['f_median_hz'] for t in Bd['tracks'] if t['kind'] == 'stable' and t['t_end_s'] - t['t_start_s'] > 0.5 * span]
        for x in steady:
            j = int(np.searchsorted(f, x)); D[:, max(0, j - 3):j + 4] = np.nan
        m0 = np.nanmedian(D); s0 = 1.4826 * np.nanmedian(np.abs(D - m0))
        c = Bd['_gcache'] = dict(thr_base=(m0, s0), t_row=np.nanmedian(S['T'], 1), f=f, fsrc=S['fsrc'], D=D, step=float(np.median(np.diff(f))))
    return c


STACK_Z = 7.0         # detection threshold of the Doppler-corrected stack (look-elsewhere over ~49 offsets included)


def stacked(A, p, tx, shift=0.0, min_el=5.0):
    """Doppler-corrected stack: average the residual along the predicted curve over every sweep with the satellite
    above min_el, for each trial transmitter offset within +-GUIDE_HZ. The top 10 % of rows per offset are dropped
    (a terrestrial burst crossing the window cannot fake a continuous carrier). z = peak over the spread of the
    other offsets. Detects steady carriers ~sqrt(N) weaker than the per-sweep search."""
    f_tx = tx['f_hz'] + shift
    for Bd in A['bands']:
        b = Bd['band']
        if not (b['lo_hz'] <= f_tx <= b['hi_hz']):
            continue
        c = _band_cache(Bd); D = c['D']; t = c['t_row']; ok = np.isfinite(t)
        el = np.full(len(t), -90.0); el[ok] = np.interp(t[ok], p['t'], p['el'])
        rows = np.flatnonzero(ok & (el > min_el))
        if len(rows) < 6:
            return None
        fp = f_tx * (1 - np.interp(t[rows], p['t'], p['rr']) / sd.C)
        offs = np.arange(-GUIDE_HZ, GUIDE_HZ + c['step'] / 2, c['step'])
        idx = np.clip(np.searchsorted(c['f'], fp[:, None] + offs[None, :]), 0, len(c['f']) - 1)
        lin = 10 ** (D[rows[:, None], idx] / 10)
        lin = np.sort(np.where(np.isfinite(lin), lin, np.nan), 0)
        n = np.sum(np.isfinite(lin), 0); keep = np.maximum(1, (n * 0.9).astype(int))
        prof = np.array([np.nanmean(lin[:k, j]) if n[j] >= 6 else np.nan for j, k in enumerate(keep)])
        if np.isfinite(prof).sum() < 20:
            return None
        k = int(np.nanargmax(prof)); far = np.abs(np.arange(len(offs)) - k) > 4
        base, spread = np.nanmedian(prof[far]), np.nanstd(prof[far])
        z = float((prof[k] - base) / spread) if spread > 0 else 0.0
        return dict(z=z, offset_hz=float(offs[k]), excess_db=float(10 * np.log10(prof[k] / base)), rows=int(len(rows)), n=int(n[k]),
                    t_first_s=float(t[rows[0]] - A['t_ref']), t_last_s=float(t[rows[-1]] - A['t_ref']))
    return None


def guided(A, p, tx, shift=0.0, k_sigma=5.0):
    """Search the band residual along one predicted Doppler curve, sweep by sweep (bursty beacons included).
    Returns hits [(sweep, t_s, f_obs, offset_hz, level_db)] or None when the curve is not inside a band."""
    f_tx = tx['f_hz'] + shift
    for Bd in A['bands']:
        b = Bd['band']
        if not (b['lo_hz'] <= f_tx <= b['hi_hz']):
            continue
        S = Bd['S']; c = _band_cache(Bd); D = c['D']; m0, s0 = c['thr_base']; thr = m0 + k_sigma * s0
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


def consistent_hits(hits, tol=1.5e3, min_hits=3):
    """Largest group of >= min_hits hits whose offsets agree within tol (a real transmitter keeps its offset and
    therefore follows the predicted Doppler). If the prediction moves > 1 kHz across the group, the observed
    frequencies must move with it (a steady line inside the search window is rejected)."""
    if len(hits) < min_hits:
        return []
    best = []
    for o in [h[3] for h in hits]:
        g = [h for h in hits if abs(h[3] - o) <= tol]
        if len(g) > len(best):
            best = g
    if len(best) < min_hits:
        return []
    fo = np.array([h[2] for h in best]); fp = fo - np.array([h[3] for h in best])
    if np.ptp(fp) > 1e3 and np.ptp(fo) < 0.3 * np.ptp(fp):
        return []
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
    qth = json.loads(Path(qth_path).read_text()); db = sd.load(satdb_dir); tles = sd.load_tles(satdb_dir)
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
            if not c and tr['kind'] != 'doppler':
                continue
            best = c[0] if c else None; conf = sd.confidence(best, tr['rms_hz'], tr['shift_hz']) if c else 'frequency-only'
            if conf == 'frequency-only':
                narrow = tr['format']['bw_hz'] <= sd.RES_BINS * step and tr['rms_hz'] < 200.0
                if tr['kind'] == 'doppler' and narrow:  # orbit-only: which satellite's pass has this carrier's Doppler curve
                    # (a wide signal's peak wanders inside its band, so its 'curve' cannot tell orbits apart)
                    om = sd.match_orbit(t + t_ref, fx, tles, qth, tr['rms_hz'])
                    res.setdefault('orbit_candidates', []).append(dict(f_mhz=tr['f_median_hz'] / 1e6, t_start_s=tr['t_start_s'], t_end_s=tr['t_end_s'],
                                                                      candidates=[(o['name'], o['norad'], round(o['rms_hz'])) for o in om[:4]]))
                    if om and (len(om) == 1 or om[1]['rms_hz'] >= 1.3 * om[0]['rms_hz']):     # attribute only a unique orbit
                        o = om[0]; same_plane = [x['name'] for x in om[1:4] if x['rms_hz'] < 1.5 * o['rms_hz']]
                        tr['id'] = dict(norad=o['norad'], name=o['name'], transmitter='(not in the transmitter list)', mode='', baud=None, f_tx_hz=None,
                                        offset_hz=o['offset_hz'], rms_hz=o['rms_hz'], confidence='orbit (Doppler shape)' + (' ambiguous: ' + ', '.join(same_plane) if same_plane else ''),
                                        alternatives=len(om) - 1, format_check='n/a')
                        used.add(o['norad'])
                        res['ids'].append(dict(kind='orbit', band=Bd['band']['name'], f_mhz=tr['f_median_hz'] / 1e6, t_start_s=tr['t_start_s'], t_end_s=tr['t_end_s'],
                                               observed_format=tr['format']['cls'], observed_bw_hz=tr['format']['bw_hz'], **tr['id']))
                continue                            # a track is only attributed to a transmitter when its Doppler shape was tested
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
                c = consistent_hits(guided(A, p, tx, shift=sh) or []); ctrl_n += 1; ctrl_hits += bool(c)
            if g:
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
    # Doppler-corrected stacking for every pass still unheard, with the same off-curve control
    st_n = st_false = 0; res['stacked'] = []
    for p in preds:
        if p['norad'] in used:
            continue
        for tx in p['transmitters']:
            if not tx['alive']:
                continue
            r = stacked(A, p, tx)
            if r is None:
                continue
            for sh in (CONTROL_HZ, -CONTROL_HZ):
                cr = stacked(A, p, tx, shift=sh); st_n += 1; st_false += bool(cr and cr['z'] >= STACK_Z)
            res['stacked'].append(dict(norad=p['norad'], name=p['name'], f_mhz=tx['f_hz'] / 1e6, **r))
            if r['z'] >= STACK_Z:
                used.add(p['norad'])
                Bd = next(B for B in A['bands'] if B['band']['lo_hz'] <= tx['f_hz'] <= B['band']['hi_hz'])
                same = [q['name'] for q in preds if q['norad'] != p['norad'] and any(abs(x['f_hz'] - tx['f_hz']) < 2 * GUIDE_HZ for x in q['transmitters'])]
                res['ids'].append(dict(kind='stacked', band=Bd['band']['name'], f_mhz=(tx['f_hz'] + r['offset_hz']) / 1e6, t_start_s=r['t_first_s'], t_end_s=r['t_last_s'],
                                       observed_format=f"steady carrier in the Doppler-corrected stack (+{r['excess_db']:.2f} dB over {r['rows']} sweeps)", observed_bw_hz=None,
                                       norad=p['norad'], name=p['name'], transmitter=tx['description'], mode=tx['mode'], baud=tx['baud'], f_tx_hz=tx['f_hz'],
                                       offset_hz=r['offset_hz'], rms_hz=None, confidence=f"stacked z={r['z']:.1f}" + (' ambiguous: ' + ', '.join(same[:3]) if same else ''),
                                       alternatives=len(same), format_check='n/a (stack)', peak_db=r['excess_db']))
                break
    res['stack_control'] = dict(searches=st_n, false_detections=st_false, threshold_z=STACK_Z)
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
