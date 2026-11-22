#!/usr/bin/env python3
"""Waterfalls / heatmaps for a soapy_power hop scan (-F json), all built from the artifact-free residual.

Processing (same as analyze_hopscan.py, plus):
  * the receiver DC/low-frequency skirt (median over hops) and the slow floor are removed;
  * every record (sweep x hop) is levelled to its own median, so start-up transients and hop-to-hop
    gain steps cannot paint bands into the picture;
  * +-12 kHz around each hop centre (DC mask + unreliable template core) is left blank, never filled;
  * each hop is drawn at its own measured time (staircase), not at the sweep start.

Outputs in OUTDIR (prefix <tag>_):
  waterfall.png       hero, 3840x2160: detection-enhanced residual (power average 3 bins x 5 sweeps,
                      max-pooled to pixels so 1-bin lines survive), 10-min mean spectrum on top
  waterfall_raw.png   raw receiver output incl. the artifact, power-averaged to pixels
  waterfall_zoom.png  the strongest candidates at native 244 Hz x per-sweep resolution + level vs time
  waterfall.html      self-contained interactive viewer of the full native-resolution residual
usage: hopscan_waterfall.py SCAN.jsonl OUTDIR [--tag NAME]   (analyze_hopscan.py calls render() itself)"""
import base64, datetime, gzip, json, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze_hopscan as ah                      # noqa: E402  (sets the Agg backend and the dark style)
import matplotlib.pyplot as plt                   # noqa: E402
from matplotlib import colormaps                  # noqa: E402
from matplotlib.ticker import MultipleLocator     # noqa: E402
from scipy.ndimage import find_objects, label, uniform_filter   # noqa: E402

CENTRE_KHZ = 12.0          # blank +-12 kHz around each hop centre
SMOOTH_BINS, SMOOTH_SWEEPS = 3, 5
MASKED = '#3a4256'         # colour of blank (masked) cells: neutral, never confusable with the colormap
CMAP = 'inferno'


def prepare(A):
    nsw, nh, keep, step = A['nsw'], A['nh'], A['hi'] - A['lo'], A['step']
    off_khz = np.tile(np.arange(A['lo'], A['hi']) - A['n'] // 2, nh) * step / 1e3
    R = A['Res'].copy()
    R[:, abs(off_khz) < CENTRE_KHZ] = np.nan
    blk = np.zeros((nsw, nh))
    for h in range(nh):
        s = slice(h * keep, (h + 1) * keep)
        blk[:, h] = np.nanmedian(R[:, s], 1)
        R[:, s] -= blk[:, h][:, None]
    fin = np.isfinite(R)
    sig = 1.4826 * np.nanmedian(abs(R[fin]))
    L = np.where(fin, 10 ** (R / 10), 0.0); w = fin.astype(float)
    num = uniform_filter(L, (SMOOTH_SWEEPS, SMOOTH_BINS), mode='constant')   # constant 0: edges average only real rows
    den = uniform_filter(w, (SMOOTH_SWEEPS, SMOOTH_BINS), mode='constant')
    E = np.where(den > 0.5, 10 * np.log10(np.maximum(num, 1e-30) / np.maximum(den, 1e-30)), np.nan)
    sig_e = 1.4826 * np.nanmedian(abs(E[np.isfinite(E)] - np.nanmedian(E)))
    t0 = A['T0'][0, 0]
    W = dict(R=R, E=E, blk=blk, off_khz=off_khz, sig=float(sig), sig_e=float(sig_e), keep=keep,
             ts=A['T0'] - t0, te=A['T1'] - t0, t0=t0,
             start=datetime.datetime.fromtimestamp(t0, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'))
    W['events'] = find_events(A, W)
    return W


def find_events(A, W, k_sigma=8.0, line_khz=1.5, group_khz=15.0):
    """Intermittent emissions: connected regions of the enhanced residual above k_sigma*sigma_e that are
    neither a persistent line candidate (+-line_khz) nor present in half the sweeps. Regions that overlap in
    time and lie within group_khz are one event. Times come from the hop's own record timestamps."""
    R, E, f, keep, nsw = W['R'], W['E'], A['f'], W['keep'], A['nsw']
    thr = max(k_sigma * W['sig_e'], 0.6)
    lab, _ = label(np.nan_to_num(E, nan=-99.0) > thr, structure=np.ones((3, 3)))
    lines = np.array([c['f_mhz'] for c in A['cand']]); comps = []
    for i, sl in enumerate(find_objects(lab), 1):
        if sl is None:
            continue
        sub = np.where(lab[sl] == i, R[sl], np.nan)
        if not np.isfinite(sub).any():
            continue
        rr, cc = np.unravel_index(np.nanargmax(sub), sub.shape); j = sl[1].start + cc
        if (len(lines) and np.min(abs(lines - f[j])) * 1e3 < line_khz) or sl[0].stop - sl[0].start >= nsw / 2:
            continue
        comps.append(dict(r0=sl[0].start, r1=sl[0].stop, c0=sl[1].start, c1=sl[1].stop, j=int(j), peak=float(np.nanmax(sub))))
    comps.sort(key=lambda c: c['c0']); groups = []
    for c in comps:
        for g in groups:
            if any(c['r0'] < o['r1'] and o['r0'] < c['r1'] and abs(f[c['j']] - f[o['j']]) * 1e3 < group_khz for o in g):
                g.append(c); break
        else:
            groups.append([c])
    ev = []
    for g in groups:
        r0, r1 = min(c['r0'] for c in g), max(c['r1'] for c in g); c0, c1 = min(c['c0'] for c in g), max(c['c1'] for c in g)
        top = max(g, key=lambda c: c['peak']); j = top['j']; h = min(j // keep, A['nh'] - 1)
        ncol = max(1, c1 - c0); seg = R[r0:r1, c0:c1]
        rowlev = 10 * np.log10(np.nanmean(10 ** (seg / 10), 1))
        act = np.flatnonzero(rowlev > 3 * W['sig'] / np.sqrt(max(1.0, ncol / 1.73)))
        a, b = (r0 + act[0], r0 + act[-1] + 1) if len(act) else (min(r0 + 2, r1 - 1), max(r1 - 2, r0 + 1))
        peaks = sorted({round(float(f[c['j']]), 4) for c in g})
        ev.append(dict(f_mhz=float(f[j]), f_lo_mhz=float(f[c0]), f_hi_mhz=float(f[c1 - 1]), width_khz=float(max(1, ncol - (SMOOTH_BINS - 1)) * A['step'] / 1e3),
                       peaks_mhz=peaks, t_start_s=float(W['ts'][a, h]), t_end_s=float(W['te'][b - 1, h]), sweeps=int(b - a),
                       active_sweeps=int(len(act)), peak_db=float(top['peak']),
                       mean_db=float(10 * np.log10(np.nanmean(10 ** (R[a:b, max(0, j - 1):j + 2] / 10)))),
                       hop=int(h), offset_khz=float(W['off_khz'][j]), r0=int(a), r1=int(b), c0=int(c0), c1=int(c1)))
    ev.sort(key=lambda e: -e['peak_db'])
    return ev


def _pfactor(keep, nh, target_px):
    """Smallest divisor p of keep that gives no more columns than pixels (a cell never falls between pixels)."""
    for p in range(1, keep + 1):
        if keep % p == 0 and keep * nh / p <= target_px:
            return p
    return keep


def pool(Z, keep, nh, p, how):
    out = []
    for h in range(nh):
        B = Z[:, h * keep:(h + 1) * keep].reshape(Z.shape[0], keep // p, p)
        out.append(np.nanmax(B, 2) if how == 'max' else 10 * np.log10(np.nanmean(10 ** (B / 10), 2)))
    return out


def draw(ax, A, W, Z, how, vmin, vmax, cmap=CMAP, target_px=5400):
    """Staircase waterfall: hop h's rows span its own measured start times."""
    keep, nh, f, step_mhz = W['keep'], A['nh'], A['f'], A['step'] / 1e6
    p = _pfactor(keep, nh, target_px)
    cm = colormaps[cmap].copy(); cm.set_bad(MASKED)
    ax.set_facecolor(MASKED)
    m = None
    blocks = pool(Z, keep, nh, p, how)
    if callable(vmin):
        vmin, vmax = vmin(blocks), vmax(blocks)
    for h, B in enumerate(blocks):
        cols = np.arange(h * keep, (h + 1) * keep, p)
        xe = np.append(f[cols] - step_mhz / 2, f[(h + 1) * keep - 1] + step_mhz / 2)
        ye = np.append(W['ts'][:, h], W['te'][-1, h])
        m = ax.pcolormesh(xe, ye, np.ma.masked_invalid(B), cmap=cm, vmin=vmin, vmax=vmax, shading='flat', rasterized=True)
    ax.grid(False); ax.set_xlim(f[0], f[-1]); ax.set_ylim(0, W['te'][-1].max())
    return m, p


def noise_stats(blocks):
    v = np.concatenate([b.ravel() for b in blocks]); v = v[np.isfinite(v)]
    med = float(np.median(v)); return med, float(1.4826 * np.median(abs(v - med)))


def _labels(ax, cands, span_mhz, n=10, rows=(0.62, 0.80, 0.98)):
    """Label the strongest lines in rows at fixed heights so no two labels collide (leader line to the peak)."""
    top = sorted(cands[:n], key=lambda c: c['f_mhz']); need = 0.034 * span_mhz; last = [-1e9] * len(rows)
    for c in top:
        x = c['f_mhz']; k = next((i for i in range(len(rows)) if x - last[i] > need), int(np.argmin(last)))
        last[k] = x
        ax.plot([x], [c['peak_excess_db']], marker='v', ms=7, color=ah.C2, mec='none', zorder=5)
        ax.annotate(f"{x:.4f}", (x, c['peak_excess_db']), xytext=(x, rows[k]), textcoords=ax.get_xaxis_transform(), ha='center', va='bottom',
                    fontsize=11, color=ah.C2, arrowprops=dict(arrowstyle='-', color=ah.C2, lw=.5, alpha=.6, shrinkA=1, shrinkB=4))


def hero(A, W, out, tag):
    s, f, nsw = A['summary'], A['f'], A['nsw']
    fig = plt.figure(figsize=(24, 13.5), dpi=160)
    fig.text(0.035, 0.965, f'Waterfall  {f[0]:.1f}–{f[-1]:.1f} MHz', fontsize=30, fontweight='bold', va='top')
    fig.text(0.035, 0.922, f"{nsw} sweeps × {A['nh']} hops · start {W['start']} · {s['span_s']:.0f} s · native {s['step_hz']:.0f} Hz bins · "
             f"residual after receiver-artifact and floor removal, every record levelled to its own median\n"
             f"enhanced: power average {SMOOTH_BINS} bins × {SMOOTH_SWEEPS} sweeps (σ {W['sig_e']:.3f} dB vs {W['sig']:.3f} dB native), then max per pixel so single-bin lines survive · "
             f"grey = ±{CENTRE_KHZ:.0f} kHz blank around each hop centre (▼)", fontsize=12.5, color='#8fa0bd', va='top', linespacing=1.5)
    ax0 = fig.add_axes([0.05, 0.735, 0.895, 0.13])
    mean = 10 * np.log10(np.nanmean(10 ** (W['R'] / 10), 0))
    ax0.plot(f, mean, color=ah.C4, lw=.6)
    ax0.axhline(0, color='#8fa0bd', lw=.6)
    band = 3 * W['sig'] / np.sqrt(nsw)
    ax0.fill_between(f, -band, band, color='#8fa0bd', alpha=.3, lw=0)
    for c in A['centers'] / 1e6:
        ax0.axvline(c, color=ah.C2, lw=.8, ls=':', alpha=.7)
    top = max([c['peak_excess_db'] for c in A['cand']] + [1.0])
    ax0.set_xlim(f[0], f[-1]); ax0.set_ylim(-0.35, top / 0.58)
    _labels(ax0, A['cand'], f[-1] - f[0])
    for e in W['events']:
        ax0.plot([e['f_mhz']], [0.04], marker='^', ms=8, color=ah.C1, mec='none', transform=ax0.get_xaxis_transform(), zorder=6); ax0.set_xticklabels([])
    ax0.set_ylabel('dB, 10-min\nmean', fontsize=12); ax0.tick_params(labelsize=11)
    ax0.xaxis.set_major_locator(MultipleLocator(0.5)); ax0.xaxis.set_minor_locator(MultipleLocator(0.1))
    ax = fig.add_axes([0.05, 0.075, 0.895, 0.645])
    px = int(0.895 * 24 * 160); st = {}
    def lo_(b): st['m'], st['s'] = noise_stats(b); return st['m'] - 2 * st['s']
    def hi_(b): return st['m'] + max(12 * st['s'], 0.5)
    m, p = draw(ax, A, W, W['E'], 'max', lo_, hi_, target_px=px)
    ax.set_xlabel('MHz', fontsize=14); ax.set_ylabel('seconds since start', fontsize=14); ax.tick_params(labelsize=12)
    ax.xaxis.set_major_locator(MultipleLocator(0.5)); ax.xaxis.set_minor_locator(MultipleLocator(0.1))
    tmax = W['te'][-1].max(); pad = 0.012 * (f[-1] - f[0])
    for q, e in enumerate(sorted(W['events'], key=lambda e: e['f_mhz'])):
        ax.add_patch(plt.Rectangle((e['f_lo_mhz'] - pad, e['t_start_s'] - 2), e['f_hi_mhz'] - e['f_lo_mhz'] + 2 * pad, e['t_end_s'] - e['t_start_s'] + 4,
                                   fill=False, ec=ah.C1, lw=1.3, zorder=6))
        right = e['f_hi_mhz'] + pad < f[-1] - 0.12 * (f[-1] - f[0])
        ax.annotate(f"{e['f_mhz']:.4f} MHz  {e['peak_db']:+.1f} dB  {e['t_end_s'] - e['t_start_s']:.0f} s",
                    ((e['f_hi_mhz'] + pad) if right else (e['f_lo_mhz'] - pad), (e['t_start_s'] + e['t_end_s']) / 2), xytext=(6 if right else -6, 0),
                    textcoords='offset points', ha='left' if right else 'right', va='center', fontsize=11.5, color=ah.C1, zorder=7,
                    bbox=dict(fc='#070b14cc', ec='none', pad=1.5))
    for c in A['centers'] / 1e6:
        ax.annotate('', (c, W['te'][-1].max()), xytext=(c, W['te'][-1].max() * 1.025), arrowprops=dict(arrowstyle='-|>', color=ah.C2, lw=1), annotation_clip=False)
    cax = fig.add_axes([0.952, 0.075, 0.01, 0.645]); cb = fig.colorbar(m, cax=cax)
    cb.set_label(f'dB above median floor · pixel noise {st["m"]:+.2f} ± {st["s"]:.3f} dB', fontsize=12); cb.ax.tick_params(labelsize=11)
    fig.text(0.05, 0.008, f"pixel = {p} bins ({p * s['step_hz'] / 1e3:.2f} kHz) max-pooled · hop centres ▼ · levels: {s['unit']}, uncalibrated, relative · "
             f"lines are candidates (real carrier or receiver spur not yet decided) · cyan boxes: intermittent emissions ({len(W['events'])}) · {s['records']} records, statuses {s['statuses']}", fontsize=11, color='#8fa0bd')
    fig.savefig(out / f'{tag}_waterfall.png', dpi=160); plt.close(fig)


def raw(A, W, out, tag):
    s, f, S = A['summary'], A['f'], A['S']
    fig = plt.figure(figsize=(24, 13.5), dpi=160)
    fig.text(0.035, 0.965, f'Raw receiver output  {f[0]:.1f}–{f[-1]:.1f} MHz', fontsize=30, fontweight='bold', va='top')
    fig.text(0.035, 0.918, f"{A['nsw']} sweeps · start {W['start']} · power average to pixels · the bright columns at every hop centre are the receiver DC/low-frequency skirt "
             f"(identical in all hops, spread {s['dc_artifact_hop_spread_db']:.2f} dB), removed in the residual views", fontsize=12.5, color='#8fa0bd', va='top')
    ax0 = fig.add_axes([0.05, 0.735, 0.895, 0.13])
    ax0.plot(f, A['Med'], color=ah.C1, lw=.6); ax0.plot(f, A['floor'] + A['tpl_st'], color=ah.C2, lw=.6, alpha=.8)
    ax0.set_xlim(f[0], f[-1]); ax0.set_xticklabels([]); ax0.set_ylabel('dB, median', fontsize=12); ax0.tick_params(labelsize=11)
    ax0.xaxis.set_major_locator(MultipleLocator(0.5)); ax0.xaxis.set_minor_locator(MultipleLocator(0.1))
    ax0.legend(['median over sweeps', 'floor + artifact model'], frameon=False, fontsize=11, loc='upper left', ncol=2)
    ax = fig.add_axes([0.05, 0.075, 0.895, 0.645])
    m, p = draw(ax, A, W, S, 'mean', np.nanpercentile(S, 0.5), np.nanpercentile(S, 99.95), cmap='magma', target_px=int(0.895 * 24 * 160))
    ax.set_xlabel('MHz', fontsize=14); ax.set_ylabel('seconds since start', fontsize=14); ax.tick_params(labelsize=12)
    ax.xaxis.set_major_locator(MultipleLocator(0.5)); ax.xaxis.set_minor_locator(MultipleLocator(0.1))
    cb = fig.colorbar(m, cax=fig.add_axes([0.952, 0.075, 0.01, 0.645])); cb.set_label(s['unit'], fontsize=12); cb.ax.tick_params(labelsize=11)
    fig.savefig(out / f'{tag}_waterfall_raw.png', dpi=160); plt.close(fig)


def zoom(A, W, out, tag, n=8, n_ev=4, half_khz=15.0):
    f, R, s = A['f'], W['R'], A['summary']
    evs = []                                          # one panel per emitter frequency, every burst of it marked
    for e in W['events']:
        same = next((v for v in evs if abs(v['f_mhz'] - e['f_mhz']) * 1e3 < 1.5), None)
        if same:
            same['spans'].append((e['t_start_s'], e['t_end_s'])); same['event']['peak_db'] = max(same['event']['peak_db'], e['peak_db'])
        elif len(evs) < n_ev:
            evs.append(dict(f_mhz=e['f_mhz'], hop=e['hop'], offset_khz=e['offset_khz'], event=dict(e), spans=[(e['t_start_s'], e['t_end_s'])]))
    cands = evs + [dict(c, event=None) for c in A['cand'][:n]]
    if not cands:
        return
    step_khz = A['step'] / 1e3; hb = int(round(half_khz / step_khz))
    cols = 4; rows = (len(cands) + cols - 1) // cols
    fig = plt.figure(figsize=(24, 6.2 * rows + 1.6), dpi=130)
    fig.text(0.03, 1 - 0.35 / (6.2 * rows + 1.6), f'{len(evs)} intermittent emitters (cyan) and the {len(cands) - len(evs)} strongest persistent lines at native resolution ({s["step_hz"]:.0f} Hz × one sweep)',
             fontsize=24, fontweight='bold', va='top')
    fig.text(0.03, 1 - 0.85 / (6.2 * rows + 1.6), f"residual, no smoothing, colour −2σ…+6σ (emissions: −2σ…+20σ) (σ = {W['sig']:.3f} dB) · right: level of the ±2 bins around the line per sweep "
             f"(thin) and its 5-sweep mean (thick); dashed = +3σ of a 5-bin average", fontsize=12, color='#8fa0bd', va='top')
    cm = colormaps[CMAP].copy(); cm.set_bad(MASKED)
    H = 6.2 * rows + 1.6; top = 1 - 1.5 / H; cellh = (top - 0.05) / rows
    if evs and len(evs) % cols:                       # persistent lines start on a fresh row
        cands = evs + [None] * (cols - len(evs) % cols) + cands[len(evs):]
    for q, c in enumerate(cands):
        if c is None:
            continue
        r_, k_ = divmod(q, cols)
        x0 = 0.035 + k_ * 0.243; y0 = top - (r_ + 1) * cellh + 0.06 / rows
        j = int(np.argmin(abs(f - c['f_mhz']))); a, b = max(0, j - hb), min(len(f), j + hb + 1)
        h = c['hop']; ts = W['ts'][:, h]; tmax = W['te'][-1, h]
        ax = fig.add_axes([x0, y0, 0.15, cellh - 0.11 / rows])
        ax.imshow(np.ma.masked_invalid(R[:, a:b]), aspect='auto', origin='lower', interpolation='nearest', cmap=cm,
                  vmin=-2 * W['sig'], vmax=(20 if c['event'] else 6) * W['sig'], extent=[(f[a] - c['f_mhz']) * 1e3 - step_khz / 2, (f[b - 1] - c['f_mhz']) * 1e3 + step_khz / 2, 0, tmax])
        ax.grid(False); ax.set_xlabel('kHz from line', fontsize=10); ax.tick_params(labelsize=9)
        if k_ == 0: ax.set_ylabel('seconds', fontsize=10)
        e = c['event']
        if e:
            ax.set_title(f"{c['f_mhz']:.4f} MHz  peak {e['peak_db']:+.1f} dB", fontsize=13, color=ah.C1)
            for t_a, t_b in c['spans']:
                ax.axhspan(t_a, t_b, xmin=0, xmax=0.02, color=ah.C1)
        else:
            ax.set_title(f"{c['f_mhz']:.4f} MHz  +{c['peak_excess_db']:.2f} dB", fontsize=13, color=ah.C2)
        ax.text(0.02, 0.985, (', '.join(f"{t_a:.0f}–{t_b:.0f} s" for t_a, t_b in sorted(c['spans'])) + f" · ≥{e['width_khz']:.1f} kHz" if e else f"{c['sweeps_detected_pct']:.0f}% of sweeps >3σ") + f" · hop {A['centers'][h]/1e6:.2f} {c['offset_khz']:+.0f} kHz",
                transform=ax.transAxes, fontsize=9, va='top', color='w', bbox=dict(fc='#000000aa', ec='none', pad=2))
        lin = 10 ** (R[:, max(0, j - 2):j + 3] / 10)
        lev = 10 * np.log10(np.nanmean(lin, 1)); run = np.convolve(np.nan_to_num(lev), np.ones(5) / 5, 'same')
        t2 = fig.add_axes([x0 + 0.162, y0, 0.07, cellh - 0.11 / rows])
        t2.plot(lev, ts + np.diff(np.append(ts, tmax)) / 2, color=ah.C1, lw=.6); t2.plot(run, ts + np.diff(np.append(ts, tmax)) / 2, color=ah.C2, lw=1.6)
        t2.axvline(0, color='#8fa0bd', lw=.6); t2.axvline(3 * W['sig'] / np.sqrt(5 / 1.5), color=ah.C3, lw=.8, ls='--')
        t2.set_ylim(0, tmax); t2.set_yticklabels([]); t2.tick_params(labelsize=9); t2.set_xlabel('dB', fontsize=10)
    fig.savefig(out / f'{tag}_waterfall_zoom.png', dpi=130); plt.close(fig)


# ---- interactive viewer -------------------------------------------------------------------------------
Q_LO, Q_STEP, Q_LIN = -2.0, 0.025, 240          # codes 0..239: -2.0 .. +3.975 dB in 0.025 dB; 240..254 above; 255 = blank


def quantize(R):
    hi = float(max(np.nanmax(R) if np.isfinite(R).any() else 4.0, 4.0))
    top_step = (hi - (Q_LO + Q_LIN * Q_STEP)) / 14 if hi > Q_LO + Q_LIN * Q_STEP else 1.0
    q = np.full(R.shape, 255, np.uint8); fin = np.isfinite(R)
    v = np.clip(R[fin], Q_LO, hi)
    lin = np.round((v - Q_LO) / Q_STEP)
    up = Q_LIN + np.round((v - (Q_LO + Q_LIN * Q_STEP)) / top_step)
    q[fin] = np.where(lin < Q_LIN, lin, np.minimum(up, 254)).astype(np.uint8)
    return q, top_step


def dequantize(q, top_step):
    q = q.astype(float)
    v = np.where(q < Q_LIN, Q_LO + q * Q_STEP, Q_LO + Q_LIN * Q_STEP + (q - Q_LIN) * top_step)
    v[q == 255] = np.nan
    return v


def _lut(name):
    return [int(round(x * 255)) for i in range(256) for x in colormaps[name](i / 255)[:3]]


def viewer(A, W, out, tag):
    q, top_step = quantize(W['R'])
    blob = base64.b64encode(gzip.compress(q.tobytes(), 9)).decode('ascii')
    s, f = A['summary'], A['f']
    meta = dict(title=f'Waterfall {f[0]:.1f}–{f[-1]:.1f} MHz', tag=tag, start=W['start'], rows=int(q.shape[0]), cols=int(q.shape[1]),
                f0=float(f[0]), step=float(A['step'] / 1e6), keep=int(W['keep']), nh=int(A['nh']),
                centers=[float(c) for c in A['centers'] / 1e6], qlo=Q_LO, qstep=Q_STEP, qlin=Q_LIN, qtop=float(top_step),
                T=[[round(float(x), 3) for x in row] for row in W['ts']], tend=round(float(W['te'][-1].max()), 3),
                sigma=round(W['sig'], 4), unit=s['unit'], span=round(s['span_s'], 1), records=s['records'],
                cands=[dict(f=round(c['f_mhz'], 5), db=round(c['peak_excess_db'], 2), pct=round(c['sweeps_detected_pct']), off=round(c['offset_khz']))
                       for c in A['cand']],
                events=[dict(f=round(e['f_mhz'], 5), db=round(e['peak_db'], 1), t0=round(e['t_start_s']), t1=round(e['t_end_s'])) for e in W['events']],
                luts={k: _lut(k) for k in ('inferno', 'viridis', 'magma', 'gray')}, centre_khz=CENTRE_KHZ)
    html = VIEWER.replace('__META__', json.dumps(meta, separators=(',', ':'))).replace('__DATA__', blob).replace('__TITLE__', meta['title'])
    (out / f'{tag}_waterfall.html').write_text(html, encoding='utf-8')
    return q, top_step


def write_events(W, out, tag):
    import csv
    with open(out / f'{tag}_events.csv', 'w', newline='') as fh:
        w = csv.writer(fh); w.writerow(['f_mhz', 'f_lo_mhz', 'f_hi_mhz', 'width_khz', 'peaks_mhz', 't_start_s', 't_end_s', 'sweeps', 'active_sweeps', 'peak_db', 'mean_db', 'hop', 'offset_khz'])
        for e in W['events']:
            w.writerow([f"{e['f_mhz']:.4f}", f"{e['f_lo_mhz']:.4f}", f"{e['f_hi_mhz']:.4f}", f"{e['width_khz']:.2f}", ' '.join(f'{x:.4f}' for x in e['peaks_mhz']),
                        f"{e['t_start_s']:.1f}", f"{e['t_end_s']:.1f}", e['sweeps'], e['active_sweeps'], f"{e['peak_db']:.2f}", f"{e['mean_db']:.2f}", e['hop'], f"{e['offset_khz']:+.1f}"])


def render(A, out, tag):
    W = A.get('W') or prepare(A)
    write_events(W, out, tag)
    hero(A, W, out, tag); raw(A, W, out, tag); zoom(A, W, out, tag); viewer(A, W, out, tag)
    return W


VIEWER = r'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{color-scheme:dark;--bg:#070b14;--panel:#0b1222;--fg:#c9d4e6;--mut:#8fa0bd;--line:#24324f;--acc:#ffb000;--c1:#00d1ff}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
body{display:flex;flex-direction:column}
header{padding:10px 16px 4px}h1{margin:0;font-size:20px}header p{margin:2px 0 0;color:var(--mut);font-size:12px}
#bar{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;padding:6px 16px;border-bottom:1px solid var(--line)}
#bar label{color:var(--mut);display:flex;gap:6px;align-items:center}
select,button,input{background:var(--panel);color:var(--fg);border:1px solid #2b3a55;border-radius:6px;padding:3px 7px;font:inherit}
button{cursor:pointer}button:hover{border-color:var(--c1)}input[type=range]{padding:0;width:110px;accent-color:var(--c1)}
#main{flex:1 1 0;display:grid;grid-template-columns:minmax(0,1fr) 190px;grid-template-rows:120px minmax(0,1fr) 26px;min-height:0;padding:6px 10px 0}
canvas{display:block;width:100%;height:100%;min-width:0;min-height:0}
#spec{grid-column:1;grid-row:1}#wf{grid-column:1;grid-row:2;cursor:crosshair;touch-action:none}#ax{grid-column:1;grid-row:3}
#tr{grid-column:2;grid-row:2}#side{grid-column:2;grid-row:1;padding:4px 0 0 10px;color:var(--mut);font-size:11px;overflow:hidden}
#read{padding:6px 16px;border-top:1px solid var(--line);font-variant-numeric:tabular-nums;min-height:30px;white-space:nowrap;overflow:auto}
#read b{color:var(--acc);font-weight:600}
footer{padding:4px 16px 10px;color:var(--mut);font-size:11px}
#msg{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;background:var(--bg);font-size:16px}
button,select{min-height:32px}
@media(max-width:700px){#main{grid-template-columns:minmax(0,1fr);grid-template-rows:90px minmax(0,1fr) 26px}#tr,#side{display:none}#read{white-space:normal;font-size:12px}header h1{font-size:17px}}
</style></head><body>
<div id="msg">decoding …</div>
<header><h1 id="h1"></h1><p id="sub"></p></header>
<div id="bar">
 <label>smooth f <select id="fb"><option>1</option><option selected>3</option><option>5</option><option>9</option><option>17</option></select> bins</label>
 <label>time <select id="tsw"><option>1</option><option>3</option><option selected>5</option><option>9</option><option>15</option></select> sweeps</label>
 <label>pixel <select id="pool"><option value="max" selected>max</option><option value="mean">mean</option></select></label>
 <label>low <input id="lo" type="range" min="-4" max="4" step="0.25" value="-2"><span id="lov"></span></label>
 <label>high <input id="hi" type="range" min="2" max="40" step="0.5" value="12"><span id="hiv"></span></label>
 <label>colours <select id="cm"><option>inferno</option><option>viridis</option><option>magma</option><option>gray</option></select></label>
 <label>jump <select id="cand"><option value="">candidates …</option></select></label>
 <button id="zin" title="zoom in (+)">＋</button><button id="zout" title="zoom out (−)">−</button><button id="reset" title="full band (double-click)">full band</button>
</div>
<div id="main"><canvas id="spec"></canvas><div id="side"></div><canvas id="wf"></canvas><canvas id="tr"></canvas><canvas id="ax"></canvas></div>
<div id="read">hover the waterfall · wheel or ＋/− to zoom · drag to pan · double-click for the full band</div>
<footer id="foot"></footer>
<script id="meta" type="application/json">__META__</script>
<script id="data" type="application/octet-stream">__DATA__</script>
<script>
"use strict";
const M=JSON.parse(document.getElementById('meta').textContent);
const R=M.rows,C=M.cols,G=46;            // G: left gutter for labels (css px)
let V,D,sig=M.sigma,x0=0,x1=C,hover=null,drag=null;
const $=id=>document.getElementById(id);
function dq(q){return q===255?NaN:(q<M.qlin?M.qlo+q*M.qstep:M.qlo+M.qlin*M.qstep+(q-M.qlin)*M.qtop)}
async function decode(){
  const s=atob(document.getElementById('data').textContent.trim()),n=s.length,b=new Uint8Array(n);
  for(let i=0;i<n;i++)b[i]=s.charCodeAt(i);
  if(!('DecompressionStream' in window))throw new Error('this browser lacks DecompressionStream (use a current Safari, Chrome or Firefox)');
  const buf=await new Response(new Blob([b]).stream().pipeThrough(new DecompressionStream('gzip'))).arrayBuffer();
  const q=new Uint8Array(buf),lut=new Float32Array(256);for(let i=0;i<256;i++)lut[i]=dq(i);
  V=new Float32Array(R*C);for(let i=0;i<q.length;i++)V[i]=lut[q[i]];
}
function smooth(fb,ts){                    // power average over fb bins x ts sweeps, blanks excluded
  if(fb===1&&ts===1){D=V;sig=M.sigma;return}
  const hf=fb>>1,ht=ts>>1,H=new Float32Array(R*C),Hw=new Float32Array(R*C),ps=new Float64Array(C+1),pw=new Float64Array(C+1);
  for(let r=0;r<R;r++){const o=r*C;ps[0]=pw[0]=0;
    for(let c=0;c<C;c++){const v=V[o+c],ok=v===v;ps[c+1]=ps[c]+(ok?Math.pow(10,v/10):0);pw[c+1]=pw[c]+(ok?1:0)}
    for(let c=0;c<C;c++){const a=Math.max(0,c-hf),b=Math.min(C,c+hf+1);H[o+c]=ps[b]-ps[a];Hw[o+c]=(pw[b]-pw[a])/(b-a)}}
  D=new Float32Array(R*C);
  for(let c=0;c<C;c++){
    for(let r=0;r<R;r++){let s=0,w=0;const a=Math.max(0,r-ht),b=Math.min(R,r+ht+1);
      for(let k=a;k<b;k++){s+=H[k*C+c];w+=Hw[k*C+c]}
      const cnt=b-a;D[r*C+c]=(w/cnt>0.5)?10*Math.log10(s/(w*(Math.min(C,c+hf+1)-Math.max(0,c-hf))/1)):NaN}}
  const smp=[];for(let i=0;i<D.length;i+=97){const v=D[i];if(v===v)smp.push(v)}
  smp.sort((a,b)=>a-b);const med=smp[smp.length>>1],dev=smp.map(v=>Math.abs(v-med)).sort((a,b)=>a-b);sig=1.4826*dev[dev.length>>1];
}
const fOf=c=>M.f0+c*M.step, hopOf=c=>M.hopcol?M.hopcol[c]:Math.min(M.nh-1,Math.floor(c/M.keep));
function offKhz(c){const h=hopOf(c);return (fOf(c)-M.centers[h])*1e3}
function cv(id){const e=$(id),r=e.getBoundingClientRect(),d=window.devicePixelRatio||1;e.width=Math.max(1,Math.round(r.width*d));e.height=Math.max(1,Math.round(r.height*d));
  const g=e.getContext('2d');g.setTransform(d,0,0,d,0,0);return [g,r.width,r.height]}
function colAt(px,w){return x0+(px-G)*(x1-x0)/(w-G)}
function agg(c0,c1,r,how){let m=-Infinity,s=0,n=0;for(let c=c0;c<c1;c++){const v=D[r*C+c];if(v===v){if(v>m)m=v;s+=Math.pow(10,v/10);n++}}
  return n?(how==='max'?m:10*Math.log10(s/n)):NaN}
function niceStep(span,px,minpx){const raw=span*minpx/px,e=Math.pow(10,Math.floor(Math.log10(raw)));for(const m of[1,2,5,10])if(m*e>=raw)return m*e;return 10*e}
let lutRGB=null;
let LO=0,HI=1,PS=1;
function pixstats(colv){const s=[];for(let i=0;i<colv.length;i+=7){const v=colv[i];if(v===v)s.push(v)}if(!s.length)return[0,sig];
  s.sort((a,b)=>a-b);const m=s[s.length>>1],d=s.map(v=>Math.abs(v-m)).sort((a,b)=>a-b);return[m,Math.max(1e-3,1.4826*d[d.length>>1])]}
function draw(){
  const how=$('pool').value;lutRGB=M.luts[$('cm').value];
  const [g,w,h]=cv('wf'),pw=Math.max(1,Math.round((w-G)*(window.devicePixelRatio||1)));
  const img=new ImageData(pw,R),px=img.data,colv=new Float32Array(pw*R);
  for(let p=0;p<pw;p++){const c0=Math.max(0,Math.floor(x0+p*(x1-x0)/pw)),c1=Math.min(C,Math.max(c0+1,Math.floor(x0+(p+1)*(x1-x0)/pw)));
    for(let r=0;r<R;r++)colv[r*pw+p]=agg(c0,c1,r,how)}
  const [pm,ps]=pixstats(colv);PS=ps;const lo=pm+(+$('lo').value)*ps,hi=pm+(+$('hi').value)*ps;LO=lo;HI=hi;
  $('lov').textContent=(lo>=0?'+':'')+lo.toFixed(2)+' dB';$('hiv').textContent=(hi>=0?'+':'')+hi.toFixed(2)+' dB';
  for(let p=0;p<pw;p++)for(let r=0;r<R;r++){const v=colv[r*pw+p],i=((R-1-r)*pw+p)*4;
      if(v!==v){px[i]=58;px[i+1]=66;px[i+2]=86}else{const k=Math.max(0,Math.min(255,Math.round((v-lo)/(hi-lo)*255)))*3;px[i]=lutRGB[k];px[i+1]=lutRGB[k+1];px[i+2]=lutRGB[k+2]}
      px[i+3]=255}
  const off=document.createElement('canvas');off.width=pw;off.height=R;off.getContext('2d').putImageData(img,0,0);
  g.fillStyle='#070b14';g.fillRect(0,0,w,h);g.imageSmoothingEnabled=false;g.drawImage(off,G,0,w-G,h);
  g.fillStyle='#8fa0bd';g.font='11px sans-serif';g.textAlign='right';
  const tstep=niceStep(M.tend,h,40);for(let t=0;t<=M.tend;t+=tstep){const y=h-(t/M.tend)*h;g.fillText(t.toFixed(0)+' s',G-4,Math.max(10,y+4));g.fillRect(G-2,y,2,1)}
  for(const cc of M.centers){const c=(cc-M.f0)/M.step;if(c>=x0&&c<=x1){const x=G+(c-x0)/(x1-x0)*(w-G);g.fillStyle='#ffb000';g.beginPath();g.moveTo(x-4,0);g.lineTo(x+4,0);g.lineTo(x,7);g.fill()}}
  for(const tk of (M.tracks||[])){g.strokeStyle=tk.kind==='doppler'?'#00d1ff':'#ff4d6d';g.lineWidth=1.2;g.beginPath();let pen=false;
    for(let i=0;i<tk.f.length;i++){const c=(tk.f[i]-M.f0)/M.step;if(c<x0||c>x1){pen=false;continue}const x=G+(c-x0)/(x1-x0)*(w-G),y=h-(tk.r[i]+.5)/R*h;if(pen)g.lineTo(x,y);else g.moveTo(x,y);pen=true}g.stroke();
    const c0=(tk.f[0]-M.f0)/M.step;if(c0>=x0&&c0<=x1){g.fillStyle=g.strokeStyle;g.font='11px sans-serif';g.textAlign='left';g.fillText(tk.label,G+(c0-x0)/(x1-x0)*(w-G)+5,h-(tk.r[0]+.5)/R*h+12)}}
  g.lineWidth=1;
  if(hover){const x=G+(hover.c-x0)/(x1-x0)*(w-G);g.strokeStyle='rgba(255,255,255,.35)';g.beginPath();g.moveTo(x+.5,0);g.lineTo(x+.5,h);g.stroke()}
  drawSpec(colv,pw,how);drawAx();drawTrace(colv,pw);
}
function drawSpec(colv,pw,how){
  const [g,w,h]=cv('spec');g.fillStyle='#0b1222';g.fillRect(G,0,w-G,h);
  const mean=new Float32Array(pw),mx=new Float32Array(pw);let ymin=-0.3,ymax=0.6;
  for(let p=0;p<pw;p++){let s=0,n=0,m=-Infinity;for(let r=0;r<R;r++){const v=colv[r*pw+p];if(v===v){s+=Math.pow(10,v/10);n++;if(v>m)m=v}}
    mean[p]=n?10*Math.log10(s/n):NaN;mx[p]=n?m:NaN;if(mean[p]>ymax)ymax=mean[p]}
  ymax+=0.25;const X=p=>G+p*(w-G)/pw,Y=v=>h-14-(v-ymin)/(ymax-ymin)*(h-20);
  g.strokeStyle='#24324f';g.fillStyle='#8fa0bd';g.font='10px sans-serif';g.textAlign='right';
  const st=niceStep(ymax-ymin,h,28);for(let v=Math.ceil(ymin/st)*st;v<=ymax;v+=st){g.beginPath();g.moveTo(G,Y(v)+.5);g.lineTo(w,Y(v)+.5);g.stroke();g.fillText((v>0?'+':'')+v.toFixed(st<0.1?2:1),G-4,Y(v)+3)}
  g.strokeStyle='#7CFC9A';g.lineWidth=1;g.beginPath();let pen=false;
  for(let p=0;p<pw;p++){const v=mean[p];if(v!==v){pen=false;continue}const x=X(p),y=Y(v);if(pen)g.lineTo(x,y);else g.moveTo(x,y);pen=true}g.stroke();
  g.fillStyle='#ffb000';g.textAlign='center';
  for(const k of M.cands){const c=(k.f-M.f0)/M.step;if(c<x0||c>x1)continue;const x=G+(c-x0)/(x1-x0)*(w-G);g.beginPath();g.moveTo(x-4,4);g.lineTo(x+4,4);g.lineTo(x,11);g.fill();
    if((x1-x0)<C/3)g.fillText(k.f.toFixed(4),x,24)}
  g.fillStyle='#00d1ff';for(const k of M.events){const c=(k.f-M.f0)/M.step;if(c<x0||c>x1)continue;const x=G+(c-x0)/(x1-x0)*(w-G);g.beginPath();g.moveTo(x-4,h-14);g.lineTo(x+4,h-14);g.lineTo(x,h-21);g.fill()}
  g.textAlign='left';g.fillStyle='#8fa0bd';g.fillText('mean over the run, dB above floor ('+$('fb').value+'×'+$('tsw').value+', '+how+' per pixel)',G+6,h-3);
}
function drawAx(){
  const [g,w,h]=cv('ax');g.fillStyle='#8fa0bd';g.font='11px sans-serif';g.textAlign='center';
  const f0=fOf(x0),f1=fOf(x1),st=niceStep(f1-f0,w-G,90),dec=Math.max(0,-Math.floor(Math.log10(st)));
  for(let f=Math.ceil(f0/st)*st;f<=f1;f+=st){const x=G+(f-f0)/(f1-f0)*(w-G);g.fillRect(x,0,1,5);g.fillText(f.toFixed(dec)+(dec<1?'':''),x,17)}
  g.textAlign='left';g.fillText('MHz',4,17);
}
function drawTrace(colv,pw){
  const [g,w,h]=cv('tr');g.fillStyle='#0b1222';g.fillRect(0,0,w,h);
  let lo=LO,hi=HI;if(hover){const p=Math.max(0,Math.min(pw-1,hover.p));for(let r=0;r<R;r++){const v=colv[r*pw+p];if(v===v){if(v>hi)hi=v;if(v<lo)lo=v}}}
  const X=v=>8+(v-lo)/(hi-lo)*(w-16);
  g.strokeStyle='#24324f';g.beginPath();g.moveTo(X(0)+.5,0);g.lineTo(X(0)+.5,h);g.stroke();
  g.fillStyle='#8fa0bd';g.font='10px sans-serif';g.fillText('level vs time at cursor',8,12);
  if(!hover)return;const p=Math.max(0,Math.min(pw-1,hover.p));
  g.strokeStyle='#00d1ff';g.beginPath();let pen=false;
  for(let r=0;r<R;r++){const v=colv[r*pw+p];if(v!==v){pen=false;continue}const x=X(v),y=h-(r+.5)/R*h;if(pen)g.lineTo(x,y);else g.moveTo(x,y);pen=true}g.stroke();
  g.fillStyle='#8fa0bd';g.fillText(lo.toFixed(2),4,h-4);g.textAlign='right';g.fillText((hi>=0?'+':'')+hi.toFixed(2)+' dB',w-4,h-4);g.textAlign='left';
}
function readout(e){
  const r=$('wf').getBoundingClientRect(),xs=e.clientX-r.left,ys=e.clientY-r.top;if(xs<G)return;
  const c=Math.max(0,Math.min(C-1,Math.floor(colAt(xs,r.width)))),row=Math.max(0,Math.min(R-1,Math.floor((1-ys/r.height)*R)));
  const pw=Math.max(1,Math.round((r.width-G)*(window.devicePixelRatio||1)));hover={c,p:Math.floor((xs-G)/(r.width-G)*pw)};
  const h=hopOf(c),v=D[row*C+c],t=M.T[row][h];
  let s=0,n=0;for(let k=0;k<R;k++){const u=D[k*C+c];if(u===u){s+=Math.pow(10,u/10);n++}}const mean=n?10*Math.log10(s/n):NaN;
  const fmt=x=>x!==x?'blank':(x>=0?'+':'')+x.toFixed(2)+' dB';
  $('read').innerHTML=`<b>${fOf(c).toFixed(5)} MHz</b> · t <b>${t.toFixed(1)} s</b> (sweep ${row+1}/${R}, hop ${h+1} at ${M.centers[h].toFixed(2)} MHz, ${offKhz(c)>=0?'+':''}${offKhz(c).toFixed(1)} kHz) · value <b>${fmt(v)}</b>${v===v?' ('+(v/sig).toFixed(1)+'σ)':''} · run mean ${fmt(mean)}`;
  draw();
}
function zoomAt(c,fac){const span=Math.max(40,Math.min(C,(x1-x0)*fac));x0=Math.max(0,Math.min(C-span,c-(c-x0)*span/(x1-x0)));x1=x0+span;draw()}
function setup(){
  $('h1').textContent=M.title+' · interactive waterfall';
  $('sub').textContent=`${R} sweeps × ${M.nh} hops · start ${M.start} · ${M.span} s · native ${(M.step*1e6).toFixed(0)} Hz bins · residual after receiver-artifact and floor removal, each record levelled · native σ ${M.sigma.toFixed(3)} dB`;
  $('foot').textContent=`Levels ${M.unit}, uncalibrated and relative to the median floor. Grey = blank: ±${M.centre_khz} kHz around each hop centre (▼). Rows are sweeps; the hover time is the measured start of that hop. Orange ▼ persistent lines, cyan ▲ intermittent emissions. Whether a line is a real carrier or a receiver spur is not decided by this data. Data quantised to 0.025 dB.`;
  const grp=(label,items,txt)=>{if(!items.length)return;const g=document.createElement('optgroup');g.label=label;for(const k of items){const o=document.createElement('option');o.value=k.f;o.textContent=txt(k);g.appendChild(o)}$('cand').appendChild(g)};
  grp('Doppler / drift tracks',(M.tracks||[]).map(t=>({f:t.f[Math.floor(t.f.length/2)],label:t.label})),k=>k.label);
  grp('intermittent emissions',M.events,k=>`${k.f.toFixed(4)} MHz ${k.db>=0?'+':''}${k.db.toFixed(1)} dB · ${k.t0}–${k.t1} s`);
  grp('persistent lines',M.cands,k=>`${k.f.toFixed(4)} MHz +${k.db.toFixed(2)} dB (${k.pct}%)`);
  $('side').innerHTML=`<div>${M.events.length} intermittent emissions</div><div style="margin-top:4px">${M.cands.length} persistent lines ≥ 0.5 dB</div><div style="margin-top:4px">${M.records} records</div><div style="margin-top:4px">colour range = displayed pixels' median + low…high × their spread</div>`;
  const re=()=>{smooth(+$('fb').value,+$('tsw').value);draw()};
  for(const id of['fb','tsw'])$(id).onchange=re;for(const id of['pool','cm'])$(id).onchange=draw;for(const id of['lo','hi'])$(id).oninput=draw;
  $('cand').onchange=e=>{if(!e.target.value)return;const c=(+e.target.value-M.f0)/M.step,span=Math.round(25/(M.step*1e3))*2;x0=Math.max(0,c-span/2);x1=Math.min(C,x0+span);draw()};
  $('reset').onclick=()=>{x0=0;x1=C;draw()};$('zin').onclick=()=>zoomAt((x0+x1)/2,0.5);$('zout').onclick=()=>zoomAt((x0+x1)/2,2);
  const wf=$('wf');
  for(const id of['wf','spec'])$(id).addEventListener('wheel',e=>{e.preventDefault();const r=$(id).getBoundingClientRect();zoomAt(colAt(e.clientX-r.left,r.width),Math.pow(1.0015,e.deltaY))},{passive:false});
  wf.addEventListener('pointerdown',e=>{drag={x:e.clientX,a:x0,b:x1};wf.setPointerCapture(e.pointerId)});
  wf.addEventListener('pointermove',e=>{if(drag){const r=wf.getBoundingClientRect(),dc=(e.clientX-drag.x)*(drag.b-drag.a)/(r.width-G);x0=Math.max(0,Math.min(C-(drag.b-drag.a),drag.a-dc));x1=x0+(drag.b-drag.a);draw()}else readout(e)});
  wf.addEventListener('pointerup',()=>drag=null);wf.addEventListener('dblclick',()=>{x0=0;x1=C;draw()});
  wf.addEventListener('pointerleave',()=>{if(!drag){hover=null;draw()}});
  window.addEventListener('keydown',e=>{if(e.key==='+'||e.key==='=')zoomAt((x0+x1)/2,0.5);else if(e.key==='-')zoomAt((x0+x1)/2,2);
    else if(e.key==='ArrowLeft'){const s=(x1-x0)*0.2;x0=Math.max(0,x0-s);x1=x0+Math.min(C,(x1-x0));draw()}else if(e.key==='ArrowRight'){const sp=x1-x0;x0=Math.min(C-sp,x0+sp*0.2);x1=x0+sp;draw()}});
  window.addEventListener('resize',draw);
  re();
}
decode().then(()=>{$('msg').remove();setup()}).catch(e=>{$('msg').textContent='Could not decode the embedded data: '+e.message});
</script></body></html>'''


def main():
    a = [x for x in sys.argv[1:] if not x.startswith('--')]
    if len(a) < 2:
        print(__doc__); return 2
    out = Path(a[1]); out.mkdir(parents=True, exist_ok=True)
    tag = sys.argv[sys.argv.index('--tag') + 1] if '--tag' in sys.argv else 'hopscan'
    a = [x for x in a if x != tag]
    W = render(ah.analyse(a[0], out, tag), out, tag)
    print(json.dumps(dict(sigma_native_db=W['sig'], sigma_enhanced_db=W['sig_e'], record_level_sd_db=float(np.nanstd(W['blk'])))))
    return 0


if __name__ == '__main__':
    sys.exit(main())
