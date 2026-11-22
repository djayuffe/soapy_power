#!/usr/bin/env python3
"""Superscan analysis: interleaved multi-band hop scan (soapy_power --centers ... -F json) with Doppler tracking.

usage: analyze_superscan.py SCAN.jsonl OUTDIR [--tag NAME] [--bands bands.json] [--qth qth.json] [--satdb DIR]
  qth.json: {"lat_deg", "lon_deg", "alt_m"}; DIR: cache from satdb.py refresh (defaults: ../qth.json, ../satdb next to the session)
  bands.json: {"dc_exclude_hz": 1e5, "usable_halfwidth_hz": 8.5e5, "bands": [{"name", "lo_hz", "hi_hz"}, ...]}
  (default: bands.json next to SCAN.jsonl, else every hop's trusted window as its own band)

Per band: every output bin is taken from the hop that sees it closest to its centre while staying outside
the DC exclusion; floor = slow percentile of the hop's own time-low spectrum; every record levelled.
Doppler: narrowband peaks per sweep (3-bin power average, > 5 sigma) are linked into tracks with a gate
that allows the largest LEO Doppler rate; each track is fitted with constant, linear and S-curve
f(t) = f0 - A tanh((t - tca)/tau). A track is reported as LEO-Doppler-like only when the frequency
falls with time, the shift and rate are inside LEO bounds for its carrier, and the S/linear fit beats
a constant. That is a signature, not an identification: no orbit data is used.
Outputs (prefix <tag>_): analysis.pdf, summary.json, tracks.csv, lines.csv, events.csv, waterfall_<band>.png,
doppler.png, waterfall_<band>.html (interactive, full resolution, tracks overlaid)."""
import csv, datetime, json, math, sys, textwrap, warnings
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze_hopscan as ah                      # noqa: E402  style + Agg backend
import hopscan_waterfall as hw                    # noqa: E402  viewer template, quantiser, colormaps
import superscan_id as sid                       # noqa: E402  identification (SatNOGS + SGP4) and signal format
import matplotlib.pyplot as plt                   # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages   # noqa: E402
from matplotlib.ticker import MultipleLocator, AutoMinorLocator   # noqa: E402
from scipy.ndimage import percentile_filter, uniform_filter, label, find_objects   # noqa: E402
from scipy.optimize import curve_fit              # noqa: E402

warnings.filterwarnings('ignore')
C_LIGHT = 299792458.0
V_LEO = 7.6e3                  # m/s, used only for the indicative minimum-range figure
R_MIN = 300e3                  # m, closest plausible LEO slant range (bounds the Doppler rate)
TRACK_GAP = 5                  # sweeps a track may miss (bursty beacons) before it ends
MIN_POINTS, MIN_SPAN_S = 8, 20.0
BG, PANEL, FG = ah.BG, ah.PANEL, ah.FG
C_DOP, C_DRIFT, C_STABLE, C_EVENT, C_PRED = '#00d1ff', '#ff4d6d', '#8fa0bd', '#7CFC9A', '#ffd166'


# ---------------------------------------------------------------------------------------------- loading
def load(path):
    order, hop, sweep, recs, io = [], [], [], [], []; meta0 = None; statuses = set(); conts = set(); last_h = None; sw = 0
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line); m = r['metadata']; c = m['tuned_center_hz']
            k = next((i for i, o in enumerate(order) if abs(c - o) < 1), None)
            if k is None:
                order.append(c); k = len(order) - 1
            if last_h is not None and k <= last_h:
                sw += 1
            last_h = k
            p = np.array([np.nan if v is None else v for v in r['power']], np.float32)
            recs.append((sw, k, p, datetime.datetime.fromisoformat(r['time_start']).timestamp(), datetime.datetime.fromisoformat(r['time_stop']).timestamp()))
            io.append((sw, k, m.get('host_io', {})))
            statuses.add(str(m.get('measurement_status'))); conts.add(str(m.get('continuity')))
            if meta0 is None:
                meta0 = (r, m)
    if not recs:
        raise SystemExit('no records')
    nsw, nh, n = sw + 1, len(order), len(recs[0][2])
    P = np.full((nsw, nh, n), np.nan, np.float32); T0 = np.full((nsw, nh), np.nan); T1 = np.full((nsw, nh), np.nan)
    for s_, h, p, a, b in recs:
        P[s_, h] = p; T0[s_, h] = a; T1[s_, h] = b
    r0, m = meta0
    info = dict(step_hz=float(r0['step_hz']), bins=n, hops=nh, sweeps=int(nsw), records=len(recs), centers_hz=[float(c) for c in order],
                sample_rate_hz=m.get('sample_rate_hz'), window=m.get('window'), segments=m.get('segment_count'), unit=r0['unit'],
                statuses=sorted(statuses), continuity=sorted(conts), gain=(m.get('context') or {}).get('gains'))
    return P, T0, T1, np.array(order), io, info


def default_bands(centers, dc, usable):
    out = []
    for c in centers:
        out.append(dict(name=f'{(c - usable) / 1e6:.3f}-{(c - dc) / 1e6:.3f} MHz', lo_hz=c - usable, hi_hz=c - dc))
        out.append(dict(name=f'{(c + dc) / 1e6:.3f}-{(c + usable) / 1e6:.3f} MHz', lo_hz=c + dc, hi_hz=c + usable))
    return out


# --------------------------------------------------------------------------------------------- residual
def residual(P):
    """Per hop: floor = 30th-percentile filter (50 kHz) of the 20th time-percentile spectrum; each record levelled."""
    nsw, nh, n = P.shape; R = np.empty_like(P); F = np.zeros((nh, n), np.float32)
    for h in range(nh):
        q = np.nanpercentile(P[:, h], 20, axis=0)
        q = np.where(np.isfinite(q), q, np.nanmedian(q))
        F[h] = uniform_filter(percentile_filter(q, 30, size=205), 41)
        R[:, h] = P[:, h] - F[h]
    return R, F


def stitch(R, T0, T1, centers, step, band, dc, usable):
    n = R.shape[2]; f = np.arange(band['lo_hz'], band['hi_hz'] + step / 2, step)
    hop = np.full(len(f), -1); col = np.zeros(len(f), int)
    for k, x in enumerate(f):
        off = np.abs(x - centers); ok = (off >= dc) & (off <= usable)
        if ok.any():
            h = int(np.argmin(np.where(ok, off, np.inf))); hop[k] = h; col[k] = int(round((x - centers[h]) / step)) + n // 2
    good = hop >= 0; f, hop, col = f[good], hop[good], col[good]
    B = R[:, hop, col].astype(float)
    fsrc = centers[hop] + (col - n // 2) * step
    hops_used = sorted(set(hop.tolist()))
    for h in hops_used:                            # level every record of this band to its own median
        m = hop == h; B[:, m] -= np.nanmedian(B[:, m], 1)[:, None]
    Tm = (T0 + T1) / 2
    return dict(f=f, fsrc=fsrc, hop=hop, col=col, B=B, T=Tm[:, hop], hops=hops_used, T0=T0[:, hops_used], T1=T1[:, hops_used])


def smooth_f(B, nb=3):
    fin = np.isfinite(B); L = np.where(fin, 10 ** (B / 10), 0.0)
    num = uniform_filter(L, (1, nb), mode='constant'); den = uniform_filter(fin.astype(float), (1, nb), mode='constant')
    return np.where(den > 0.5, 10 * np.log10(np.maximum(num, 1e-30) / np.maximum(den, 1e-30)), np.nan)


def mad(x):
    x = x[np.isfinite(x)]; m = np.median(x); return float(m), float(1.4826 * np.median(np.abs(x - m)))


# ---------------------------------------------------------------------------------------------- doppler
def s_curve(t, f0, A, tca, tau):
    return f0 - A * np.tanh((t - tca) / tau)


def peaks(D, thr, max_per_row=80):
    out = []
    for s in range(D.shape[0]):
        d = D[s]; v = np.nan_to_num(d, nan=-99.0)
        loc = (v > thr) & (v >= np.roll(v, 1)) & (v >= np.roll(v, -1)) & (v >= np.roll(v, 2)) & (v >= np.roll(v, -2))
        idx = np.flatnonzero(loc)
        if len(idx) > max_per_row:
            idx = idx[np.argsort(-v[idx])[:max_per_row]]
        out.append(sorted(idx.tolist(), key=lambda i: -v[i]))
    return out


def track(S, thr, sigma):
    """Greedy nearest-neighbour linking with a rate-limited gate; one peak per track per sweep."""
    D, fs, T = S['D'], S['fsrc'], S['T']; step = float(np.median(np.diff(S['f'])))
    fmid = float(np.mean(S['f'])); rate_max = fmid * V_LEO ** 2 / (C_LIGHT * R_MIN)
    P = peaks(D, thr); live, done = [], []
    for s, idx in enumerate(P):
        taken = set()
        for tr in live:
            tr['claimed'] = False
        for i in idx:
            t = T[s, i]
            if not np.isfinite(t):
                continue
            # sub-bin frequency: parabolic interpolation on the dB profile
            if 0 < i < D.shape[1] - 1 and np.isfinite(D[s, i - 1]) and np.isfinite(D[s, i + 1]):
                a, b, c = D[s, i - 1], D[s, i], D[s, i + 1]; den = a - 2 * b + c
                d = 0.5 * (a - c) / den if den < 0 else 0.0
            else:
                d = 0.0
            fx = fs[i] + max(-0.5, min(0.5, d)) * step
            best, bd = None, None
            for tr in live:
                if tr['claimed'] or s - tr['s'][-1] > TRACK_GAP:
                    continue
                dt = t - tr['t'][-1]
                pred = tr['f'][-1] + tr['rate'] * dt
                gate = 3 * step + rate_max * abs(dt) * (0.35 if len(tr['f']) >= 4 else 1.0)
                dd = abs(fx - pred)
                if dd <= gate and (bd is None or dd < bd):
                    best, bd = tr, dd
            if best is None:
                live.append(dict(s=[s], t=[t], f=[fx], i=[i], p=[float(D[s, i])], rate=0.0, claimed=True))
            else:
                best['s'].append(s); best['t'].append(t); best['f'].append(fx); best['i'].append(i); best['p'].append(float(D[s, i])); best['claimed'] = True
                if len(best['f']) >= 3:
                    tt, ff = np.array(best['t'][-6:]), np.array(best['f'][-6:])
                    best['rate'] = float(np.polyfit(tt - tt[0], ff, 1)[0]) if np.ptp(tt) > 0 else 0.0
            taken.add(i)
        still = []
        for tr in live:
            (still if s - tr['s'][-1] <= TRACK_GAP else done).append(tr)
        live = still
    done += live
    return [tr for tr in done if len(tr['f']) >= MIN_POINTS and tr['t'][-1] - tr['t'][0] >= MIN_SPAN_S]


def classify(tr, step, t_ref):
    t = np.array(tr['t']) - t_ref; f = np.array(tr['f']); n = len(f); fm = float(np.median(f))
    rate_max = fm * V_LEO ** 2 / (C_LIGHT * R_MIN); shift_max = 2.2 * fm * 8.0e3 / C_LIGHT
    rss0 = float(np.sum((f - f.mean()) ** 2))
    b, a = np.polyfit(t - t.mean(), f, 1); rss1 = float(np.sum((f - (a + b * (t - t.mean()))) ** 2))
    fit = dict(model='constant', f0=float(f.mean()), slope=0.0, A=0.0, tca=None, tau=None, rms=math.sqrt(rss0 / n))
    best_rss = rss0
    if n >= 4 and rss1 < best_rss:
        fit = dict(model='linear', f0=float(a), slope=float(b), A=0.0, tca=None, tau=None, rms=math.sqrt(rss1 / n)); best_rss = rss1
    if n >= 8 and np.ptp(f) > 2 * step:
        span = float(np.ptp(t)); A0 = max(float(np.ptp(f)) / 2, step)
        try:
            p, _ = curve_fit(s_curve, t, f, p0=[float(np.median(f)), A0, float(np.median(t)), max(span / 4, 30.0)],
                             bounds=([f.min() - shift_max, 0.0, t.min() - 900, 15.0], [f.max() + shift_max, shift_max, t.max() + 900, 900.0]), maxfev=8000)
            rss2 = float(np.sum((f - s_curve(t, *p)) ** 2))
            if rss2 < 0.8 * best_rss:
                fit = dict(model='s-curve', f0=float(p[0]), A=float(p[1]), tca=float(p[2]), tau=float(p[3]), slope=float(-p[1] / p[3]), rms=math.sqrt(rss2 / n))
        except Exception:
            pass
    # observed shift / rate from the fitted curve over the observed span
    tt = np.linspace(t.min(), t.max(), 200)
    if fit['model'] == 's-curve':
        ff = s_curve(tt, fit['f0'], fit['A'], fit['tca'], fit['tau'])
        rate_peak = -fit['A'] / fit['tau'] if t.min() <= fit['tca'] <= t.max() else float(np.min(np.gradient(ff, tt)))
    else:
        ff = fit['f0'] + fit['slope'] * (tt - (t.mean() if fit['model'] == 'linear' else 0)); rate_peak = fit['slope']
    shift = float(ff[-1] - ff[0])
    # F-test-like improvement over a constant
    improve = 1 - best_rss / rss0 if rss0 > 0 else 0.0
    noise_ok = fit['rms'] <= 1.5 * step
    falling = shift < -2.5 * step
    within = abs(shift) <= shift_max and abs(rate_peak) <= 1.15 * rate_max
    if falling and within and improve > 0.6 and noise_ok:
        kind = 'doppler'
    elif abs(shift) > 2.5 * step and improve > 0.6:
        kind = 'drift'
    else:
        kind = 'stable'
    out = dict(kind=kind, model=fit['model'], n=n, t_start_s=float(t.min()), t_end_s=float(t.max()), f_first_hz=float(f[0]), f_last_hz=float(f[-1]),
               f_median_hz=fm, shift_hz=shift, rate_peak_hz_s=float(rate_peak), rms_hz=float(fit['rms']), improvement=float(improve),
               mean_level_db=float(np.mean(tr['p'])), max_level_db=float(np.max(tr['p'])), shift_bound_hz=shift_max, rate_bound_hz_s=rate_max,
               f0_hz=float(fit['f0']), A_hz=float(fit['A']), tca_s=fit['tca'], tau_s=fit['tau'])
    if kind == 'doppler' and fit['model'] == 's-curve':
        out['tca_observed'] = bool(t.min() <= fit['tca'] <= t.max())
        out['v_radial_max_kms'] = float(fit['A'] * C_LIGHT / fit['f0'] / 1e3)
        out['r_min_km_if_leo'] = float(fit['f0'] * V_LEO ** 2 / (C_LIGHT * (fit['A'] / fit['tau'])) / 1e3) if out['tca_observed'] else None
    return out


def common_mode(tracks):
    """Receiver LO drift moves every line of a hop together: mark drift that the hop's stable-length tracks share."""
    by_hop = {}
    for tr in tracks:
        by_hop.setdefault(tr['hop'], []).append(tr)
    for h, trs in by_hop.items():
        long_ = [t for t in trs if t['t_end_s'] - t['t_start_s'] > 120]
        if len(long_) >= 3:
            med = float(np.median([t['rate_peak_hz_s'] for t in long_]))
            for t in trs:
                if t['kind'] != 'stable' and abs(med) > 0.5 and abs(t['rate_peak_hz_s'] - med) < 0.3 * abs(med):
                    t['kind'] = 'common-mode drift'
    return tracks


# -------------------------------------------------------------------------------------------- lines/events
def persistent_lines(S, sig, step, moving=()):
    """Lines in the run mean that are present in >= 25 % of sweeps and not the footprint of a moving track."""
    mres = 10 * np.log10(np.nanmean(10 ** (S['B'] / 10), 0)); out = []
    for a, b in ah.runs(np.nan_to_num(mres, nan=-9) >= 0.5):
        j = a + int(np.nanargmax(mres[a:b + 1])); fj = S['fsrc'][j]
        pct = float(100 * np.nanmean(S['B'][:, j] > 3 * sig))
        if pct < 25 or any(lo - 1e3 <= fj <= hi + 1e3 for lo, hi in moving):
            continue
        out.append(dict(f_mhz=float(fj / 1e6), excess_db=float(mres[j]), width_khz=float((b - a + 1) * step / 1e3), sweeps_pct=pct, hop=int(S['hop'][j])))
    return sorted(out, key=lambda c: -c['excess_db']), mres


def events(S, sig, lines, tracks, t_ref):
    E = 10 * np.log10(np.maximum(uniform_filter(np.nan_to_num(10 ** (S['B'] / 10), nan=1.0), (3, 3)), 1e-30))
    m0, s0 = mad(E); lab, _ = label(E > max(m0 + 8 * s0, 0.8), structure=np.ones((3, 3))); out = []
    lf = np.array([c['f_mhz'] for c in lines]) * 1e6
    for i, sl in enumerate(find_objects(lab), 1):
        if sl is None or sl[0].stop - sl[0].start >= S['B'].shape[0] / 2 or (sl[0].stop - sl[0].start <= 1 and sl[1].stop - sl[1].start <= 3):
            continue
        sub = np.where(lab[sl] == i, S['B'][sl], np.nan)
        if not np.isfinite(sub).any():
            continue
        rr, cc = np.unravel_index(np.nanargmax(sub), sub.shape); j = sl[1].start + cc; fj = S['fsrc'][j]
        if len(lf) and np.min(abs(lf - fj)) < 1500:
            continue
        Ts = S['T'][sl]                              # rows of an incomplete sweep have no time stamp
        if not np.isfinite(Ts).any():
            continue
        t0 = float(np.nanmin(Ts) - t_ref); t1 = float(np.nanmax(Ts) - t_ref)
        in_track = any(tr['t_start_s'] - 5 <= t0 <= tr['t_end_s'] + 5 and abs(np.interp(t0, [tr['t_start_s'], tr['t_end_s']], [tr['f_first_hz'], tr['f_last_hz']]) - fj) < 5e3 for tr in tracks)
        out.append(dict(f_mhz=float(fj / 1e6), t_start_s=t0, t_end_s=t1, peak_db=float(np.nanmax(sub)), sweeps=int(sl[0].stop - sl[0].start),
                        width_khz=float((sl[1].stop - sl[1].start) * np.median(np.diff(S['f'])) / 1e3), on_track=bool(in_track)))
    return sorted(out, key=lambda e: -e['peak_db'])


def wideband(S, sig, t_ref, frac_min=0.02):
    """Records where a hop's whole section of the band lights up (many bins > 3 sigma in one sweep):
    broadband transients / impulsive interference, reported per hop and sweep."""
    out = []
    for h in S['hops']:
        cols = np.flatnonzero(S['hop'] == h); X = S['B'][:, cols]
        frac = np.nanmean(X > 3 * sig, 1)
        for s_ in np.flatnonzero(frac > frac_min):
            lin = 10 * np.log10(np.nanmean(10 ** (X[s_] / 10)))
            out.append(dict(t_s=float(np.nanmean(S['T'][s_, cols]) - t_ref) if np.isfinite(S['T'][s_, cols]).any() else float('nan'), sweep=int(s_), f_lo_mhz=float(S['f'][cols[0]] / 1e6), f_hi_mhz=float(S['f'][cols[-1]] / 1e6),
                            bins_over_3sigma_pct=float(100 * frac[s_]), mean_excess_db=float(lin), hop_mhz=None))
    return sorted(out, key=lambda e: e['t_s'])


def refine_wide(raw_tracks, S, sig, step, t_ref, wide_hz=2e3):
    """A wide signal's peak wanders inside its band: replace each point by the power-weighted centroid of the
    signal (Doppler-aligned -10 dB width), then merge tracks of the same signal (time overlap, same centroid)."""
    out = []
    for tr in raw_tracks:
        rows = [int(np.nanargmin(np.abs(S['T'][:, i] - t))) for t, i in zip(tr['t'], tr['i'])]
        fm = sid.measure_format(S, rows, list(tr['i']), sig, step, 1.0)
        tr['bw_hz'] = fm['bw_hz']
        if fm['bw_hz'] > wide_hz:
            half = max(2, int(fm['bw_hz'] / step / 2)); nf = []
            for r, i, f0 in zip(rows, tr['i'], tr['f']):
                a, b = max(0, i - half), min(S['B'].shape[1], i + half + 1)
                x = np.clip(10 ** (np.nan_to_num(S['B'][r, a:b], nan=-99.0) / 10) - 1.0, 0, None)
                nf.append(float(np.sum(S['fsrc'][a:b] * x) / np.sum(x)) if x.sum() > 0 else f0)
            tr['f'] = nf; tr['centroid'] = True
        out.append(tr)
    out.sort(key=lambda t: -len(t['f'])); keep = []
    for tr in out:
        dup = False
        for k in keep:
            t0, t1 = max(tr['t'][0], k['t'][0]), min(tr['t'][-1], k['t'][-1])
            if t1 - t0 > 0.5 * min(tr['t'][-1] - tr['t'][0], k['t'][-1] - k['t'][0]):
                tol = max(k.get('bw_hz', step), tr.get('bw_hz', step)) / 2 + 2 * step
                if abs(np.median(tr['f']) - np.median(k['f'])) < tol:
                    dup = True
                    for t, f, i, pw in zip(tr['t'], tr['f'], tr['i'], tr['p']):       # fill sweeps the kept track missed
                        if all(abs(t - x) > 0.5 for x in k['t']):
                            k['t'].append(t); k['f'].append(f); k['i'].append(i); k['p'].append(pw)
                    o = np.argsort(k['t']); k['t'] = [k['t'][j] for j in o]; k['f'] = [k['f'][j] for j in o]; k['i'] = [k['i'][j] for j in o]; k['p'] = [k['p'][j] for j in o]
                    break
        if not dup:
            keep.append(tr)
    return keep


def origin_note(A):
    L = [x for b in A['bands'] for x in b.get('lines', [])]
    n2 = [x for x in L if len(x.get('views', [])) >= 2]
    if not n2:
        return 'Stable lines real vs receiver spur: not decided (each line seen from one LO; use satplan.py --dither).'
    k = lambda p: sum(x.get('origin', '').startswith(p) for x in L)
    return (f"Line origin from two LOs per frequency: {k('RF')} RF, {k('LO spur')} LO-related spur, {k('single')} seen from one LO only, "
            f"{sum('reference harmonic' in x.get('origin', '') for x in L)} at a 10 MHz multiple (B200 reference?) — lines CSV.")


def line_origin(lines, Rr, centers, step, dc, usable):
    """RF signal or LO-related spur: look at each persistent line from every hop whose trusted window covers it.
    Present from every covering LO -> RF; from only some -> LO-related spur; a single covering LO -> unverified."""
    n = Rr.shape[2]
    for L in lines:
        f = L['f_mhz'] * 1e6; seen = []
        for h, c in enumerate(centers):
            off = f - c
            if not (dc <= abs(off) <= usable):
                continue
            j = int(round(off / step)) + n // 2
            seg = Rr[:, h, max(0, j - 30):j + 31].astype(float)
            prof = 10 * np.log10(np.nanmean(10 ** (seg / 10), 0)); base = np.nanmedian(prof)
            ex = float(np.nanmax(prof[27:34]) - base) if prof.size >= 34 else float('nan')
            seen.append(dict(lo_mhz=float(c / 1e6), offset_khz=float(off / 1e3), excess_db=ex))
        L['views'] = seen
        if len(seen) < 2:
            L['origin'] = 'single LO (unverified)'
        else:
            pres = [v['excess_db'] >= max(0.3, 0.4 * L['excess_db']) for v in seen]
            L['origin'] = 'RF (all %d LOs)' % len(seen) if all(pres) else ('LO spur (only %s)' % ', '.join(f"{v['lo_mhz']:.2f}{v['offset_khz']:+.0f}k" for v, p in zip(seen, pres) if p) if any(pres) else 'not resolved')
    # the same LO offset carrying a line in two different hops is LO-relative even if each was seen from one LO only
    offs = [(L, round(v['offset_khz'] / (2 * step / 1e3))) for L in lines for v in L['views'] if v['excess_db'] >= max(0.3, 0.4 * L['excess_db'])]
    for L, o in offs:
        los = {round(v['lo_mhz'], 3) for M, oo in offs if oo == o for v in M['views'] if round(v['offset_khz'] / (2 * step / 1e3)) == o}
        if len(los) >= 2 and not L['origin'].startswith('RF'):
            L['origin'] = f"LO spur (same {o * 2 * step / 1e3:+.0f} kHz offset in {len(los)} hops)"
    for L in lines:                                  # internal reference: the B200's 40 MHz clock (10 MHz steps flagged too)
        f = L['f_mhz'] * 1e6; k = round(f / 10e6)
        if abs(f - k * 10e6) < 2.5e3 and not L['origin'].startswith('LO spur'):
            L['origin'] += f" · at {k * 10} MHz (reference harmonic?)"
    return lines


# ------------------------------------------------------------------------------------------------ analysis
def analyse(path, bands_cfg):
    P, T0, T1, centers, io, info = load(path)
    step = info['step_hz']; dc = bands_cfg.get('dc_exclude_hz', 1e5); usable = bands_cfg.get('usable_halfwidth_hz', 8.5e5)
    bands = bands_cfg.get('bands') or default_bands(centers, dc, usable)
    Rr, F = residual(P); t_ref = float(np.nanmin(T0))
    out = []
    for bd in bands:
        S = stitch(Rr, T0, T1, centers, step, bd, dc, usable)
        if not len(S['f']):
            continue
        S['D'] = smooth_f(S['B'], 3)
        _, sig = mad(S['B']); dm, dsig = mad(S['D'])
        thr = max(dm + 5 * dsig, 0.8)
        trs = []
        for tr in refine_wide(track(S, thr, dsig), S, sig, step, t_ref):
            c = classify(tr, step, t_ref); c['hop'] = int(S['hop'][int(np.median(tr['i']))]); c['points'] = (np.array(tr['t']) - t_ref, np.array(tr['f']))
            c['band'] = bd['name']; c['centroid'] = bool(tr.get('centroid')); c['bw_hz'] = tr.get('bw_hz'); trs.append(c)
        trs = common_mode(trs)
        moving = [(min(t['points'][1]), max(t['points'][1])) for t in trs if t['kind'] != 'stable']
        lines, mres = persistent_lines(S, sig, step, moving)
        lines = line_origin(lines, Rr, centers, step, dc, usable)
        ev = events(S, sig, lines, [t for t in trs if t['kind'] == 'doppler'], t_ref)
        out.append(dict(band=bd, S=S, sig=sig, dsig=dsig, thr=thr, tracks=trs, lines=lines, mres=mres, events=ev, wideband=wideband(S, sig, t_ref)))
    dwell = (T1 - T0); gaps = T0[:, 1:] - T1[:, :-1]; wrap = T0[1:, 0] - T1[:-1, -1]
    timing = dict(sweep_period_s=float(np.nanmedian(np.diff(T0[:, 0]))), dwell_s=float(np.nanmedian(dwell)),
                  retune_gap_s={f"{centers[h] / 1e6:.2f}->{centers[h + 1] / 1e6:.2f}": float(np.nanmedian(gaps[:, h])) for h in range(len(centers) - 1)},
                  span_s=float(np.nanmax(T1) - t_ref), wrap_gap_s=float(np.nanmedian(wrap)) if len(wrap) else None,
                  max_read_latency_ms=float(max([x.get('read_latency_max_ms', 0) or 0 for _, _, x in io] + [0])))
    if len(wrap):
        timing['retune_gap_s'][f"{centers[-1] / 1e6:.2f}->{centers[0] / 1e6:.2f}"] = float(np.nanmedian(wrap))
    return dict(P=P, T0=T0, T1=T1, centers=centers, info=info, F=F, bands=out, t_ref=t_ref, timing=timing, io=io,
                start=datetime.datetime.fromtimestamp(t_ref, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'))


# --------------------------------------------------------------------------------------------------- plots
def slug(name):
    return ''.join(ch if ch.isalnum() else '_' for ch in name.split('(')[0].strip()).strip('_').lower()


def waterfall(ax, X, S, t_ref, vmin, vmax, cmap='inferno', target_px=3400):
    """Per hop block of the band, rows at that hop's own measured times; columns pooled (max) to whole pixels."""
    f = S['f'] / 1e6; cm = plt.get_cmap(cmap).copy(); cm.set_bad(hw.MASKED); ax.set_facecolor(hw.MASKED); m = None
    p = max(1, int(math.ceil(len(f) / target_px)))
    k = 0
    while k < len(f):
        h = S['hop'][k]; e = k
        while e < len(f) and S['hop'][e] == h:
            e += 1
        cols = np.arange(k, e, p); Z = np.stack([np.nanmax(X[:, c:min(c + p, e)], 1) for c in cols], 1)
        xe = np.append(f[cols] - (f[1] - f[0]) / 2, f[e - 1] + (f[1] - f[0]) / 2)
        hi = list(S['hops']).index(h); ts = S['T0'][:, hi] - t_ref
        ok = np.isfinite(ts)
        ye = np.append(ts[ok], (S['T1'][:, hi] - t_ref)[ok][-1])
        m = ax.pcolormesh(xe, ye, np.ma.masked_invalid(Z[ok]), cmap=cm, vmin=vmin, vmax=vmax, shading='flat', rasterized=True)
        k = e
    ax.grid(False); ax.set_xlim(f[0], f[-1])
    return m


def band_figure(A, Bd, out, tag):
    S, t_ref = Bd['S'], A['t_ref']; f = S['f'] / 1e6; name = Bd['band']['name']
    fig = plt.figure(figsize=(24, 13.5), dpi=150)
    nd = sum(t['kind'] == 'doppler' for t in Bd['tracks'])
    fig.text(0.035, 0.965, name, fontsize=28, fontweight='bold', va='top')
    fig.text(0.035, 0.925, f"{A['info']['sweeps']} sweeps · revisit {A['timing']['sweep_period_s']:.2f} s · start {A['start']} · native {A['info']['step_hz']:.0f} Hz bins · "
             f"residual (3-bin power average, no time smoothing so Doppler is not smeared), max per pixel\n"
             f"{len(Bd['lines'])} persistent lines · {nd} LEO-Doppler-like tracks · {len(Bd['events'])} intermittent emissions · {len(Bd['wideband'])} wideband transients · "
             "hops " + ', '.join(f"{A['centers'][h] / 1e6:.2f}" for h in S['hops']) + " MHz", fontsize=12.5, color='#8fa0bd', va='top', linespacing=1.5)
    ax0 = fig.add_axes([0.05, 0.745, 0.895, 0.13])
    ax0.plot(f, Bd['mres'], color=ah.C4, lw=.6); ax0.axhline(0, color='#8fa0bd', lw=.5)
    ax0.set_xlim(f[0], f[-1]); ax0.tick_params(labelbottom=False); ax0.set_ylabel('dB, run\nmean', fontsize=12)
    ax0.set_ylim(-0.4, max(2.0, np.nanpercentile(Bd['mres'], 99.99) + 0.6))
    ax = fig.add_axes([0.05, 0.075, 0.895, 0.655])
    stats = {}
    D = S['D']; m0, s0 = mad(D)
    mm = waterfall(ax, D, S, t_ref, m0 - 1 * s0, m0 + max(14 * s0, 1.5))
    for tr in Bd['tracks']:
        if tr['kind'] == 'stable':
            continue
        t, fx = tr['points']; col = C_DOP if tr['kind'] == 'doppler' else C_DRIFT
        if tr['model'] == 's-curve':
            tt = np.linspace(t.min(), t.max(), 200)
            ax.plot(s_curve(tt, tr['f0_hz'], tr['A_hz'], tr['tca_s'], tr['tau_s']) / 1e6, tt, '--', color=col, lw=1.1, alpha=.75)
        else:
            ax.plot(fx / 1e6, t, '-', color=col, lw=1.0, alpha=.6)
        ax.annotate(f"{tr['f_median_hz'] / 1e6:.4f} {tr['kind']} {tr['shift_hz'] / 1e3:+.2f} kHz", (fx[0] / 1e6, t[0]), xytext=(6, -10), textcoords='offset points',
                    fontsize=10.5, color=col, bbox=dict(fc='#070b14cc', ec='none', pad=1.5))
    (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim(); pos = ax.get_position(); W, H = fig.get_size_inches() * 72
    mhz_pt, s_pt = (x1 - x0) / (pos.width * W), abs(y1 - y0) / (pos.height * H); placed = []
    for q, (lab, tt, ff, heard) in enumerate(sid.predicted_curves(A, Bd['band'])):
        ax.plot(ff / 1e6, tt, ls='-' if heard else ':', color=C_PRED, lw=1.0 if heard else 1.3, alpha=.9)
        if q >= 12 and not heard:                   # label the highest passes only (list on the passes page)
            continue
        lab = lab[:26]; lx = ff[-1] / 1e6 + 3 * mhz_pt; w = (len(lab) * 5.6 + 4) * mhz_pt
        for row in range(5):                        # next free row below the curve's end, else no label
            ly = tt[-1] - row * 13 * s_pt
            if not any(abs(ly - py) < 12 * s_pt and lx < px1 and px0 < lx + w for px0, px1, py in placed):
                break
        else:
            continue
        placed.append((lx, lx + w, ly))
        ax.annotate(lab, (ff[-1] / 1e6, ly), xytext=(3, -2), textcoords='offset points', fontsize=9.5, color=C_PRED, va='top',
                    bbox=dict(fc='#070b14cc', ec='none', pad=1))
    for w in Bd['wideband']:
        ax.plot([w['f_lo_mhz'], w['f_hi_mhz']], [w['t_s'] + 3.5] * 2, color=C_EVENT, lw=1.6, alpha=.9)
        ax.annotate(f"wideband {w['bins_over_3sigma_pct']:.0f}% bins >3σ", (w['f_hi_mhz'], w['t_s'] + 3.5), xytext=(4, 0), textcoords='offset points', va='center',
                    fontsize=10, color=C_EVENT, bbox=dict(fc='#070b14cc', ec='none', pad=1))
    for e in Bd['events']:
        if not e['on_track']:
            ax.add_patch(plt.Rectangle((e['f_mhz'] - 0.004 * (f[-1] - f[0]), e['t_start_s'] - 2), 0.008 * (f[-1] - f[0]), e['t_end_s'] - e['t_start_s'] + 4, fill=False, ec=C_EVENT, lw=1.1))
    ax.set_xlabel('MHz', fontsize=14); ax.set_ylabel('seconds since start', fontsize=14); ax.tick_params(labelsize=12)
    ax.set_ylim(0, A['timing']['span_s'])
    cb = fig.colorbar(mm, cax=fig.add_axes([0.952, 0.075, 0.01, 0.655])); cb.set_label(f'dB above floor (3-bin average, σ {s0:.3f} dB)', fontsize=12)
    fig.text(0.05, 0.01, f"cyan = LEO-Doppler-like track (dashed: S-curve fit) · red = drifting · green = intermittent / wideband · yellow = predicted Doppler of satellites above the horizon (solid: heard) · "
             f"levels {A['info']['unit']}, uncalibrated · grey = not covered (DC exclusion ±{A['cfg'].get('dc_exclude_hz', 1e5) / 1e3:.0f} kHz handled by hop choice)", fontsize=11, color='#8fa0bd')
    name_ = f'{tag}_waterfall_{slug(name)}.png'; fig.savefig(out / name_, dpi=150); return fig, name_


def doppler_figure(A, out, tag):
    trs = [dict(t, _Bd=Bd) for Bd in A['bands'] for t in Bd['tracks'] if t['kind'] in ('doppler', 'drift', 'common-mode drift')]
    trs.sort(key=lambda t: (t['kind'] != 'doppler', -abs(t['shift_hz'])))
    trs = trs[:8]
    fig = plt.figure(figsize=(24, 13.5), dpi=130)
    fig.text(0.035, 0.965, 'Doppler and drift tracks', fontsize=28, fontweight='bold', va='top')
    fig.text(0.035, 0.92, 'native-resolution residual around each track (244 Hz × one sweep) with the fitted model dashed; a LEO pass falls in frequency along an S-curve '
             'f0 − A·tanh((t − tca)/τ); bounds from v = 8 km/s and r ≥ 300 km for the carrier', fontsize=12.5, color='#8fa0bd', va='top')
    if not trs:
        fig.text(0.5, 0.5, 'No drifting or Doppler-like track in this run.\nEvery linked track kept its frequency within 2.5 bins.', ha='center', va='center', fontsize=22, color='#8fa0bd')
        return fig
    cols = 4; rows = (len(trs) + cols - 1) // cols
    for q, tr in enumerate(trs):
        r_, k_ = divmod(q, cols)
        ax = fig.add_axes([0.05 + k_ * 0.237, 0.86 - (r_ + 1) * (0.78 / max(rows, 2)) + 0.05, 0.2, 0.78 / max(rows, 2) - 0.1])
        t, fx = tr['points']; ref = tr['f0_hz']; col = C_DOP if tr['kind'] == 'doppler' else C_DRIFT
        S = tr['_Bd']['S']; half = max(abs(tr['shift_hz']) / 2, abs(fx - ref).max()) + 4e3
        sel = np.flatnonzero(np.abs(S['fsrc'] - ref) <= half)
        if len(sel) > 1:
            Z = S['B'][:, sel]; hi_ = list(S['hops']).index(S['hop'][sel[len(sel) // 2]]); ts = S['T0'][:, hi_] - A['t_ref']
            ok = np.isfinite(ts); cm = plt.get_cmap('inferno').copy(); cm.set_bad(hw.MASKED); sg = tr['_Bd']['sig']
            xs = (S['fsrc'][sel] - ref) / 1e3; dx = (xs[1] - xs[0]) / 2
            ax.pcolormesh(np.append(xs - dx, xs[-1] + dx), np.append(ts[ok], ts[ok][-1] + A['timing']['sweep_period_s']), np.ma.masked_invalid(Z[ok]),
                          cmap=cm, vmin=-2 * sg, vmax=max(10 * sg, tr['mean_level_db']), shading='flat', rasterized=True)
        ax.set_facecolor(hw.MASKED); ax.grid(False)
        tt = np.linspace(t.min(), t.max(), 300)
        if tr['model'] == 's-curve':
            ax.plot((s_curve(tt, tr['f0_hz'], tr['A_hz'], tr['tca_s'], tr['tau_s']) - ref) / 1e3 + 1.2, tt, '--', color=col, lw=1.2)
            if tr['tca_s'] is not None and t.min() <= tr['tca_s'] <= t.max():
                ax.axhline(tr['tca_s'], color=ah.C2, lw=.7, ls=':')
        else:
            ax.plot((fx - ref) / 1e3 + 1.2, t, '--', color=col, lw=1.0)
        ax.set_ylabel('s since start'); ax.set_xlabel(f"kHz from {ref / 1e6:.4f} MHz (fit drawn 1.2 kHz right)")
        ax.set_title(f"{tr['f_median_hz'] / 1e6:.4f} MHz · {tr['kind']}", color=col, fontsize=13)
        txt = f"shift {tr['shift_hz'] / 1e3:+.2f} kHz (LEO max {tr['shift_bound_hz'] / 1e3:.1f} total)\npeak rate {tr['rate_peak_hz_s']:+.1f} Hz/s (LEO max {tr['rate_bound_hz_s']:.0f})\nfit {tr['model']}, rms {tr['rms_hz']:.0f} Hz, {tr['n']} pts"
        if tr.get('r_min_km_if_leo'):
            txt += f"\nTCA {tr['tca_s']:.0f} s · r_min≈{tr['r_min_km_if_leo']:.0f} km (if v=7.6 km/s)"
        ax.text(0.02, 0.03, txt, transform=ax.transAxes, fontsize=9.5, va='bottom', color='w', bbox=dict(fc='#000000aa', ec='none', pad=3))
    return fig


def summary_page(A, pdf, out, tag):
    s = A['summary']
    hops_txt = ', '.join(f'{c / 1e6:.2f}' for c in A['centers']) if len(A['centers']) <= 8 else f"{A['centers'][0] / 1e6:.2f}–{A['centers'][-1] / 1e6:.2f}"
    fig = ah.page(pdf, 'Superscan with Doppler tracking', f"{A['info']['sweeps']} sweeps × {A['info']['hops']} hops ({hops_txt} MHz) · gain {(A['info'].get('gain') or {}).get('PGA', '?')} dB · "
                  f"{A['timing']['span_s']:.0f} s from {A['start']} · {A['info']['step_hz']:.0f} Hz bins · unit {A['info']['unit']} (uncalibrated)")
    tiles = [(f"{A['timing']['sweep_period_s']:.2f} s", 'revisit per band'), (f"{s['records']}", f"records · {','.join(s['statuses'])}"),
             (str(s['doppler_tracks']), 'LEO-Doppler-like tracks'), (str(s['drift_tracks']), 'other drifting tracks'),
             (str(s['persistent_lines']), 'persistent lines ≥0.5 dB'), (str(s['events']), 'intermittent emissions')]
    for i, (v, l) in enumerate(tiles):
        x = 0.04 + i * 0.158
        fig.patches.append(plt.Rectangle((x, 0.74), 0.148, 0.12, transform=fig.transFigure, fc=PANEL, ec='#2b3a55'))
        fig.text(x + 0.074, 0.815, v, ha='center', fontsize=20, fontweight='bold', color=ah.C1); fig.text(x + 0.074, 0.765, l, ha='center', fontsize=9.5, color='#8fa0bd')
    y = 0.69; dy = min(0.13, 0.52 / (len(A['bands']) + 1)); wrapw = 190 if len(A['bands']) > 3 else 175
    for Bd in A['bands']:
        dop = [t for t in Bd['tracks'] if t['kind'] == 'doppler']; dr = [t for t in Bd['tracks'] if t['kind'] in ('drift', 'common-mode drift')]
        head = f"{Bd['band']['name']}: σ {Bd['sig']:.3f} dB · {len(Bd['lines'])} lines · {len(dop)} Doppler-like · {len(dr)} drifting · {len(Bd['events'])} intermittent · {len(Bd['wideband'])} wideband"
        body = []
        for t in dop[:3]:
            body.append(f"Doppler-like {t['f_median_hz'] / 1e6:.4f} MHz: {t['t_start_s']:.0f}–{t['t_end_s']:.0f} s, shift {t['shift_hz'] / 1e3:+.2f} kHz, peak rate {t['rate_peak_hz_s']:+.1f} Hz/s"
                        + (f", TCA {t['tca_s']:.0f} s" if t.get('tca_observed') else ''))
        for e in Bd['events'][:2]:
            body.append(f"intermittent {e['f_mhz']:.4f} MHz {e['peak_db']:+.1f} dB at {e['t_start_s']:.0f}–{e['t_end_s']:.0f} s")
        if Bd['wideband']:
            body.append(f"{len(Bd['wideband'])} wideband transient(s) at " + ', '.join(f"{w['t_s']:.0f} s ({w['f_lo_mhz']:.2f}–{w['f_hi_mhz']:.2f} MHz, {w['bins_over_3sigma_pct']:.0f}% bins >3σ)" for w in Bd['wideband'][:4]))
        if Bd['lines']:
            body.append('strongest lines: ' + ', '.join(f"{c['f_mhz']:.4f} (+{c['excess_db']:.2f}, {c.get('origin', '?').split(' (')[0]})" for c in Bd['lines'][:4]))
        fig.text(0.04, y, head, fontsize=12.5, fontweight='bold', va='top')
        fig.text(0.04, y - 0.028, '\n'.join(textwrap.wrap(' · '.join(body) or 'no content above the detection thresholds', wrapw)[:3]), fontsize=9.5 if len(A['bands']) > 3 else 10.5, va='top', color='#c9d4e6')
        y -= dy
    I = A.get('id', {})
    if I.get('available'):
        ids = I['ids']; nh = I['not_heard']; ct = I.get('control', {})
        conf = [x for x in ids if x['confidence'] != 'frequency-only']; cand = [x for x in ids if x['confidence'] == 'frequency-only']
        head = (f"Identification: {len(conf)} confirmed by Doppler · {len(cand)} frequency-only candidate(s) · {len(I['passes'])} satellites in band above the horizon"
                f" · {len(nh)} ≥10° not heard")
        ids = conf + cand
        body = [f"{x['name']} ({x['norad']}) {x['mode']} {int(x['baud'] or 0)} Bd at {x['f_mhz']:.4f} MHz: {x['confidence']}, offset {x['offset_hz'] / 1e3:+.1f} kHz"
                + (f", rms {x['rms_hz']:.0f} Hz" if x.get('rms_hz') is not None else '') + f", format {x['format_check']}" for x in ids[:4]]
        body += [f"not heard: " + ', '.join(f"{p['name']} (max {p['max_el']:.0f}°)" for p in nh[:8])] if nh else []
        body += [f"false-alarm controls: off-curve {ct.get('false_detections', 0)}/{ct.get('searches', 0)}, stack {I.get('stack_control', {}).get('false_detections', 0)}/{I.get('stack_control', {}).get('searches', 0)}"]
        fig.text(0.04, y, head, fontsize=12.5, fontweight='bold', va='top', color=C_PRED)
        fig.text(0.04, y - 0.028, '\n'.join(textwrap.wrap(' · '.join(body) or 'no observed signal matched a predicted downlink', wrapw)[:4]), fontsize=9.5, va='top', color='#c9d4e6')
        y -= dy + 0.03
        nd = ('Absolute levels (uncalibrated). ' + origin_note(A) + ' Identification uses the SatNOGS transmitter list and TLEs dated '
              + str((I.get('db_meta') or {}).get('fetched_utc', '?'))[:16] + f" and the observer position ({I.get('qth_source', 'unknown source')}); "
              'a confirmed match is a Doppler-curve fit, a frequency-only candidate is a coincidence in frequency; nothing is decoded.')
    else:
        nd = f"Which satellite a track belongs to: identification not available ({I.get('reason', 'not run')}). Absolute levels (uncalibrated). " + origin_note(A)
    if A['cfg'].get('dc_exclude_hz', 1e5) < 5e4:
        nd = f"WARNING: DC exclusion {A['cfg'].get('dc_exclude_hz', 0) / 1e3:.0f} kHz < 50 kHz: the receiver DC skirt produces false tracks; use a satplan.py plan. " + nd
    fig.text(0.04, max(y, 0.12), 'Not decided by this data', fontsize=12.5, fontweight='bold', color=ah.C2, va='top')
    fig.text(0.04, max(y, 0.12) - 0.03, '\n'.join(textwrap.wrap(nd, 175)), fontsize=10.5, va='top', color='#c9d4e6')
    fig.savefig(out / f'{tag}_p1_summary.png', dpi=110); pdf.savefig(fig); plt.close(fig)


def id_pages(A, pdf, out, tag):
    I = A.get('id', {})
    fig = ah.page(pdf, 'Identification and signal format', 'observed tracks/events matched to predicted downlinks (SatNOGS transmitters, SGP4 Doppler) · observed format from the Doppler-aligned spectrum')
    if not I.get('available'):
        fig.text(0.5, 0.5, f"Identification not available: {I.get('reason', '')}", ha='center', fontsize=18, color='#8fa0bd')
    rows = [['band', 'f MHz', 'time s', 'observed format', 'BW kHz', 'satellite', 'mode / baud', 'offset kHz', 'rms Hz', 'confidence', 'format check']]
    for Bd in A['bands']:
        for t in Bd['tracks']:
            if t['kind'] == 'stable' and 'id' not in t:
                continue
            i = t.get('id', {})
            rows.append([Bd['band']['name'].split('(')[0].strip(), f"{t['f_median_hz'] / 1e6:.4f}", f"{t['t_start_s']:.0f}-{t['t_end_s']:.0f}", t['format']['cls'][:44], f"{t['format']['bw_hz'] / 1e3:.1f}",
                         f"{i.get('name', '-')[:22]}" + (f" ({i['norad']})" if i else ''), f"{i.get('mode', '')} {int(i.get('baud') or 0) or ''}", f"{i['offset_hz'] / 1e3:+.1f}" if i else '',
                         f"{i['rms_hz']:.0f}" if i.get('rms_hz') is not None else '', i.get('confidence', 'no match'), i.get('format_check', '')[:30]])
        for e in Bd['events']:
            i = e.get('id', {})
            rows.append([Bd['band']['name'].split('(')[0].strip(), f"{e['f_mhz']:.4f}", f"{e['t_start_s']:.0f}-{e['t_end_s']:.0f}", e['format']['cls'][:44], f"{e['format']['bw_hz'] / 1e3:.1f}",
                         f"{i.get('name', '-')[:22]}" + (f" ({i['norad']})" if i else ''), f"{i.get('mode', '')} {int(i.get('baud') or 0) or ''}", f"{i['offset_hz'] / 1e3:+.1f}" if i else '',
                         '', i.get('confidence', 'no match'), i.get('format_check', '')[:30]])
    if len(rows) == 1:
        rows.append(['—', '', '', 'no track or intermittent emission detected', '', '', '', '', '', '', ''])
    ax = fig.add_axes([0.03, 0.08, 0.94, 0.78]); ax.axis('off')
    tb = ax.table(cellText=rows[1:24], colLabels=rows[0], loc='upper left', cellLoc='left', colWidths=[.1, .06, .07, .2, .05, .15, .09, .06, .05, .07, .1])
    tb.auto_set_font_size(False); tb.set_fontsize(9)
    for (r, c), cell in tb.get_celld().items():
        cell.set_facecolor(PANEL if r else '#14203a'); cell.set_edgecolor('#2b3a55'); cell.set_text_props(color=FG)
    fig.savefig(out / f'{tag}_p7_identification.png', dpi=110); pdf.savefig(fig); plt.close(fig)
    # passes timeline
    P = I.get('passes', [])
    fig = ah.page(pdf, 'Satellites above the horizon with in-band downlinks', f"{len(P)} satellites · elevation vs time over the scan · thick = heard (matched) · position: {I.get('qth_source', 'n/a')}")
    if P:
        ax = fig.add_axes([0.07, 0.1, 0.6, 0.75])
        for k, p in enumerate(P[:40]):
            t = np.array(p['curve_t_s']); el = np.array(p['curve_el']); elp = np.where(el > 0, el, np.nan)
            ax.plot(t, elp, lw=2.4 if p['heard'] else 0.9, color=C_DOP if p['heard'] else C_PRED, alpha=1 if p['heard'] else .7)
            j = int(np.argmax(el)); ax.annotate(p['name'][:16], (t[j], el[j]), fontsize=8, color='#c9d4e6', ha='center', va='bottom')
        ax.axvspan(0, A['timing']['span_s'], color='#ffffff08'); ax.set_xlim(-60, A['timing']['span_s'] + 60); ax.set_ylim(0, 90)
        ax.set_xlabel('seconds since scan start'); ax.set_ylabel('elevation (deg)')
        ax2 = fig.add_axes([0.7, 0.1, 0.28, 0.75]); ax2.axis('off')
        lines = [(f"{'✓' if p['heard'] else '·'} {p['name'][:18]:18s} {p['max_el']:3.0f}° " + ', '.join(f"{x['f_mhz']:.3f} {x['mode'][:10]}" for x in p['transmitters'][:2]))[:62] for p in P[:40]]
        ax2.text(0, 1, '\n'.join(lines), va='top', fontsize=7.8, family='monospace', color='#c9d4e6')
    else:
        fig.text(0.5, 0.5, 'No satellite with an in-band downlink was above the horizon during the scan' if I.get('available') else f"not available: {I.get('reason', '')}",
                 ha='center', fontsize=18, color='#8fa0bd')
    fig.savefig(out / f'{tag}_p8_passes.png', dpi=110); pdf.savefig(fig); plt.close(fig)


def gain_page(A, pdf, out, tag):
    gc = A.get('gaincal')
    if not gc:
        return
    fig = ah.page(pdf, 'Gain calibration', f"chosen {gc.get('chosen_gain_db')} dB · {gc.get('reason', '')}")
    png = A.get('gaincal_png')
    if png and Path(png).exists():
        ax = fig.add_axes([0.04, 0.16, 0.92, 0.68]); ax.imshow(plt.imread(png)); ax.axis('off')
    rows = [r for r in gc.get('rows', [])]
    txt = '   '.join(f"{r['gain_db']:.0f} dB: {'safe' if r['safe'] else 'UNSAFE'}"
                   + (f", noise {r['input_noise_db']:.1f}, max {r['max_component']:.2f}, p99.9 {r['p999_component']:.3f}" if r.get('input_noise_db') is not None else f", rc {r['rc']}") for r in rows)
    fig.text(0.04, 0.08, '\n'.join(textwrap.wrap(txt, 190)), fontsize=9.5, color='#c9d4e6', va='top')
    fig.savefig(out / f'{tag}_p0_gain.png', dpi=110); pdf.savefig(fig); plt.close(fig)


def timing_page(A, pdf, out, tag):
    fig = ah.page(pdf, 'Timing and integrity', 'dwell per hop, retune gaps per transition (incl. the 137↔435 MHz jumps), host read latency')
    tm = A['timing']; ax = fig.add_axes([0.06, 0.12, 0.5, 0.7])
    keys = list(tm['retune_gap_s'].keys()); vals = [tm['retune_gap_s'][k] * 1e3 for k in keys]
    ax.barh(range(len(keys)), vals, color=ah.C1, height=.6); ax.set_yticks(range(len(keys))); ax.set_yticklabels(keys); ax.set_xlabel('median retune gap (ms)')
    for i, v in enumerate(vals):
        ax.text(v, i, f' {v:.0f} ms', va='center', fontsize=10)
    ax2 = fig.add_axes([0.64, 0.12, 0.32, 0.7]); ax2.axis('off')
    rows = [('records', A['info']['records']), ('complete sweeps', A['info']['sweeps']), ('statuses', ', '.join(A['info']['statuses'])),
            ('continuity', ', '.join(A['info']['continuity'])), ('sweep period', f"{tm['sweep_period_s']:.3f} s"), ('dwell', f"{tm['dwell_s']:.3f} s"),
            ('span', f"{tm['span_s']:.0f} s"), ('max read latency', f"{tm['max_read_latency_ms']:.1f} ms")]
    for i, (k, v) in enumerate(rows):
        ax2.text(0, 0.95 - i * 0.08, k, fontsize=12, va='top', color='#8fa0bd'); ax2.text(0.45, 0.95 - i * 0.08, str(v), fontsize=12, va='top')
    fig.savefig(out / f'{tag}_p9_timing.png', dpi=110); pdf.savefig(fig); plt.close(fig)


def viewer(A, Bd, out, tag):
    S = Bd['S']; f = S['f']
    R = S['B']; q, top = hw.quantize(R)
    import base64, gzip
    blob = base64.b64encode(gzip.compress(q.tobytes(), 9)).decode('ascii')
    hops = list(S['hops']); hopcol = [hops.index(h) for h in S['hop']]
    Tm = [[round(float(x - A['t_ref']), 3) if np.isfinite(x) else 0.0 for x in row] for row in S['T0']]
    tr_meta = []
    for t in Bd['tracks']:
        if t['kind'] == 'stable':
            continue
        tt, fx = t['points']; rows = [int(np.argmin(np.abs(S['T0'][:, hops.index(t['hop'])] - A['t_ref'] - x))) for x in tt]
        lab = f"{t['f_median_hz'] / 1e6:.4f} {t['kind']} {t['shift_hz'] / 1e3:+.2f} kHz" + (f" = {t['id']['name']} ({t['id']['confidence']})" if 'id' in t else '')
        tr_meta.append(dict(kind=t['kind'], f=[round(float(x) / 1e6, 6) for x in fx], r=rows, label=lab))
    T0b = S['T0'][:, 0] - A['t_ref']
    for lab, tt, ff, heard in sid.predicted_curves(A, Bd['band']):
        rows = [int(np.argmin(np.abs(T0b - x))) for x in tt]
        tr_meta.append(dict(kind='predicted', f=[round(float(x) / 1e6, 6) for x in ff], r=rows, label='pred ' + lab))
    meta = dict(title=Bd['band']['name'], tag=tag, start=A['start'], rows=int(q.shape[0]), cols=int(q.shape[1]), f0=float(f[0] / 1e6), step=float((f[1] - f[0]) / 1e6),
                keep=int(q.shape[1]), nh=len(hops), hopcol=hopcol, centers=[float(A['centers'][h] / 1e6) for h in hops], qlo=hw.Q_LO, qstep=hw.Q_STEP, qlin=hw.Q_LIN,
                qtop=float(top), T=Tm, tend=round(float(A['timing']['span_s']), 3), sigma=round(Bd['sig'], 4), unit=A['info']['unit'], span=round(A['timing']['span_s'], 1),
                records=A['info']['records'], cands=[dict(f=round(c['f_mhz'], 5), db=round(c['excess_db'], 2), pct=round(c['sweeps_pct']), off=0) for c in Bd['lines']],
                events=[dict(f=round(e['f_mhz'], 5), db=round(e['peak_db'], 1), t0=round(e['t_start_s']), t1=round(e['t_end_s'])) for e in Bd['events']],
                tracks=tr_meta, luts={k: hw._lut(k) for k in ('inferno', 'viridis', 'magma', 'gray')}, centre_khz=A['cfg'].get('dc_exclude_hz', 1e5) / 1e3)
    html = hw.VIEWER.replace('__META__', json.dumps(meta, separators=(',', ':'))).replace('__DATA__', blob).replace('__TITLE__', meta['title'])
    name = f'{tag}_waterfall_{slug(Bd["band"]["name"])}.html'; (out / name).write_text(html, encoding='utf-8'); return name


def main():
    a = [x for x in sys.argv[1:] if not x.startswith('--')]
    tag = sys.argv[sys.argv.index('--tag') + 1] if '--tag' in sys.argv else 'superscan'
    bpath = sys.argv[sys.argv.index('--bands') + 1] if '--bands' in sys.argv else None
    qpath = sys.argv[sys.argv.index('--qth') + 1] if '--qth' in sys.argv else None
    dbdir = sys.argv[sys.argv.index('--satdb') + 1] if '--satdb' in sys.argv else None
    a = [x for x in a if x not in (tag, bpath, qpath, dbdir)]
    if len(a) < 2:
        print(__doc__); return 2
    src, out = Path(a[0]), Path(a[1]); out.mkdir(parents=True, exist_ok=True)
    if bpath is None and (src.parent / 'bands.json').exists():
        bpath = str(src.parent / 'bands.json')
    cfg = json.loads(Path(bpath).read_text()) if bpath else {}
    A = analyse(src, cfg); A['cfg'] = cfg
    qpath = qpath or (str(src.parent.parent / 'qth.json') if (src.parent.parent / 'qth.json').exists() else None)
    dbdir = dbdir or (str(src.parent.parent / 'satdb') if (src.parent.parent / 'satdb').exists() else None)
    sid.identify(A, dbdir, qpath)
    gcp = src.parent / 'gaincal.json'
    if gcp.exists():
        A['gaincal'] = json.loads(gcp.read_text()); A['gaincal_png'] = str(src.parent / 'gaincal.png')
    allt = [t for Bd in A['bands'] for t in Bd['tracks']]
    A['summary'] = s = dict(tag=tag, file=str(src), start=A['start'], **{k: v for k, v in A['info'].items()}, timing=A['timing'],
                            doppler_tracks=sum(t['kind'] == 'doppler' for t in allt), drift_tracks=sum(t['kind'] in ('drift', 'common-mode drift') for t in allt),
                            stable_tracks=sum(t['kind'] == 'stable' for t in allt), persistent_lines=sum(len(Bd['lines']) for Bd in A['bands']),
                            events=sum(len(Bd['events']) for Bd in A['bands']), wideband=sum(len(Bd['wideband']) for Bd in A['bands']),
                            bands=[dict(name=Bd['band']['name'], lo_hz=Bd['band']['lo_hz'], hi_hz=Bd['band']['hi_hz'], sigma_db=Bd['sig'], detect_threshold_db=Bd['thr'],
                                        hops=[float(A['centers'][h]) for h in Bd['S']['hops']], lines=Bd['lines'][:40], events=Bd['events'], wideband=Bd['wideband'],
                                        tracks=[{k: v for k, v in t.items() if k != 'points'} for t in Bd['tracks'] if t['kind'] != 'stable' or 'id' in t]) for Bd in A['bands']],
                            identification=dict({k: v for k, v in A['id'].items() if k not in ('passes', 'not_heard')},
                                                not_heard=[{k: v for k, v in p.items() if not k.startswith('curve_')} for p in A['id'].get('not_heard', [])]),
                            gain_selection=dict(used=A['info'].get('gain'), calibration={k: v for k, v in (A.get('gaincal') or {}).items() if k != 'rows'} or None),
                            passes=[{k: v for k, v in p.items() if not k.startswith('curve_')} for p in A['id'].get('passes', [])])
    pdf = PdfPages(out / f'{tag}_analysis.pdf')
    summary_page(A, pdf, out, tag)
    gain_page(A, pdf, out, tag)
    for Bd in A['bands']:
        fig, _ = band_figure(A, Bd, out, tag); pdf.savefig(fig); plt.close(fig)
    fig = doppler_figure(A, out, tag); fig.savefig(out / f'{tag}_doppler.png', dpi=130); pdf.savefig(fig); plt.close(fig)
    id_pages(A, pdf, out, tag); timing_page(A, pdf, out, tag); pdf.close()
    views = [viewer(A, Bd, out, tag) for Bd in A['bands']]
    (out / f'{tag}_summary.json').write_text(json.dumps(s, indent=1, default=float))
    with open(out / f'{tag}_tracks.csv', 'w', newline='') as fh:
        w = csv.writer(fh); w.writerow(['band', 'kind', 'model', 'f_median_mhz', 't_start_s', 't_end_s', 'points', 'shift_hz', 'rate_peak_hz_s', 'rms_hz', 'tca_s', 'f0_mhz', 'mean_level_db',
                                        'observed_format', 'observed_bw_hz', 'id_name', 'id_norad', 'id_mode', 'id_baud', 'id_offset_hz', 'id_rms_hz', 'id_confidence', 'format_check'])
        for t in sorted(allt, key=lambda t: (t['kind'] != 'doppler', t['f_median_hz'])):
            i = t.get('id', {})
            w.writerow([t['band'], t['kind'], t['model'], f"{t['f_median_hz'] / 1e6:.6f}", f"{t['t_start_s']:.1f}", f"{t['t_end_s']:.1f}", t['n'], f"{t['shift_hz']:.0f}",
                        f"{t['rate_peak_hz_s']:.2f}", f"{t['rms_hz']:.0f}", '' if t['tca_s'] is None else f"{t['tca_s']:.1f}", f"{t['f0_hz'] / 1e6:.6f}", f"{t['mean_level_db']:.2f}",
                        t['format']['cls'], f"{t['format']['bw_hz']:.0f}", i.get('name', ''), i.get('norad', ''), i.get('mode', ''), i.get('baud', ''),
                        f"{i['offset_hz']:.0f}" if i else '', f"{i['rms_hz']:.0f}" if i.get('rms_hz') is not None else '', i.get('confidence', ''), i.get('format_check', '')])
    with open(out / f'{tag}_passes.csv', 'w', newline='') as fh:
        w = csv.writer(fh); w.writerow(['norad', 'name', 'sat_status', 'max_el_deg', 't_max_el_s', 't_rise_s', 't_set_s', 'heard', 'downlinks'])
        for p in A['id'].get('passes', []):
            w.writerow([p['norad'], p['name'], p['sat_status'], f"{p['max_el']:.1f}", f"{p['t_max_el_s']:.0f}", f"{p['t_rise_s']:.0f}", f"{p['t_set_s']:.0f}", p['heard'],
                        '; '.join(f"{x['f_mhz']:.4f} {x['mode']} {int(x['baud'] or 0)}{'' if x['alive'] else ' (dead)'}" for x in p['transmitters'])])
    with open(out / f'{tag}_lines.csv', 'w', newline='') as fh:
        w = csv.writer(fh); w.writerow(['band', 'f_mhz', 'excess_db', 'width_khz', 'sweeps_pct', 'origin', 'views'])
        for Bd in A['bands']:
            for c in Bd['lines']:
                w.writerow([Bd['band']['name'], f"{c['f_mhz']:.5f}", f"{c['excess_db']:.2f}", f"{c['width_khz']:.2f}", f"{c['sweeps_pct']:.0f}", c.get('origin', ''),
                            '; '.join(f"LO {v['lo_mhz']:.2f} {v['offset_khz']:+.0f} kHz {v['excess_db']:+.2f} dB" for v in c.get('views', []))])
    with open(out / f'{tag}_events.csv', 'w', newline='') as fh:
        w = csv.writer(fh); w.writerow(['band', 'f_mhz', 't_start_s', 't_end_s', 'sweeps', 'peak_db', 'width_khz', 'on_doppler_track'])
        for Bd in A['bands']:
            for e in Bd['events']:
                w.writerow([Bd['band']['name'], f"{e['f_mhz']:.5f}", f"{e['t_start_s']:.1f}", f"{e['t_end_s']:.1f}", e['sweeps'], f"{e['peak_db']:.2f}", f"{e['width_khz']:.2f}", e['on_track']])
    print(json.dumps(dict(sweeps=s['sweeps'], records=s['records'], doppler=s['doppler_tracks'], drift=s['drift_tracks'], stable=s['stable_tracks'],
                          lines=s['persistent_lines'], events=s['events'], wideband=s['wideband'],
                          identified=sum(x['confidence'] != 'frequency-only' for x in A['id'].get('ids', [])),
                          candidates=sum(x['confidence'] == 'frequency-only' for x in A['id'].get('ids', [])),
                          passes=len(A['id'].get('passes', [])), id_available=A['id'].get('available'), id_reason=A['id'].get('reason'), viewers=views)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
