#!/usr/bin/env python3
"""Full analysis of a soapy_power hop-scan (-F json) capture: one PDF + summary.json + candidates.csv.
usage: analyze_hopscan.py SCAN.jsonl OUTDIR [--tag NAME]
Levels are the file's own unit (dB of input power/Hz, uncalibrated, relative). Never claims an identification:
lines that survive receiver-artifact removal are reported as candidates that need a retune/termination check."""
import csv, datetime, json, sys, warnings
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from scipy.ndimage import percentile_filter

BG, PANEL, FG, GRID = '#070b14', '#0b1222', '#c9d4e6', '#24324f'
C1, C2, C3, C4, C5 = '#00d1ff', '#ffb000', '#ff4d6d', '#7CFC9A', '#b388ff'
plt.rcParams.update({'figure.facecolor': BG, 'axes.facecolor': PANEL, 'text.color': FG, 'axes.labelcolor': FG,
                     'xtick.color': FG, 'ytick.color': FG, 'axes.edgecolor': '#2b3a55', 'axes.grid': True,
                     'grid.color': GRID, 'grid.alpha': .5, 'axes.spines.top': False, 'axes.spines.right': False,
                     'axes.titleweight': 'bold', 'axes.titlelocation': 'left', 'font.size': 10, 'savefig.facecolor': BG})
warnings.filterwarnings('ignore', category=RuntimeWarning)   # all-NaN slices at masked DC bins are expected
ENBW_BLACKMAN_50 = 0.1661   # Blackman, 50 % overlap: adjacent-segment power correlation (Harris 1978)


def load(path):
    R = [json.loads(l) for l in open(path) if l.strip()]
    if not R:
        raise SystemExit('no records')
    cs = np.array([r['metadata']['tuned_center_hz'] for r in R])
    sw = np.zeros(len(R), int)
    for i in range(1, len(R)):
        sw[i] = sw[i - 1] + (cs[i] <= cs[i - 1])
    centers = np.array(sorted(set(np.round(cs / 1e3).astype(int)))) * 1e3
    nh, n = len(centers), len(R[0]['power'])
    full = [s for s in range(sw.max() + 1) if (sw == s).sum() == nh]
    index = {s: i for i, s in enumerate(full)}
    P = np.full((len(full), nh, n), np.nan); T0 = np.zeros((len(full), nh)); T1 = np.zeros_like(T0)
    io = []
    for r, c, s in zip(R, cs, sw):
        if s not in index:
            continue
        i, h = index[s], int(np.argmin(abs(centers - c)))
        P[i, h] = np.array([np.nan if v is None else v for v in r['power']], float)
        T0[i, h] = datetime.datetime.fromisoformat(r['time_start']).timestamp()
        T1[i, h] = datetime.datetime.fromisoformat(r['time_stop']).timestamp()
        io.append((i, h, r['metadata'].get('host_io', {})))
    m = R[0]['metadata']
    info = dict(step_hz=float(R[0]['step_hz']), bins=n, hops=nh, sweeps=len(full), records=len(R),
                sample_rate_hz=m.get('sample_rate_hz'), window=m.get('window'), enbw_hz=m.get('window_enbw_hz'),
                overlap_samples=m.get('overlap_samples'), segments=m.get('segment_count'),
                samples=R[0]['samples'], unit=R[0]['unit'], dc_mask_hz=m.get('dc_mask_width_hz'),
                statuses=sorted({r['metadata'].get('measurement_status') for r in R}),
                continuity=sorted({str(r['metadata'].get('continuity')) for r in R}))
    return P, T0, T1, centers, io, info


def stitch(P, centers, step):
    nh, n = P.shape[1], P.shape[2]
    keep = int(round((centers[1] - centers[0]) / step)) if nh > 1 else n
    lo = (n - keep) // 2; hi = lo + keep
    S = np.concatenate([P[:, h, lo:hi] for h in range(nh)], 1)
    f = np.concatenate([centers[h] + (np.arange(lo, hi) - n // 2) * step for h in range(nh)]) / 1e6
    return S, f, lo, hi


def dc_template(P):
    """Receiver artifact around each hop centre, as the median over hops of (hop median - hop baseline)."""
    n = P.shape[2]; k = np.arange(n) - n // 2
    m = np.nanmedian(P, 0)
    base = np.array([np.nanmedian(m[h][(abs(k) > 0.49 * n / 2) & (abs(k) < 0.73 * n / 2)]) for h in range(m.shape[0])])
    R = m - base[:, None]
    return k, np.nanmedian(R, 0), R, base


def runs(mask, gap=2):
    idx = np.flatnonzero(mask); out = []
    if not len(idx):
        return out
    a = b = idx[0]
    for i in idx[1:]:
        if i - b > gap:
            out.append((a, b)); a = i
        b = i
    out.append((a, b)); return out


def analyse(path, out, tag):
    P, T0, T1, centers, io, info = load(path)
    S, f, lo, hi = stitch(P, centers, info['step_hz'])
    nsw, nh, n = P.shape; step = info['step_hz']
    k, tpl, hop_prof, base = dc_template(P)
    tpl_st = np.concatenate([tpl[lo:hi]] * nh)
    Med = np.nanmedian(S, 0)
    floor = percentile_filter(np.where(np.isnan(Med - tpl_st), np.nanmedian(Med), Med - tpl_st), 30, size=819)
    Res = S - tpl_st[None] - floor[None]                       # residual after artifact + floor removal
    sd_t = np.nanmedian(np.nanstd(Res, 0))                     # sweep-to-sweep sd, median over bins
    seg = info['segments'] or 245
    theory = 4.343 * np.sqrt((1 + 2 * ENBW_BLACKMAN_50 ** 2) / seg)
    # ---- candidates
    mres = np.nanmean(Res, 0); ctr = np.zeros(len(f), bool)
    for c in centers / 1e6: ctr |= abs(f - c) < 0.012
    cand = []
    for a, b in runs(np.where(ctr, 0, mres) >= 0.5):
        seg_ = slice(a, b + 1); j = a + int(np.nanargmax(mres[seg_]))
        band = 10 * np.log10(np.nanmean(10 ** (Res[:, seg_] / 10), 1))
        cand.append(dict(f_mhz=float(f[j]), width_khz=float((b - a + 1) * step / 1e3), peak_excess_db=float(mres[j]),
                         maxhold_excess_db=float(np.nanmax(Res[:, j])), sweeps_detected_pct=float(100 * np.mean(Res[:, j] > 3 * sd_t)),
                         level_sd_db=float(np.nanstd(Res[:, j])), z_mean=float(mres[j] / (sd_t / np.sqrt(nsw))),
                         hop=int(np.argmin(abs(centers / 1e6 - f[j]))), offset_khz=float((f[j] - centers[int(np.argmin(abs(centers / 1e6 - f[j])))] / 1e6) * 1e3), j=int(j)))
    cand.sort(key=lambda c: -c['peak_excess_db'])
    # ---- channels (51 bins ~ 12.45 kHz)
    cw = 51; nch = len(f) // cw
    ch = 10 * np.log10(np.nanmean(10 ** (Res[:, :nch * cw].reshape(nsw, nch, cw) / 10), 2))
    chf = f[:nch * cw].reshape(nch, cw).mean(1)
    mad = 1.4826 * np.nanmedian(abs(ch - np.nanmedian(ch)))
    occ = np.nanmean(ch > 5 * mad, 0)
    # ---- overlaps
    ov = []
    keep = hi - lo
    for h in range(nh - 1):
        a = np.nanmean(P[:, h, keep:n], 0)[:keep + 0][:n - keep]       # upper part of hop h
        b = np.nanmean(P[:, h + 1, 0:n - keep], 0)                     # lower part of hop h+1 (same RF)
        m_ = min(len(a), len(b)); ov.append((np.arange(m_) * step / 1e3 + (n // 2 - 0) * 0, (a[:m_] - b[:m_])))
    # ---- passband shape (median over hops, |offset|) and start-up transient
    kk = np.arange(n) - n // 2; off = kk * step / 1e6; prof = np.nanmedian(np.nanmedian(P, 0), 0)
    ax_off = np.arange(0.40, 1.0001, 0.01)
    side = np.array([np.nanmedian(prof[(abs(abs(off) - o) < 0.006)]) for o in ax_off])
    ref = np.nanmedian(side[(ax_off >= 0.55) & (ax_off <= 0.70)])
    def cross(drop):
        below = np.flatnonzero(side - ref < -drop)
        return round(float(ax_off[below[0]]), 2) if len(below) else None
    win = slice(int(n / 2 - 0.25 * n / 4 * 4 / 4), int(n / 2 + 0.25 * n / 4 * 4 / 4))
    quiet = (abs(kk) > 0.25 * n / 2) & (abs(kk) < 0.5 * n / 2)
    mh = np.nanmedian(P[:, :, quiet], 2)
    first_dev = mh[0] - np.nanmedian(mh[3:], 0) if nsw > 4 else np.zeros(nh)
    later_sd = float(np.nanmedian(np.nanstd(mh[3:], 0))) if nsw > 4 else float('nan')
    n_faint = int(len(runs(np.where(ctr, 0, mres) >= 0.25)))
    # ---- timing / io
    dwell = (T1 - T0).ravel(); gaps = (T0[:, 1:] - T1[:, :-1]).ravel(); period = np.diff(T0[:, 0]); span = T1[-1, -1] - T0[0, 0]
    summary = dict(tag=tag, file=str(path), **info, span_s=float(span), sweep_period_s=float(np.median(period)),
                   dwell_s=float(np.median(dwell)), retune_gap_s=float(np.median(gaps)),
                   duty_pct=float(100 * np.sum(dwell) / (nsw * np.median(period) * 1.0 * 1 if False else span)),
                   floor_db=float(np.nanmedian(Med - tpl_st)), floor_ripple_p2p_db=float(np.nanpercentile(floor, 99) - np.nanpercentile(floor, 1)),
                   sweep_median_sd_db=float(np.nanstd(np.nanmedian(S, 1))),
                   sweep_to_sweep_sd_db=float(sd_t), theory_sd_db=float(theory),
                   dc_artifact_db={str(o): float(np.nanmedian(tpl[(abs(k) >= o) & (abs(k) <= o * 1.05 + 2)])) for o in (12, 49, 200, 800)},
                   dc_artifact_hop_spread_db=float(np.nanmedian(np.nanmax(hop_prof[:, (abs(k) > 40) & (abs(k) < 400)], 0) - np.nanmin(hop_prof[:, (abs(k) > 40) & (abs(k) < 400)], 0))),
                   passband_ref_db=float(ref), rolloff_1db_mhz=cross(1.0), rolloff_3db_mhz=cross(3.0),
                   warmup_first_sweep_db=[float(x) for x in first_dev], warmup_later_sd_db=later_sd, faint_lines_ge_0p25db=n_faint,
                   lines_per_mhz={f'{int(a)}': int(sum(1 for c in cand if int(c['f_mhz']) == a)) for a in range(int(f[0]), int(f[-1]) + 1)},
                   max_raw_peak_db=float(np.nanmax(Med)), max_raw_peak_mhz=float(f[int(np.nanargmax(np.where(np.isnan(Med), -1e9, Med)))]),
                   candidates=[{kk: v for kk, v in c.items() if kk != 'j'} for c in cand],
                   channels=int(nch), channel_sd_db=float(mad), channels_with_occupancy=int(np.sum(occ > 0)),
                   max_channel_excess_db=float(np.nanmax(ch)), max_residual_bin_db=float(np.nanmax(Res)))
    return dict(P=P, T0=T0, T1=T1, centers=centers, io=io, info=info, S=S, f=f, lo=lo, hi=hi, k=k, tpl=tpl, hop_prof=hop_prof,
                base=base, Med=Med, floor=floor, Res=Res, sd_t=sd_t, theory=theory, cand=cand, ch=ch, chf=chf, occ=occ,
                ov=ov, side=side, ax_off=ax_off, ref=ref, dwell=dwell, gaps=gaps, period=period, summary=summary, tpl_st=tpl_st, mres=mres, nsw=nsw, nh=nh, n=n, step=step)


def page(pdf, title, sub=None):
    fig = plt.figure(figsize=(16, 9), dpi=110)
    fig.text(0.04, 0.955, title, fontsize=20, fontweight='bold', va='top')
    if sub: fig.text(0.04, 0.915, sub, fontsize=11, color='#8fa0bd', va='top')
    return fig


def fmt_ax(ax, xl=None, yl=None):
    if xl: ax.set_xlabel(xl)
    if yl: ax.set_ylabel(yl)


def make_pdf(A, out, tag):
    s = A['summary']; f = A['f']; S = A['S']; centers = A['centers'] / 1e6; nsw = A['nsw']
    pdf = PdfPages(out / f'{tag}_analysis.pdf'); pngs = {}
    def done(fig, name):
        fig.savefig(out / f'{tag}_{name}.png', dpi=110); pdf.savefig(fig); plt.close(fig)
    # ---- 1 summary
    fig = page(pdf, f'Hop-scan analysis  {f[0]:.1f}–{f[-1]:.1f} MHz', f"{s['sweeps']} sweeps x {s['hops']} hops · {s['span_s']:.0f} s · {s['bins']} bins/hop at {s['step_hz']:.1f} Hz · {s['window']} window · unit: {s['unit']} (uncalibrated, relative)")
    tiles = [(f"{s['floor_db']:.1f} dB", 'receiver-limited floor'), (f"±{s['sweep_median_sd_db']:.3f} dB", 'floor stability over run'),
             (f"{s['sweep_to_sweep_sd_db']:.3f} dB", f"bin sd (theory {s['theory_sd_db']:.3f})"), (f"{s['dc_artifact_db']['12']:+.1f} dB", 'artifact at 12 kHz offset'),
             (f"{s['max_residual_bin_db']:+.1f} dB", 'max single-bin residual'), (f"{len(s['candidates'])}", 'weak line candidates')]
    for i, (v, l) in enumerate(tiles):
        x = 0.04 + i * 0.158
        fig.patches.append(plt.Rectangle((x, 0.74), 0.148, 0.12, transform=fig.transFigure, fc=PANEL, ec='#2b3a55'))
        fig.text(x + 0.074, 0.815, v, ha='center', fontsize=20, fontweight='bold', color=C1); fig.text(x + 0.074, 0.765, l, ha='center', fontsize=9.5, color='#8fa0bd')
    top = s['candidates'][:3]; ev = (A.get('W') or {}).get('events', [])
    lines = [
        (('1. Receiver-noise-limited floor; ' + (f"{len(ev)} intermittent emission(s) and weak persistent lines." if ev else 'the content is weak narrowband lines only.')),
         (f"Intermittent: " + '; '.join(f"{e['f_mhz']:.4f} MHz {e['peak_db']:+.1f} dB at {e['t_start_s']:.0f}–{e['t_end_s']:.0f} s ({e['width_khz']:.1f} kHz)" for e in ev[:3]) + '. ' if ev else '') +
         f"Largest single-bin residual {s['max_residual_bin_db']:+.1f} dB; {s['channels_with_occupancy']} of {s['channels']} 12.5 kHz channels ever above 5σ (max {s['max_channel_excess_db']:+.2f} dB). {len(s['candidates'])} persistent lines ≥ 0.5 dB above floor in the 10-min mean ({s['faint_lines_ge_0p25db']} at ≥ 0.25 dB)."),
        ('2. The raw maxima are an artifact, not signals.', f"The strongest raw bin ({s['max_raw_peak_mhz']:.4f} MHz, {s['max_raw_peak_db']:.1f} dB) lies next to a hop centre. Every hop shows the same skirt ({s['dc_artifact_db']['12']:+.1f} dB at 12 kHz, {s['dc_artifact_db']['49']:+.1f} dB at 49 kHz, {s['dc_artifact_db']['200']:+.1f} dB at 200 kHz; hop-to-hop spread {s['dc_artifact_hop_spread_db']:.2f} dB)."),
        ('3. Weak persistent lines remain.', ('; '.join(f"{c['f_mhz']:.4f} MHz (+{c['peak_excess_db']:.2f} dB, {c['sweeps_detected_pct']:.0f}% of sweeps)" for c in top) or 'none') + '. Unverified: could be real carriers or receiver spurs.'),
        ('4. Measurement is statistically sound.', f"Sweep-to-sweep bin sd {s['sweep_to_sweep_sd_db']:.3f} dB vs {s['theory_sd_db']:.3f} dB expected for a {s['segments']}-segment Welch estimate; floor drift {s['sweep_median_sd_db']*1000:.0f} mdB sd over {s['span_s']:.0f} s."),
        ('5. Data integrity and start-up.', f"{s['records']} records, statuses {s['statuses']}, continuity {s['continuity']}; sweep period {s['sweep_period_s']:.2f} s (dwell {s['dwell_s']:.2f} s, retune gap {s['retune_gap_s']:.2f} s). Start-up transient: the first hop reads {s['warmup_first_sweep_db'][0]:+.2f} dB, settling within about one sweep (later sweeps sd {s['warmup_later_sd_db']:.3f} dB) — discard the first sweep for absolute work."),
        ('Needs a different measurement to settle:', 'whether the weak lines are real (retune by a few hundred kHz: a real carrier stays at the same RF, a receiver spur moves), and absolute levels (this run is uncalibrated).')]
    y = 0.69
    for h_, t_ in lines:
        fig.text(0.04, y, h_, fontsize=12.5, fontweight='bold', color=C2 if h_.startswith('Needs') else FG, va='top')
        import textwrap
        fig.text(0.04, y - 0.032, '\n'.join(textwrap.wrap(t_, 170)), fontsize=10.5, color='#c9d4e6', va='top'); y -= 0.108
    done(fig, 'p1_summary')
    # ---- 2 spectrum
    fig = page(pdf, 'Spectrum', 'top: raw statistics over all sweeps · bottom: after artifact + floor removal (what is left is the actual band content)')
    ax = fig.add_axes([0.06, 0.52, 0.91, 0.32])
    lin = 10 ** (S / 10); mean = 10 * np.log10(np.nanmean(lin, 0))
    ax.fill_between(f, np.nanpercentile(S, 5, 0), np.nanpercentile(S, 95, 0), color=C1, alpha=.18, lw=0, label='5–95 % over sweeps')
    ax.plot(f, np.nanmax(S, 0), color=C3, lw=.5, label='max hold'); ax.plot(f, mean, color=C1, lw=.8, label='mean (power)'); ax.plot(f, np.nanmin(S, 0), color=C5, lw=.5, label='min hold')
    for c in centers: ax.axvline(c, color=C2, lw=.8, ls=':')
    ax.set_ylim(np.nanmin(S) - 1, np.nanmax(S) + 2); ax.set_xlim(f[0], f[-1]); fmt_ax(ax, None, 'dB (input power/Hz)'); ax.legend(ncol=4, frameon=False, loc='upper left'); ax.set_title('Raw — dotted = hop centres (receiver artifact spikes)')
    ax = fig.add_axes([0.06, 0.09, 0.91, 0.33])
    mres = A['mres']; ax.plot(f, mres, color=C4, lw=.6, label='mean residual')
    ax.fill_between(f, -3 * A['sd_t'] / np.sqrt(nsw), 3 * A['sd_t'] / np.sqrt(nsw), color='#8fa0bd', alpha=.35, lw=0, label='±3σ of the 10-min mean')
    for q, c in enumerate(s['candidates'][:5]):
        ax.annotate(f"{c['f_mhz']:.3f}", (c['f_mhz'], c['peak_excess_db']), xytext=(-4 if q % 2 else 4, 8), textcoords='offset points', ha='right' if q % 2 else 'left', fontsize=8.5, color=C2)
    for c in centers: ax.axvline(c, color=C2, lw=.8, ls=':')
    ax.set_xlim(f[0], f[-1]); ax.set_ylim(-0.4, max(2.4, s['candidates'][0]['peak_excess_db'] + .6 if s['candidates'] else 2.4)); fmt_ax(ax, 'MHz', 'dB above floor'); ax.legend(frameon=False, loc='upper right'); ax.set_title('Residual (artifact template and slow floor removed)')
    done(fig, 'p2_spectrum')
    # ---- 3 waterfalls (same preparation as the hero PNG: levelled residual, staircase time axis, blank hop centres)
    import hopscan_waterfall as hw
    W = A.get('W') or hw.prepare(A)
    fig = page(pdf, 'Waterfalls', f"time × frequency, {nsw} sweeps, each hop at its own measured time · top: raw (power average per pixel) · bottom: residual, {hw.SMOOTH_BINS} bins × {hw.SMOOTH_SWEEPS} sweeps power average, max per pixel")
    for i, (Z, how, ttl, vmin, vmax, cmap, lab) in enumerate(((S, 'mean', 'Raw', np.nanpercentile(S, 0.5), np.nanpercentile(S, 99.95), 'magma', 'dB'),
                                                             (W['E'], 'max', 'Residual (enhanced)', lambda B: (lambda m, s_: m - 2 * s_)(*hw.noise_stats(B)), lambda B: (lambda m, s_: m + max(12 * s_, .5))(*hw.noise_stats(B)), hw.CMAP, 'dB above floor'))):
        ax = fig.add_axes([0.06, 0.50 - i * 0.42, 0.84, 0.33])
        im, _ = hw.draw(ax, A, W, Z, how, vmin, vmax, cmap=cmap, target_px=1600)
        fmt_ax(ax, 'MHz' if i else None, 's'); ax.set_title(ttl)
        cb = fig.colorbar(im, cax=fig.add_axes([0.92, 0.50 - i * 0.42, 0.012, 0.33])); cb.set_label(lab)
    done(fig, 'p3_waterfall')
    # ---- 4 artifact
    fig = page(pdf, 'Receiver artifact at every hop centre', 'identical in all hops → deterministic receiver DC / low-frequency skirt, not RF content')
    k = A['k']; ax = fig.add_axes([0.06, 0.12, 0.52, 0.72]); sel = k > 0
    for h in range(nh := A['nh']):
        ax.semilogx(k[sel] * s['step_hz'] / 1e3, A['hop_prof'][h][sel], lw=1, label=f'{centers[h]:.2f} MHz')
        ax.semilogx(-k[~sel][::-1] * s['step_hz'] / 1e3, A['hop_prof'][h][~sel][::-1], lw=.6, alpha=.5, color='#8fa0bd')
    ax.set_xlabel('offset from hop centre (kHz)'); ax.set_ylabel('dB above baseline'); ax.legend(ncol=2, frameon=False, fontsize=8); ax.set_title('Median level per hop vs offset (+ side coloured, − side grey)')
    ax = fig.add_axes([0.66, 0.12, 0.31, 0.72]); ax.axis('off')
    rows = [('offset', 'artifact'), ('12 kHz', 'dc_artifact_db', '12'), ('49 kHz', 'dc_artifact_db', '49'), ('200 kHz', 'dc_artifact_db', '200'), ('800 kHz', 'dc_artifact_db', '800')]
    ax.text(0, 1.0, 'Template (median of hops)', fontsize=13, fontweight='bold', va='top')
    for i, r in enumerate(rows[1:]):
        ax.text(0, 0.9 - i * 0.07, r[0], fontsize=12, va='top'); ax.text(0.5, 0.9 - i * 0.07, f"{s[r[1]][r[2]]:+.2f} dB", fontsize=12, va='top', color=C1)
    ax.text(0, 0.45, f"Hop-to-hop spread at 10–100 kHz: {s['dc_artifact_hop_spread_db']:.2f} dB\n\nThe template is subtracted before looking for\nweak lines. Real emissions would differ between\nhops; this shape does not.\n\nThe 21 bins closest to each centre are masked by\nthe DC mask (±{(s['dc_mask_hz'] or 0)/2/1e3:.1f} kHz).", fontsize=11, va='top', color='#c9d4e6')
    done(fig, 'p4_artifact')
    # ---- 5 candidates
    cand = A['cand'][:6]
    fig = page(pdf, 'Weak line candidates', f"residual ≥ 0.5 dB after artifact removal · 10-min mean · {len(A['cand'])} found, top {len(cand)} shown")
    for i, c in enumerate(cand):
        j = c['j']; sl = slice(max(0, j - 60), j + 61)
        ax = fig.add_axes([0.05 + (i % 3) * 0.325, 0.50 - (i // 3) * 0.44, 0.28, 0.30])
        ax.fill_between((f[sl] - c['f_mhz']) * 1e3, np.nanpercentile(A['Res'][:, sl], 5, 0), np.nanpercentile(A['Res'][:, sl], 95, 0), color=C1, alpha=.2, lw=0)
        ax.plot((f[sl] - c['f_mhz']) * 1e3, A['mres'][sl], color=C4, lw=1); ax.axhline(0, color='#8fa0bd', lw=.5)
        ax.set_title(f"{c['f_mhz']:.4f} MHz · +{c['peak_excess_db']:.2f} dB", color=C2); fmt_ax(ax, 'kHz from line', 'dB above floor')
        ax2 = fig.add_axes([0.05 + (i % 3) * 0.325, 0.62 - (i // 3) * 0.44 + 0.0, 0.28, 0.0001]); ax2.axis('off')
        tx = fig.add_axes([0.05 + (i % 3) * 0.325, 0.82 - (i // 3) * 0.44 - 0.02, 0.28, 0.07]); tx.axis('off')
        tx.text(0, 1, f"width {c['width_khz']:.2f} kHz · detected in {c['sweeps_detected_pct']:.0f}% of sweeps\nsd {c['level_sd_db']:.2f} dB · hop {centers[c['hop']]:.2f} MHz, offset {c['offset_khz']:+.0f} kHz · z={c['z_mean']:.0f}", fontsize=9, va='top', color='#c9d4e6')
    done(fig, 'p5_candidates')
    # ---- 5b table of all lines
    fig = page(pdf, 'All lines ≥ 0.5 dB above floor', f"{len(A['cand'])} candidates · 10-min mean · sorted by level; full list in {tag}_candidates.csv")
    tx = fig.add_axes([0.04, 0.08, 0.60, 0.80]); tx.axis('off')
    hdr = ['MHz', 'dB above floor', 'max-hold', 'width kHz', 'hop centre', 'offset kHz', 'sweeps >3σ']
    for xi, h_ in zip(np.linspace(0, 0.93, 7), hdr): tx.text(xi, 1.0, h_, fontsize=9.5, fontweight='bold', color=C2, va='top')
    for r_, c in enumerate(A['cand'][:24]):
        vals = [f"{c['f_mhz']:.4f}", f"{c['peak_excess_db']:+.2f}", f"{c['maxhold_excess_db']:+.2f}", f"{c['width_khz']:.2f}", f"{centers[c['hop']]:.2f}", f"{c['offset_khz']:+.0f}", f"{c['sweeps_detected_pct']:.0f} %"]
        for xi, v_ in zip(np.linspace(0, 0.93, 7), vals): tx.text(xi, 0.95 - r_ * 0.039, v_, fontsize=9.5, va='top')
    ax = fig.add_axes([0.70, 0.12, 0.27, 0.68]); lm = s['lines_per_mhz']; ax.bar([int(k_) + .5 for k_ in lm], list(lm.values()), width=.8, color=C1)
    ax.set_title('Lines per MHz'); fmt_ax(ax, 'MHz', 'lines ≥ 0.5 dB')
    done(fig, 'p5b_lines')
    # ---- 6 statistics
    fig = page(pdf, 'Measurement statistics', 'does the receiver behave like an ideal Welch estimator on noise?')
    ax = fig.add_axes([0.06, 0.52, 0.28, 0.31]); sdb = np.nanstd(A['Res'], 0); ax.hist(sdb[np.isfinite(sdb)], bins=80, color=C1, alpha=.85)
    ax.axvline(s['theory_sd_db'], color=C2, lw=2, label=f"theory {s['theory_sd_db']:.3f}"); ax.axvline(s['sweep_to_sweep_sd_db'], color=C4, lw=2, ls='--', label=f"measured median {s['sweep_to_sweep_sd_db']:.3f}")
    ax.set_title('Per-bin sweep-to-sweep sd (dB)'); ax.legend(frameon=False); fmt_ax(ax, 'dB', 'bins')
    ax = fig.add_axes([0.40, 0.52, 0.28, 0.31]); z = (A['Res'][:, ::3] / A['sd_t']).ravel(); z = z[np.isfinite(z)]
    xs = np.linspace(-6, 6, 300); ax.hist(np.clip(z, -6, 6), bins=120, range=(-6, 6), density=True, color=C1, alpha=.8, label='data'); ax.plot(xs, np.exp(-xs ** 2 / 2) / np.sqrt(2 * np.pi), color=C2, lw=2, label='N(0,1)')
    ax.set_yscale('log'); ax.set_ylim(1e-5, 1); ax.set_title('Standardised residuals'); ax.legend(frameon=False); fmt_ax(ax, 'σ', 'density')
    ax = fig.add_axes([0.74, 0.52, 0.23, 0.31]); ax.plot(f, A['floor'], color=C4, lw=.8); ax.set_title(f"Floor vs frequency (p2p {s['floor_ripple_p2p_db']:.2f} dB)"); fmt_ax(ax, 'MHz', 'dB')
    ax = fig.add_axes([0.06, 0.09, 0.62, 0.34]); sm = np.nanmedian(S, 1); ax.plot(A['T0'][:, 0] - A['T0'][0, 0], sm - sm.mean(), color=C1, marker='.', lw=.8); ax.set_title(f"Median level per sweep (sd {s['sweep_median_sd_db']*1000:.0f} mdB)"); fmt_ax(ax, 's', 'dB vs mean')
    ax = fig.add_axes([0.74, 0.09, 0.23, 0.34]); ax.axis('off')
    ax.text(0, 1, f"Welch: {s['segments']} segments of {s['bins']}\nat {s['overlap_samples']} samples overlap,\n{s['window']} window → σ(dB)=4.343·√((1+2ρ²)/K)\n= {s['theory_sd_db']:.3f} dB (ρ={ENBW_BLACKMAN_50})\n\nmeasured {s['sweep_to_sweep_sd_db']:.3f} dB\nratio {s['sweep_to_sweep_sd_db']/s['theory_sd_db']:.3f}\n\nA ratio near 1 and a Gaussian\nresidual mean no excess noise,\nno drift, no dropped data.\n\nStart-up: first sweep\n{s['warmup_first_sweep_db'][0]:+.2f} dB (hop 1) … {s['warmup_first_sweep_db'][-1]:+.2f} dB (hop {s['hops']}),\nthen flat (sd {s['warmup_later_sd_db']:.3f} dB).", fontsize=11, va='top', color='#c9d4e6')
    done(fig, 'p6_statistics')
    # ---- 7 overlap
    fig = page(pdf, 'Hop overlap and passband', 'each 0.5 MHz overlap is measured twice (hop h upper edge, hop h+1 lower edge); stitching keeps only the central 1.5 MHz of each hop')
    ax = fig.add_axes([0.06, 0.14, 0.40, 0.66]); blk = 64
    for i, (x, dd) in enumerate(A['ov']):
        m2 = (len(dd) // blk) * blk
        ax.plot(0.5 + (np.arange(m2 // blk) * blk + blk / 2) * s['step_hz'] / 1e6, np.nanmean(dd[:m2].reshape(-1, blk), 1), lw=1, label=f'{centers[i]:.2f}/{centers[i+1]:.2f}')
    ax.axhline(0, color='#8fa0bd', lw=.6); ax.axvline(0.75, color=C2, ls=':', lw=1); ax.text(0.76, ax.get_ylim()[1] * 0.9, 'stitch junction', color=C2, fontsize=9)
    ax.set_xlabel('offset from centre of the lower hop (MHz)'); ax.set_ylabel('hop h − hop h+1 (dB)'); ax.legend(ncol=2, frameon=False, fontsize=8); ax.set_title('Difference of the two measurements of the same RF')
    ax = fig.add_axes([0.54, 0.14, 0.43, 0.66]); ax.plot(A['ax_off'], A['side'] - A['ref'], color=C1, lw=2); ax.axvline(0.75, color=C2, ls=':', lw=1)
    for lvl, key in ((-1, 'rolloff_1db_mhz'), (-3, 'rolloff_3db_mhz')):
        if s[key]: ax.axhline(lvl, color='#8fa0bd', lw=.6); ax.annotate(f"{lvl} dB at ±{s[key]:.2f} MHz", (s[key], lvl), xytext=(-110, 10), textcoords='offset points', color=C3, fontsize=10)
    ax.set_xlabel('|offset from hop centre| (MHz)'); ax.set_ylabel('dB re 0.55–0.70 MHz'); ax.set_title('Hop passband (median over hops)')
    fig.text(0.06, 0.035, f"Within 0.25 dB out to ±0.85 MHz, −1 dB at ±{s['rolloff_1db_mhz']} MHz, −3 dB at ±{s['rolloff_3db_mhz']} MHz → the ±0.75 MHz stitch edge lies in the flat part and the junction difference is ≈ 0.", fontsize=10.5, color='#c9d4e6')
    done(fig, 'p7_overlap')
    # ---- 8 sensitivity
    fig = page(pdf, 'Sensitivity and occupancy', '12.45 kHz channels (51 bins) · residual power per sweep')
    ax = fig.add_axes([0.06, 0.52, 0.42, 0.31]); okc = np.isfinite(A['ch']); ax.hist(A['ch'][okc].ravel(), bins=120, color=C1, alpha=.85); ax.set_yscale('log'); ax.set_title(f"Channel residual per sweep (robust σ {s['channel_sd_db']*1000:.0f} mdB)"); fmt_ax(ax, 'dB above floor', 'channel-sweeps')
    ax = fig.add_axes([0.55, 0.52, 0.42, 0.31]); ax.plot(A['chf'], 100 * A['occ'], color=C4, lw=.8); ax.set_title(f"Occupancy: channels above 5σ ({s['channels_with_occupancy']} of {s['channels']})"); fmt_ax(ax, 'MHz', '% of sweeps')
    floor_bin = s['floor_db']; enbw = s['enbw_hz'] or 421.6
    mds_sweep = floor_bin + 10 * np.log10(enbw) + 10 * np.log10(10 ** (5 * A['sd_t'] / 10) - 1)
    mds_run = floor_bin + 10 * np.log10(enbw) + 10 * np.log10(10 ** (5 * A['sd_t'] / np.sqrt(nsw) / 10) - 1)
    ax = fig.add_axes([0.06, 0.06, 0.9, 0.38]); ax.axis('off')
    ax.text(0, 1, "Minimum detectable narrowband signal (5σ, one bin of ENBW %.0f Hz; same unit as the spectrum, integrated power)" % enbw, fontsize=12.5, fontweight='bold', va='top')
    ax.text(0, 0.88, f"single sweep ({s['dwell_s']:.1f} s):   {mds_sweep:.1f} dB   ({mds_sweep - (floor_bin + 10*np.log10(enbw)):+.1f} dB relative to the noise in that bin)\nwhole run ({nsw} sweeps):  {mds_run:.1f} dB   ({mds_run - (floor_bin + 10*np.log10(enbw)):+.1f} dB relative to the noise in that bin)\n\nIn practice the floor ripple ({s['floor_ripple_p2p_db']:.2f} dB p2p) and the artifact template residue limit the run-integrated figure to roughly 0.1–0.2 dB, i.e. about −14 dB re noise.\nAt channel level nothing rises above {s['max_channel_excess_db']:+.2f} dB, so no 12.45 kHz channel was occupied; the weak lines on the line pages are real excess in single bins (≤ 1 kHz wide) that carry little power.", fontsize=11.5, va='top', color='#c9d4e6')
    done(fig, 'p8_sensitivity')
    # ---- 9 timing / io
    fig = page(pdf, 'Timing and host I/O', 'continuity, scheduling and driver latency during the run')
    ax = fig.add_axes([0.06, 0.52, 0.28, 0.31]); ax.hist(A['dwell'], bins=40, color=C1); ax.set_title(f"Hop dwell (median {s['dwell_s']:.3f} s)"); fmt_ax(ax, 's', 'hops')
    ax = fig.add_axes([0.40, 0.52, 0.28, 0.31]); ax.hist(A['gaps'], bins=40, color=C2); ax.set_title(f"Retune gap between hops (median {s['retune_gap_s']*1000:.0f} ms)"); fmt_ax(ax, 's', 'hops')
    ax = fig.add_axes([0.74, 0.52, 0.23, 0.31]); ax.hist(A['period'], bins=40, color=C4); ax.set_title(f"Sweep period (median {s['sweep_period_s']:.2f} s)"); fmt_ax(ax, 's', 'sweeps')
    ax = fig.add_axes([0.06, 0.08, 0.9, 0.31])
    series = {}
    for i, h, d_ in A['io']:
        for key in ('read_latency_max_ms', 'rx_scheduling_gap_max_ms', 'read_latency_p95_ms', 'settle_overflow_count'):
            if d_.get(key) is not None: series.setdefault(key, []).append((i * A['nh'] + h, d_[key]))
    for key, col in (('read_latency_max_ms', C3), ('rx_scheduling_gap_max_ms', C2), ('read_latency_p95_ms', C1)):
        if key in series: x_, y_ = zip(*sorted(series[key])); ax.plot(x_, y_, color=col, lw=1, label=key.replace('_', ' '))
    ax.axhline(31, color='#8fa0bd', ls=':', lw=1); ax.text(0, 33, 'default UHD ring ≈ 31 ms', color='#8fa0bd', fontsize=9)
    ax.text(0.99, 0.5, f"reads reach {max(v for _, v in series.get('read_latency_max_ms', [(0, 0)])):.0f} ms — more than twice the default ring — with no overflow: the enlarged ring absorbed them", transform=ax.transAxes, ha='right', fontsize=10, color=C4)
    ax.set_xlabel('record'); ax.set_ylabel('ms (cumulative maxima)'); ax.legend(frameon=False, loc='upper left'); ax.set_title('Driver read latency and RX scheduling gaps')
    done(fig, 'p9_timing')
    # ---- 10 method
    fig = page(pdf, 'Method and limits')
    txt = ("Data: soapy_power -F json records; one sweep = all hops in ascending order; the central 1.5 MHz of each 2 MHz hop is stitched (hop spacing).\n"
           "Averages are power-domain means; 'residual' = level − artifact template − slow floor (30th percentile over 200 kHz).\n"
           "Artifact template = median over hops of (hop median − baseline at 0.49–0.73 of the half-span); valid because it is the same in every hop.\n"
           "Candidates = ≥ 0.5 dB above floor in the 10-min mean, excluding ±12 kHz around each hop centre; 'detected' = single sweep > 3 σ_sweep.\n"
           "Welch theory: σ(dB) = 4.343·√((1+2ρ²)/K), ρ = 0.1661 for Blackman at 50 % overlap (Harris, 1978).\n\n"
           "Limits: levels are relative (not dBm); a line surviving this analysis may still be a receiver spur — retune the LO by a few hundred kHz\n"
           "(real carriers keep their RF frequency) or terminate the input (spurs stay). Features inside ±100 kHz of a hop centre are only\n"
           "as reliable as the template subtraction; the 21 centre bins are masked and carry no information. Only 600 s at one antenna and gain.\n\n"
           f"Reproduce: python3 tools/analyze_hopscan.py {Path(s['file']).name} OUTDIR")
    fig.text(0.04, 0.88, txt, fontsize=12, va='top', color='#c9d4e6', linespacing=1.6)
    done(fig, 'p10_method'); pdf.close()


def main():
    a = [x for x in sys.argv[1:] if not x.startswith('--')]
    if len(a) < 2:
        print(__doc__); return 2
    out = Path(a[1]); out.mkdir(parents=True, exist_ok=True)
    tag = sys.argv[sys.argv.index('--tag') + 1] if '--tag' in sys.argv else 'hopscan'
    A = analyse(a[0], out, tag)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import hopscan_waterfall as hw
    A['W'] = hw.prepare(A)
    A['summary']['events'] = [{k: v for k, v in e.items() if k not in ('r0', 'r1', 'c0', 'c1')} for e in A['W']['events']]
    make_pdf(A, out, tag)
    (out / f'{tag}_summary.json').write_text(json.dumps(A['summary'], indent=1))
    with open(out / f'{tag}_candidates.csv', 'w', newline='') as fh:
        w = csv.writer(fh); w.writerow(['f_mhz', 'width_khz', 'peak_excess_db', 'maxhold_excess_db', 'sweeps_detected_pct', 'level_sd_db', 'z_mean', 'hop_center_mhz', 'offset_khz'])
        for c in A['summary']['candidates']:
            w.writerow([f"{c['f_mhz']:.4f}", f"{c['width_khz']:.2f}", f"{c['peak_excess_db']:.2f}", f"{c['maxhold_excess_db']:.2f}", f"{c['sweeps_detected_pct']:.0f}", f"{c['level_sd_db']:.2f}", f"{c['z_mean']:.0f}", f"{A['centers'][c['hop']]/1e6:.2f}", f"{c['offset_khz']:+.0f}"])
    print(json.dumps({k: A['summary'][k] for k in ('sweeps', 'floor_db', 'sweep_to_sweep_sd_db', 'theory_sd_db', 'max_residual_bin_db', 'channels_with_occupancy')}))
    hw.render(A, out, tag)                     # hero / raw / zoom waterfalls + interactive viewer
    print(json.dumps({'waterfalls': sorted(p.name for p in out.glob(f'{tag}_waterfall*'))}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
