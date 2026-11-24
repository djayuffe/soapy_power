#!/usr/bin/env python3
"""Low-level spectral morphology of the cellular downlink captures.

The raw session stores only 8192-bin FFT power per record (no IQ, no phase),
so true demodulation is impossible. This tool therefore analyses what the
stored spectra do contain at the finest available scale: 10 kHz resampled
spectra, plateau-referenced occupied bandwidth and edge widths, ripple,
spectral flatness, a spatial periodogram (the ~45 kHz comb plus 180/200 kHz
controls), per-pass level and centroid tracks and narrow-component
correlations.

Every statement is spectral-only and relative. Nothing here is demodulation,
EVM, a constellation, a symbol rate, a modulation order, a cell identity, an
ARFCN or an operator. "Consistent with" never means "identified".

usage: report_modulation.py SESSION [--out PNG] [--raster-out PNG]
                                  [--dyn-out PNG] [--md MD]
                                  [--cache NPZ] [--use-cache]
"""
import argparse
import json
import sys
import textwrap
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_cellular as rc  # noqa: E402  (style, band metadata, helpers)

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.gridspec import GridSpec, GridSpecFromSubplotSpec  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

warnings.filterwarnings('ignore', category=RuntimeWarning)

BG = rc.BG
PANEL = rc.PANEL
CARD = rc.CARD
FG = rc.FG
MUTED = rc.MUTED
GRID = rc.GRID
ON = rc.ON
OFF = rc.OFF
GREEN = rc.GREEN
RED = rc.RED
GOLD = rc.GOLD
PURPLE = rc.PURPLE

FINE_RES_MHZ = 0.01
EDGE_HZ = rc.EDGE_HZ
DC_HZ = rc.DC_HZ
COVER_MIN = rc.COVER_MIN

BAND_ORDER = ['b28', 'lte800', 'gsm900', 'mhz1800', 'lte2100', 'lte2600']
BANDS = [{**b, 'res': FINE_RES_MHZ}
         for b in (rc.BANDS + [rc.SURVEY_BANDS[0]])]
BANDS = sorted(BANDS, key=lambda b: BAND_ORDER.index(b['key']))
BY_KEY = {b['key']: b for b in BANDS}

# Published threshold extents from CELLULAR.md. The fine-view windows below
# are the same blocks, trimmed to a measurable covered span where a DC mask or
# a threshold-limited edge would otherwise define the result. They are analysis
# windows, not modulation bandwidths or channel assignments. Each is padded
# before edge metrics; the pad is clipped to the nominal band and the masks.
WINDOWS = [
    dict(key='b28', label='700', lo=768.25, hi=777.75, variant='off',
         published=(768.15, 777.80),
         note='separate 761.5–765.3 MHz block not analysed; ON has a '
              '3 MHz DC gap at 767.99–770.98'),
    dict(key='lte800', label='LTE800-A', lo=801.30, hi=810.50, variant='on',
         published=(801.30, 810.50),
         note='lower of two adjacent 9.2 MHz spans'),
    dict(key='lte800', label='LTE800-B', lo=811.30, hi=820.50, variant='on',
         published=(811.30, 820.50),
         note='upper of two adjacent 9.2 MHz spans'),
    dict(key='gsm900', label='900-930', lo=930.55, hi=935.80, variant='on',
         published=(930.34, 939.92),
         note='right edge is the 935.87–938.86 MHz DC mask; published extent '
              'continued behind it'),
    dict(key='gsm900', label='900-954', lo=950.40, hi=959.45, variant='on',
         published=(954.38, 959.50),
         note='shallow plateau starts near 950.4 MHz; published threshold '
              'extent began at 954.38'),
    dict(key='mhz1800', label='1800-1811', lo=1811.05, hi=1820.15, variant='on',
         published=(1811.48, 1820.12),
         note='lower 1800 MHz span'),
    dict(key='mhz1800', label='1800-1820', lo=1820.95, hi=1831.15, variant='on',
         published=(1820.98, 1839.02),
         note='left of the 1831.20–1834.19 MHz DC mask; a separate right '
              'sub-span at 1834.2–1839.0 is not analysed'),
    dict(key='mhz1800', label='1800-1840', lo=1840.75, hi=1859.05, variant='on',
         published=(1840.72, 1859.02),
         note='contains the 1840.89 MHz narrow component'),
    dict(key='mhz1800', label='1800-1862', lo=1862.22, hi=1876.20, variant='on',
         published=(1862.18, 1876.06),
         note='weak upper tail; starts after the 1859.18–1862.17 MHz DC mask'),
    dict(key='lte2100', label='LTE2100', lo=2110.95, hi=2129.05, variant='off',
         published=(2110.95, 2129.05),
         note='ON misses the lower 3 MHz behind the 2111.01–2113.99 MHz DC mask'),
]


def fmt_span(a, b):
    return f'{a:.2f}–{b:.2f}'


def fmt_delta(x, nd=1):
    if x is None or not np.isfinite(x):
        return '—'
    if abs(x) < 0.05:
        return '0.0'
    return f'{x:+.{nd}f}'


def load_band_meta():
    return {b['key']: b for b in BANDS}


def extract_fine(path, bands, res_mhz=FINE_RES_MHZ, edge_hz=EDGE_HZ,
                 dc_hz=DC_HZ):
    """Per-pass cell means from linear-power interpolation.

    Cell centres lie on a fixed 10 kHz grid. For each record the native
    6.8359 kHz PSD is linearly interpolated in linear power at those centres,
    then averaged over records in the pass. Interpolating avoids the periodic
    occupancy beat that nearest-bin assignment produces when the native bin
    width is not commensurate with the grid (10 / 6.8359 is not an integer).
    The output is 10*log10(mean linear power), relative and uncalibrated.
    """
    step = res_mhz * 1e6
    grids = {}
    for b in bands:
        n = int(np.ceil((b['b1'] - b['b0']) / res_mhz))
        grids[b['key']] = (n, b['b0'] * 1e6 + (np.arange(n) + 0.5) * step)
    passes = []
    prev = None
    cur = None
    records = 0
    skipped = 0
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            pw = r.get('power')
            if not pw:
                skipped += 1
                continue
            meta = r.get('metadata') or {}
            center = meta.get('tuned_center_hz')
            if center is None:
                skipped += 1
                continue
            center = float(center)
            if prev is None or center < prev - 1e6:
                cur = {'sums': {}, 'cnts': {}, 't_start': r.get('time_start'),
                       't_stop': r.get('time_stop')}
                for b in bands:
                    n = grids[b['key']][0]
                    cur['sums'][b['key']] = np.zeros(n)
                    cur['cnts'][b['key']] = np.zeros(n, dtype=np.int32)
                passes.append(cur)
            prev = center
            records += 1
            cur['t_stop'] = r.get('time_stop')
            pw = np.asarray(pw, float)
            f0 = float(r['first_bin_center_hz'])
            w = float(r['bin_width_hz'])
            start = float(r['start_hz'])
            stop = float(r['stop_hz'])
            fr = f0 + w * np.arange(pw.size)
            good = (np.isfinite(pw) & (fr >= start + edge_hz)
                    & (fr <= stop - edge_hz) & (np.abs(fr - center) >= dc_hz))
            if not good.any():
                continue
            lin = np.power(10.0, pw[good] / 10.0)
            fr = fr[good]
            for b in bands:
                n, fc = grids[b['key']]
                # Only interpolate inside the record's usable span; do not
                # extrapolate beyond the native bins or across the DC mask.
                tgt = ((fc >= start + edge_hz) & (fc <= stop - edge_hz)
                       & (fc >= fr[0]) & (fc <= fr[-1])
                       & (np.abs(fc - center) >= dc_hz))
                if not tgt.any():
                    continue
                cur['sums'][b['key']][tgt] += np.interp(fc[tgt], fr, lin)
                cur['cnts'][b['key']][tgt] += 1

    out = {}
    for b in bands:
        mat = np.full((len(passes), int(np.ceil((b['b1'] - b['b0'])
                                             / res_mhz))), np.nan)
        for i, p in enumerate(passes):
            cnt = p['cnts'][b['key']]
            ok = cnt > 0
            mat[i, ok] = 10.0 * np.log10(p['sums'][b['key']][ok] / cnt[ok])
        out[b['key']] = mat
    if passes:
        tt = np.array([datetime.fromisoformat(p['t_start']).timestamp()
                       for p in passes])
        out['_t'] = tt - tt[0]
        out['_abs'] = tt
    else:
        out['_t'] = np.array([])
        out['_abs'] = np.array([])
    return out, records, len(passes), skipped


def load_data(sess, args):
    cache = Path(args.cache) if args.cache else sess / 'report' / 'modulation_cache.npz'
    if args.use_cache and cache.exists():
        z = np.load(cache)
        meta = json.loads(z['meta'].item()) if 'meta' in z.files else {}
        if (meta.get('edge_hz') == EDGE_HZ and meta.get('dc_hz') == DC_HZ
                and meta.get('bands') == [b['key'] for b in BANDS]
                and meta.get('res_mhz') == FINE_RES_MHZ):
            data = {k: z[k] for k in z.files if k != 'meta'}
            print(f'loaded cache {cache}')
            return data, meta
        print(f'cache {cache} does not match this analysis; re-extracting')
    on, on_records, on_passes, on_skip = extract_fine(
        sess / 'features-on.jsonl', BANDS)
    off, off_records, off_passes, off_skip = extract_fine(
        sess / 'features-off.jsonl', BANDS)
    data = {}
    for k, v in on.items():
        data[f'on_{k}'] = v
    for k, v in off.items():
        data[f'off_{k}'] = v
    meta = dict(edge_hz=EDGE_HZ, dc_hz=DC_HZ, res_mhz=FINE_RES_MHZ,
                method='linear_power_interp',
                bands=[b['key'] for b in BANDS],
                on_records=on_records, on_passes=on_passes,
                off_records=off_records, off_passes=off_passes,
                on_skipped=on_skip, off_skipped=off_skip,
                extracted_at=datetime.now(timezone.utc).isoformat())
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache, **data, meta=json.dumps(meta))
    print(f'wrote cache {cache}')
    return data, meta


def axis_f(b):
    n = int(np.ceil((b['b1'] - b['b0']) / b['res']))
    return b['b0'] + (np.arange(n) + 0.5) * b['res']


def band_stats(mat, b):
    """Coverage, floor and pass percentiles on the fine grid."""
    f = axis_f(b)
    cov = np.isfinite(mat).mean(axis=0)
    ok = cov >= COVER_MIN
    if not ok.any():
        raise ValueError(f'no covered cells in {b["name"]}')
    med = np.nanmedian(mat, axis=0)
    floor = float(np.nanpercentile(med[ok], 10))
    return dict(
        f=f, cov=cov, ok=ok, med=med,
        p10=np.nanpercentile(mat, 10, axis=0),
        p25=np.nanpercentile(mat, 25, axis=0),
        p75=np.nanpercentile(mat, 75, axis=0),
        p90=np.nanpercentile(mat, 90, axis=0),
        p95=np.nanpercentile(mat, 95, axis=0),
        floor=floor, cover=float(ok.mean()),
    )


def window_slice(b, m, win, pad=0.5):
    lo = max(b['b0'], win['lo'] - pad)
    hi = min(b['b1'], win['hi'] + pad)
    sel = (m['f'] >= lo - 1e-9) & (m['f'] <= hi + 1e-9)
    if not sel.any():
        return None
    return dict(lo=lo, hi=hi, sel=sel, f=m['f'][sel],
                med=m['med'][sel], ok=m['ok'][sel])


def runs(mask):
    """Inclusive (start, end) index pairs of contiguous True values."""
    idx = np.where(mask)[0]
    out = []
    if not idx.size:
        return out
    s = p = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - p > 1:
            out.append((s, p))
            s = i
        p = i
    out.append((s, p))
    return out


def pick_run(rs, peak_i):
    """Run containing the peak, else the longest run."""
    for r in rs:
        if r[0] <= peak_i <= r[1]:
            return r
    return max(rs, key=lambda r: r[1] - r[0])


def run_clipped(f, ok, i, j, n, step):
    """Whether a run side is clipped by the window or a coverage/mask gap."""
    left = (i <= 0 or not ok[i - 1]
            or (f[i] - f[i - 1]) > step * 1.5)
    right = (j >= n - 1 or not ok[j + 1]
             or (f[j + 1] - f[j]) > step * 1.5)
    return bool(left), bool(right)


def block_metrics(b, m, win, pad=0.5):
    """Plateau-referenced morphology of one published block window.

    Widths are measured on a 150 kHz rolling-median spectrum relative to the
    in-window 85th percentile (the plateau), so narrow edge spikes and single
    cells do not define the block. A run that touches a coverage gap, the
    1.5 MHz DC mask or the padded window is reported as clipped: its width is
    a lower bound, never a completed edge. The baseline subtraction for OBW is
    a display reference, not a correction and not an absolute floor.
    """
    ws = window_slice(b, m, win, pad=pad)
    if ws is None:
        return None
    f, y, ok = ws['f'], ws['med'], ws['ok']
    finite = np.isfinite(y) & ok
    if finite.sum() < 10:
        return None
    step = b['res']
    peak_i = int(np.nanargmax(np.where(finite, y, np.nan)))
    peak = float(y[peak_i])
    ys = rc.rollmed(np.where(finite, y, np.nan), 15)
    inwin = finite & (f >= win['lo'] - 1e-9) & (f <= win['hi'] + 1e-9)
    plateau = float(np.nanpercentile(ys[inwin], 85))
    local_floor = float(np.nanpercentile(ys[finite], 10))
    band_floor = float(m['floor'])
    snr = plateau - band_floor

    widths = {}
    runs_by = {}
    # Block support: the plateau-10 dB run. Widths are then the outermost
    # crossings of each higher level inside that support, so an interior fade
    # or a narrow edge spike cannot collapse the reported width to one run.
    rs = runs(finite & np.isfinite(ys) & (ys >= plateau - 10.0))
    support = pick_run(rs, peak_i) if rs else (peak_i, peak_i)
    i0, i1 = support
    cl_sup, cr_sup = run_clipped(f, ok, i0, i1, len(f), step)
    support_clipped = cl_sup or cr_sup
    for drop in (3.0, 10.0, 20.0):
        good = np.isfinite(ys) & (ys >= plateau - drop)
        idx = np.where(good[i0:i1 + 1])[0]
        if not idx.size:
            widths[drop] = None
            runs_by[drop] = None
            continue
        i, j = i0 + int(idx[0]), i0 + int(idx[-1])
        runs_by[drop] = (i, j)
        # A side is clipped if the support itself is clipped and the crossing
        # sits on that support boundary.
        cl = support_clipped and i == i0
        cr = support_clipped and j == i1
        widths[drop] = dict(
            width=float(f[j] - f[i]), lo=float(f[i]), hi=float(f[j]),
            clipped_l=bool(cl), clipped_r=bool(cr),
        )
    body = support
    obw = None
    if body is not None:
        i, j = body
        seg = slice(i, j + 1)
        bl = 10.0 ** (local_floor / 10.0)
        p_ex = np.maximum(10.0 ** (np.nan_to_num(ys[seg], nan=-300.0) / 10.0)
                          - bl, 0.0)
        tot = float(p_ex.sum())
        if tot > 0:
            c = np.cumsum(p_ex)
            k0 = int(np.searchsorted(c, 0.005 * tot))
            k1 = min(int(np.searchsorted(c, 0.995 * tot)), len(p_ex) - 1)
            obw = float(f[i + k1] - f[i + k0])

    # Ripple, flatness, troughs and shoulders use the plateau-10 dB body.
    rip = p95p5 = flat = None
    troughs = []
    shoulders = {}
    if body:
        i, j = body
        seg = slice(i, j + 1)
        vals = ys[seg]
        good = np.isfinite(vals)
        if good.sum() >= 10:
            v = vals[good]
            rip = float(np.percentile(v, 75) - np.percentile(v, 25))
            p95p5 = float(np.percentile(v, 95) - np.percentile(v, 5))
            bl = 10.0 ** (local_floor / 10.0)
            pex = np.maximum(10.0 ** (v / 10.0) - bl, 0.0)
            pos = pex[pex > 0]
            if pos.size >= 10 and pos.size >= 0.5 * pex.size:
                flat = float(np.exp(np.mean(np.log(pos))) / np.mean(pos))
        # Troughs at least 6 dB below plateau and 0.12 MHz wide.
        deep = np.isfinite(ys) & (f >= f[i]) & (f <= f[j]) & (ys < plateau - 6.0)
        for a, c in runs(deep):
            if (c - a) * step >= 0.12:
                troughs.append(dict(lo=float(f[a]), hi=float(f[c]),
                                    depth=float(plateau - np.nanmin(ys[a:c + 1]))))
        # 3->20 dB transition measured outward from each -3 dB crossing.
        w3 = widths.get(3.0)
        if w3 and np.isfinite(ys).any():
            i3 = int(np.argmin(np.abs(f - w3['lo'])))
            j3 = int(np.argmin(np.abs(f - w3['hi'])))
            for side, rng, edge_i in (('left', range(i3, -1, -1), i3),
                                      ('right', range(j3, len(f)), j3)):
                c20 = None
                for k in rng:
                    v = ys[k]
                    if not np.isfinite(v):
                        break
                    if v <= plateau - 20.0:
                        c20 = k
                        break
                shoulders[side] = (abs(float(f[c20] - f[edge_i]))
                                   if c20 is not None else None)
    troughs.sort(key=lambda t: -t['depth'])
    w10 = widths.get(10.0)
    w3 = widths.get(3.0)
    return dict(
        f=f, med=y, ok=ok, ys=ys, peak=peak, peak_f=float(f[peak_i]),
        plateau=plateau, local_floor=local_floor, band_floor=band_floor,
        snr=snr, obw99=obw, widths=widths, shoulders=shoulders,
        ripple=rip, ripple_p95p5=p95p5, flatness=flat, troughs=troughs[:4],
        peak_excess=peak - plateau,
        coverage=float(ok.mean()), span=(ws['lo'], ws['hi']),
        w10=w10, w3=w3,
        reliable=bool(snr >= 10.0 and w10 is not None
                      and not (w10['clipped_l'] or w10['clipped_r'])),
        clip_note=('mask/window-clipped' if w10 and
                   (w10['clipped_l'] or w10['clipped_r']) else ''),
    )


def segment_for(m, lo, hi, min_mhz=3.0):
    """Largest contiguous covered run inside [lo, hi]."""
    f = m['f']
    sel = (f >= lo - 1e-9) & (f <= hi + 1e-9) & m['ok'] & np.isfinite(m['med'])
    rs = [r for r in runs(sel) if (r[1] - r[0]) * 0.01 >= min_mhz]
    if not rs:
        return None
    return max(rs, key=lambda r: r[1] - r[0])


def _spectrum_seg(f, y, ok, lo, hi, trim=0.02, detrend_mhz=1.0):
    g = segment_for(dict(f=f, med=y, ok=ok), lo, hi)
    if g is None:
        return None
    i, j = g
    n0 = j - i + 1
    t = max(1, int(trim * n0))
    i, j = i + t, j - t
    ff = f[i:j + 1].copy()
    yy = y[i:j + 1].copy()
    if not np.isfinite(yy).all():
        return None
    base = rc.rollmed(yy, int(round(detrend_mhz / 0.01)))
    z = yy - base
    z = z - np.nanmean(z)
    win = np.hanning(len(z))
    return dict(f=ff, z=z, win=win, span=(ff[0], ff[-1]), n=len(z))


def spatial_periodogram(f, y, ok, lo, hi, n_null=120, seed=7):
    """Spatial periodogram of the detrended log-spectrum.

    x is frequency in MHz, so the transform variable is cycles/MHz and a peak
    at q cycles/MHz corresponds to a period of 1000/q kHz. The observed
    spectrum is detrended with a 1 MHz rolling median, which preserves
    structure at 25-400 kHz. Permutation nulls use the same window and
    trimming; they test the fixed spectral raster, not a physical source.
    """
    s = _spectrum_seg(f, y, ok, lo, hi)
    if s is None:
        return None
    z, win = s['z'], s['win']
    n = s['n']
    nfft = 1 << int(np.ceil(np.log2(n * 16)))
    fr = np.fft.rfftfreq(nfft, d=0.01)
    msel = (fr >= 2.5) & (fr <= 40.0)
    P = np.abs(np.fft.rfft(z * win, nfft)) ** 2
    Ps = np.convolve(P, np.ones(5) / 5, mode='same')
    Pm, fr_m = Ps[msel], fr[msel]
    k = int(np.argmax(Pm))
    df = fr[1] - fr[0]

    def contrast(Pv, f0):
        kk = int(np.argmin(np.abs(fr - f0)))
        bg = (np.abs(fr - f0) <= 3.0) & (np.abs(fr - f0) > 4 * df)
        if not bg.any() or Pv[kk] <= 0:
            return np.nan
        return float(10.0 * np.log10(Pv[kk] / np.median(Pv[bg])))

    spokes = [45.0, 60.0, 90.0, 180.0, 200.0]
    obs = {sp: contrast(Ps, 1000.0 / sp) for sp in spokes}
    rng = np.random.default_rng(seed)
    null = {sp: [] for sp in spokes}
    for _ in range(n_null):
        zp = rng.permutation(z)
        Pn = np.abs(np.fft.rfft(zp * win, nfft)) ** 2
        Pn = np.convolve(Pn, np.ones(5) / 5, mode='same')
        for sp in spokes:
            null[sp].append(contrast(Pn, 1000.0 / sp))
    bgpk = (np.abs(fr_m - fr_m[k]) <= 3.0) & \
           (np.abs(fr_m - fr_m[k]) > 4 * df)
    peak_contrast = float(10.0 * np.log10(Pm[k] / np.median(Pm[bgpk])))
    return dict(
        period_khz=float(1000.0 / fr_m[k]), peak_contrast=peak_contrast,
        contrasts=obs,
        null95={sp: float(np.nanpercentile(null[sp], 95)) for sp in spokes},
        freq=fr[msel], power=Pm,
        span=s['span'], n=n,
    )


def fit_component(mat, m, lo, hi, period_khz):
    """Per-pass projection onto one sinusoid of the detrended log-spectrum.

    The amplitude is descriptive: it is the peak-to-peak swing of a fitted
    sinusoid in dB, not a modulation depth or a physical power fraction.
    Phase coherence R is the resultant length of the per-pass phases.
    """
    s = _spectrum_seg(m['f'], m['med'], m['ok'], lo, hi)
    if s is None:
        return None
    ff, win = s['f'], s['win']
    g = segment_for(m, lo, hi)
    if g is None:
        return None
    i, j = g
    n0 = j - i + 1
    t = max(1, int(0.02 * n0))
    i, j = i + t, j - t
    fq = 1000.0 / period_khz
    amps, phases, periods = [], [], []
    for row in mat:
        yy = row[i:j + 1]
        if not np.isfinite(yy).all():
            continue
        base = rc.rollmed(yy, 100)
        z = yy - base
        z = z - np.mean(z)
        C = np.sum(z * win * np.exp(-2j * np.pi * fq * ff))
        a = 2.0 * abs(C) / np.sum(win)
        amps.append(a)
        phases.append(float(np.angle(C)))
        # per-pass best period in a +/-15% window around the tested period
        nfft = 1 << int(np.ceil(np.log2(len(z) * 16)))
        P = np.abs(np.fft.rfft(z * win, nfft)) ** 2
        frq = np.fft.rfftfreq(nfft, d=0.01)
        sel = (frq >= fq * 0.85) & (frq <= fq * 1.15)
        if sel.any():
            periods.append(1000.0 / frq[sel][int(np.argmax(P[sel]))])
    amps = np.asarray(amps)
    ph = np.asarray(phases)
    R = float(abs(np.mean(np.exp(1j * ph)))) if ph.size else np.nan
    return dict(
        period=period_khz, amp=float(np.mean(amps)) if amps.size else np.nan,
        pp=float(2.0 * np.mean(amps)) if amps.size else np.nan,
        R=R, n=int(amps.size), phases=ph, amps=amps,
        period_med=float(np.median(periods)) if periods else np.nan,
        period_sd=float(np.std(periods)) if periods else np.nan,
    )


def pass_tracks(mat, m, met, min_snr=6.0):
    """Per-pass power-centroid frequency, body level and occupancy.

    The centroid uses linear power above the local 10th-percentile baseline
    inside the plateau-10 dB body. Unlike an argmax it does not jump to a
    narrow edge spike or to the strongest fading finger, so its spread measures
    body motion rather than peak selection.
    """
    if met['w10'] is None:
        lo, hi = met['span']
    else:
        lo, hi = met['w10']['lo'], met['w10']['hi']
    f = m['f']
    sel = (f >= lo) & (f <= hi) & m['ok']
    if sel.sum() < 5:
        return None
    fsel = f[sel]
    bl = 10.0 ** (met['local_floor'] / 10.0)
    cen, lv = [], []
    for row in mat:
        yy = row[sel].copy()
        good = np.isfinite(yy)
        if good.sum() < 5:
            continue
        wgt = np.maximum(10.0 ** (np.where(good, yy, -300.0) / 10.0) - bl,
                         0.0)
        if wgt.sum() <= 0:
            continue
        cen.append(float(np.sum(fsel * wgt) / np.sum(wgt)))
        lv.append(float(np.nanmedian(np.where(good, yy, np.nan))))
    if not cen:
        return None
    cen, lv = np.asarray(cen), np.asarray(lv)
    return dict(
        centroid=cen, level=lv, n=int(cen.size),
        centroid_med=float(np.median(cen)),
        centroid_std=float(np.std(cen, ddof=1)) if cen.size > 1 else np.nan,
        level_med=float(np.median(lv)),
        level_sigma=float(np.std(lv, ddof=1)) if lv.size > 1 else np.nan,
        occ=float(np.mean(lv >= met['band_floor'] + min_snr)),
        span=(lo, hi),
    )


def narrow_components(data, tag, key, freqs):
    """Local-baseline metrics and a per-pass excess series for each component."""
    b = BY_KEY[key]
    m = band_stats(data[f'{tag}_{key}'], b)
    f = m['f']
    out = []
    series = {}
    for f0 in freqs:
        lsel = (f >= f0 - 0.6) & (f <= f0 + 0.6) & m['ok']
        if lsel.sum() < 10:
            continue
        ff, yy = f[lsel], m['med'][lsel]
        lok = m['ok'][lsel]
        base = float(np.nanmedian(np.r_[yy[ff < f0 - 0.15],
                                        yy[ff > f0 + 0.15]]))
        core = (ff >= f0 - 0.13) & (ff <= f0 + 0.13) & np.isfinite(yy)
        if not core.any():
            continue
        pi = int(np.where(core)[0][int(np.nanargmax(yy[core]))])
        pf, pk = float(ff[pi]), float(yy[pi])
        widths = {}
        for d in (3.0, 10.0, 20.0):
            rs = runs(np.isfinite(yy) & (yy >= pk - d))
            if not rs:
                widths[d] = None
                continue
            a, c = pick_run(rs, pi)
            cl, cr = run_clipped(ff, lok, a, c, len(ff), b['res'])
            widths[d] = dict(width=float(ff[c] - ff[a]),
                             clipped_l=cl, clipped_r=cr)
        j = int(np.argmin(np.abs(f - pf)))
        lev, exc = [], []
        for row in data[f'{tag}_{key}']:
            v = row[j]
            if not np.isfinite(v):
                continue
            rs = (f >= f0 - 0.6) & (f <= f0 + 0.6) & np.isfinite(row)
            if rs.sum() < 10:
                continue
            lev.append(float(v))
            exc.append(float(v - np.median(row[rs])))
        lev, exc = np.asarray(lev), np.asarray(exc)
        series[f'{f0:.2f}'] = exc
        out.append(dict(
            f0=f0, peak_f=pf, level=pk, above=pk - base, base=base,
            w3=widths[3.0], w10=widths[10.0], w20=widths[20.0],
            n=int(lev.size),
            level_sigma=float(np.std(lev, ddof=1)) if lev.size > 1 else np.nan,
            exc_sigma=float(np.std(exc, ddof=1)) if exc.size > 1 else np.nan,
            persist=float(np.mean(exc > 6.0)) if exc.size else np.nan,
            exc=exc,
        ))
    keys = list(series)
    corr = None
    if len(keys) > 1:
        n = len(keys)
        corr = np.full((n, n), np.nan)
        for i, a in enumerate(keys):
            for j, c in enumerate(keys):
                x, y = series[a], series[c]
                good = np.isfinite(x) & np.isfinite(y)
                if good.sum() > 3:
                    corr[i, j] = float(np.corrcoef(x[good], y[good])[0, 1])
    return out, keys, corr


CONTROLS = [
    ('b28', 780.0, 790.0, '700 quiet upper', 'off'),
    ('lte2600', 2621.0, 2639.0, '2600 noise floor', 'on'),
    ('lte2600', 2642.0, 2660.0, '2600 noise floor 2', 'off'),
    ('lte2100', 2130.0, 2169.0, '2100 upper quiet', 'off'),
    ('mhz1800', 1806.3, 1811.0, '1800 lower quiet', 'off'),
    ('gsm900', 940.5, 945.0, '900 inter-block gap', 'off'),
    ('gsm900', 940.5, 945.0, '900 inter-block gap', 'on'),
]

NARROW = {
    'gsm900': [945.39, 946.63, 947.81, 949.81, 926.91],
    'mhz1800': [1840.89],
}


def analyse(data):
    out = []
    for win in WINDOWS:
        b = BY_KEY[win['key']]
        tag = 'off' if win['variant'] == 'off' else 'on'
        mat = data[f'{tag}_{b["key"]}']
        m = band_stats(mat, b)
        met = block_metrics(b, m, win)
        if met is None:
            continue
        ras = spatial_periodogram(met['f'], met['med'], met['ok'],
                                  met['span'][0], met['span'][1])
        f45 = fit_component(mat, m, met['span'][0], met['span'][1], 45.0)
        fpk = (fit_component(mat, m, met['span'][0], met['span'][1],
                             ras['period_khz']) if ras else None)
        tr = pass_tracks(mat, m, met)
        out.append(dict(win=win, tag=tag, b=b, m=m, met=met, ras=ras,
                        f45=f45, fpk=fpk, tracks=tr))
    return out


def analyse_controls(data):
    out = []
    for key, lo, hi, label, tag in CONTROLS:
        b = BY_KEY[key]
        m = band_stats(data[f'{tag}_{key}'], b)
        ras = spatial_periodogram(m['f'], m['med'], m['ok'], lo, hi, n_null=60)
        if ras is None:
            continue
        f45 = fit_component(data[f'{tag}_{key}'], m, lo, hi, 45.0)
        fpk = fit_component(data[f'{tag}_{key}'], m, lo, hi,
                            ras['period_khz'])
        out.append(dict(key=key, tag=tag, label=label, span=(lo, hi),
                        ras=ras, f45=f45, fpk=fpk))
    return out


def fmt_num(x, nd=2, dash='—'):
    return dash if x is None or not np.isfinite(x) else f'{x:.{nd}f}'


def print_summary(blocks, controls, narrow):
    print('\nBLOCK MORPHOLOGY (plateau-referenced, relative dB only)')
    for r in blocks:
        met, w = r['met'], r['win']
        def wf(x):
            if not x:
                return '—'
            tag = '≥' if (x['clipped_l'] or x['clipped_r']) else ''
            return f"{tag}{x['width']:.2f}[{x['lo']:.2f},{x['hi']:.2f}]"
        print(f"{w['label']:10s} {r['tag']:3s} win {w['lo']:.2f}-{w['hi']:.2f} "
              f"plateau {met['plateau']:.1f} snr {met['snr']:.1f} "
              f"pk+{met['peak_excess']:.1f} OBW {fmt_num(met['obw99'])} "
              f"w3 {wf(met['widths'][3.0])} w10 {wf(met['widths'][10.0])} "
              f"w20 {wf(met['widths'][20.0])} rip {fmt_num(met['ripple'])} "
              f"p95p5 {fmt_num(met['ripple_p95p5'])} flat {fmt_num(met['flatness'],3)} "
              f"shr L/R {fmt_num(met['shoulders'].get('left'))}/"
              f"{fmt_num(met['shoulders'].get('right'))} "
              f"rel {met['reliable']}")
        for t in met['troughs'][:2]:
            print(f"    trough {t['lo']:.2f}-{t['hi']:.2f} depth {t['depth']:.1f}")
        if r['ras']:
            ra = r['ras']
            print(f"    raster peak {ra['period_khz']:.2f} kHz "
                  f"contrast {ra['peak_contrast']:.1f} dB | 45k "
                  f"{ra['contrasts'][45.0]:.1f}/{ra['null95'][45.0]:.1f} "
                  f"180k {ra['contrasts'][180.0]:.1f}/{ra['null95'][180.0]:.1f} "
                  f"200k {ra['contrasts'][200.0]:.1f}/{ra['null95'][200.0]:.1f}")
        for label, fit in (('45k', r['f45']), ('peak', r['fpk'])):
            if fit:
                print(f"    fit {label:4s} T {fit['period']:.2f} pp {fit['pp']:.2f} dB "
                      f"R {fit['R']:.2f} N {fit['n']} per-pass T "
                      f"{fit['period_med']:.2f}±{fit['period_sd']:.2f}")
        tr = r['tracks']
        if tr:
            print(f"    tracks centroid {tr['centroid_med']:.2f}±"
                  f"{tr['centroid_std']:.2f} MHz level sigma "
                  f"{tr['level_sigma']:.2f} dB occ {100*tr['occ']:.0f}% "
                  f"N {tr['n']}")

    print('\nCONTROLS')
    for c in controls:
        ra, f45, fpk = c['ras'], c['f45'], c['fpk']
        print(f"{c['label']:20s} {c['tag']:3s} {c['span'][0]:.2f}-{c['span'][1]:.2f} "
              f"peak {ra['period_khz']:.2f} ({ra['peak_contrast']:.1f} dB) "
              f"45k contrast {ra['contrasts'][45.0]:.1f} "
              f"pp45 {f45['pp']:.2f} R {f45['R']:.2f} | "
              f"pppk {fpk['pp']:.2f} R {fpk['R']:.2f} T {fpk['period']:.2f}")

    print('\nNARROW COMPONENTS')
    for key, comps, keys, corr in narrow:
        print(key)
        for c in comps:
            print(f"  {c['f0']:.2f} peak {c['peak_f']:.2f} {c['level']:.1f} "
                  f"above {c['above']:.1f} w3 {c['w3']['width']:.2f} "
                  f"w10 {c['w10']['width']:.2f} pers {100*c['persist']:.0f}% "
                  f"exc sigma {c['exc_sigma']:.2f} dB N {c['n']}")
        if corr is not None:
            print('  corr:')
            print('        ' + ' '.join(f'{k:>7s}' for k in keys))
            for i, k in enumerate(keys):
                print(f'  {k:>7s} ' + ' '.join(f'{corr[i,j]:7.2f}'
                                               for j in range(len(keys))))


def _bracket(ax, lo, hi, y, color, text, clipped_l=False, clipped_r=False):
    ax.plot([lo, hi], [y, y], color=color, lw=2.4, solid_capstyle='butt',
            alpha=.95, zorder=6)
    for x in (lo, hi):
        ax.plot([x, x], [y - .45, y + .45], color=color, lw=1.4, zorder=6)
    if clipped_l:
        ax.annotate('', xy=(lo - .45, y), xytext=(lo, y),
                    arrowprops=dict(arrowstyle='->', color=color, lw=1.0,
                                    ls=(0, (2, 1.4))), zorder=6)
    if clipped_r:
        ax.annotate('', xy=(hi + .45, y), xytext=(hi, y),
                    arrowprops=dict(arrowstyle='->', color=color, lw=1.0,
                                    ls=(0, (2, 1.4))), zorder=6)
    ax.text((lo + hi) / 2, y + .55, text, ha='center', va='bottom',
            color=color, fontsize=6.0, zorder=7,
            bbox=dict(facecolor=BG, edgecolor='none', alpha=.65, pad=1.0))


def _band_panel(ax, data, key, tag, lo, hi, title, highlights=(),
                marks=(), recs=(), note_text=None, ycap=None,
                brackets=True, note_loc='lower right'):
    b = BY_KEY[key]
    m = band_stats(data[f'{tag}_{key}'], b)
    other_tag = 'off' if tag == 'on' else 'on'
    mo = band_stats(data[f'{other_tag}_{key}'], b)
    f = m['f']
    floor = m['floor']
    sel = (f >= lo) & (f <= hi)
    x = f[sel]
    y = m['med'][sel] - floor
    p25 = m['p25'][sel] - floor
    p75 = m['p75'][sel] - floor
    if recs:
        yb = 0.0
    else:
        yb = 0.0
    data_top = max(float(np.nanmax(y)),
                   float(np.nanpercentile(y, 99.7)) + 2.0, 8.0)
    ymin = min(-2.0, float(np.nanpercentile(y, 2)))
    ymax = data_top + 6.0 + 2.6 * max(0, len(recs) - 1)
    if ycap is not None:
        ymax = min(ymax, ycap)
    capped = float(np.nanmax(y)) > ymax - 0.6
    yb = ymax - 3.0 - 2.6 * max(0, len(recs) - 1)
    yd = np.minimum(y, ymax - .45)
    p25d = np.minimum(p25, ymax - .45)
    p75d = np.minimum(p75, ymax - .45)
    rc.style(ax, title=title, xlabel='frequency (MHz)',
             ylabel='dB rel. band 10th pct')
    bad = (~m['ok']) & (f >= lo) & (f <= hi)
    for a, c in runs(bad):
        ax.axvspan(f[a], f[c], color='#64748b', alpha=.18, lw=0, zorder=0)
    so = (f >= lo) & (f <= hi)
    ax.plot(f[so], np.minimum(mo['med'][so] - floor, ymax - .45),
            color=OFF if tag == 'on' else ON, lw=.60, alpha=.40, zorder=2,
            label=f'{other_tag.upper()} median')
    ax.fill_between(x, p25d, p75d, color=ON if tag == 'on' else OFF,
                    alpha=.13, lw=0, zorder=1)
    ax.plot(x, yd, color=ON if tag == 'on' else OFF, lw=1.15, zorder=3,
            label=f'{tag.upper()} median')
    ax.set_xlim(lo, hi)
    ax.set_ylim(ymin, ymax)
    for k, r in enumerate(recs):
        met = r['met']
        ax.plot([met['w10']['lo'] if met['w10'] else met['span'][0],
                 met['w10']['hi'] if met['w10'] else met['span'][1]],
                [met['plateau'] - floor] * 2, color=GREEN, lw=.8,
                ls=(0, (5, 3)), alpha=.75, zorder=4)
        if not brackets:
            continue
        yy = yb + 2.6 * k
        w3, w10 = met['widths'][3.0], met['widths'][10.0]
        if w3:
            txt = f"−3 dB {w3['width']:.2f}"
            if w3['clipped_l'] or w3['clipped_r']:
                txt = '≥ ' + txt
            _bracket(ax, w3['lo'], w3['hi'], yy, GREEN, txt,
                     w3['clipped_l'], w3['clipped_r'])
        if w10:
            txt = f"−10 dB {w10['width']:.2f}"
            if w10['clipped_l'] or w10['clipped_r']:
                txt = '≥ ' + txt
            _bracket(ax, w10['lo'], w10['hi'], yy + 1.55, GOLD, txt,
                     w10['clipped_l'], w10['clipped_r'])
    for a, c in highlights:
        ytop = ax.get_ylim()[1]
        ax.plot([a, c], [ytop - 1.0, ytop - 1.0], color=GREEN, lw=3.2,
                solid_capstyle='butt', alpha=.8, clip_on=False, zorder=8)
    for f0, lab in marks:
        ax.axvline(f0, color=GOLD, lw=.7, ls=':', alpha=.7, zorder=4)
        ax.plot(f0, ax.get_ylim()[1] - 0.9, marker='v', color=GOLD, ms=5.0,
                zorder=8, clip_on=False)
        if lab:
            ax.text(f0, ax.get_ylim()[1] - 1.3, lab, color=GOLD, fontsize=6.0,
                    ha='center', va='top', rotation=90, alpha=.95, zorder=9,
                    bbox=dict(facecolor=BG, edgecolor='none', alpha=.55,
                              pad=0.8))
    if note_text:
        if capped:
            note_text += f'\npeaks clipped at {ymax:.0f} dB for display'
        rc.note(ax, note_text, loc=note_loc, size=6.2)
    return m


def figure_morph(data, blocks, controls, meta):
    by = {r['win']['label']: r for r in blocks}
    fig = plt.figure(figsize=(16.2, 11.4), facecolor=BG)
    gs = GridSpec(3, 3, figure=fig, hspace=.34, wspace=.20,
                  left=.045, right=.985, top=.885, bottom=.035,
                  height_ratios=[1.0, 1.0, .52])
    fig.text(.045, .975, 'Cellular downlink — low-level spectral morphology',
             color=FG, fontsize=17, fontweight='bold', va='top')
    fig.text(.045, .951,
             '10 kHz resampled power spectra, plateau-referenced widths, '
             'spectral raster and pass-scale tracks. Relative and uncalibrated; '
             'no dBm, no demodulation, no cell identity.',
             color=MUTED, fontsize=8.4, va='top')
    fig.text(.045, .925,
             'ON/OFF are sequential runs with different tuning plans: overlays '
             'show consistency, not a simultaneous or causal comparison.',
             color=GOLD, fontsize=7.6, va='top')

    ax = fig.add_subplot(gs[0, 0])
    _band_panel(
        ax, data, 'b28', 'off', 758, 791,
        '700 MHz nominal B28 — OFF run, block B',
        highlights=[(768.25, 777.75)], recs=[by['700']], ycap=40,
        note_text='second block 761.5–765.3 MHz\n'
                  'not analysed; B28 nominal\nupper edge 803 MHz is nominal')
    ax = fig.add_subplot(gs[0, 1])
    _band_panel(
        ax, data, 'lte800', 'on', 791, 821,
        'LTE 800 nominal — two adjacent spans',
        highlights=[(801.30, 810.50), (811.30, 820.50)],
        recs=[by['LTE800-A'], by['LTE800-B']], ycap=36,
        note_text='both spans ~9.0 dB above local\nplateau-10 dB body; edges at\n'
                  'the 6.8 kHz native resolution')
    ax = fig.add_subplot(gs[0, 2])
    _band_panel(
        ax, data, 'gsm900', 'on', 925, 960,
        '900 MHz nominal — two extents, DC mask',
        highlights=[(930.55, 935.80), (950.40, 959.45)],
        marks=[(926.91, '926.91'), (945.39, '945.4'), (946.63, '946.6'),
               (947.81, '947.8'), (949.81, '949.8')],
        recs=[by['900-930'], by['900-954']], ycap=38, brackets=False,
        note_loc='lower left',
        note_text='935.87–938.86 MHz is the DC mask.\n'
                  '−3 dB widths: 4.67 (left sub-span, clipped)\n'
                  'and 8.99 (right shallow extent, 46% occupancy).')
    ax = fig.add_subplot(gs[1, 0])
    _band_panel(
        ax, data, 'mhz1800', 'on', 1805, 1880,
        '1800 MHz nominal — four spans and two masks',
        highlights=[(1811.05, 1820.15), (1820.95, 1831.15),
                    (1840.75, 1859.05), (1862.22, 1876.20)],
        marks=[(1840.89, '')],
        recs=[by['1800-1840']], ycap=34,
        note_text='DC masks at 1831.2–1834.2 and\n1859.2–1862.2 MHz. The 1840.89 MHz\n'
                  'component is +15 dB above the block plateau')
    ax = fig.add_subplot(gs[1, 1])
    _band_panel(
        ax, data, 'lte2100', 'off', 2110, 2135,
        'LTE 2100 nominal — OFF run',
        highlights=[(2110.95, 2129.05)], recs=[by['LTE2100']], ycap=27,
        note_text='−3 dB width 15.4 MHz vs −10 dB 18.0 MHz:\n'
                  'a ~2.5 MHz upper shelf. ON misses the lower\n'
                  '3 MHz behind the 2111.0–2114.0 MHz DC mask')
    ax = fig.add_subplot(gs[1, 2])
    _band_panel(
        ax, data, 'lte2600', 'on', 2620, 2690,
        'LTE 2600 nominal — no threshold block',
        highlights=[(2621.0, 2639.0), (2646.0, 2660.0)], ycap=10,
        note_text='no cell above floor+6 dB in the survey.\n'
                  'The highlighted quiet regions are the\n'
                  'raster control, not a detected signal')
    for i, (head, body, col) in enumerate([
        ('Reading the morphology',
         'Widths are measured on a 150 kHz rolling-median spectrum relative to '
         'the in-window 85th percentile (plateau), not to the global peak. '
         'A narrow edge spike or an interior fade therefore does not define '
         'the block. “≥” means the run reaches a DC mask, a coverage gap or '
         'the padded window: the width is a lower bound. OBW99 removes a local '
         '10th-percentile baseline; it is a display reference, not a correction.',
         GREEN),
        ('Spectral raster',
         'Every strong block shows a phase-stable ripple near 45 kHz '
         '(1.7–9.6 dB peak-to-peak at the fixed 45 kHz period). 45 kHz is '
         'three 15 kHz subcarriers. '
         '180 kHz and 200 kHz show no consistent contrast. The ripple is '
         'consistent with regularly spaced reference subcarriers in an OFDM '
         'downlink; the power-only data cannot identify LTE, a numerology, a '
         'reference signal or a transmitter.',
         GOLD),
        ('What cannot be concluded',
         'No IQ or phase is retained, so there is no demodulation, EVM, '
         'constellation, symbol rate or modulation order, and no cell identity. '
         '5.12 ms per record is shorter than a 10 ms LTE frame and averages '
         '~9 GSM bursts. '
         'Pass revisits are ~12.5 s ON / ~33 s OFF, so only pass-scale '
         'dynamics are visible. Nominal band labels are not ARFCNs or '
         'operators.',
         RED),
    ]):
        cax = fig.add_subplot(gs[2, i])
        rc.card_frame(cax, col)
        cax.text(.045, .90, head, transform=cax.transAxes, color=col,
                 fontsize=9.2, fontweight='bold', va='top')
        cax.text(.045, .76, textwrap.fill(body, 62), transform=cax.transAxes,
                 color=FG, fontsize=6.9, va='top', linespacing=1.45)
    return fig


RASTER_KEYS = ['700', 'LTE800-A', '900-930', '1800-1840', 'LTE2100']


def _median_fit(met, period_khz):
    s = _spectrum_seg(met['f'], met['med'], met['ok'],
                      met['span'][0], met['span'][1])
    if s is None:
        return None
    ff, z, win = s['f'], s['z'], s['win']
    fq = 1000.0 / period_khz
    C = np.sum(z * win * np.exp(-2j * np.pi * fq * ff))
    amp = 2.0 * abs(C) / np.sum(win)
    fit = amp * np.cos(2 * np.pi * fq * ff + np.angle(C))
    return dict(f=ff, z=z, fit=fit, amp=amp, pp=2 * amp, span=s['span'])


def _raster_example(ax_top, ax_bot, r, title, fit, control=False):
    met = r['met']
    s = _median_fit(met, fit['period'])
    if s is None:
        return
    ax_top.set_facecolor(PANEL)
    zs = rc.rollmed(s['z'], 3)
    ax_top.plot(s['f'], zs, color=MUTED, lw=.62, alpha=.72,
                label='detrended median')
    ax_top.plot(s['f'], s['fit'], color=GOLD if control else GREEN, lw=1.25,
                alpha=.95, label=f"fit {fit['period']:.2f} kHz")
    ax_top.axhline(0, color=GRID, lw=.6, alpha=.8)
    rc.style(ax_top, title=title, xlabel=None,
             ylabel='dB rel. 1 MHz median')
    ax_top.set_xlim(s['span'])
    ax_top.set_ylim(-10, 10)
    ax_top.text(.025, .955,
                f"T {fit['period']:.2f} kHz\n"
                f"pp med/per-pass {s['pp']:.2f}/{fit['pp']:.2f} dB\n"
                f"R {fit['R']:.2f}; N {fit['n']}",
                transform=ax_top.transAxes, va='top', ha='left',
                color=FG, fontsize=6.3, linespacing=1.35,
                bbox=dict(facecolor=BG, edgecolor=GRID, alpha=.75, pad=2.4))
    # A short zoom makes the fitted sinusoid visible: over a full 9-15 MHz
    # span each 45 kHz cycle is sub-pixel at print resolution.
    c0 = 0.5 * (s['span'][0] + s['span'][1]) - 0.36
    zi = (s['f'] >= c0) & (s['f'] <= c0 + 0.72)
    if zi.sum() > 8:
        axin = ax_top.inset_axes([.605, .095, .365, .285])
        axin.set_facecolor(BG)
        axin.plot(s['f'][zi], zs[zi], color=MUTED, lw=.65, alpha=.95)
        axin.plot(s['f'][zi], s['fit'][zi], color=GOLD if control else GREEN,
                  lw=1.35, alpha=1.0)
        axin.axhline(0, color=GRID, lw=.5)
        axin.set_xlim(c0, c0 + 0.72)
        li = max(1.5, float(np.nanpercentile(np.abs(zs[zi]), 99)) * 1.25)
        axin.set_ylim(-li, li)
        axin.set_xticks([])
        axin.set_yticks([])
        axin.text(.05, .92, '0.72 MHz zoom', transform=axin.transAxes,
                  color=MUTED, fontsize=4.6, va='top')
        for sp in axin.spines.values():
            sp.set_color(GRID)

    ra = r['ras']
    per = 1000.0 / ra['freq']
    order = np.argsort(per)
    per, P = per[order], ra['power'][order]
    yn = 10.0 * np.log10(P / np.median(P))
    ax_bot.set_facecolor(PANEL)
    ax_bot.plot(per, np.maximum(yn, -4.0), color=GOLD if control else ON,
                lw=.95, alpha=.95)
    ax_bot.set_xscale('log')
    ax_bot.set_xlim(25, 400)
    ax_bot.set_ylim(-4, max(34, float(np.nanpercentile(yn, 99.5)) + 3))
    for sp in (45, 60, 180, 200):
        ax_bot.axvline(sp, color=GREEN if sp in (45, 60) else MUTED,
                       lw=.65, ls=':', alpha=.75)
    ax_bot.axvline(ra['period_khz'], color=RED, lw=.9, alpha=.7)
    rc.style(ax_bot, title=None, xlabel='spatial period (kHz)',
             ylabel='dB rel. median')
    ax_bot.text(.03, .94,
                f"peak {ra['period_khz']:.2f} kHz, {ra['peak_contrast']:.1f} dB\n"
                f"45k null95 {ra['null95'][45.0]:.1f} dB\n"
                f"marks 45/60 green; 180/200 gray",
                transform=ax_bot.transAxes, va='top', ha='left',
                color=FG, fontsize=5.9, linespacing=1.25,
                bbox=dict(facecolor=BG, edgecolor=GRID, alpha=.72, pad=2.0))


def figure_raster(data, blocks, controls, meta):
    by = {r['win']['label']: r for r in blocks}
    ctrl = {(c['label'], c['tag']): c for c in controls}
    fig = plt.figure(figsize=(16.2, 11.0), facecolor=BG)
    gs = GridSpec(3, 6, figure=fig, hspace=.46, wspace=.32,
                  left=.045, right=.985, top=.885, bottom=.055,
                  height_ratios=[1.0, 1.0, 1.05])
    fig.text(.045, .975, 'Spectral raster — periodicity, phase and controls',
             color=FG, fontsize=17, fontweight='bold', va='top')
    fig.text(.045, .951,
             'Each top panel is a 1 MHz-median-detrended log-spectrum; the '
             'overlay is a least-squares sinusoid in dB. The lower panel is its '
             'spatial periodogram. Phase coherence R is across passes at the '
             'fitted period. Relative, uncalibrated, spectral-only.',
             color=MUTED, fontsize=8.2, va='top')
    fig.text(.045, .930,
             'A fixed raster can be transmitter reference structure or a '
             'receiver/search artefact. The quiet-control amplitudes below are '
             'the discriminator; no source is identified.',
             color=GOLD, fontsize=7.4, va='top')

    top_keys = [('700', '700 MHz block'),
                ('LTE800-A', 'LTE800-A'),
                ('900-930', '900-930'),
                ('1800-1840', '1800-1840'),
                ('LTE2100', 'LTE2100')]
    for col, (label, title) in enumerate(top_keys):
        r = by[label]
        fit = r['fpk']
        ax_top = fig.add_subplot(gs[0, col])
        ax_bot = fig.add_subplot(gs[1, col])
        _raster_example(ax_top, ax_bot, r, title, fit)
    # sixth column: quiet control
    c = ctrl[('2600 noise floor', 'on')]
    ax_top = fig.add_subplot(gs[0, 5])
    ax_bot = fig.add_subplot(gs[1, 5])
    # build a minimal record for the control
    b = BY_KEY['lte2600']
    m = band_stats(data['on_lte2600'], b)
    met = dict(f=m['f'], med=m['med'], ok=m['ok'], span=c['span'])
    fake = dict(met=met, ras=c['ras'])
    _raster_example(ax_top, ax_bot, fake, '2600 quiet control',
                    c['fpk'], control=True)

    # Bottom-left: 45 kHz peak-to-peak amplitude, blocks vs controls.
    ax = fig.add_subplot(gs[2, 0:2])
    labels, vals, cols = [], [], []
    for r in blocks:
        if not r['f45']:
            continue
        labels.append(r['win']['label'])
        vals.append(r['f45']['pp'])
        cols.append(GREEN if r['f45']['R'] >= .7 else GOLD)
    ctrl_rows = []
    for c in controls:
        lab = c['label'] + ' ' + c['tag'].upper()
        if c['label'] == '900 inter-block gap':
            ctrl_rows.append((lab, c['f45']['pp'], GOLD))
        elif c['label'] in ('700 quiet upper', '2600 noise floor',
                            '2600 noise floor 2', '2100 upper quiet',
                            '1800 lower quiet'):
            ctrl_rows.append((lab, c['f45']['pp'], MUTED))
    # keep the five quiet controls plus the inter-block gap
    quiet = [x for x in ctrl_rows if x[2] == MUTED]
    gap = [x for x in ctrl_rows if x[2] == GOLD]
    all_lab = labels + [x[0] for x in quiet] + [x[0] for x in gap]
    all_val = vals + [x[1] for x in quiet] + [x[1] for x in gap]
    all_col = cols + [x[2] for x in quiet] + [x[2] for x in gap]
    ypos = np.arange(len(all_lab))
    ax.barh(ypos, np.maximum(all_val, 1e-3), color=all_col, alpha=.85,
            height=.72)
    for i, v in enumerate(all_val):
        ax.text(max(v, 1e-3) * 1.12, i, f'{v:.2f}', color=FG, fontsize=7.4,
                va='center', fontweight='bold')
    ax.set_yticks(ypos)
    ax.set_yticklabels(all_lab, fontsize=5.9, color=MUTED)
    ax.tick_params(axis='y', length=2)
    ax.invert_yaxis()
    ax.set_xscale('log')
    ax.set_xlim(1e-2, 20)
    qmax = max(x[1] for x in quiet)
    gmax = max((x[1] for x in gap), default=np.nan)
    ttl = f'per-pass 45 kHz pp: quiet max {qmax:.2f} dB'
    if np.isfinite(gmax):
        ttl += f'; 900 gap ON {gmax:.2f}'
    rc.style(ax, title=ttl, xlabel='dB peak-to-peak (log scale)')
    ax.axvline(qmax, color=MUTED, lw=.7, ls='--', alpha=.8)
    if gap:
        ax.axvline(gap[0][1], color=GOLD, lw=.7, ls=':', alpha=.8)

    # Bottom-middle: per-pass phase vectors at the fitted period.
    ax = fig.add_subplot(gs[2, 2:4])
    ax.set_facecolor(PANEL)
    ax.set_aspect('equal')
    th = np.linspace(0, 2 * np.pi, 200)
    ax.plot(np.cos(th), np.sin(th), color=GRID, lw=.8)
    ax.axhline(0, color=GRID, lw=.5)
    ax.axvline(0, color=GRID, lw=.5)
    for label, col in (('LTE800-A', ON), ('900-930', GREEN),
                       ('1800-1840', PURPLE)):
        r = by[label]
        ph = r['fpk']['phases']
        for p in ph:
            ax.plot([0, np.cos(p)], [0, np.sin(p)], color=col, lw=.55,
                    alpha=.30)
        R = r['fpk']['R']
        mp = np.angle(np.mean(np.exp(1j * ph)))
        ax.annotate('', xy=(R * np.cos(mp), R * np.sin(mp)), xytext=(0, 0),
                    arrowprops=dict(arrowstyle='-|>', color=col, lw=2.2,
                                    shrinkA=0, shrinkB=0))
        ax.text(1.06, .52 - .22 * list(('LTE800-A', '900-930',
                                        '1800-1840')).index(label),
                f'{label}  R {R:.2f}', color=col, fontsize=6.6, ha='left')
    ax.set_aspect('equal', adjustable='datalim')
    ax.set_xlim(-1.15, 1.70)
    ax.set_ylim(-1.30, 1.30)
    ax.set_anchor('C')
    ax.axis('off')
    ax.set_title('per-pass phase at the fitted period', color=FG,
                 fontsize=9.2, loc='left', pad=2)

    # Bottom-right: per-pass fitted period.
    ax = fig.add_subplot(gs[2, 4:6])
    rows = [r for r in blocks if r['fpk']
            and np.isfinite(r['fpk']['period_med'])
            and 44.0 < r['fpk']['period_med'] < 46.5]
    ypos = np.arange(len(rows))
    med = np.array([r['fpk']['period_med'] for r in rows])
    sd = np.array([r['fpk']['period_sd'] for r in rows])
    ax.errorbar(med, ypos, xerr=sd, fmt='o', color=ON, ecolor=MUTED,
                elinewidth=.9, capsize=2.2, ms=4.2)
    ax.axvline(45.0, color=GOLD, lw=.8, ls='--', alpha=.8)
    ax.set_yticks(ypos)
    ax.set_yticklabels([r['win']['label'] for r in rows], fontsize=6.4,
                       color=MUTED)
    ax.invert_yaxis()
    ax.set_xlim(44.85, 45.35)
    rc.style(ax, title='per-pass fitted period (median ± sd)',
             xlabel='period (kHz); dashed 45.00 = 3 × 15 kHz')
    ax.grid(False)
    ax.grid(axis='x', color=GRID, lw=.55, alpha=.55)

    return fig


DYN_COLORS = {
    'LTE800-A': '#4ade80',
    'LTE800-B': '#16a34a',
    '900-930': '#facc15',
    '900-954': '#fb923c',
    '1800-1811': '#c084fc',
    '1800-1820': '#a78bfa',
    '1800-1840': '#f472b6',
    '1800-1862': '#e879f9',
}


def figure_dynamics(data, blocks, narrow, meta):
    on = [r for r in blocks if r['tag'] == 'on' and r['tracks']]
    off = [r for r in blocks if r['tag'] == 'off' and r['tracks']]

    fig = plt.figure(figsize=(16.2, 12.6), facecolor=BG)
    gs = GridSpec(4, 6, figure=fig, hspace=.43, wspace=.36,
                  left=.050, right=.985, top=.872, bottom=.033,
                  height_ratios=[1.05, 1.0, 1.0, .55])
    fig.text(.045, .975,
             'Pass-scale dynamics — levels, centroids and narrow components',
             color=FG, fontsize=17, fontweight='bold', va='top')
    fig.text(.045, .951,
             f'{meta["on_passes"]} ON passes / {meta["off_passes"]} OFF passes. '
             'Each point is one 5.12 ms power-spectrum summary; offsets are '
             'relative to each span’s own median. ON and OFF are sequential '
             'runs and are never subtracted.',
             color=MUTED, fontsize=8.2, va='top')
    fig.text(.045, .929,
             'Level σ mixes real fading with tuning-plan and route differences. '
             'A 5.12 ms record is shorter than an LTE frame and averages ~9 GSM '
             'bursts, so only pass-scale behaviour is observable.',
             color=GOLD, fontsize=7.4, va='top')

    # --- ON body level -----------------------------------------------------
    ax = fig.add_subplot(gs[0, 0:3])
    for r in on:
        tr = r['tracks']
        x = np.arange(1, tr['n'] + 1)
        ax.plot(x, tr['level'] - tr['level_med'],
                color=DYN_COLORS.get(r['win']['label'], MUTED), lw=.95,
                marker='o', ms=1.9, alpha=.92,
                label=f"{r['win']['label']}  σ {tr['level_sigma']:.2f}")
    ax.axhline(0, color=GRID, lw=.7)
    ax.set_xlim(.5, 24.5)
    ax.set_ylim(-7.0, 13.0)
    rc.style(ax, title='ON run — body level per pass',
             xlabel='ON pass index (acquisition order)',
             ylabel='dB rel. span median')
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc='upper left', bbox_to_anchor=(.050, .900),
               ncol=8, fontsize=5.9, frameon=False, labelcolor=FG,
               handlelength=1.4, columnspacing=1.0, handletextpad=.45)

    # --- ON centroid -------------------------------------------------------
    ax = fig.add_subplot(gs[0, 3:6])
    for r in on:
        tr = r['tracks']
        x = np.arange(1, tr['n'] + 1)
        ax.plot(x, tr['centroid'] - tr['centroid_med'],
                color=DYN_COLORS.get(r['win']['label'], MUTED), lw=.90,
                marker='o', ms=1.9, alpha=.92)
    ax.axhline(0, color=GRID, lw=.7)
    ax.set_xlim(.5, 24.5)
    ax.set_ylim(-5.0, 5.0)
    rc.style(ax, title='ON run — power-centroid offset per pass',
             xlabel='ON pass index (acquisition order)',
             ylabel='MHz from span median')
    rc.note(ax, 'centroid is linear-power weighted above the local baseline;\n'
                '1800–1840 includes the 1840.89 MHz edge component',
            loc='lower left', size=5.5)

    # --- OFF run -----------------------------------------------------------
    sub = GridSpecFromSubplotSpec(2, 1, subplot_spec=gs[1, 0:3],
                                  hspace=.14)
    axl = fig.add_subplot(sub[0])
    axc = fig.add_subplot(sub[1])
    off_col = {'700': GOLD, 'LTE2100': '#60a5fa'}
    for r in off:
        tr = r['tracks']
        x = np.arange(1, tr['n'] + 1)
        col = off_col.get(r['win']['label'], MUTED)
        axl.plot(x, tr['level'] - tr['level_med'], color=col, lw=1.0,
                 marker='o', ms=2.6, alpha=.95,
                 label=f"{r['win']['label']} (OFF, N {tr['n']})")
        axc.plot(x, tr['centroid'] - tr['centroid_med'], color=col, lw=1.0,
                 marker='o', ms=2.6, alpha=.95)
    r700 = next(r for r in off if r['win']['label'] == '700')
    t700 = r700['tracks']
    y700 = t700['level'] - t700['level_med']
    i700 = int(np.argmin(y700))
    axl.annotate(f'{y700[i700]:+.1f} dB', xy=(i700 + 1, y700[i700]),
                 xytext=(i700 + 1.8, y700[i700] + 2.6), color=GOLD,
                 fontsize=5.8,
                 arrowprops=dict(arrowstyle='->', color=GOLD, lw=.7))
    axl.axhline(0, color=GRID, lw=.6)
    axl.set_xlim(.5, 9.5)
    axl.set_ylim(-16.5, 5.5)
    axc.axhline(0, color=GRID, lw=.6)
    axc.set_xlim(.5, 9.5)
    axc.set_ylim(-3.0, 5.0)
    rc.style(axl, title='OFF run — level and centroid', ylabel='dB rel. median')
    axl.legend(loc='lower right', fontsize=5.2, framealpha=.25,
               edgecolor=GRID, labelcolor=FG, handlelength=1.4)
    rc.style(axc, xlabel='OFF pass index (700: 9; LTE2100: 8)',
             ylabel='MHz from median')
    axl.tick_params(labelbottom=False)

    # --- 900 narrow components --------------------------------------------
    ax = fig.add_subplot(gs[1, 3:6])
    _, comps, keys900, corr900 = narrow[0]
    cols = [GOLD, '#fb923c', RED, '#c084fc', '#38bdf8']
    for c, col in zip(comps, cols):
        y = np.asarray(c['exc'])
        y = y - np.median(y)
        x = np.arange(1, y.size + 1)
        ax.plot(x, y, color=col, lw=.90, marker='o', ms=1.8, alpha=.92,
                label=f"{c['f0']:.2f} MHz  +{c['above']:.1f} dB")
    ax.axhline(0, color=GRID, lw=.6)
    ax.set_xlim(.5, 24.5)
    allv = np.concatenate([np.asarray(c['exc']) - np.median(c['exc'])
                           for c in comps])
    ax.set_ylim(min(-11.5, np.nanpercentile(allv, 1) - 1.0),
                max(6.0, np.nanpercentile(allv, 99) + 1.0))
    rc.style(ax, title='900 MHz narrow components — pass-to-pass excess',
             xlabel='ON pass index (acquisition order)',
             ylabel='dB rel. component median')
    ax.legend(loc='lower right', ncol=2, fontsize=4.9, framealpha=.24,
              edgecolor=GRID, labelcolor=FG, borderpad=.4,
              handlelength=1.4, columnspacing=.8, handletextpad=.5)
    ax.text(.025, .955,
            'labels: median-spectrum peak above local baseline\n'
            'persistence ≥ 96 %',
            transform=ax.transAxes, ha='left', va='top', color=MUTED,
            fontsize=5.3, linespacing=1.3,
            bbox=dict(facecolor=BG, edgecolor=GRID, alpha=.72, pad=2.2))

    # --- level sigma and occupancy ----------------------------------------
    ax = fig.add_subplot(gs[2, 0:2])
    rows = [r for r in blocks if r['tracks']]
    labels = [r['win']['label'] for r in rows]
    vals = np.array([r['tracks']['level_sigma'] for r in rows])
    occ = np.array([r['tracks']['occ'] for r in rows])
    ypos = np.arange(len(rows))
    ax.barh(ypos, vals, color=[GREEN if r['tag'] == 'on' else GOLD
                               for r in rows], alpha=.86, height=.70)
    for i, (v, o) in enumerate(zip(vals, occ)):
        ax.text(v + .08, i, f'{v:.2f}   {100*o:.0f}%', color=FG,
                fontsize=6.6, va='center')
    ax.set_yticks(ypos)
    ax.set_yticklabels(labels, fontsize=6.1, color=MUTED)
    ax.invert_yaxis()
    ax.set_xlim(0, 5.8)
    rc.style(ax, title='body-level σ and occupancy',
             xlabel='dB (σ across passes); label: σ and % passes above floor+6')
    ax.grid(False)
    ax.grid(axis='x', color=GRID, lw=.55, alpha=.55)

    # --- 1800 narrow component --------------------------------------------
    ax = fig.add_subplot(gs[2, 2:4])
    _, comps1800, _, _ = narrow[1]
    c = comps1800[0]
    y = np.asarray(c['exc'])
    x = np.arange(1, y.size + 1)
    med = float(np.median(y))
    sig = float(np.std(y, ddof=1))
    ax.fill_between(x, med - sig, med + sig, color=PURPLE, alpha=.13, lw=0)
    ax.plot(x, y, color=PURPLE, lw=1.0, marker='o', ms=2.2, alpha=.95)
    ax.axhline(med, color=GOLD, lw=.8, ls='--', alpha=.85)
    ax.set_xlim(.5, 24.5)
    ax.set_ylim(max(0.0, float(np.min(y)) - 3.0),
                float(np.max(y)) + 3.0)
    rc.style(ax, title='1800 MHz — 1840.89 MHz component',
             xlabel='ON pass index (acquisition order)',
             ylabel='dB above local baseline')
    ax.text(.03, .95,
            f"−3 dB width {c['w3']['width']:.2f} MHz\n"
            f"median-spectrum peak +{c['above']:.1f} dB\n"
            f"per-pass median {med:.1f} ± {sig:.2f} dB (σ)\n"
            f"persistence {100*c['persist']:.0f} %; N {c['n']}",
            transform=ax.transAxes, va='top', ha='left', color=FG,
            fontsize=5.8, linespacing=1.35,
            bbox=dict(facecolor=BG, edgecolor=GRID, alpha=.75, pad=2.3))

    # --- correlation heatmap ----------------------------------------------
    ax = fig.add_subplot(gs[2, 4:6])
    n = len(keys900)
    cmap = LinearSegmentedColormap.from_list('dyn_corr', [RED, CARD, GREEN])
    im = ax.imshow(corr900, cmap=cmap, vmin=-.6, vmax=.6)
    for i in range(n):
        for j in range(n):
            v = corr900[i, j]
            ax.text(j, i, f'{v:.2f}', ha='center', va='center',
                    color=BG if abs(v) > .32 else FG, fontsize=6.0)
    ax.set_xticks(range(n))
    ax.set_xticklabels(keys900, rotation=30, ha='right', fontsize=6.1,
                       color=MUTED)
    ax.set_yticks(range(n))
    ax.set_yticklabels(keys900, fontsize=6.1, color=MUTED)
    ax.grid(False)
    rc.style(ax, title='900 narrow-component excess correlation',
             xlabel='component (MHz); max |r| = 0.44')
    cb = fig.colorbar(im, ax=ax, fraction=.046, pad=.025)
    cb.ax.tick_params(labelsize=5.6, colors=MUTED, length=2)
    cb.outline.set_edgecolor(GRID)

    # --- cards -------------------------------------------------------------
    for i, (head, body, col) in enumerate([
        ('Tracks',
         'Body level is the per-pass median across the plateau−10 dB body; '
         'the centroid is the linear-power weighted mean above the local '
         'baseline. Both are offsets from each span’s own 24-pass (ON) or '
         '9-pass (OFF) median, so no absolute power is implied. Pass index is '
         'acquisition order, not wall time: ON revisits are ~12.5 s apart and '
         'OFF ~33 s apart.',
         GREEN),
        ('Narrow components',
         'Excess is measured against a pass-local 1.2 MHz median. The 1840.89 '
         'MHz component is 20.7 dB above its local baseline with a 0.16 MHz '
         '−3 dB width and 100 % persistence; the 900 MHz components persist in '
         '96–100 % of passes. Correlation is descriptive and is computed only '
         'on the 24 ON passes; it does not establish a common source.',
         PURPLE),
        ('Cannot conclude',
         'Level and centroid changes are not attributed to transmit power, '
         'scheduler, path loss, antenna or route. No IQ or phase is retained, '
         'so there is no demodulation, EVM, constellation, symbol rate or '
         'modulation order, and no cell identity. The 150–160 MHz question '
         'remains unresolved; no ON/OFF pair is simultaneous.',
         RED),
    ]):
        cax = fig.add_subplot(gs[3, i * 2:(i + 1) * 2])
        rc.card_frame(cax, col)
        cax.text(.045, .90, head, transform=cax.transAxes, color=col,
                 fontsize=9.2, fontweight='bold', va='top')
        cax.text(.045, .75, textwrap.fill(body, 74), transform=cax.transAxes,
                 color=FG, fontsize=6.6, va='top', linespacing=1.45)
    return fig


def _num(x, nd=2):
    return '—' if x is None or not np.isfinite(x) else f'{x:.{nd}f}'


def _wstr(w):
    if not w:
        return '—'
    s = f"{w['width']:.2f}"
    if w['clipped_l'] or w['clipped_r']:
        s = '≥' + s
    return s


def _spanstr(w):
    if not w:
        return '—'
    return f"{w['lo']:.2f}–{w['hi']:.2f}"


def write_markdown(path, blocks, controls, narrow, meta, session_name):
    by = {r['win']['label']: r for r in blocks}
    n900 = next(n for n in narrow if n[0] == 'gsm900')
    L = []
    add = L.append

    add('# Cellular downlink: low-level modulation morphology')
    add('')
    add('Relative, uncalibrated spectral analysis of the '
        f'`{session_name}` captures. The input was an antenna on RX2; ON and '
        'OFF are sequential 300 s windows, not simultaneous measurements. All '
        'levels are relative. No dBm, noise figure, absolute floor or '
        'traceability is claimed.')
    add('')
    add('Band and channel names are **nominal allocations** (LTE B20/B8/B3/B1/'
        'B7/B28; GSM 900/1800 downlink). No decoding, cell identity, ARFCN or '
        'operator attribution is attempted. The analysis is spectral-only: no '
        'IQ or phase was retained, so nothing here is a demodulation.')
    add('')
    add('![Cellular low-level modulation morphology](cellular_modulation.png)')
    add('')
    add('![Spectral raster: periodicity, phase and controls]'
        '(cellular_modulation_raster.png)')
    add('')
    add('![Pass-scale dynamics](cellular_modulation_dynamics.png)')
    add('')
    add('**Figure guide.** Figure 1 shows the six morphology panels with the '
        'other run overlaid as a thin line and the measured −3/−10 dB brackets '
        'where the span permits (the 900 MHz panel uses a text note because its '
        'left extent is DC-mask clipped). '
        'Figure 2 shows the detrended median spectrum, its 0.72 MHz zoom and the '
        'spatial periodogram for five blocks plus a quiet control, then the '
        '45 kHz amplitudes against quiet controls, the per-pass phase vectors '
        'and the per-pass fitted period. Figure 3 shows the ON and OFF '
        'pass-scale tracks, the level σ/occupancy summary, the 900 MHz '
        'narrow-component series and correlation matrix, and the 1840.89 MHz '
        'component. Every figure is relative and uncalibrated.')
    add('')
    add('## Scope and data')
    add('')
    add('| | |')
    add('| --- | --- |')
    add(f'| Session | `{session_name}` |')
    add('| Device | B200mini `30B2A32`, antenna `RX2`, 20 dB |')
    add('| Captures | 5,125 ON records / 7,061 OFF records; '
        f'{meta["on_passes"]} ON / {meta["off_passes"]} OFF pass groups |')
    add('| Raw record | 8,192 FFT bins, Hann window, '
        '`bin_width_hz = 6835.9375`, `window_enbw_hz = 10253.906`, '
        '286,720 samples = 5.12 ms at 56 MS/s |')
    add('| Fine grid | 10 kHz, linear-power interpolation from the native bins '
        '(`linear_power_interp`) |')
    add('| Masks | 8 MHz capture edge; ±1.5 MHz DC; cells with < 50 % pass '
        'coverage |')
    add('| Deep dive | LTE 800, 900 MHz, 1800 MHz, LTE 2100, LTE 2600; '
        '700 MHz B28 as a sixth morphology panel |')
    add('')
    add('The fine cache does not add information: it resamples the same '
        '8192-bin power spectra onto a 10 kHz grid. It removes the 60 kHz '
        'resampling beat that nearest-bin assignment produced and makes the '
        '45 kHz raster and the narrow-component widths legible. The 150 kHz '
        'rolling median used for morphology is a measurement smoother, not a '
        'correction; no notch, exclusion or level correction is applied.')
    add('')
    add('A 5.12 ms record is shorter than a 10 ms LTE frame and averages about '
        'nine GSM bursts. Pass revisits are about 12.5 s ON and 33 s OFF. The '
        'analysis therefore resolves pass-scale behaviour, not frame, slot or '
        'burst structure.')
    add('')
    add('## Method')
    add('')
    add('**Fine spectrum.** Each band is extracted from the per-pass power '
        'matrices, interpolated in linear power to 10 kHz, masked at the '
        'capture edge (±8 MHz), the DC gap (±1.5 MHz around the tuned centre) '
        'and cells with < 50 % pass coverage. The reported spectrum is the '
        'median across passes in each run. ON and OFF are never subtracted and '
        'no common reference is imposed.')
    add('')
    add('**Morphology.** A 150 kHz rolling median gives the block shape. The '
        'plateau is the 85th percentile of that smoothed spectrum inside the '
        'analysis window. Widths are the outermost crossings of the plateau '
        'at −3, −10 and −20 dB inside the plateau−10 dB support, so a narrow '
        'edge spike or an interior fade cannot collapse the width to one run. '
        'A side that reaches a DC mask, a coverage gap or the padded window is '
        'marked `≥`: the width is a lower bound. OBW99 is the 99 % power '
        'interval of the same body after removing a local 10th-percentile '
        'baseline; it is a display reference, not a correction. Ripple is the '
        'IQR of the smoothed body; p95−p5 is its 5–95 % range; flatness is the '
        'geometric-to-arithmetic mean ratio of baseline-subtracted linear '
        'power. Shoulders are the 3→20 dB transition widths outside the −3 dB '
        'crossings. Troughs are runs at least 6 dB below the plateau and at '
        'least 0.12 MHz wide.')
    add('')
    add('**Raster.** The median spectrum is detrended by a 1 MHz rolling '
        'median. A spatial periodogram over 25–400 kHz is computed with a '
        'Tukey window; `null95` is the 95th percentile of 60 permutation '
        'spectra with the same sample positions. A fixed-period sinusoid is '
        'then fitted to every pass at 45 kHz and at the periodogram maximum. '
        '`R` is the phase coherence of those per-pass fits, `pp` their mean '
        'peak-to-peak amplitude in dB, and the per-pass period is the '
        'periodogram maximum within ±15 % of the tested period. `R` and '
        'contrast are not amplitude: a tiny quiet-control ripple can be '
        'coherent and can have high relative contrast.')
    add('')
    add('**Dynamics.** Per pass, the body level is the median across the '
        'plateau−10 dB body; the power centroid is the linear-power weighted '
        'mean frequency above the local baseline. Level σ is the standard '
        'deviation of the body level across passes; occupancy is the fraction '
        'of passes whose body level exceeds the band floor +6 dB. These are '
        'descriptive pass statistics, not measurement uncertainties and not '
        'transmit powers.')
    add('')
    add('**Narrow components.** For each listed frequency, the local baseline '
        'is the median of the median spectrum in a 1.2 MHz window excluding '
        '±0.15 MHz around the peak. The peak, its −3/−10/−20 dB widths and the '
        'per-pass excess at the peak bin are reported. Persistence is the '
        'fraction of ON passes with excess > 6 dB. Correlations are computed '
        'on the 24 ON passes only and are descriptive; no multiple-comparison '
        'correction is applied.')
    add('')
    add('## Morphology summary')
    add('')
    add('| Block | Run | Window (MHz) | plateau−floor (dB) | peak exc. (dB) | '
        'OBW99 (MHz) | −3 dB (MHz) | −10 dB (MHz) | −20 dB (MHz) | Reliable |')
    add('| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: |')
    for r in blocks:
        met, w = r['met'], r['win']
        add(f"| {w['label']} | {r['tag'].upper()} | "
            f"{w['lo']:.2f}–{w['hi']:.2f} | {met['snr']:.1f} | "
            f"+{met['peak_excess']:.1f} | {_num(met['obw99'])} | "
            f"{_wstr(met['widths'][3.0])} | {_wstr(met['widths'][10.0])} | "
            f"{_wstr(met['widths'][20.0])} | "
            f"{'yes' if met['reliable'] else 'no'} |")
    add('')
    add('`plateau−floor` is the smoothed plateau minus the band 10th-percentile '
        'floor. `peak exc.` is a single-cell maximum above the plateau, often an '
        'edge spike or a narrow component; it is not the body level. `Reliable = '
        'no` means a width reaches a mask, a coverage gap or the padded window, '
        'or the block is threshold-limited; the width is then a lower bound or '
        'a threshold extent, not a channel bandwidth.')
    add('')
    add('| Block | Ripple IQR (dB) | p95−p5 (dB) | Flatness | Shoulders L/R '
        '(MHz) | Troughs |')
    add('| --- | ---: | ---: | ---: | ---: | --- |')
    for r in blocks:
        met = r['met']
        tr = '; '.join(f"{t['lo']:.2f}–{t['hi']:.2f} ({t['depth']:.1f} dB)"
                       for t in met['troughs'][:2]) or '—'
        add(f"| {r['win']['label']} | {_num(met['ripple'])} | "
            f"{_num(met['ripple_p95p5'])} | {_num(met['flatness'], 3)} | "
            f"{_num(met['shoulders'].get('left'))}/"
            f"{_num(met['shoulders'].get('right'))} | {tr} |")
    add('')
    add('### Per-block notes')
    add('')
    add('**700 MHz nominal B28 (OFF, block B).** The analysed block is '
        f"{by['700']['win']['lo']:.2f}–{by['700']['win']['hi']:.2f} MHz. "
        f"Its plateau is {by['700']['met']['snr']:.1f} dB above the band floor, "
        f"the −3 dB width is {_wstr(by['700']['met']['widths'][3.0])} MHz and "
        f"the −10 dB width {_wstr(by['700']['met']['widths'][10.0])} MHz: the "
        'shape is close to rectangular (flatness '
        f"{by['700']['met']['flatness']:.3f}, ripple "
        f"{by['700']['met']['ripple']:.2f} dB, shoulders "
        f"{_num(by['700']['met']['shoulders'].get('left'))}/"
        f"{_num(by['700']['met']['shoulders'].get('right'))} MHz). It does "
        '**not** carry the 45 kHz raster: the fixed 45 kHz fit is '
        f"{by['700']['f45']['pp']:.2f} dB peak-to-peak with R "
        f"{by['700']['f45']['R']:.2f}, and the periodogram maximum is "
        f"{by['700']['ras']['period_khz']:.2f} kHz at only "
        f"{by['700']['fpk']['pp']:.2f} dB with R "
        f"{by['700']['fpk']['R']:.2f}; its 13.0 dB relative contrast comes "
        'from a 0.15 dB ripple, not from the 45 kHz raster. The block is '
        'OFF-variant; the ON plan '
        'masks part of it. A second OFF block at 761.5–765.3 MHz is visible '
        'but not analysed. The 803 MHz upper edge of B28 is nominal; the '
        'analysis stops at 791 MHz to avoid duplicating LTE 800.')
    add('')
    add('**LTE 800 nominal (ON).** Two adjacent spans: '
        f"A {by['LTE800-A']['win']['lo']:.2f}–{by['LTE800-A']['win']['hi']:.2f} "
        f"MHz and B {by['LTE800-B']['win']['lo']:.2f}–"
        f"{by['LTE800-B']['win']['hi']:.2f} MHz. A: −3 dB "
        f"{_wstr(by['LTE800-A']['met']['widths'][3.0])} MHz, −10 dB "
        f"{_wstr(by['LTE800-A']['met']['widths'][10.0])} MHz, OBW99 "
        f"{_num(by['LTE800-A']['met']['obw99'])} MHz, plateau−floor "
        f"{by['LTE800-A']['met']['snr']:.1f} dB. B: −3 dB "
        f"{_wstr(by['LTE800-B']['met']['widths'][3.0])} MHz, −10 dB "
        f"{_wstr(by['LTE800-B']['met']['widths'][10.0])} MHz, OBW99 "
        f"{_num(by['LTE800-B']['met']['obw99'])} MHz, plateau−floor "
        f"{by['LTE800-B']['met']['snr']:.1f} dB. Both are about 9 MHz wide on "
        'the fine grid, consistent with a nominal 9 MHz LTE channel, but the '
        'measurement does not identify a channel or a numerology. The peak '
        f"excursions (+{by['LTE800-A']['met']['peak_excess']:.1f} and "
        f"+{by['LTE800-B']['met']['peak_excess']:.1f} dB) are single-cell edge "
        'spikes; the body ripple is 4.15 and 1.53 dB. Both spans carry the '
        f"45 kHz raster ({by['LTE800-A']['f45']['pp']:.2f} and "
        f"{by['LTE800-B']['f45']['pp']:.2f} dB per-pass pp at the fixed "
        f"period; peak-period R {by['LTE800-A']['fpk']['R']:.2f} and "
        f"{by['LTE800-B']['fpk']['R']:.2f}).")
    add('')
    add('**900 MHz nominal (ON).** Left span '
        f"{by['900-930']['win']['lo']:.2f}–{by['900-930']['win']['hi']:.2f} "
        f"MHz: −3 dB {_wstr(by['900-930']['met']['widths'][3.0])} MHz, −10 dB "
        f"{_wstr(by['900-930']['met']['widths'][10.0])} MHz, OBW99 "
        f"{_num(by['900-930']['met']['obw99'])} MHz, plateau−floor "
        f"{by['900-930']['met']['snr']:.1f} dB. The −10 dB run reaches the DC "
        'mask at 935.87–938.86 MHz, so 5.22 MHz is a lower bound, not a channel '
        'width; the block is detected across the mask in the survey. Right span '
        f"{by['900-954']['win']['lo']:.2f}–"
        f"{by['900-954']['win']['hi']:.2f} MHz: −3 dB "
        f"{_wstr(by['900-954']['met']['widths'][3.0])} MHz, −10 dB "
        f"{_wstr(by['900-954']['met']['widths'][10.0])} MHz, plateau−floor "
        f"{by['900-954']['met']['snr']:.1f} dB, occupancy only "
        f"{100*by['900-954']['tracks']['occ']:.0f} %. This is a threshold "
        'extent, not an established 9 MHz channel; troughs at 949.92–950.49 and '
        '959.52–959.95 MHz are about 7 dB deep. The left span has the strongest '
        f"raster: {by['900-930']['fpk']['pp']:.2f} dB pp, contrast "
        f"{by['900-930']['ras']['peak_contrast']:.1f} dB, R "
        f"{by['900-930']['fpk']['R']:.2f}, period "
        f"{by['900-930']['fpk']['period_med']:.2f}±"
        f"{by['900-930']['fpk']['period_sd']:.2f} kHz.")
    add('')
    k1800 = ('1800-1811', '1800-1820', '1800-1840', '1800-1862')
    pp1800 = [by[k]['f45']['pp'] for k in k1800]
    r1800 = [by[k]['fpk']['R'] for k in k1800]
    add('**1800 MHz nominal (ON).** Four spans. '
        f"1811: −3 dB {_wstr(by['1800-1811']['met']['widths'][3.0])}, −10 dB "
        f"{_wstr(by['1800-1811']['met']['widths'][10.0])} MHz, plateau−floor "
        f"{by['1800-1811']['met']['snr']:.1f} dB. "
        f"1820: −3 dB {_wstr(by['1800-1820']['met']['widths'][3.0])}, −10 dB "
        f"{_wstr(by['1800-1820']['met']['widths'][10.0])} MHz, plateau−floor "
        f"{by['1800-1820']['met']['snr']:.1f} dB; the span fills the window, so "
        'these are lower bounds. '
        f"1840: −3 dB {_wstr(by['1800-1840']['met']['widths'][3.0])}, −10 dB "
        f"{_wstr(by['1800-1840']['met']['widths'][10.0])} MHz, plateau−floor "
        f"{by['1800-1840']['met']['snr']:.1f} dB; the 1840.89 MHz narrow "
        'component sits at its left edge. '
        f"1862: −3 dB {_wstr(by['1800-1862']['met']['widths'][3.0])}, −10 dB "
        f"{_wstr(by['1800-1862']['met']['widths'][10.0])} MHz, plateau−floor "
        f"{by['1800-1862']['met']['snr']:.1f} dB; this weak tail is "
        f"threshold-limited and its occupancy is "
        f"{100*by['1800-1862']['tracks']['occ']:.0f} %. All four carry a "
        f"45 kHz raster between {min(pp1800):.2f} and {max(pp1800):.2f} "
        f"dB pp at the fixed period, with peak-period R "
        f"{min(r1800):.2f}–{max(r1800):.2f}. "
        'The ~1831–1834 and 1859–1862 MHz breaks coincide with ON DC masks; '
        'they are measurement gaps, not emission gaps.')
    add('')
    add('**LTE 2100 nominal (OFF).** One span '
        f"{by['LTE2100']['win']['lo']:.2f}–{by['LTE2100']['win']['hi']:.2f} "
        f"MHz: −3 dB {_wstr(by['LTE2100']['met']['widths'][3.0])} MHz, −10 dB "
        f"{_wstr(by['LTE2100']['met']['widths'][10.0])} MHz, OBW99 "
        f"{_num(by['LTE2100']['met']['obw99'])} MHz, plateau−floor "
        f"{by['LTE2100']['met']['snr']:.1f} dB. The −3 dB width is 2.57 MHz "
        'narrower than −10 dB: a broad upper shelf rather than a rectangular '
        'channel. The ON plan misses the lower ~3 MHz behind the '
        '2111.01–2113.99 MHz DC mask. The 45 kHz raster is weak and '
        f"pass-variable: {by['LTE2100']['f45']['pp']:.2f} dB pp, R "
        f"{by['LTE2100']['f45']['R']:.2f} (fixed 45 kHz; peak-period R "
        f"{by['LTE2100']['fpk']['R']:.2f}), period "
        f"{by['LTE2100']['fpk']['period_med']:.2f}±"
        f"{by['LTE2100']['fpk']['period_sd']:.2f} kHz, N "
        f"{by['LTE2100']['f45']['n']}.")
    add('')
    add('**LTE 2600 nominal.** No cell reaches floor +6 dB in either run. '
        'The ON per-pass peak median is +2.4 dB above the 10th-percentile '
        'floor and OFF +2.3 dB (survey). No wideband or narrow block is '
        'claimed. The two highlighted regions (2621–2639 MHz ON and '
        '2642–2660 MHz OFF) are quiet controls, not detections. Absence of a '
        'threshold crossing is not proof that no signal exists below it.')
    add('')
    add('## Spectral raster')
    add('')
    add('| Block | 45 kHz pp (dB) | peak T (kHz) | peak pp (dB) | peak contrast '
        '(dB) | 45k null95 (dB) | 180k contrast (dB) | 200k contrast (dB) | '
        'R (peak) | per-pass T (kHz) | N |')
    add('| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | '
        '---: | ---: |')
    for r in blocks:
        ra, f45, fpk = r['ras'], r['f45'], r['fpk']
        add(f"| {r['win']['label']} | {f45['pp']:.2f} | "
            f"{ra['period_khz']:.2f} | {fpk['pp']:.2f} | "
            f"{ra['peak_contrast']:.1f} | {ra['null95'][45.0]:.1f} | "
            f"{ra['contrasts'][180.0]:.1f} | "
            f"{ra['contrasts'][200.0]:.1f} | {fpk['R']:.2f} | "
            f"{fpk['period_med']:.2f}±{fpk['period_sd']:.2f} | "
            f"{fpk['n']} |")
    add('')
    add('The 45 kHz amplitude is the fixed-period per-pass fit; `peak T` and '
        '`peak pp` are the periodogram maximum. `null95` is the permutation '
        '95th percentile at 45 kHz; `contrast` is the periodogram peak relative '
        'to its median. Periods cluster at 45.00–45.16 kHz. 45 kHz is exactly '
        '3 × 15 kHz. This is **consistent with** a 3-subcarrier comb such as '
        'reference-signal placement in an OFDM downlink, but it is not an '
        'identification: a receiver/search artifact or an intermodulation '
        'product could also be periodic.')
    add('')
    add('| Control | Run | Span (MHz) | 45 kHz pp (dB) | 45k contrast (dB) | '
        'R (45k) | peak T (kHz) | peak pp (dB) |')
    add('| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |')
    for c in controls:
        add(f"| {c['label']} | {c['tag'].upper()} | "
            f"{c['span'][0]:.2f}–{c['span'][1]:.2f} | "
            f"{c['f45']['pp']:.2f} | {c['ras']['contrasts'][45.0]:.1f} | "
            f"{c['f45']['R']:.2f} | {c['ras']['period_khz']:.2f} | "
            f"{c['fpk']['pp']:.2f} |")
    add('')
    qmax = max(c['f45']['pp'] for c in controls
               if c['label'] not in ('900 inter-block gap',))
    add(f'The quiet controls span 0.02–{qmax:.2f} dB pp at 45 kHz. The strong '
        'blocks span 1.67–9.62 dB pp at the fixed 45 kHz period (2.68–9.61 dB '
        'at the peak period), about 4–25× the quiet maximum. The 700 MHz block '
        '(0.06 dB) is at the quiet level; LTE 2100 (0.68 dB) is above it but '
        'far below the strong blocks and has R 0.25 with only eight OFF '
        'passes. The 900 inter-block gap in ON shows 2.41 dB pp at 45 kHz '
        '(OFF 0.14 dB): well above the quiet controls and at the low end of '
        'the block amplitudes, comparable to the 1800 spans, but the gap is '
        'not a detected block. Its cause is unresolved and it is not '
        'attributed to a transmitter or to the feature bundle.')
    add('')
    add('Two cautions follow from the controls. First, phase coherence alone is '
        'not amplitude: the 700 quiet control has R 0.83 at 0.05 dB pp, and the '
        '2600 ON control has 0.39 dB pp but 24.2 dB relative contrast. Second, '
        '180 kHz and 200 kHz show no consistent contrast across the strong '
        'blocks (180 kHz −8.0 to +4.5 dB; 200 kHz −6.0 to +5.5 dB), so there is '
        'no spectral evidence here for a 180 kHz PRB comb or a 200 kHz GSM '
        'burst raster at these spans.')
    add('')
    add('## ON/OFF overlays and extents')
    add('')
    add('Figure 1 overlays the other run’s median at the same within-band '
        'reference. The overlay is a display aid, not a subtraction, and no '
        'difference is computed from it. Where a level delta is quoted '
        'elsewhere in this results set, the valid quantity is the matched '
        'common-span median from `CELLULAR.md`: the intersection of the two '
        'threshold extents. Own-span medians are not like-for-like when the '
        'extents differ. That applies to the 900 MHz 954 MHz span (ON starts '
        'higher) and the 1800 MHz 1862 MHz span (OFF extends further on both '
        'sides). The 700 MHz and LTE 2100 morphology panels are OFF-variant; '
        'their ON overlays are shown for context only. The 1831–1834 and '
        '1859–1862 MHz breaks coincide with ON DC masks and are measurement '
        'gaps, not emission gaps.')
    add('')
    add('## Pass-scale dynamics')
    add('')
    add('| Block | Run | Centroid (MHz) | Centroid σ (MHz) | Level σ (dB) | '
        'Occupancy | Level range (dB) | N |')
    add('| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |')
    for r in blocks:
        tr = r['tracks']
        lv = tr['level'] - tr['level_med']
        add(f"| {r['win']['label']} | {r['tag'].upper()} | "
            f"{tr['centroid_med']:.2f} | {tr['centroid_std']:.2f} | "
            f"{tr['level_sigma']:.2f} | {100*tr['occ']:.0f} % | "
            f"{lv.min():+.2f} … {lv.max():+.2f} | {tr['n']} |")
    add('')
    add('Level σ is the pass-to-pass standard deviation of the body median, '
        'not an uncertainty. It includes real fading, tuning-plan and route '
        'differences. The 900 MHz spans are the most stable (σ 0.74–0.96 dB); '
        'the 700 MHz OFF block has σ 4.82 dB and one pass 14.35 dB below its '
        'median; the 1800 MHz spans reach single-pass positive excursions of '
        '+9.0 to +11.5 dB. None of these is attributed to transmit power, '
        'scheduler, path loss or antenna.')
    add('')
    add('The power centroid is biased by strong narrow components. In '
        '1800–1840 the 1840.89 MHz component sits at the left edge and the '
        f"centroid wanders {by['1800-1840']['tracks']['centroid_std']:.2f} MHz "
        '(σ), so that value is not body motion alone. The 900–930 centroid is '
        f"stable at {by['900-930']['tracks']['centroid_std']:.2f} MHz. LTE 2100 "
        f"has a {by['LTE2100']['tracks']['centroid_std']:.2f} MHz centroid σ "
        'over only eight OFF passes. ON/OFF tracks are separate sequential '
        'runs and are not subtracted.')
    add('')
    add('## Narrow components')
    add('')
    add('| Run | MHz | Peak MHz | Above local baseline (dB) | −3 dB (MHz) | '
        '−10 dB (MHz) | Persistence | Excess σ (dB) | N |')
    add('| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |')
    for key, comps, keys, corr in narrow:
        for c in comps:
            add(f"| {key} | {c['f0']:.2f} | {c['peak_f']:.2f} | "
                f"+{c['above']:.1f} | {c['w3']['width']:.2f} | "
                f"{c['w10']['width']:.2f} | {100*c['persist']:.0f} % | "
                f"{c['exc_sigma']:.2f} | {c['n']} |")
    add('')
    add('`Above local baseline` is the peak of the median spectrum over a '
        '1.2 MHz local baseline; `excess σ` is the standard deviation of the '
        'per-pass excess at the peak bin. The −3 dB widths (0.06–0.16 MHz) are '
        'measured on the 10 kHz fine grid; they differ from the 20/50 kHz '
        'survey values by up to one grid cell and are not modulation '
        'bandwidths.')
    add('')
    add('The 900 MHz components are narrow and persistent: 947.81 MHz is the '
        'strongest (+41.6 dB above its local baseline), while 949.81 and '
        '926.91 MHz have larger excess σ (3.82 and 4.43 dB) than the stable '
        'trio (0.40–1.63 dB). 926.91 MHz persists in 96 % of ON passes, not '
        '100 %. The 1840.89 MHz component is +20.7 dB above its local baseline '
        'with a 0.16 MHz −3 dB width in the median spectrum; its per-pass '
        'median is 17.7 dB with σ 4.32 dB. These are spectral shapes only: a '
        'narrow component could be a CW carrier, an intermodulation product, a '
        'receiver spur or a narrowband emission. No identity is claimed.')
    add('')
    add('Correlation of the 900 MHz per-pass excess (24 ON passes):')
    add('')
    keys = n900[2]
    corr = n900[3]
    add('| | ' + ' | '.join(keys) + ' |')
    add('| --- | ' + ' | '.join('---:' for _ in keys) + ' |')
    for i, k in enumerate(keys):
        add(f'| {k} | ' + ' | '.join(f'{corr[i, j]:.2f}'
                                     for j in range(len(keys))) + ' |')
    add('')
    add('The largest |r| is 0.44 (946.63 vs 949.81); 945.39 vs 947.81 is 0.42. '
        'With 24 passes and no multiple-comparison correction these are weak '
        'descriptive associations, not evidence of a common modulation or a '
        'common source. No strong common pass-scale modulation is present.')
    add('')
    add('## Candidate interpretations')
    add('')
    add('| Observation | Consistent with | Not established / caveat |')
    add('| --- | --- | --- |')
    add('| 45 kHz comb, period 45.00–45.16 kHz, peak-period R 0.74–1.00, '
        '1.7–9.6 dB pp at fixed 45 kHz | a 3 × 15 kHz reference-subcarrier '
        'comb in an OFDM downlink | receiver/search artifact or '
        'intermodulation; no phase, no demodulation; source not identified |')
    add('| ~9 MHz LTE 800 spans and ~18 MHz 1800/2100 extents | wideband '
        'downlink emissions | widths are plateau/threshold measurements, not '
        'channel bandwidths; no standard or operator identified |')
    add('| LTE 2100 −3 dB 15.44 MHz vs −10 dB 18.01 MHz | a broad upper shelf '
        'from the transmit envelope or receive filter | no transmitter or '
        'filter identification; OFF only; ON misses the lower edge behind DC |')
    add('| 700 MHz OFF block: 9.34 MHz, flatness 0.971, no 45 kHz raster | a '
        'different signal type or a different OFF-plan/receiver state | '
        'sequential OFF data; the ON plan masks part of the block; not '
        'attributed |')
    add('| 900 MHz narrow components, 0.06–0.09 MHz −3 dB, 96–100 % persistence '
        '| CW, intermodulation, receiver spurs or narrowband emissions | below '
        'a 180 kHz PRB and a 200 kHz GSM channel; no identity or ARFCN |')
    add('| 1840.89 MHz component, 0.16 MHz −3 dB, +20.7 dB above local '
        'baseline | a narrow carrier or an aggregation artifact | biases the '
        '1800–1840 power centroid; no identity |')
    add('| 900 inter-block gap ON, 2.41 dB pp at 45 kHz | weak or intermediate '
        'raster in the gap | not a detected block; cause unresolved; OFF shows '
        '0.14 dB |')
    add('')
    add('## What the evidence is consistent with')
    add('')
    add('- The ~45 kHz raster is **consistent with** a 3 × 15 kHz spectral comb, '
        'such as reference-signal placement in an OFDM downlink. It is present '
        'in every strong block (LTE 800 A/B, 900-930, 900-954, all four 1800 '
        'spans) with periods 45.00–45.16 kHz, phase R 0.74–1.00 and amplitudes '
        '1.7–9.6 dB pp at the fixed 45 kHz period (2.7–9.6 dB pp at the '
        'peak period), and is absent or weak in the 700 MHz OFF block, LTE 2100 '
        'and all quiet controls. The data cannot distinguish reference signals '
        'from a receiver/search artifact or an intermodulation product.')
    add('- The ~9 MHz LTE 800 spans, the ~18 MHz 1800–1840 span and the ~18 MHz '
        'LTE 2100 −10 dB span are **consistent with** wideband downlink '
        'emissions. The widths are threshold/plateau measurements, not channel '
        'bandwidths, and no demodulation is performed.')
    add('- The LTE 2100 −3 dB width (15.44 MHz) is 2.57 MHz narrower than −10 dB '
        '(18.01 MHz), a broad upper shelf. This is **consistent with** a wider '
        'transmitted envelope or a filter shape, but no transmitter or standard '
        'is identified.')
    add('- The 700 MHz OFF block is flat and nearly rectangular and lacks the '
        '45 kHz raster. That is **consistent with** a different signal type or '
        'a different OFF-plan/receiver state; because the runs are sequential '
        'and the plans differ, it is not attributed to a technology or an '
        'operator.')
    add('- The 900 MHz narrow components and the 1840.89 MHz component are '
        '**consistent with** narrow spectral features, but their widths are '
        'below a 180 kHz PRB and a 200 kHz GSM channel. They could be CW, '
        'intermodulation, receiver spurs or narrowband emissions; no identity '
        'is claimed.')
    add('')
    add('## What cannot be concluded')
    add('')
    add('- **No demodulation.** Raw JSONL stores 8192-bin FFT power only; the '
        'rejected IQ directory has an empty record list. There is no phase, so '
        'no EVM, constellation, symbol rate, modulation order, cell identity, '
        'ARFCN or operator attribution is possible.')
    add('- **No absolute level.** All values are relative and uncalibrated. No '
        'dBm, EIRP, path loss, noise figure, absolute floor or traceability is '
        'claimed. `plateau−floor` and peak excess are differences, not powers.')
    add('- **No causal ON/OFF difference.** ON and OFF are sequential 300 s '
        'windows with different tuning plans. Overlays show consistency, not a '
        'simultaneous comparison; differences include RF-environment change.')
    add('- **No frame or burst resolution.** A 5.12 ms record is shorter than a '
        '10 ms LTE frame and averages about nine GSM bursts. Only pass-scale '
        'dynamics are observable.')
    add('- **No per-feature ablation.** The ON/OFF plans differ, so a feature '
        'cannot be switched in isolation. The 900 inter-block gap ON excess '
        '(2.41 dB pp at 45 kHz) is intermediate and unresolved.')
    add('- **No proof of absence.** LTE 2600 and several survey windows have no '
        'threshold crossing. That is not proof that no signal exists below the '
        'threshold. Widths that reach a mask, a coverage gap or the padded '
        'window are lower bounds (`≥`), not completed edges.')
    add('- **No raster source.** A fixed 45 kHz raster can be transmitter '
        'reference structure or a receiver/search artifact. The quiet controls '
        'constrain its amplitude but do not identify its origin.')
    add('')
    add('## 150–160 MHz')
    add('')
    add('The session’s 150–160 MHz behaviour remains unresolved: the ON '
        'per-pass median range is 18.78 dB while OFF sits near its floor. It is '
        'outside the cellular bands, is not used here, and is not notched, '
        'excluded or corrected. Resolving it requires a controlled 50 Ω input '
        'and/or an alternating ON/OFF source.')
    add('')
    add('## Reproduce')
    add('')
    add('```sh')
    add('env -u PYTHONPATH sessions/.venv/bin/python tools/report_modulation.py \\')
    add(f'    sessions/{session_name} --use-cache \\')
    add('    --out docs/results/' + session_name + '/cellular_modulation.png \\')
    add('    --raster-out docs/results/' + session_name +
        '/cellular_modulation_raster.png \\')
    add('    --dyn-out docs/results/' + session_name +
        '/cellular_modulation_dynamics.png \\')
    add('    --md docs/results/' + session_name + '/MODULATION.md')
    add('```')
    add('')
    add('The fine cache is `sessions/' + session_name +
        '/report/modulation_cache.npz` (10 kHz, linear power interpolation, '
        f'{meta["on_records"]} ON / {meta["off_records"]} OFF records). Raw '
        'JSONL stays in `sessions/` and is not committed.')
    add('')
    Path(path).write_text('\n'.join(L) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('session')
    ap.add_argument('--out', default=None)
    ap.add_argument('--raster-out', default=None)
    ap.add_argument('--dyn-out', default=None)
    ap.add_argument('--md', default=None)
    ap.add_argument('--cache', default=None)
    ap.add_argument('--use-cache', action='store_true')
    a = ap.parse_args()
    sess = Path(a.session)
    outpath = Path(a.out) if a.out else sess / 'report' / 'cellular_modulation.png'
    rasterpath = (Path(a.raster_out) if a.raster_out
                  else outpath.with_name('cellular_modulation_raster.png'))
    dynpath = (Path(a.dyn_out) if a.dyn_out
               else outpath.with_name('cellular_modulation_dynamics.png'))
    mdpath = Path(a.md) if a.md else sess / 'report' / 'MODULATION.md'
    for p in (outpath, rasterpath, dynpath, mdpath):
        p.parent.mkdir(parents=True, exist_ok=True)
    data, meta = load_data(sess, a)
    print(meta)
    blocks = analyse(data)
    controls = analyse_controls(data)
    narrow = []
    for key, comps in NARROW.items():
        narrow.append((key, *narrow_components(data, 'on', key, comps)))
    print_summary(blocks, controls, narrow)
    fig = figure_morph(data, blocks, controls, meta)
    fig.savefig(outpath, bbox_inches='tight')
    print('wrote', outpath)
    fig = figure_raster(data, blocks, controls, meta)
    fig.savefig(rasterpath, bbox_inches='tight')
    print('wrote', rasterpath)
    fig = figure_dynamics(data, blocks, narrow, meta)
    fig.savefig(dynpath, bbox_inches='tight')
    print('wrote', dynpath)
    write_markdown(mdpath, blocks, controls, narrow, meta, sess.name)
    print('wrote', mdpath)


if __name__ == '__main__':
    raise SystemExit(main())
