#!/usr/bin/env python3
"""Detailed ordinary-vs-all-features dashboard for a fullrange session.

Ordinary = plain scan (features OFF). All ON = RF state, headroom with retries,
mask-aware coverage, rejection evidence and per-hop telemetry. Relative,
uncalibrated levels; no notches, exclusions or corrections in either mode.
usage: report_versus.py SESSION [--out PATH]"""
import argparse
import json
import re
import textwrap
from pathlib import Path

import numpy as np

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

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
    ('150–160 MHz', 150.0, 160.0),
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


def num(x):
    return np.array([np.nan if v is None else v for v in x], float)


def levels(v):
    return {int(m): float(x) for m, x in zip(v['cell_mhz'], v['cell_level_db'])
            if x is not None and np.isfinite(x) and 70 <= m < 6000}


def observations(v):
    return {int(m): int(n) for m, n in zip(v['cell_mhz'], v['cell_observations'])}


def rolling(x, w=41):
    return np.array([np.nanmedian(x[max(0, i - w // 2):i + w // 2 + 1])
                     for i in range(len(x))])


def session_meta(sess, compare, on):
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

        serial = re.search(r'serial=([^,]+)', after('-d') or '')
        if serial:
            device = serial.group(1)
        antenna = after('-A')
        gain = after('-g')
    return date, device, antenna, gain, compare.get('input'), float(on.get('seconds') or 0)


def style(ax, title=None, xlabel=None, ylabel=None):
    ax.set_facecolor(PANEL)
    ax.tick_params(colors=MUTED, labelsize=8, length=3)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.grid(color=GRID, lw=.55, alpha=.55)
    if title:
        ax.set_title(title, color=FG, fontsize=11, fontweight='bold', loc='left', pad=6)
    if xlabel:
        ax.set_xlabel(xlabel, color=MUTED, fontsize=8.4)
    if ylabel:
        ax.set_ylabel(ylabel, color=MUTED, fontsize=8.4)


def mode_card(fig, spec, title, color, lines):
    ax = fig.add_subplot(spec)
    ax.axis('off')
    ax.add_patch(plt.Rectangle((0, 0), 1, 1, transform=ax.transAxes,
                               fc='#0b1426', ec=color, lw=1.2))
    ax.text(.045, .88, title, transform=ax.transAxes, color=color,
            fontsize=11.2, fontweight='bold', va='top')
    for i, (value, label) in enumerate(lines):
        y = .68 - i * .15
        ax.text(.045, y, value, transform=ax.transAxes, color=FG,
                fontsize=12.0 if i == 0 else 8.9, fontweight='bold' if i == 0 else 'normal',
                va='center')
        ax.text(.50, y, label, transform=ax.transAxes, color=MUTED,
                fontsize=7.9, va='center')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('session')
    ap.add_argument('--out', default=None)
    a = ap.parse_args()
    sess = Path(a.session)
    outpath = Path(a.out) if a.out else sess / 'report' / 'versus_fullrange.png'
    outpath.parent.mkdir(parents=True, exist_ok=True)

    compare = json.loads((sess / 'compare.json').read_text())
    on, off = compare['variants']['features-on'], compare['variants']['features-off']
    lon, loff = levels(on), levels(off)
    non, noff = observations(on), observations(off)
    common = np.array(sorted(set(lon) & set(loff)))
    don = np.array([lon[m] for m in common])
    doff = np.array([loff[m] for m in common])
    delta = don - doff
    offset = np.nanmedian(delta)
    dd = delta - offset
    mad = 1.4826 * np.nanmedian(np.abs(dd - np.nanmedian(dd)))
    over3 = int((np.abs(dd) > 3).sum())
    obs_on = np.array([non[m] for m in common])
    obs_off = np.array([noff[m] for m in common])

    w = json.loads((sess / 'waterfall.json').read_text())
    f2 = w['start_mhz'] + w['cell_mhz'] * (np.arange(len(w['features-on']['grid'][0])) + .5)
    grids = {}
    for key in ('features-on', 'features-off'):
        grids[key] = {
            'g': num([x for row in w[key]['grid'] for x in row]).reshape(len(w[key]['grid']), -1),
            't': num(w[key]['t0_s']),
        }
    gon, goff = grids['features-on'], grids['features-off']

    sig_on = np.nanstd(gon['g'], axis=0)
    sig_off = np.nanstd(goff['g'], axis=0)
    band = (f2 >= 150) & (f2 <= 160)
    b_on = np.nanmedian(gon['g'][:, band], axis=1)
    b_off = np.nanmedian(goff['g'][:, band], axis=1)

    date, device, antenna, gain, inp, seconds = session_meta(sess, compare, on)
    sec_per_sweep_on = on['seconds'] / on['passes']
    sec_per_sweep_off = off['seconds'] / off['passes']

    plt.rcParams.update({
        'figure.facecolor': BG, 'savefig.facecolor': BG, 'text.color': FG,
        'axes.facecolor': PANEL, 'axes.edgecolor': GRID, 'axes.labelcolor': MUTED,
        'xtick.color': MUTED, 'ytick.color': MUTED, 'grid.color': GRID,
        'font.family': 'DejaVu Sans', 'axes.titleweight': 'bold',
    })
    fig = plt.figure(figsize=(26, 21), dpi=105)
    gs = GridSpec(6, 12, figure=fig, top=.905, bottom=.048, left=.045, right=.985,
                  hspace=.62, wspace=.55, height_ratios=[.70, 1.20, 1.45, 1.18, 1.05, 1.15])

    fig.text(.045, .978, 'ORDINARY SCAN  vs  ALL FEATURES ON', fontsize=25,
             fontweight='bold', color=FG, ha='left', va='top')
    bits = [date, f'B200mini {device}' if device else 'B200mini',
            f"{'antenna' if inp != 'terminated' else '50 Ω terminated'} {antenna}" if antenna else None,
            f'{gain} dB' if gain else None, f'2 × {seconds:.0f} s', '70 MHz – 6 GHz',
            'relative, uncalibrated']
    fig.text(.045, .951, '  ·  '.join(b for b in bits if b),
             fontsize=10.5, color=MUTED, ha='left', va='top')
    fig.text(.045, .928,
             'Ordinary = plain scan.  All ON = RF state + headroom ×2 retries + mask-aware coverage + rejection evidence + per-hop telemetry.  '
             'Same receiver and antenna, 300 s each; no notches, exclusions or corrections in either mode.',
             fontsize=9.2, color=MUTED, ha='left', va='top')

    # 1 mode cards
    mode_card(fig, gs[0, :4], 'ORDINARY  ·  PLAIN SCAN', OFF, [
        (f"{off['records']:,}", 'records'),
        (f"{off['hops']} centers · {off['passes']:.2f} sweeps", f"{sec_per_sweep_off:.1f} s per sweep"),
        (f"{off['records_per_s']:.2f} rec/s · {np.median(obs_off):.0f} looks/cell", 'no per-hop telemetry'),
        (f"{off['cells_1MHz']:,} cells · 0 temperatures", 'no RF-state or headroom checks'),
        ('plain hop plan', '6.986 MHz center spacing'),
    ])
    mode_card(fig, gs[0, 4:8], 'ALL FEATURES ON', ON, [
        (f"{on['records']:,}", 'records'),
        (f"{on['hops']} centers · {on['passes']:.2f} sweeps", f"{sec_per_sweep_on:.1f} s per sweep"),
        (f"{on['records_per_s']:.2f} rec/s · {np.median(obs_on):.0f} looks/cell", '5,125 fresh temperatures'),
        (f"{on['cells_1MHz']:,} cells · 0 rejected IQ", '0 validity failures'),
        ('mask-aware plan', '27.98 MHz center spacing'),
    ])

    ax = fig.add_subplot(gs[0, 8:])
    ax.axis('off')
    ax.add_patch(plt.Rectangle((0, 0), 1, 1, transform=ax.transAxes,
                               fc='#0b1426', ec=GREEN, lw=1.2))
    ax.text(.045, .90, 'ON − OFF', transform=ax.transAxes, color=GREEN,
            fontsize=11.5, fontweight='bold', va='top')
    lines = [
        (f'{offset:+.3f} dB', 'median level difference'),
        (f'{mad:.3f} dB', 'robust σ'),
        (f'{np.nanpercentile(np.abs(dd), 95):.3f} dB', 'p95 |Δ|'),
        (f'{over3}', 'cells |Δ| > 3 dB'),
        (f'{len(common):,}', 'common 1 MHz cells'),
    ]
    for i, (value, label) in enumerate(lines):
        y = .70 - i * .14
        ax.text(.045, y, value, transform=ax.transAxes, color=FG,
                fontsize=10.5 if i else 13, fontweight='bold', va='center')
        ax.text(.48, y, label, transform=ax.transAxes, color=MUTED, fontsize=8.1, va='center')

    # 2 spectrum + delta histogram
    ax = fig.add_subplot(gs[1, :8])
    style(ax, '1 · Spectrum, 1 MHz cells — ordinary orange, all ON sky',
          'RF (MHz, log)', 'dB (relative)')
    ax.plot(common, doff, color=OFF, lw=.36, alpha=.48, label='ordinary')
    ax.plot(common, don, color=ON, lw=.36, alpha=.62, label='all ON')
    ax.set_xscale('log')
    ax.set_xlim(70, 6000)
    ax.set_xticks([70, 100, 200, 500, 1000, 2000, 3000, 6000])
    ax.set_xticklabels(['70', '100', '200', '500', '1G', '2G', '3G', '6G'])
    lo = np.nanpercentile(np.r_[don, doff], .3) - 1
    hi = np.nanmax(np.r_[don, doff]) + 2.5
    ax.set_ylim(lo, hi)
    for name, fq in (('FM', 98), ('DAB', 225), ('LTE800', 805), ('GSM900', 935),
                     ('2.4G', 2440), ('WiFi5', 5500)):
        ax.axvline(fq, color='#334155', lw=.7, ls=':', alpha=.8)
        ax.text(fq, hi - .4, name, color=GOLD, fontsize=6.8, ha='center', va='top', rotation=90)
    ax.legend(frameon=False, fontsize=8, labelcolor=MUTED, ncol=2, loc='lower right')

    ax = fig.add_subplot(gs[1, 8:])
    style(ax, '2 · All ON − ordinary per 1 MHz cell', 'Δ (dB, clipped ±5)', 'cells (log)')
    ax.hist(np.clip(dd, -5, 5), bins=90, color=GREEN, alpha=.85, edgecolor='none')
    ax.set_yscale('log')
    for s in (-3 * mad, 3 * mad):
        ax.axvline(s, color=GOLD, lw=.9, ls='--', alpha=.8)
    ax.axvline(0, color=MUTED, lw=.7)
    ax.text(.97, .95, f'median Δ {offset:+.3f} dB\nrobust σ {mad:.3f} dB\n'
            f'p95 |Δ| {np.nanpercentile(np.abs(dd), 95):.3f} dB\n'
            f'{over3} cells > 3 dB', transform=ax.transAxes, ha='right', va='top',
            color=FG, fontsize=8.4,
            bbox=dict(facecolor=BG, edgecolor=GRID, alpha=.82, pad=3))

    # 3 waterfalls side by side
    vmin = np.nanpercentile(np.r_[gon['g'][:, f2 <= 1000], goff['g'][:, f2 <= 1000]], 2)
    vmax = np.nanpercentile(np.r_[gon['g'][:, f2 <= 1000], goff['g'][:, f2 <= 1000]], 99.6)
    m = f2 <= 1000
    x = f2[m]
    xe = np.r_[x[0] - 1, x + 1]
    for col, (title, data, color) in enumerate((
            ('3 · Ordinary waterfall, 70–1000 MHz', goff, OFF),
            ('4 · All ON waterfall, 70–1000 MHz', gon, ON))):
        ax = fig.add_subplot(gs[2, col * 6:(col + 1) * 6])
        style(ax, title, 'RF (MHz)', 'time (s)')
        t = data['t']
        te = np.r_[0, (t[1:] + t[:-1]) / 2, seconds] if t.size > 1 else np.array([0, seconds])
        gg = np.clip(data['g'][:, m], vmin, vmax)
        pc = ax.pcolormesh(xe, te, gg, cmap='turbo', shading='flat', vmin=vmin, vmax=vmax)
        ax.set_xlim(70, 1000)
        ax.set_ylim(0, seconds)
        for name, fq in (('FM', 98), ('DAB', 225), ('LTE800', 805), ('GSM900', 935)):
            ax.axvline(fq, color='white', lw=.6, ls=':', alpha=.45)
            ax.text(fq, seconds * .985, name, color='white', fontsize=6.8,
                    ha='center', va='top', rotation=90, alpha=.85)
        cb = fig.colorbar(pc, ax=ax, pad=.012, aspect=22)
        cb.set_label('dB (relative)', color=MUTED, fontsize=8)
        cb.ax.tick_params(colors=MUTED, labelsize=7.2)
        cb.outline.set_edgecolor(GRID)

    # 5 delta vs RF
    ax = fig.add_subplot(gs[3, :7])
    style(ax, '5 · Level difference against frequency (median offset removed)',
          'RF (MHz, log)', 'Δ (dB)')
    ax.scatter(common, np.clip(dd, -6, 6), s=1.7, color='#7391b8', alpha=.55, linewidths=0)
    ax.plot(common, rolling(dd), color=GREEN, lw=1.25, label='rolling median (41 cells)')
    ax.axhspan(-3 * mad, 3 * mad, color=GOLD, alpha=.10)
    ax.axhline(0, color=MUTED, lw=.6)
    far = np.abs(dd) > 3 * mad
    ax.scatter(common[far], np.clip(dd[far], -6, 6), s=3, color=RED, alpha=.75,
               linewidths=0, label=f'|Δ| > 3σ ({int(far.sum())})')
    ax.axvspan(150, 160, color=PURPLE, alpha=.14)
    ax.text(155, 5.5, '150–160', color=PURPLE, fontsize=7.4, ha='center')
    ax.annotate('~260 MHz intermittent', xy=(261, 5.0), xytext=(330, 5.6),
                color=PURPLE, fontsize=7.2,
                arrowprops=dict(arrowstyle='-', color=PURPLE, lw=.6))
    ax.annotate('signal edge', xy=(777, -5.2), xytext=(900, -5.6),
                color=RED, fontsize=7.2,
                arrowprops=dict(arrowstyle='-', color=RED, lw=.6))
    ax.set_xscale('log')
    ax.set_xlim(70, 6000)
    ax.set_ylim(-6.4, 6.4)
    ax.set_xticks([70, 100, 200, 500, 1000, 2000, 3000, 6000])
    ax.set_xticklabels(['70', '100', '200', '500', '1G', '2G', '3G', '6G'])
    ax.legend(frameon=False, fontsize=7.5, labelcolor=MUTED, loc='lower right', ncol=2)

    # 6 band table
    ax = fig.add_subplot(gs[3, 7:])
    ax.axis('off')
    ax.set_title('6 · Band summary, median per 1 MHz cell', color=FG, fontsize=11,
                 fontweight='bold', loc='left', pad=6)
    rows = []
    for name, b0, b1 in BANDS:
        sel = (common >= b0) & (common < b1)
        if not sel.any():
            continue
        rows.append([name, f'{int(sel.sum())}', f'{np.nanmedian(doff[sel]):.1f}',
                     f'{np.nanmedian(don[sel]):.1f}',
                     f'{np.nanmedian(don[sel] - doff[sel]):+.2f}'])
    rows.append(['70–6000 MHz', f'{len(common):,}', f'{np.nanmedian(doff):.1f}',
                 f'{np.nanmedian(don):.1f}', f'{np.nanmedian(don - doff):+.2f}'])
    table = ax.table(cellText=rows,
                     colLabels=['band', 'cells', 'ordinary', 'all ON', 'Δ'],
                     cellLoc='left', colWidths=[.35, .10, .18, .18, .19],
                     bbox=[0, 0, 1, .98])
    table.auto_set_font_size(False)
    table.set_fontsize(6.6)
    for (r, c), cell in table.get_celld().items():
        cell.set_edgecolor('#1e2d47')
        cell.set_facecolor('#0b1426' if r else '#15233c')
        cell.set_text_props(color=FG if r else '#a9c1e8',
                            fontweight='bold' if r == 0 else 'normal')
        if r == len(rows) and c in (2, 3, 4):
            cell.set_facecolor('#13253d')
    ax.text(0, -.035, 'Δ = median per-cell difference; includes RF-environment change (antenna input).',
            transform=ax.transAxes, fontsize=7.0, color=MUTED)

    # 7 150-160 per pass
    ax = fig.add_subplot(gs[4, :4])
    style(ax, '7 · 150–160 MHz band median per sweep', 'time (s)', 'dB (relative)')
    ax.plot(goff['t'], b_off, 'o-', color=OFF, lw=1.1, ms=3.2, label='ordinary')
    ax.plot(gon['t'], b_on, 'o-', color=ON, lw=1.1, ms=3.2, label='all ON')
    ax.set_xlim(0, seconds)
    ax.legend(frameon=False, fontsize=8, labelcolor=MUTED, ncol=2)
    ax.text(.02, .05, f'ON range {np.nanmax(b_on) - np.nanmin(b_on):.1f} dB  ·  '
            f'OFF range {np.nanmax(b_off) - np.nanmin(b_off):.1f} dB\n'
            'cause unresolved: needs 50 Ω / controlled ON-OFF',
            transform=ax.transAxes, color=PURPLE, fontsize=7.2,
            bbox=dict(facecolor=BG, edgecolor=GRID, alpha=.82, pad=3))

    # 8 pass-to-pass sigma
    ax = fig.add_subplot(gs[4, 4:8])
    style(ax, '8 · Pass-to-pass σ, 2 MHz cells', 'RF (MHz, log)', 'σ across sweeps (dB)')
    ax.plot(f2, sig_off, color=OFF, lw=.55, alpha=.75, label='ordinary')
    ax.plot(f2, sig_on, color=ON, lw=.55, alpha=.75, label='all ON')
    ax.axvspan(150, 160, color=PURPLE, alpha=.12)
    ax.set_xscale('log')
    ax.set_xlim(70, 6000)
    ax.set_ylim(0, np.nanpercentile(np.r_[sig_on, sig_off], 99.5) * 1.15)
    ax.set_xticks([70, 100, 200, 500, 1000, 2000, 3000, 6000])
    ax.set_xticklabels(['70', '100', '200', '500', '1G', '2G', '3G', '6G'])
    ax.legend(frameon=False, fontsize=7.5, labelcolor=MUTED, ncol=2, loc='upper right')
    ax.text(.02, .95, f'median σ  OFF {np.nanmedian(sig_off):.3f} dB\n'
            f'median σ  ON  {np.nanmedian(sig_on):.3f} dB\n'
            f'{goff["g"].shape[0]} vs {gon["g"].shape[0]} sweeps',
            transform=ax.transAxes, va='top', color=FG, fontsize=8.0,
            bbox=dict(facecolor=BG, edgecolor=GRID, alpha=.82, pad=3))
    ax.text(.02, .05, 'axis clipped; different depth and sweep counts',
            transform=ax.transAxes, color=MUTED, fontsize=7.0)

    # 9 looks per cell
    ax = fig.add_subplot(gs[4, 8:])
    style(ax, '9 · Looks per 1 MHz cell', 'observations per cell', 'cells')
    bins = np.arange(0, 62, 2)
    ax.hist(obs_off, bins=bins, color=OFF, alpha=.55, label='ordinary')
    ax.hist(obs_on, bins=bins, color=ON, alpha=.55, label='all ON')
    ax.set_yscale('log')
    ax.legend(frameon=False, fontsize=8, labelcolor=MUTED, ncol=2)
    ax.text(.02, .95, f'median  OFF {np.median(obs_off):.0f}\nmedian  ON  {np.median(obs_on):.0f}',
            transform=ax.transAxes, va='top', color=FG, fontsize=8.3,
            bbox=dict(facecolor=BG, edgecolor=GRID, alpha=.82, pad=3))

    # 10 top differences
    ax = fig.add_subplot(gs[5, :6])
    ax.axis('off')
    ax.set_title('10 · Largest 1 MHz differences', color=FG, fontsize=11,
                 fontweight='bold', loc='left', pad=6)
    idx = np.argsort(-np.abs(dd))[:8]
    rows = []
    for i in idx:
        mhz = int(common[i])
        if 150 <= mhz <= 160:
            note = '150–160 band'
        elif 255 <= mhz <= 270:
            note = '~260 MHz feature'
        elif 770 <= mhz <= 780:
            note = 'sharp signal edge'
        elif 95 <= mhz <= 105:
            note = 'FM band'
        else:
            note = ''
        rows.append([f'{mhz}', f'{doff[i]:.1f}', f'{don[i]:.1f}', f'{dd[i]:+.1f}', note])
    table = ax.table(cellText=rows,
                     colLabels=['MHz', 'ordinary', 'all ON', 'Δ', 'note'],
                     cellLoc='left', colWidths=[.12, .18, .17, .15, .38],
                     bbox=[0, .07, 1, .89])
    table.auto_set_font_size(False)
    table.set_fontsize(7.4)
    for (r, c), cell in table.get_celld().items():
        cell.set_edgecolor('#1e2d47')
        cell.set_facecolor('#0b1426' if r else '#15233c')
        cell.set_text_props(color=FG if r else '#a9c1e8',
                            fontweight='bold' if r == 0 else 'normal')
    ax.text(0, .005, 'Largest cells sit on sharp edges and intermittent bands, not a uniform offset.',
            transform=ax.transAxes, fontsize=7.0, color=MUTED)

    # 11 interpretation
    ax = fig.add_subplot(gs[5, 6:])
    ax.axis('off')
    ax.set_title('11 · What the comparison does and does not show', color=FG,
                 fontsize=11, fontweight='bold', loc='left', pad=6)
    tx = [
        '• Median level difference is −0.04 dB; robust σ 0.32 dB, so no measurable median shift from the feature bundle.',
        '• All ON completes full sweeps 2.9× faster (12.6 vs 36.1 s) because its mask-aware plan uses 215 centers instead of 849. It gets half the looks per cell (24 vs 48).',
        '• All ON adds 5,125 fresh temperatures, RF-state validity, chain fingerprint, headroom retries, coverage guarantee and rejection evidence. No acquisition was rejected on this antenna.',
        '• Neither mode applies a spur notch, exclusion or level correction. The 150–160 MHz and ~260 MHz differences appear in the ON window and remain unresolved; they are not attributed to the features.',
        '• Plans differ, so this is not a per-feature ablation. Isolating headroom, coverage, RF state or telemetry needs a 50 Ω input and one feature at a time.',
    ]
    y = .96
    for line in tx:
        wrapped = textwrap.fill(line, 96)
        ax.text(0, y, wrapped, fontsize=8.0, va='top', color=FG,
                transform=ax.transAxes, linespacing=1.35)
        y -= .062 * (wrapped.count('\n') + 1) + .050

    fig.text(.045, .012,
             'Relative, uncalibrated levels (no dBm, noise figure or traceability).  '
             'Ordinary and all-ON see the same antenna at different times, so differences include RF-environment change.  '
             'No automatic notch, exclusion or correction.',
             color=MUTED, fontsize=8)

    fig.savefig(outpath, bbox_inches='tight')
    print('wrote', outpath)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
