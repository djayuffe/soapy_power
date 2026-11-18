#!/usr/bin/env python3
"""One-page full-range dashboard: spectrum, ON/OFF effects, 3D-supporting waterfall,
telemetry drift and pass-to-pass stability. Relative, uncalibrated levels only.
usage: report_hero.py SESSION [--out PATH]"""
import argparse
import json
import re
from datetime import datetime
from pathlib import Path

import numpy as np

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec, GridSpecFromSubplotSpec

BG = '#05080f'
PANEL = '#0a1020'
FG = '#dbe7ff'
MUTED = '#8fa3c4'
GRID = '#22304a'
ON = '#38bdf8'
OFF = '#fb923c'
GREEN = '#34d399'
RED = '#f87171'
GOLD = '#fbbf24'
PURPLE = '#c084fc'

BANDS = [
    ('FM broadcast', 87.5, 108.0),
    ('Airband', 118.0, 137.0),
    ('DAB / VHF', 174.0, 240.0),
    ('TETRA / PMR', 380.0, 470.0),
    ('LTE 800', 791.0, 862.0),
    ('GSM 900', 880.0, 960.0),
    ('GNSS L1', 1560.0, 1590.0),
    ('GSM 1800', 1710.0, 1880.0),
    ('UMTS', 1920.0, 2170.0),
    ('ISM 2.4 GHz', 2400.0, 2483.5),
    ('LTE 2600', 2500.0, 2690.0),
    ('5G 3.5 GHz', 3400.0, 3800.0),
    ('WiFi 5 GHz', 5150.0, 5850.0),
]
MARK = [('FM', 98), ('DAB', 225), ('LTE800', 805), ('GSM900', 935),
        ('2.4G', 2440), ('WiFi5', 5500)]


def num(x):
    return np.array([np.nan if v is None else v for v in x], float)


def load_compare(sess):
    path = sess / 'compare.json'
    if not path.exists():
        raise SystemExit(f'missing {path}; run fullrange_compare.py first')
    c = json.loads(path.read_text())
    variants = c.get('variants', {})
    if 'features-on' not in variants or 'features-off' not in variants:
        raise SystemExit('compare.json lacks features-on/features-off')
    return c, variants['features-on'], variants['features-off']


def levels(v):
    return {int(m): float(x) for m, x in zip(v['cell_mhz'], v['cell_level_db'])
            if x is not None and np.isfinite(x) and 70 <= m < 6000}


def load_waterfall(sess):
    path = sess / 'waterfall.json'
    if not path.exists():
        raise SystemExit(f'missing {path}; run fullrange_waterfall.py first')
    w = json.loads(path.read_text())
    f = w['start_mhz'] + w['cell_mhz'] * (np.arange(len(w['features-on']['grid'][0])) + .5)
    out = {}
    for key in ('features-on', 'features-off'):
        if key not in w:
            continue
        g = num([x for row in w[key]['grid'] for x in row]).reshape(len(w[key]['grid']), -1)
        t = num(w[key]['t0_s'])
        ok = np.isfinite(g).mean(1) > .5
        out[key] = {'grid': g[ok], 't': t[ok], 'passes': int(ok.sum())}
    return f, out


def read_telemetry(path, seconds):
    """All RFIC temperature readings and the pass index from tuned-centre resets."""
    times, temps, passes = [], [], []
    t0 = None
    prev = None
    p = -1
    with open(path) as fh:
        for line in fh:
            r = json.loads(line)
            meta = r.get('metadata') or {}
            ts = r.get('time_start')
            if not ts:
                continue
            t = datetime.fromisoformat(ts)
            t0 = t0 or t
            c = meta.get('tuned_center_hz')
            if c is not None:
                if prev is None or c < prev - 1e6:
                    p += 1
                prev = c
            after = ((meta.get('rf_state') or {}).get('after') or {})
            temp = (after.get('validity') or {}).get('temperature_c')
            if temp is not None and np.isfinite(float(temp)):
                times.append((t - t0).total_seconds())
                temps.append(float(temp))
                passes.append(p)
    return (np.array(times), np.array(temps), np.array(passes)) if temps else (
        np.array([]), np.array([]), np.array([]))


def ols(x, y):
    k = np.isfinite(x) & np.isfinite(y)
    x, y = x[k], y[k]
    if x.size < 3:
        return None
    slope, intercept = np.polyfit(x, y, 1)
    pred = slope * x + intercept
    ss_res = np.sum((y - pred) ** 2)
    se = np.sqrt(ss_res / max(x.size - 2, 1) / np.sum((x - x.mean()) ** 2))
    r = float(np.corrcoef(x, y)[0, 1])
    return {'slope': float(slope), 'se': float(se), 'r': r, 'n': int(x.size),
            'line': (float(x.min()), float(x.max()), float(slope * x.min() + intercept),
                     float(slope * x.max() + intercept))}


def rolling(x, w=11):
    return np.array([np.nanmedian(x[max(0, i - w // 2):i + w // 2 + 1]) for i in range(len(x))])


def session_meta(sess, compare, on):
    """Best-effort date, device, antenna and gain from the session name and argv0."""
    date = None
    m = re.search(r'(\d{4})(\d{2})(\d{2})-\d{6}', sess.name)
    if m:
        date = '-'.join(m.groups())
    device = antenna = gain = None
    argv = sess / 'features-on.argv0'
    if argv.exists():
        parts = argv.read_text().split('\x00')

        def after(flag):
            return parts[parts.index(flag) + 1] if flag in parts else None

        driver = after('-d') or ''
        serial = re.search(r'serial=([^,]+)', driver)
        if serial:
            device = serial.group(1)
        antenna = after('-A')
        gain = after('-g')
    return {
        'date': date,
        'device': device,
        'antenna': antenna,
        'gain': gain,
        'input': compare.get('input'),
        'seconds': float(on.get('seconds') or 0),
    }


def style(ax, title=None, xlabel=None, ylabel=None):
    ax.set_facecolor(PANEL)
    ax.tick_params(colors=MUTED, labelsize=8.5, length=3)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.grid(color=GRID, lw=.55, alpha=.55)
    if title:
        ax.set_title(title, color=FG, fontsize=11.2, fontweight='bold', loc='left', pad=7)
    if xlabel:
        ax.set_xlabel(xlabel, color=MUTED, fontsize=8.6)
    if ylabel:
        ax.set_ylabel(ylabel, color=MUTED, fontsize=8.6)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('session')
    ap.add_argument('--out', default=None)
    a = ap.parse_args()
    sess = Path(a.session)
    outpath = Path(a.out) if a.out else sess / 'report' / 'hero_fullrange.png'
    outpath.parent.mkdir(parents=True, exist_ok=True)

    compare, on, off = load_compare(sess)
    f2, wf = load_waterfall(sess)
    lon, loff = levels(on), levels(off)
    common = np.array(sorted(set(lon) & set(loff)))
    don = np.array([lon[m] for m in common])
    doff = np.array([loff[m] for m in common])
    raw_delta = don - doff
    offset = np.nanmedian(raw_delta)
    delta = raw_delta - offset
    mad = 1.4826 * np.nanmedian(np.abs(delta - np.nanmedian(delta)))
    agree = compare.get('agreement', {})

    t, temp, pass_idx = read_telemetry(sess / 'features-on.jsonl', on.get('seconds') or 300)
    if t.size == 0:
        t = num([p[0] for p in on.get('temperature_series', [])])
        temp = num([p[1] for p in on.get('temperature_series', [])])
        pass_idx = np.zeros(t.size, int)
    seconds = float(on.get('seconds') or (t.max() if t.size else 300))

    G = wf['features-on']['grid']
    tp = wf['features-on']['t']
    med_cell = np.nanmedian(G, axis=0)
    drift = np.nanmedian(G - med_cell, axis=1)
    pass_temp = np.full(G.shape[0], np.nan)
    for p in range(G.shape[0]):
        sel = pass_idx == p
        if sel.any():
            pass_temp[p] = np.nanmean(temp[sel])
    if np.isfinite(pass_temp).sum() < 3:
        pass_temp = np.interp(tp, t, temp) if t.size and np.isfinite(temp).all() else np.full(G.shape[0], np.nan)
    fit = ols(pass_temp, drift)
    sig = np.nanstd(G, axis=0)

    # ---- figure ----
    plt.rcParams.update({
        'figure.facecolor': BG, 'savefig.facecolor': BG, 'text.color': FG,
        'axes.facecolor': PANEL, 'axes.edgecolor': GRID, 'axes.labelcolor': MUTED,
        'xtick.color': MUTED, 'ytick.color': MUTED, 'grid.color': GRID,
        'font.family': 'DejaVu Sans', 'axes.titleweight': 'bold',
    })
    fig = plt.figure(figsize=(26, 19), dpi=105)
    gs = GridSpec(5, 12, figure=fig, top=.935, bottom=.055, left=.045, right=.985,
                  hspace=.62, wspace=.55, height_ratios=[.52, 1.30, 1.45, 1.08, 1.03])

    meta = session_meta(sess, compare, on)
    bits = [meta['date'], f"B200mini {meta['device']}" if meta['device'] else 'B200mini',
            f"{'antenna' if meta['input'] != 'terminated' else '50 Ω terminated'} {meta['antenna']}" if meta['antenna'] else None,
            f"{meta['gain']} dB" if meta['gain'] else None,
            f"2 × {meta['seconds']:.0f} s" if meta['seconds'] else None,
            'features ON (telemetry, RF state, headroom, coverage) vs OFF',
            'relative, uncalibrated']
    fig.text(.045, .977, 'SOAPY_POWER  ·  FULL RANGE  70 MHz – 6 GHz', fontsize=25,
             fontweight='bold', color=FG, ha='left', va='top')
    fig.text(.045, .951, '  ·  '.join(b for b in bits if b),
             fontsize=10.5, color=MUTED, ha='left', va='top')

    # KPI tiles
    tg = GridSpecFromSubplotSpec(1, 6, subplot_spec=gs[0, :], wspace=.22)
    tiles = [
        (f'{on.get("records", 0):,}'.replace(',', ' '), 'records features ON', ON),
        (f'{on.get("passes", 0):.0f} / {off.get("passes", 0):.1f}', 'passes ON / OFF', GREEN),
        (f'{off.get("records_per_s", 0) / max(on.get("records_per_s", 1), 1e-9):.2f}×', 'OFF / ON record rate', GOLD),
        (f'{offset:+.2f} dB', 'median Δ ON − OFF', FG),
        (f'{int((np.abs(delta) > 3).sum())}', 'cells |Δ| > 3 dB, offset removed', RED),
        (f'{np.nanmin(temp):.2f}–{np.nanmax(temp):.2f} °C', 'RFIC temperature', PURPLE),
    ]
    for i, (value, label, color) in enumerate(tiles):
        ax = fig.add_subplot(tg[0, i])
        ax.axis('off')
        ax.add_patch(plt.Rectangle((0, 0), 1, 1, transform=ax.transAxes,
                                   fc='#0b1426', ec='#1c2c47', lw=1))
        ax.text(.5, .60, value, transform=ax.transAxes, ha='center', va='center',
                fontsize=16.5 if len(value) < 12 else 14, fontweight='bold', color=color)
        ax.text(.5, .22, label, transform=ax.transAxes, ha='center', va='center',
                fontsize=7.7, color=MUTED)

    # 1 full spectrum
    ax = fig.add_subplot(gs[1, :8])
    style(ax, '1 · Full-range spectrum, 1 MHz cells — ON sky, OFF orange',
          'RF (MHz, log)', 'dB (relative)')
    ax.plot(common, don, color=ON, lw=.38, alpha=.62, label='ON')
    ax.plot(common, doff, color=OFF, lw=.38, alpha=.48, label='OFF')
    ax.plot(common, rolling(don, 21), color='#e0f2fe', lw=.9, alpha=.9, label='ON rolling median')
    ax.set_xscale('log')
    ax.set_xlim(70, 6000)
    ax.set_xticks([70, 100, 200, 500, 1000, 2000, 3000, 6000])
    ax.set_xticklabels(['70', '100', '200', '500', '1G', '2G', '3G', '6G'])
    lo = np.nanpercentile(np.r_[don, doff], .3) - 1
    hi = np.nanmax(np.r_[don, doff]) + 2.5
    ax.set_ylim(lo, hi)
    for name, fq in MARK:
        if 70 <= fq <= 6000:
            ax.axvline(fq, color='#334155', lw=.7, ls=':', alpha=.8)
            ax.text(fq, hi - .4, name, color=GOLD, fontsize=6.8, ha='center', va='top', rotation=90)
    ax.legend(frameon=False, fontsize=7.6, labelcolor=MUTED, ncol=3, loc='lower right')

    # 2 delta histogram
    ax = fig.add_subplot(gs[1, 8:])
    style(ax, '2 · ON − OFF per 1 MHz cell (median offset removed)', 'Δ (dB, clipped ±5)', 'cells (log)')
    ax.hist(np.clip(delta, -5, 5), bins=90, color=GREEN, alpha=.85, edgecolor='none')
    ax.set_yscale('log')
    for s in (-3 * mad, 3 * mad):
        ax.axvline(s, color=GOLD, lw=.9, ls='--', alpha=.8)
    ax.axvline(0, color=MUTED, lw=.7)
    ax.text(.97, .95, f'robust σ {mad:.2f} dB\np95 |Δ| {np.nanpercentile(np.abs(delta), 95):.2f} dB\n'
            f'{agree.get("common_cells", len(common))} common cells',
            transform=ax.transAxes, ha='right', va='top', color=FG, fontsize=8.4)

    # 3 waterfall 70-1000
    ax = fig.add_subplot(gs[2, :7])
    style(ax, '3 · Features ON waterfall, 2 MHz cells, 70–1000 MHz',
          'RF (MHz)', 'time (s)')
    m = (f2 >= 70) & (f2 <= 1000)
    x = f2[m]
    x_edges = np.r_[x[0] - 1, x + 1]
    y = tp[:G.shape[0]]
    y_edges = np.r_[0, (y[1:] + y[:-1]) / 2, seconds] if y.size > 1 else np.array([0, seconds])
    gg = np.clip(G[:, m], np.nanpercentile(G, 2), np.nanpercentile(G, 99.6))
    pc = ax.pcolormesh(x_edges, y_edges, gg, cmap='turbo', shading='flat')
    ax.set_xlim(70, 1000)
    ax.set_ylim(0, seconds)
    cb = fig.colorbar(pc, ax=ax, pad=.012, aspect=24)
    cb.set_label('dB (relative)', color=MUTED, fontsize=8)
    cb.ax.tick_params(colors=MUTED, labelsize=7.5)
    cb.outline.set_edgecolor(GRID)
    for name, fq in MARK[:4]:
        ax.axvline(fq, color='white', lw=.6, ls=':', alpha=.5)
        ax.text(fq, seconds * .985, name, color='white', fontsize=7, ha='center', va='top', rotation=90, alpha=.85)

    # 4 band table
    ax = fig.add_subplot(gs[2, 7:])
    ax.axis('off')
    ax.set_title('4 · Band summary (nominal allocations)', color=FG, fontsize=11.2,
                 fontweight='bold', loc='left', pad=7)
    rows = []
    for name, b0, b1 in BANDS:
        mo = np.array([m for m in common if b0 <= m <= b1])
        if mo.size == 0:
            continue
        vo = np.array([lon[m] for m in mo])
        vf = np.array([loff[m] for m in mo])
        do = np.array([lon[m] - loff[m] for m in mo])
        rows.append([name, f'{mo.size}', f'{np.nanmedian(vo):.1f}', f'{np.nanpercentile(vo, 95):.1f}',
                     f'{np.nanmedian(vf):.1f}', f'{np.nanmedian(do):+.2f}'])
    rows.append(['70–6000 MHz', f'{common.size}', f'{np.nanmedian(don):.1f}',
                 f'{np.nanpercentile(don, 95):.1f}', f'{np.nanmedian(doff):.1f}',
                 f'{np.nanmedian(don - doff):+.2f}'])
    table = ax.table(cellText=rows, colLabels=['band', 'cells', 'med ON', 'p95 ON', 'med OFF', 'med Δ'],
                     loc='center', cellLoc='left', colWidths=[.31, .10, .14, .14, .15, .13])
    table.auto_set_font_size(False)
    table.set_fontsize(7.2)
    table.scale(1, 1.24)
    for (r, c), cell in table.get_celld().items():
        cell.set_edgecolor('#1e2d47')
        cell.set_facecolor('#0b1426' if r else '#15233c')
        cell.set_text_props(color=FG if r else '#a9c1e8', fontweight='bold' if r == 0 else 'normal')

    # 5 delta vs RF
    ax = fig.add_subplot(gs[3, :7])
    style(ax, '5 · ON − OFF against frequency (median offset removed)', 'RF (MHz)', 'Δ (dB)')
    ax.scatter(common, np.clip(delta, -6, 6), s=1.7, color='#7391b8', alpha=.55, linewidths=0)
    ax.plot(common, rolling(delta, 41), color=GREEN, lw=1.25, label='rolling median (41 cells)')
    ax.axhspan(-3 * mad, 3 * mad, color=GOLD, alpha=.10)
    ax.axhline(0, color=MUTED, lw=.6)
    far = np.abs(delta) > 3 * mad
    ax.scatter(common[far], np.clip(delta[far], -6, 6), s=3, color=RED, alpha=.75, linewidths=0,
               label=f'|Δ| > 3σ ({int(far.sum())})')
    ax.axvspan(150, 160, color=PURPLE, alpha=.12)
    ax.text(155, 5.5, '150–160', color=PURPLE, fontsize=7.5, ha='center')
    ax.set_xscale('log')
    ax.set_xlim(70, 6000)
    ax.set_ylim(-6.2, 6.2)
    ax.set_xticks([70, 100, 200, 500, 1000, 2000, 3000, 6000])
    ax.set_xticklabels(['70', '100', '200', '500', '1G', '2G', '3G', '6G'])
    ax.legend(frameon=False, fontsize=7.6, labelcolor=MUTED, loc='lower right', ncol=2)

    # 6 temperature time series
    ax = fig.add_subplot(gs[3, 7:])
    style(ax, '6 · RFIC temperature, features ON (quantised sensor)', 'time (s)', '°C')
    ax.plot(t, temp, color=GOLD, lw=.7, alpha=.9)
    ax.scatter(t[::max(1, t.size // 400)], temp[::max(1, t.size // 400)], s=1.4, color='#fde68a', alpha=.8)
    ax.set_xlim(0, seconds)
    lo, hi = np.nanmin(temp), np.nanmax(temp)
    pad = max((hi - lo) * .22, .08)
    ax.set_ylim(lo - pad, hi + pad)
    uniq = np.unique(temp)
    step = float(np.median(np.diff(uniq))) if uniq.size > 1 else 0.0
    ax.text(.02, .95, f'{t.size:,} fresh readings\n{lo:.2f}–{hi:.2f} °C · span {hi - lo:.2f} °C\n'
            f'{uniq.size} levels · {step:.3f} °C step',
            transform=ax.transAxes, va='top', color=FG, fontsize=8.3,
            bbox=dict(facecolor=BG, edgecolor=GRID, alpha=.82, pad=3))

    # 7 150-160 band
    ax = fig.add_subplot(gs[4, :4])
    style(ax, '7 · 150–160 MHz band median per pass', 'time (s)', 'dB (relative)')
    for key, label, color in (('features-on', 'ON', ON), ('features-off', 'OFF', OFF)):
        if key not in wf:
            continue
        Gw = wf[key]['grid']
        band = (f2 >= 150) & (f2 <= 160)
        if band.any() and Gw.size:
            yv = np.nanmedian(Gw[:, band], axis=1)
            ax.plot(wf[key]['t'][:yv.size], yv, 'o-', color=color, lw=1.1, ms=3.2, label=label)
    ax.axhline(np.nanmedian(don[(common >= 150) & (common <= 160)]), color=ON, lw=.6, ls=':', alpha=.6)
    ax.set_xlim(0, seconds)
    ax.legend(frameon=False, fontsize=8, labelcolor=MUTED, ncol=2)
    ax.text(.02, .05, 'cause unresolved: needs 50 Ω / controlled ON-OFF', transform=ax.transAxes,
            color=PURPLE, fontsize=7.4,
            bbox=dict(facecolor=BG, edgecolor=GRID, alpha=.82, pad=2.5))

    # 8 pass-to-pass sigma
    ax = fig.add_subplot(gs[4, 4:8])
    style(ax, '8 · Pass-to-pass stability, features ON (2 MHz cells)', 'RF (MHz, log)', 'σ across passes (dB)')
    ax.plot(f2, sig, color=GREEN, lw=.55, alpha=.9)
    ax.axvspan(150, 160, color=PURPLE, alpha=.12)
    ax.set_xscale('log')
    ax.set_xlim(70, 6000)
    ax.set_ylim(0, np.nanmax(sig) * 1.12)
    ax.set_xticks([70, 100, 200, 500, 1000, 2000, 3000, 6000])
    ax.set_xticklabels(['70', '100', '200', '500', '1G', '2G', '3G', '6G'])
    ax.text(.02, .95, f'median σ {np.nanmedian(sig):.2f} dB\np95 σ {np.nanpercentile(sig, 95):.2f} dB\n'
            f'{G.shape[0]} passes', transform=ax.transAxes, va='top', color=FG, fontsize=8.3,
            bbox=dict(facecolor=BG, edgecolor=GRID, alpha=.82, pad=3))

    # 9 drift vs temperature
    ax = fig.add_subplot(gs[4, 8:])
    style(ax, '9 · Band-median drift vs temperature', 'RFIC temperature (°C)', 'drift (dB)')
    if fit:
        sc = ax.scatter(pass_temp, drift, c=tp[:len(drift)] / 60, cmap='viridis', s=30,
                        edgecolor='white', linewidth=.3, zorder=3)
        x0, x1, y0, y1 = fit['line']
        ax.plot([x0, x1], [y0, y1], color=GOLD, lw=1.2)
        ax.text(.02, .95, f'{fit["slope"]:+.3f} ± {fit["se"]:.3f} dB/°C\nr {fit["r"]:+.2f} · {fit["n"]} passes',
                transform=ax.transAxes, va='top', color=FG, fontsize=8.3,
                bbox=dict(facecolor=BG, edgecolor=GRID, alpha=.82, pad=3))
        cb = fig.colorbar(sc, ax=ax, pad=.02, aspect=18)
        cb.set_label('pass start (min)', color=MUTED, fontsize=7.5)
        cb.ax.tick_params(colors=MUTED, labelsize=7)
        cb.outline.set_edgecolor(GRID)
    ax.text(.02, .05, 'diagnostic only: antenna input', transform=ax.transAxes, color=MUTED, fontsize=7.3,
            bbox=dict(facecolor=BG, edgecolor=GRID, alpha=.82, pad=2.5))

    fig.text(.045, .018,
             'Relative, uncalibrated levels (no dBm, noise figure or traceability). Antenna input: ON/OFF differences and drift include RF-environment change. '
             'Band names are nominal allocations, not verified identities. No automatic notch, exclusion or correction.',
             color=MUTED, fontsize=8)

    fig.savefig(outpath, bbox_inches='tight')
    print('wrote', outpath)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
