#!/usr/bin/env python3
"""Cellular downlink dashboard for a fullrange session.

Extracts LTE 800 / 900 MHz / 1800 MHz / LTE 2100 / LTE 2600 from the raw
features-on and features-off JSONL captures, detects wideband blocks and
isolated narrow components, and writes a dashboard plus a detailed markdown
report. Relative, uncalibrated levels only; nominal band labels; no decoding,
cell identity, operator attribution, notch, exclusion or level correction.

usage: report_cellular.py SESSION [--out PNG] [--md MD] [--use-cache]
"""
import argparse
import json
import re
import textwrap
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec, GridSpecFromSubplotSpec
from matplotlib.patches import Rectangle

warnings.filterwarnings('ignore', category=RuntimeWarning)

BG = '#05080f'
PANEL = '#0a1020'
CARD = '#0b1426'
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
    dict(key='lte800', name='LTE 800', alloc='B20 downlink', b0=791.0, b1=821.0,
         res=0.05, block_start=800.0, narrow=False, color=GREEN, xstep=5,
         tick='LTE800'),
    dict(key='gsm900', name='900 MHz', alloc='GSM 900 / LTE B8 downlink', b0=925.0, b1=960.0,
         res=0.02, block_start=925.0, narrow=True, color=GOLD, xstep=5,
         tick='900'),
    dict(key='mhz1800', name='1800 MHz', alloc='GSM 1800 / LTE B3 downlink', b0=1805.0, b1=1880.0,
         res=0.02, block_start=1805.0, narrow=True, color=PURPLE, xstep=10,
         tick='1800'),
    dict(key='lte2100', name='LTE 2100', alloc='B1 downlink', b0=2110.0, b1=2170.0,
         res=0.05, block_start=2110.0, narrow=False, color=RED, xstep=10,
         tick='LTE2100'),
    dict(key='lte2600', name='LTE 2600', alloc='B7 downlink', b0=2620.0, b1=2690.0,
         res=0.05, block_start=2620.0, narrow=False, color=MUTED, xstep=10,
         tick='LTE2600'),
]

EDGE_HZ = 8e6
DC_HZ = 1.5e6
COVER_MIN = 0.5
BLOCK_SEED_DB = 6.0
BLOCK_EXT_DB = 4.0
BLOCK_MIN_MHZ = 1.0
BLOCK_GAP_MHZ = 0.5
BLOCK_SHALLOW_MHZ = 3.5
NARROW_MAX_MHZ = 0.5
NARROW_ISO_DB = 9.0
NARROW_MIN_DB = 8.0
CLUSTER_MIN_NARROW = 2
CLUSTER_MAX_ABOVE_DB = 12.0


def rollmed(x, w):
    """Rolling median that ignores NaNs; w is the window in samples."""
    n = len(x)
    out = np.full(n, np.nan)
    h = max(1, int(w)) // 2
    for i in range(n):
        seg = x[max(0, i - h):min(n, i + h + 1)]
        seg = seg[np.isfinite(seg)]
        if seg.size:
            out[i] = np.median(seg)
    return out


def fmt_span(a, b):
    return f'{a:.2f}–{b:.2f}'


def fmt_delta(x):
    if abs(x) < 0.05:
        return '0.0'
    return f'{x:+.1f}'


def md_table(headers, rows, aligns=None):
    if aligns is None:
        aligns = ['---'] * len(headers)
    out = ['| ' + ' | '.join(headers) + ' |',
           '| ' + ' | '.join(aligns) + ' |']
    for row in rows:
        out.append('| ' + ' | '.join(str(x) for x in row) + ' |')
    return '\n'.join(out)


def style(ax, title=None, xlabel=None, ylabel=None):
    ax.set_facecolor(PANEL)
    ax.tick_params(colors=MUTED, labelsize=8, length=3)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.grid(color=GRID, lw=.55, alpha=.55)
    if title:
        ax.set_title(title, color=FG, fontsize=10.6, fontweight='bold',
                     loc='left', pad=5)
    if xlabel:
        ax.set_xlabel(xlabel, color=MUTED, fontsize=8.2)
    if ylabel:
        ax.set_ylabel(ylabel, color=MUTED, fontsize=8.2)


def note(ax, text, loc='lower right', color=MUTED, size=6.9, **kw):
    x, y, ha, va = {
        'lower right': (.975, .035, 'right', 'bottom'),
        'lower left': (.025, .035, 'left', 'bottom'),
        'upper left': (.025, .965, 'left', 'top'),
        'upper right': (.975, .965, 'right', 'top'),
    }[loc]
    ax.text(x, y, text, transform=ax.transAxes, ha=ha, va=va, color=color,
            fontsize=size, linespacing=1.35,
            bbox=dict(facecolor=BG, edgecolor=GRID, alpha=.80, pad=2.8), **kw)


def card_frame(ax, edge):
    ax.axis('off')
    ax.add_patch(Rectangle((0, 0), 1, 1, transform=ax.transAxes,
                           fc=CARD, ec=edge, lw=1.1))
def extract(path, edge_hz=EDGE_HZ, dc_hz=DC_HZ):
    """Aggregate every band into per-pass, per-cell mean dB values."""
    passes = []
    prev = None
    cur = None
    records = 0
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            pw = r.get('power')
            if not pw:
                continue
            meta = r.get('metadata') or {}
            center = meta.get('tuned_center_hz')
            if center is None:
                continue
            if prev is None or center < prev - 1e6:
                cur = {'sums': {}, 'cnts': {},
                       't_start': r.get('time_start'),
                       't_stop': r.get('time_stop')}
                for b in BANDS:
                    n = int(np.ceil((b['b1'] - b['b0']) / b['res']))
                    cur['sums'][b['key']] = np.zeros(n)
                    cur['cnts'][b['key']] = np.zeros(n)
                passes.append(cur)
            prev = center
            records += 1
            cur['t_stop'] = r.get('time_stop')
            pw = np.asarray(pw, float)
            f0 = float(r['first_bin_center_hz'])
            w = float(r['bin_width_hz'])
            start = float(r['start_hz'])
            stop = float(r['stop_hz'])
            for b in BANDS:
                step = b['res'] * 1e6
                lo = max(b['b0'] * 1e6, start + edge_hz)
                hi = min(b['b1'] * 1e6, stop - edge_hz)
                if hi <= lo:
                    continue
                i0 = max(0, int(np.ceil((lo - f0) / w)))
                i1 = min(pw.size, int(np.floor((hi - f0) / w)) + 1)
                if i1 <= i0:
                    continue
                fr = f0 + w * np.arange(i0, i1)
                vals = pw[i0:i1]
                m = (np.isfinite(vals) & (fr >= start + edge_hz)
                     & (fr <= stop - edge_hz) & (np.abs(fr - center) >= dc_hz)
                     & (fr >= b['b0'] * 1e6) & (fr < b['b1'] * 1e6))
                if not m.any():
                    continue
                n = cur['cnts'][b['key']].size
                idx = np.minimum(((fr[m] - b['b0'] * 1e6) / step).astype(np.int32), n - 1)
                cur['sums'][b['key']] += np.bincount(idx, weights=vals[m], minlength=n)
                cur['cnts'][b['key']] += np.bincount(idx, minlength=n)
    out = {}
    for b in BANDS:
        arr = np.full((len(passes), passes[0]['cnts'][b['key']].size), np.nan)
        for i, p in enumerate(passes):
            ok = p['cnts'][b['key']] > 0
            arr[i, ok] = p['sums'][b['key']][ok] / p['cnts'][b['key']][ok]
        out[b['key']] = arr
    tt = np.array([datetime.fromisoformat(p['t_start']).timestamp() for p in passes])
    out['_t'] = tt - tt[0]
    out['_abs'] = tt
    return out, records, len(passes)


def band_metrics(mat, b):
    res = b['res']
    f = b['b0'] + (np.arange(mat.shape[1]) + .5) * res
    cov = np.isfinite(mat).mean(axis=0)
    ok = cov >= COVER_MIN
    if not ok.any():
        raise ValueError(f"no covered cells in {b['name']}")
    med = np.nanmedian(mat, axis=0)
    p10 = np.nanpercentile(mat, 10, axis=0)
    p90 = np.nanpercentile(mat, 90, axis=0)
    p95 = np.nanpercentile(mat, 95, axis=0)
    floor = float(np.nanpercentile(med[ok], 10))
    thr = floor + BLOCK_SEED_DB
    peak_i = int(np.nanargmax(np.where(ok, med, np.nan)))
    return dict(
        f=f, cov=cov, ok=ok, med=med, p10=p10, p90=p90, p95=p95,
        floor=floor, thr=thr, peak=float(med[peak_i]),
        peak_f=float(f[peak_i]), bandmed=float(np.nanmedian(med[ok])),
        persist=float(np.mean(med[ok] > thr)),
        peakhold=float(np.mean(p95[ok] > thr)),
        cover=float(ok.mean()), n_cells=int(ok.sum()),
    )


def detect_blocks(m, b):
    """Threshold blocks on the pass-median spectrum."""
    f, med, ok = m['f'], m['med'], m['ok']
    res = b['res']
    floor = m['floor']
    sm = rollmed(np.where(ok, med, np.nan), max(1, int(round(.2 / res))))
    seed, ext = floor + BLOCK_SEED_DB, floor + BLOCK_EXT_DB
    idx = np.where(ok & (f >= b['block_start']) & np.isfinite(sm)
                   & (sm > seed))[0]
    groups = []
    if idx.size:
        s = p = idx[0]
        for i in idx[1:]:
            if (i - p) * res > BLOCK_GAP_MHZ:
                groups.append([s, p])
                s = i
            p = i
        groups.append([s, p])
    out = []
    for s, e in groups:
        while s > 0 and ok[s - 1] and med[s - 1] > ext:
            s -= 1
        while e < len(f) - 1 and ok[e + 1] and med[e + 1] > ext:
            e += 1
        out.append([s, e])
    out.sort()
    merged = []
    for g in out:
        if merged and g[0] <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], g[1])
        else:
            merged.append(g)
    changed = True
    while changed:
        changed = False
        new = []
        for g in merged:
            if new:
                a, bb = new[-1]
                gap = sm[bb + 1:g[0]]
                gap = gap[np.isfinite(gap)]
                gap_w = (g[0] - bb - 1) * res
                shallow = (gap.size > 0 and np.median(gap) > ext
                           and gap_w <= BLOCK_SHALLOW_MHZ)
                narrow_gap = gap_w < BLOCK_GAP_MHZ
                if shallow or narrow_gap:
                    new[-1] = [a, max(bb, g[1])]
                    changed = True
                    continue
            new.append(g)
        merged = new
    rows = []
    for s, e in merged:
        width = (e - s + 1) * res
        if width < BLOCK_MIN_MHZ:
            continue
        seg = med[s:e + 1]
        fill = float(np.mean(seg > ext)) if seg.size else 0.0
        rows.append(dict(
            s=s, e=e, f0=float(f[s] - res / 2),
            f1=float(f[e] + res / 2), width=float(width),
            med=float(np.nanmedian(seg)), peak=float(np.nanmax(seg)),
            fill=fill, kind='wideband',
        ))
    return rows


def detect_narrow(f, med, mat, ok, floor, res):
    """Isolated narrow components: local max, <=0.5 MHz wide, both sides down."""
    n = len(f)
    cands = []
    for i in range(3, n - 3):
        if not ok[i] or not np.isfinite(med[i]) or med[i] < floor + NARROW_MIN_DB:
            continue
        if med[i] < np.nanmax(med[max(0, i - 4):i + 5]) - 1e-9:
            continue
        j0 = i
        while j0 > 0 and ok[j0 - 1] and med[j0 - 1] > med[i] - 3:
            j0 -= 1
        j1 = i
        while j1 < n - 1 and ok[j1 + 1] and med[j1 + 1] > med[i] - 3:
            j1 += 1
        w3 = (j1 - j0 + 1) * res
        if w3 > NARROW_MAX_MHZ:
            continue
        left = (f >= f[i] - .5) & (f <= f[i] - .2) & ok
        right = (f >= f[i] + .2) & (f <= f[i] + .5) & ok
        if left.sum() < 2 or right.sum() < 2:
            continue
        lm = float(np.nanmedian(med[left]))
        rm = float(np.nanmedian(med[right]))
        if lm > med[i] - NARROW_ISO_DB or rm > med[i] - NARROW_ISO_DB:
            continue
        cands.append(dict(
            i=i, f=float(f[i]), level=float(med[i]),
            above=float(med[i] - floor), w3=float(w3),
            pers=float(np.mean(mat[:, i] > floor + BLOCK_SEED_DB)),
            left=lm, right=rm,
        ))
    cands.sort(key=lambda d: -d['level'])
    kept = []
    for c in cands:
        if all(abs(c['f'] - k['f']) >= .5 for k in kept):
            kept.append(c)
    return sorted(kept, key=lambda d: d['f'])


def classify_blocks(blocks, narrows, floor):
    for blk in blocks:
        inside = [c for c in narrows if blk['f0'] <= c['f'] <= blk['f1']]
        blk['n_narrow'] = len(inside)
        blk['kind'] = 'wideband'
        if (len(inside) >= CLUSTER_MIN_NARROW
                and blk['med'] - floor < CLUSTER_MAX_ABOVE_DB):
            blk['kind'] = 'narrow cluster'
    return blocks


def match_blocks(on_blocks, off_blocks, b):
    """Pair ON/OFF blocks by >=50 % overlap of the smaller span."""
    items = ([dict(**x, variant='on') for x in on_blocks]
             + [dict(**x, variant='off') for x in off_blocks])
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        a, c = find(i), find(j)
        if a != c:
            parent[c] = a

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            x, y = items[i], items[j]
            ov = max(0.0, min(x['f1'], y['f1']) - max(x['f0'], y['f0']))
            if ov >= .5 * min(x['width'], y['width']):
                union(i, j)
    groups = {}
    for i, it in enumerate(items):
        groups.setdefault(find(i), []).append(it)
    rows = []
    for g in groups.values():
        ons = [x for x in g if x['variant'] == 'on']
        offs = [x for x in g if x['variant'] == 'off']
        on_span = (min(x['f0'] for x in ons), max(x['f1'] for x in ons)) if ons else None
        off_span = (min(x['f0'] for x in offs), max(x['f1'] for x in offs)) if offs else None
        on_med = float(np.median([x['med'] for x in ons])) if ons else None
        off_med = float(np.median([x['med'] for x in offs])) if offs else None
        on_pk = max((x['peak'] for x in ons), default=None)
        off_pk = max((x['peak'] for x in offs), default=None)
        note = '—'
        if len(ons) > 1 or len(offs) > 1:
            note = 'split by fade'
        if on_span and off_span:
            lower = on_span[0] > off_span[0] + 1.0
            upper = on_span[1] < off_span[1] - 1.0
            if b['key'] == 'lte2100' and lower:
                note = 'ON lower-edge gap'
            elif lower and upper:
                note = 'OFF extends both edges'
            elif lower:
                note = 'OFF extends lower'
            elif upper:
                note = 'ON tail fades earlier'
        if any(x['n_narrow'] for x in ons + offs):
            note = 'contains narrow' if note == '—' else note + '; contains narrow'
        rows.append(dict(
            band=b['key'], name=b['name'], on_span=on_span, off_span=off_span,
            on_med=on_med, off_med=off_med, on_pk=on_pk, off_pk=off_pk,
            note=note, on=ons, off=offs,
        ))
    return sorted(rows, key=lambda d: d['on_span'][0] if d['on_span'] else d['off_span'][0])
def band_card(ax, b, m_on, m_off, n_wb, n_narrow, n_cluster):
    card_frame(ax, b['color'])
    ax.text(.045, .92, b['name'], transform=ax.transAxes, color=b['color'],
            fontsize=11.4, fontweight='bold', va='top')
    ax.text(.045, .825, b['alloc'], transform=ax.transAxes, color=MUTED,
            fontsize=7.1, va='top')
    ax.text(.045, .755, f"{b['b0']:.0f}–{b['b1']:.0f} MHz · {b['res']*1000:.0f} kHz grid",
            transform=ax.transAxes, color=MUTED, fontsize=6.7, va='top')
    detected = f"{n_wb} WB"
    if n_narrow:
        detected += f" · {n_narrow} narrow"
    if n_cluster:
        detected += f" · {n_cluster} cluster"
    lines = [
        (f"{100*m_on['cover']:.0f}% / {100*m_off['cover']:.0f}%", 'ON / OFF pass coverage'),
        (f"{m_on['floor']:.1f} dB", 'ON floor (10th pct)'),
        (f"+{m_on['peak']-m_on['floor']:.1f} dB", f"peak above floor @ {m_on['peak_f']:.2f}"),
        (f"{100*m_on['persist']:.0f}% / {100*m_on['peakhold']:.0f}%", 'persistent / peak-hold'),
        (detected, 'detected spans'),
    ]
    for i, (value, label) in enumerate(lines):
        y = .63 - i * .125
        ax.text(.045, y, value, transform=ax.transAxes, color=FG,
                fontsize=8.1 if i != 4 else 7.2, fontweight='bold', va='center')
        ax.text(.50, y, label, transform=ax.transAxes, color=MUTED,
                fontsize=6.6, va='center')


def method_card(ax):
    card_frame(ax, GRID)
    ax.text(.045, .92, 'HOW THE SPANS ARE COUNTED', transform=ax.transAxes,
            color=FG, fontsize=9.8, fontweight='bold', va='top')
    lines = [
        'Cell = mean of contributing FFT bins. 0.05 MHz LTE grid; 0.02 MHz GSM grid.',
        'Floor = 10th percentile of the pass-median spectrum over covered cells.',
        'Block: seed > floor+6 dB, extend > floor+4 dB, merge gaps <0.5 MHz or shallow gaps ≤3.5 MHz, minimum width 1 MHz.',
        'Narrow: isolated local max, 3 dB width ≤0.5 MHz, both 0.2–0.5 MHz side windows ≥9 dB down.',
        'Masks: 8 MHz capture edge, ±1.5 MHz DC, <50 % coverage. LTE 800 block search starts at 800 MHz.',
        'No signal notch, exclusion from the spectra, or level correction. Masked gaps are not emission gaps.',
    ]
    y = .80
    for line in lines:
        wrapped = textwrap.fill('• ' + line, 54)
        ax.text(.045, y, wrapped, transform=ax.transAxes, color=FG, fontsize=6.3,
                va='top', linespacing=1.35)
        y -= .052 * (wrapped.count('\n') + 1) + .025


def spectrum_panel(ax, b, m_on, m_off, blocks, narrows, rows):
    f = m_on['f']
    ax.fill_between(f, m_on['p10'], m_on['p90'], color=ON, alpha=.16, lw=0)
    ax.fill_between(f, m_off['p10'], m_off['p90'], color=OFF, alpha=.07, lw=0)
    ax.plot(f, np.where(m_on['ok'], m_on['med'], np.nan), color=ON, lw=.85,
            label='ON median')
    ax.plot(f, np.where(m_off['ok'], m_off['med'], np.nan), color=OFF, lw=.85,
            label='OFF median')
    ax.axhline(m_on['floor'], color=MUTED, lw=.6, ls='--', alpha=.65)
    ax.axhline(m_on['thr'], color=GOLD, lw=.7, ls=':', alpha=.85)
    bad = ~m_on['ok']
    masked = False
    i = 0
    while i < len(f):
        if bad[i]:
            j = i
            while j + 1 < len(f) and bad[j + 1]:
                j += 1
            if (j - i + 1) >= 2:
                ax.axvspan(f[i] - b['res'] / 2, f[j] + b['res'] / 2,
                           color='#64748b', alpha=.16, lw=0,
                           label='masked' if not masked else None)
                masked = True
            i = j + 1
        else:
            i += 1
    lo = m_on['floor'] - 3.0
    hi = max(m_on['peak'], np.nanpercentile(m_on['p90'][m_on['ok']], 99)) + 4.0
    for r in rows:
        a = min(x for x in (r['on_span'], r['off_span']) if x)[0]
        c = max(x for x in (r['on_span'], r['off_span']) if x)[1]
        ax.axvspan(a, c, color=GREEN, alpha=.055, lw=0)
        ax.plot([a, c], [hi - .8, hi - .8], color=GREEN, lw=2.6,
                solid_capstyle='butt', clip_on=False, alpha=.85)
    for c in narrows:
        ax.plot([c['f'], c['f']], [m_on['floor'], c['level']], color=GOLD,
                lw=.55, ls=':', alpha=.45)
        ax.plot(c['f'], c['level'], marker='v', color=GOLD, ms=4.2,
                mec=BG, mew=.3, zorder=6)
    if narrows:
        c = max(narrows, key=lambda x: x['level'])
        ax.annotate(f"{c['f']:.2f}\n+{c['above']:.1f} dB",
                    xy=(c['f'], c['level']),
                    xytext=(c['f'] + (f[-1] - f[0]) * .12, hi - (hi - lo) * .10),
                    color=GOLD, fontsize=6.9, ha='left', va='top',
                    arrowprops=dict(arrowstyle='-', color=GOLD, lw=.6, alpha=.75))
    ax.set_xlim(b['b0'], b['b1'])
    ax.set_ylim(lo, hi)
    ax.set_xticks(np.arange(b['b0'], b['b1'] + 1e-9, b['xstep']))
    style(ax, f"{b['name']} · {b['alloc']} · {b['b0']:.0f}–{b['b1']:.0f} MHz",
          'RF (MHz)', 'dB (relative)')
    ax.legend(frameon=False, fontsize=6.6, labelcolor=MUTED, ncol=2,
              loc='upper left', handlelength=1.5, columnspacing=.9)
    note(ax, f"floor {m_on['floor']:.1f} · +6 dB {m_on['thr']:.1f}\n"
             f"ON peak {m_on['peak']:.1f} @ {m_on['peak_f']:.2f}",
         'lower right', size=6.6)
    if b['key'] == 'lte2600':
        ax.text(.5, .50, 'no cell above floor+6 dB', transform=ax.transAxes,
                ha='center', va='center', color=GOLD, fontsize=8.0,
                bbox=dict(facecolor=BG, edgecolor=GRID, alpha=.82, pad=3))


def waterfall_panel(ax, b, mat, t, floor, peak, narrows, ok=None, max_cols=900):
    res = b['res']
    n = mat.shape[1]
    if n > max_cols:
        step = int(np.ceil(n / max_cols))
        m = (n // step) * step
        pooled = np.nanmedian(mat[:, :m].reshape(mat.shape[0], -1, step), axis=2)
        f = b['b0'] + (np.arange(m // step) + .5) * step * res
    else:
        pooled = mat
        f = b['b0'] + (np.arange(n) + .5) * res
    cmap = plt.get_cmap('turbo').copy()
    cmap.set_bad(BG)
    vmin = floor
    vmax = floor + min(45.0, max(6.0, peak - floor + 2.0))
    dt = np.median(np.diff(t)) if len(t) > 1 else 1.0
    im = ax.imshow(pooled, origin='lower', aspect='auto', interpolation='nearest',
                   extent=[b['b0'], b['b1'], t[0], t[-1] + dt],
                   cmap=cmap, vmin=vmin, vmax=vmax)
    for c in narrows:
        ax.axvline(c['f'], color='white', lw=.55, ls=':', alpha=.65)
    if ok is not None:
        bad = ~ok
        i = 0
        while i < len(bad):
            if bad[i]:
                j = i
                while j + 1 < len(bad) and bad[j + 1]:
                    j += 1
                if (j - i + 1) * res >= 1.0:
                    c = b['b0'] + ((i + j) / 2 + .5) * res
                    ax.axvline(c, color='#cbd5e1', lw=.6, ls=':', alpha=.75)
                i = j + 1
            else:
                i += 1
    ax.set_xlim(b['b0'], b['b1'])
    ax.set_xticks(np.arange(b['b0'], b['b1'] + 1e-9, b['xstep']))
    style(ax, f"ON waterfall · {b['name']} · {mat.shape[0]} passes",
          'RF (MHz)', 'time (s)')
    cb = ax.figure.colorbar(im, ax=ax, pad=.012, fraction=.046, aspect=22)
    cb.set_label('dB (relative)', color=MUTED, fontsize=7.2)
    cb.ax.tick_params(colors=MUTED, labelsize=6.8)
    cb.outline.set_edgecolor(GRID)
    note(ax, f"colour {vmin:.1f} … {vmax:.1f} dB\ndark/dotted = masked", 'lower right',
         size=6.4)
def dynamic_panel(ax, summ):
    y = np.arange(len(BANDS))[::-1]
    on = [summ[b['key']]['on']['peak'] - summ[b['key']]['on']['floor'] for b in BANDS]
    off = [summ[b['key']]['off']['peak'] - summ[b['key']]['off']['floor'] for b in BANDS]
    ax.barh(y + .18, on, height=.34, color=ON, label='ON')
    ax.barh(y - .18, off, height=.34, color=OFF, alpha=.85, label='OFF')
    ax.axvline(BLOCK_SEED_DB, color=GOLD, lw=.85, ls='--', alpha=.9)
    ax.text(BLOCK_SEED_DB + 1.0, y[0] + .45, '+6 dB block threshold',
            color=GOLD, fontsize=6.8, va='bottom')
    for yi, vo, vf in zip(y, on, off):
        ax.text(vo + .9, yi + .18, f'{vo:.1f}', color=ON, fontsize=6.8, va='center')
        ax.text(vf + .9, yi - .18, f'{vf:.1f}', color=OFF, fontsize=6.8, va='center')
    ax.set_yticks(y)
    ax.set_yticklabels([b['tick'] for b in BANDS])
    ax.set_xlim(0, max(on + off) * 1.14)
    style(ax, 'Peak above band floor', 'dB above 10th-pct floor', None)
    ax.legend(frameon=False, fontsize=7.0, labelcolor=MUTED, ncol=2, loc='lower right')


def occupancy_panel(ax, summ):
    x = np.arange(len(BANDS))
    po = [100 * summ[b['key']]['on']['persist'] for b in BANDS]
    ph = [100 * summ[b['key']]['on']['peakhold'] for b in BANDS]
    pf = [100 * summ[b['key']]['off']['persist'] for b in BANDS]
    ax.bar(x - .26, po, width=.24, color=ON, label='ON persistent')
    ax.bar(x, ph, width=.24, color=ON, alpha=.42, label='ON peak-hold')
    ax.bar(x + .26, pf, width=.24, color=OFF, alpha=.62, label='OFF persistent')
    for xi, a, c, d in zip(x, po, ph, pf):
        ax.text(xi - .26, a + 2, f'{a:.0f}', color=ON, fontsize=6.4, ha='center')
        ax.text(xi, c + 2, f'{c:.0f}', color=ON, fontsize=6.4, ha='center', alpha=.8)
        ax.text(xi + .26, d + 2, f'{d:.0f}', color=OFF, fontsize=6.4, ha='center')
    ax.set_xticks(x)
    ax.set_xticklabels([b['tick'] for b in BANDS], fontsize=7.0)
    ax.set_ylim(0, 108)
    style(ax, 'Occupancy above floor+6 dB', None, '% of covered cells')
    ax.legend(frameon=False, fontsize=6.6, labelcolor=MUTED, ncol=3,
              loc='upper center', bbox_to_anchor=(.5, 1.0))


def draw_table(ax, title, headers, rows, widths, fontsize=6.3, footnote=''):
    ax.axis('off')
    ax.set_title(title, color=FG, fontsize=10.2, fontweight='bold', loc='left', pad=5)
    table = ax.table(cellText=rows, colLabels=headers, cellLoc='left',
                     colWidths=widths, bbox=[0, .07, 1, .88])
    table.auto_set_font_size(False)
    table.set_fontsize(fontsize)
    for (r, c), cell in table.get_celld().items():
        cell.set_edgecolor('#1e2d47')
        if r == 0:
            cell.set_facecolor('#15233c')
            cell.set_text_props(color='#a9c1e8', fontweight='bold')
        else:
            cell.set_facecolor('#0b1426' if r % 2 else '#0e1830')
            cell.set_text_props(color=FG)
    if footnote:
        ax.text(0, .005, footnote, transform=ax.transAxes, fontsize=6.4,
                color=MUTED, va='bottom')


def interpretation(ax, summ, rows, narrow_rows):
    ax.axis('off')
    ax.set_title('What the cellular view shows — and what it does not', color=FG,
                 fontsize=11.6, fontweight='bold', loc='left', pad=5)
    lte = [r for r in rows if r['band'] == 'lte800']
    n900 = summ['gsm900']['narrow_on']
    n18 = summ['mhz1800']['narrow_on']
    n947 = max(n900, key=lambda c: c['level'])
    n926 = min(n900, key=lambda c: c['level'])
    n1840 = max(n18, key=lambda c: c['level'])
    peak2600 = summ['lte2600']['on']['peak'] - summ['lte2600']['on']['floor']
    d_lte = summ['lte800']['on']['floor'] - summ['lte800']['off']['floor']
    d_other = max(abs(summ[k]['on']['floor'] - summ[k]['off']['floor'])
                  for k in ('gsm900', 'mhz1800', 'lte2100', 'lte2600'))
    tx = [
        f"• LTE 800: two 9.2 MHz blocks at 801.30–810.50 and 811.30–820.50, separated by a 0.80 MHz gap. "
        f"ON/OFF medians differ by {lte[0]['on_med']-lte[0]['off_med']:+.1f} and {lte[1]['on_med']-lte[1]['off_med']:+.1f} dB. "
        "The elevated 791–800 MHz lower edge is not counted as a block; its cause is not established here.",
        "• 900 MHz: a 9.6 MHz block at 930.34–939.92, interrupted by the ±1.5 MHz DC mask at ~935.9–938.9 MHz "
        "(measurement gap, not an emission gap); a 5.1 MHz block at 954.38–959.50; and five narrow components, "
        f"four of them clustered at 945.2–949.9 MHz. {n947['f']:.2f} is +{n947['above']:.1f} dB and {n947['w3']:.2f} MHz wide; "
        f"{n926['f']:.2f} is ON-only. The 930–940 plateau ripple is not a set of carriers.",
        "• 1800 MHz: wideband structure from ~1811 to ~1879 MHz, detected as four threshold spans. The breaks at "
        "~1820.5 and ~1840 MHz are deep level gaps; the 1859.2–1862.2 MHz break is the ON DC mask (OFF shows a shallow dip). "
        f"The narrow {n1840['f']:.2f} component is +{n1840['above']:.1f} dB and {n1840['w3']:.2f} MHz wide. "
        "The 1849–1859 MHz plateau ripple is not a set of carriers.",
        f"• LTE 2100: one 18.1 MHz block at 2110.95–2129.05. ON misses the 2110–2114 lower edge because of the DC mask "
        f"at 2112.5 MHz; OFF shows it. LTE 2600: no cell reaches floor+6 dB; peak +{peak2600:.1f} dB. No block is claimed.",
        f"• ON/OFF are sequential, not simultaneous; differences include RF-environment change. Floors agree within {d_other:.1f} dB "
        f"for 900/1800/2100/2600; LTE 800 differs by {abs(d_lte):.1f} dB at its elevated lower edge. No correction. "
        "150–160 MHz remains unresolved and is outside this view.",
    ]
    y = .96
    for line in tx:
        wrapped = textwrap.fill(line, 178)
        ax.text(0, y, wrapped, transform=ax.transAxes, color=FG, fontsize=8.4,
                va='top', linespacing=1.4)
        y -= .066 * (wrapped.count('\n') + 1) + .030


def figure_tables(summ, rows_all):
    wide_rows = []
    for r in rows_all:
        on_span = fmt_span(*r['on_span']) if r['on_span'] else '—'
        off_span = fmt_span(*r['off_span']) if r['off_span'] else '—'
        on_med = f"{r['on_med']:.1f}" if r['on_med'] is not None else '—'
        off_med = f"{r['off_med']:.1f}" if r['off_med'] is not None else '—'
        delta = (fmt_delta(r['on_med']-r['off_med'])
                 if r['on_med'] is not None and r['off_med'] is not None else '—')
        on_pk = f"{r['on_pk']:.1f}" if r['on_pk'] is not None else '—'
        off_pk = f"{r['off_pk']:.1f}" if r['off_pk'] is not None else '—'
        wide_rows.append([r['name'], on_span, off_span, on_med, off_med, delta,
                          on_pk, off_pk, r['note']])
    wide_rows.append(['LTE 2600', '—', '—', '—', '—', '—', '—', '—',
                      'no cell above +6 dB'])
    narrow_rows = []
    for b in BANDS:
        s = summ[b['key']]
        off = s['off']
        for c in s['narrow_on']:
            j = int(np.argmin(np.abs(off['f'] - c['f'])))
            offl = float(off['med'][j])
            note = 'both' if offl > off['floor'] + BLOCK_SEED_DB else 'ON only'
            if b['key'] == 'mhz1800':
                note += '; inside plateau'
            if c['level'] == max(x['level'] for x in s['narrow_on']):
                note += '; strongest'
            narrow_rows.append([b['name'], f"{c['f']:.2f}", f"{c['level']:.1f}",
                                f"+{c['above']:.1f}", f"{c['w3']:.2f}",
                                f"{100*c['pers']:.0f}%", f"{offl:.1f}",
                                fmt_delta(c['level']-offl), note])
    return wide_rows, narrow_rows


def make_figure(summ, on_data, blocks, narrows, rows, wide_rows, narrow_rows, meta):
    plt.rcParams.update({
        'figure.facecolor': BG, 'savefig.facecolor': BG, 'text.color': FG,
        'axes.facecolor': PANEL, 'axes.edgecolor': GRID, 'axes.labelcolor': MUTED,
        'xtick.color': MUTED, 'ytick.color': MUTED, 'grid.color': GRID,
        'font.family': 'DejaVu Sans', 'axes.titleweight': 'bold',
    })
    fig = plt.figure(figsize=(26, 28), dpi=105)
    gs = GridSpec(9, 12, figure=fig, top=.914, bottom=.035, left=.040, right=.985,
                  hspace=.55, wspace=.48,
                  height_ratios=[.62, 1.0, 1.0, 1.0, 1.0, 1.0, .95, 1.15, 1.0])

    fig.text(.040, .982, 'CELLULAR DOWNLINK  ·  LTE & GSM BANDS', fontsize=25,
             fontweight='bold', color=FG, ha='left', va='top')
    bits = [meta.get('date'), f"B200mini {meta.get('device')}" if meta.get('device') else 'B200mini',
            f"antenna {meta.get('antenna')}" if meta.get('antenna') else None,
            f"{meta.get('gain')} dB" if meta.get('gain') else None,
            f"ON {meta.get('on_records'):,} rec / {meta.get('on_passes')} passes",
            f"OFF {meta.get('off_records'):,} rec / {meta.get('off_passes')} passes",
            'relative, uncalibrated']
    fig.text(.040, .954, '  ·  '.join(b for b in bits if b),
             fontsize=10.2, color=MUTED, ha='left', va='top')
    fig.text(.040, .932,
             'Band and channel names are nominal allocations; no decoding, cell identity, ARFCN or operator attribution.  '
             'ON and OFF are sequential 300 s windows, not simultaneous.  LTE 800 block detection starts at 800 MHz; '
             'no automatic notch or level correction.',
             fontsize=9.0, color=MUTED, ha='left', va='top')

    top = GridSpecFromSubplotSpec(1, 5, subplot_spec=gs[0, :], wspace=.30)
    for i, b in enumerate(BANDS):
        s = summ[b['key']]
        band_card(fig.add_subplot(top[0, i]), b, s['on'], s['off'],
                  s['n_wb'], s['n_narrow'], s['n_cluster'])

    for row, b in enumerate(BANDS, start=1):
        s = summ[b['key']]
        ax = fig.add_subplot(gs[row, :6])
        spectrum_panel(ax, b, s['on'], s['off'], blocks[b['key']],
                       narrows[b['key']], [r for r in rows if r['band'] == b['key']])
        ax = fig.add_subplot(gs[row, 6:])
        waterfall_panel(ax, b, on_data[f"on_{b['key']}"], on_data['on__t'],
                        s['on']['floor'], s['on']['peak'], narrows[b['key']],
                        s['on']['ok'])

    dynamic_panel(fig.add_subplot(gs[6, :4]), summ)
    occupancy_panel(fig.add_subplot(gs[6, 4:8]), summ)
    method_card(fig.add_subplot(gs[6, 8:]))

    draw_table(
        fig.add_subplot(gs[7, :7]),
        'Wideband blocks — threshold crossings on the pass-median spectrum',
        ['Band', 'ON span', 'OFF span', 'ON med', 'OFF med', 'Δ', 'ON pk', 'OFF pk', 'Note'],
        wide_rows,
        [.11, .135, .135, .075, .075, .065, .075, .075, .26],
        fontsize=6.2,
        footnote='Extents are not channel bandwidths. ON/OFF medians use each variant’s own sub-blocks; the spans show their unions. '
                 'A masked interval inside a span (e.g., 935.9–938.9 MHz) is a measurement gap, not an emission gap. '
                 'The 900 MHz 945.2–949.9 MHz span is a cluster of isolated narrow components, not a wideband block.',
    )
    draw_table(
        fig.add_subplot(gs[7, 7:]),
        'Isolated narrow components',
        ['Band', 'MHz', 'ON', 'Above', '3 dB', 'Persist', 'OFF', 'Δ', 'Note'],
        narrow_rows,
        [.09, .10, .085, .08, .075, .09, .085, .07, .32],
        fontsize=6.2,
        footnote='Isolated local maxima only. The 930–940 and 1849–1859 MHz plateau ripple is excluded. '
                 'The 945.2–949.9 MHz cluster is listed here, not as a wideband block.',
    )

    interpretation(fig.add_subplot(gs[8, :]), summ, rows, narrow_rows)

    fig.text(.040, .010,
             'Relative, uncalibrated levels (no dBm, noise figure or traceability).  Antenna input; sequential ON/OFF; single device, gain and antenna.  '
             'Band names are nominal.  No automatic notch or level correction.  The 150–160 MHz behaviour remains unresolved.',
             color=MUTED, fontsize=7.8)
    return fig
def write_markdown(path, meta, summ, rows_all):
    def rng(b):
        return f"{b['b0']:.0f}–{b['b1']:.0f}"

    cov_rows = []
    summ_rows = []
    for b in BANDS:
        s = summ[b['key']]
        on, off = s['on'], s['off']
        cov_rows.append([b['name'], rng(b), f"{b['res']*1000:.0f} kHz",
                         f"{100*on['cover']:.0f}%", f"{100*off['cover']:.0f}%",
                         f"{on['floor']:.2f}", f"{off['floor']:.2f}",
                         f"{on['floor']-off['floor']:+.2f}"])
        summ_rows.append([b['name'], f"{on['floor']:.2f}", f"{on['peak']:.2f}",
                          f"+{on['peak']-on['floor']:.1f}",
                          f"{100*on['persist']:.1f}%", f"{100*on['peakhold']:.1f}%",
                          f"{off['floor']:.2f}", f"{off['peak']:.2f}",
                          f"+{off['peak']-off['floor']:.1f}",
                          f"{100*off['persist']:.1f}%"])

    wb_rows = []
    for r in rows_all:
        on_span = fmt_span(*r['on_span']) if r['on_span'] else '—'
        off_span = fmt_span(*r['off_span']) if r['off_span'] else '—'
        on_med = f"{r['on_med']:.1f}" if r['on_med'] is not None else '—'
        off_med = f"{r['off_med']:.1f}" if r['off_med'] is not None else '—'
        delta = (fmt_delta(r['on_med']-r['off_med'])
                 if r['on_med'] is not None and r['off_med'] is not None else '—')
        on_pk = f"{r['on_pk']:.1f}" if r['on_pk'] is not None else '—'
        off_pk = f"{r['off_pk']:.1f}" if r['off_pk'] is not None else '—'
        wb_rows.append([r['name'], on_span, off_span, on_med, off_med, delta,
                        on_pk, off_pk, r['note']])
    wb_rows.append(['LTE 2600', '—', '—', '—', '—', '—', '—', '—',
                    'no cell above +6 dB'])

    nr_rows = []
    for b in BANDS:
        s = summ[b['key']]
        on, off = s['on'], s['off']
        for c in s['narrow_on']:
            j = int(np.argmin(np.abs(off['f'] - c['f'])))
            offl = float(off['med'][j])
            present = offl > off['floor'] + BLOCK_SEED_DB
            note = 'both' if present else 'ON only; at OFF floor'
            if b['key'] == 'mhz1800':
                note += '; inside 1800 plateau'
            if c['level'] == max(x['level'] for x in s['narrow_on']):
                note += '; strongest'
            nr_rows.append([b['name'], f"{c['f']:.2f}", f"{c['level']:.1f}",
                            f"+{c['above']:.1f}", f"{c['w3']:.2f}",
                            f"{100*c['pers']:.0f}%", f"{offl:.1f}",
                            fmt_delta(c['level']-offl), note])

    lte = [r for r in rows_all if r['band'] == 'lte800']
    g900 = [r for r in rows_all if r['band'] == 'gsm900']
    g18 = [r for r in rows_all if r['band'] == 'mhz1800']
    l21 = [r for r in rows_all if r['band'] == 'lte2100']
    n900 = summ['gsm900']['narrow_on']
    n18 = summ['mhz1800']['narrow_on']
    n947 = [c for c in n900 if abs(c['f'] - 947.81) < .1][0]
    n926 = [c for c in n900 if abs(c['f'] - 926.91) < .1][0]
    cluster900 = [c for c in n900 if 945.20 <= c['f'] <= 949.92]
    nword = {1: 'one', 2: 'two', 3: 'three', 4: 'four', 5: 'five'}.get(
        len(cluster900), str(len(cluster900)))
    cluster_list = ', '.join(f"{c['f']:.2f}" for c in cluster900)
    d_lte0 = fmt_delta(lte[0]['on_med'] - lte[0]['off_med'])
    d_lte1 = fmt_delta(lte[1]['on_med'] - lte[1]['off_med'])
    floor_lte = abs(summ['lte800']['on']['floor'] - summ['lte800']['off']['floor'])
    n1840 = [c for c in n18 if abs(c['f'] - 1840.89) < .1][0]
    b930 = [r for r in g900 if r['on_span'] and r['on_span'][0] < 945][0]
    b954 = [r for r in g900 if r['on_span'] and r['on_span'][0] > 950][0]
    b1840 = [r for r in g18 if r['on_span'] and r['on_span'][0] < 1845][0]
    b1862 = [r for r in g18 if r['on_span'] and r['on_span'][0] > 1860][0]
    m26 = summ['lte2600']['on']
    p26o = m26['peak'] - m26['floor']
    p26f = summ['lte2600']['off']['peak'] - summ['lte2600']['off']['floor']

    text = f"""# Cellular downlink: LTE and GSM bands

Relative, uncalibrated analysis of the `fullrange-20261118-111658` captures.
The input was an antenna on RX2; ON and OFF are sequential 300 s windows, not
simultaneous measurements. All levels are relative. No dBm, noise figure,
absolute floor or traceability is claimed.

Band and channel names are **nominal allocations** (LTE B20/B8/B3/B1/B7,
GSM 900/1800 downlink). No decoding, cell identity, ARFCN or operator
attribution is attempted.

![Cellular LTE/GSM dashboard](cellular_lte_gsm.png)

## Scope and method

| | |
|---|---|
| Session | `{meta.get('session', 'fullrange-20261118-111658')}` |
| Device | B200mini `{meta.get('device', '30B2A32')}`, antenna `{meta.get('antenna', 'RX2')}`, {meta.get('gain', '20')} dB |
| Bands | LTE 800 (B20), 900 MHz (GSM 900 / LTE B8), 1800 MHz (GSM 1800 / LTE B3), LTE 2100 (B1), LTE 2600 (B7) — nominal downlink ranges |
| Grid | 0.05 MHz LTE; 0.02 MHz GSM |
| ON | {meta.get('on_records', 0):,} records, {meta.get('on_passes', 0)} pass groups (last incomplete) |
| OFF | {meta.get('off_records', 0):,} records, {meta.get('off_passes', 0)} pass groups (last incomplete) |

Each capture is a 56 MHz FFT. A cell is the mean of the contributing bin dB
values; cells with fewer than 50 % of the pass groups covered are masked.
Capture edges (8 MHz) and DC (±1.5 MHz around the tuned center) are excluded.
These are measurement-quality masks, not signal notches. No level correction is
applied. A masked interval can fall inside a detected span; it is never counted
as an emission gap. LTE 800 block detection starts at 800 MHz, and the elevated
791–800 MHz structure is reported separately rather than notched.

A **wideband block** is seeded where the 0.2 MHz smoothed pass-median exceeds
the band floor +6 dB, extended while the unsmoothed median exceeds floor +4 dB,
and merged across gaps shorter than 0.5 MHz or shallow gaps up to 3.5 MHz; the
minimum width is 1 MHz. A span containing at least two isolated narrow
components and a median less than 12 dB above floor is labelled a **narrow
cluster**, not a wideband block.

A **narrow component** is an isolated local maximum with a 3 dB width of at
most 0.5 MHz and both 0.2–0.5 MHz side windows at least 9 dB below the peak.
Plateau ripple is not counted. Floors are the 10th percentile of the
pass-median spectrum over covered cells.

## Coverage and floors

{md_table(['Band', 'Range (MHz)', 'Grid', 'ON cover', 'OFF cover', 'ON floor', 'OFF floor', 'Δ floor'],
          cov_rows, ['---', '---:', '---:', '---:', '---:', '---:', '---:', '---:'])}

The LTE 800 floor is set by the elevated 791–796 MHz structure, not by the two
wideband blocks; ON is 1.60 dB below OFF there, and the ON plan also masks
796–799 MHz with the DC gap. The other four floors agree within 0.3 dB. The
difference is reported, not corrected.

## Band summary

{md_table(['Band', 'ON floor', 'ON peak', 'ON +floor', 'ON persistent', 'ON peak-hold',
           'OFF floor', 'OFF peak', 'OFF +floor', 'OFF persistent'],
          summ_rows, ['---', '---:', '---:', '---:', '---:', '---:', '---:', '---:', '---:', '---:'])}

`persistent` = fraction of covered cells whose pass-median exceeds floor +6 dB.
`peak-hold` = fraction whose 95th percentile across passes exceeds floor +6 dB.
Peak is the maximum pass-median cell, not a calibrated level.

## Wideband blocks

{md_table(['Band', 'ON span (MHz)', 'OFF span (MHz)', 'ON med', 'OFF med', 'Δ', 'ON pk', 'OFF pk', 'Note'],
          wb_rows, ['---', '---:', '---:', '---:', '---:', '---:', '---:', '---:', '---'])}

Extents are threshold crossings on the median spectrum, not channel bandwidths.
ON/OFF medians use each variant's own detected sub-blocks; the spans show their
unions. A masked interval inside a span (for example 935.9–938.9 MHz in the
900 MHz block) is a measurement gap, not an emission gap. The 900 MHz
945.2–949.9 MHz span is a cluster of isolated narrow components, not a wideband
block.

## Narrow components

{md_table(['Band', 'MHz', 'ON level', 'Above floor', '3 dB width', 'Persistence', 'OFF level', 'Δ ON−OFF', 'Note'],
          nr_rows, ['---', '---:', '---:', '---:', '---:', '---:', '---:', '---:', '---'])}

Only isolated components are listed. The 930–940 and 1849–1859 MHz plateau
ripple is excluded and is not counted as distinct carriers.

## Per-band notes

### LTE 800 (B20)

- Two adjacent 9.20 MHz blocks at 801.30–810.50 and 811.30–820.50, separated by
  a 0.80 MHz gap at ~811 MHz. ON/OFF block medians differ by {d_lte0} dB and
  {d_lte1} dB.
- The elevated 791–800 MHz lower edge is not counted as an emission block; its
  cause is not established here. The block-edge peaks at 801.38 and 811.38 MHz
  are part of the blocks, not separate narrow components.
- The ON/OFF floor difference of {floor_lte:.2f} dB comes from the elevated
  lower edge, not from the blocks, and is not corrected.

### 900 MHz (GSM 900 / LTE B8)

- **930.34–939.92 MHz**: ~9.6 MHz wideband block. The ON median is masked at
  ~935.9–938.9 MHz by the ±1.5 MHz DC mask around the 937.36 MHz hop center;
  this is a measurement gap, not an emission gap, and the block is detected
  across it. The 930–940 MHz plateau ripple is not a set of carriers.
- **954.38–959.50 MHz**: ~5.1 MHz block. OFF extends lower to 950.50 MHz; ON
  crosses the seed threshold only from 954.38 MHz. The ON 951–954 MHz cells are
  near threshold, so the exact lower edge is method-sensitive.
- **945.20–949.92 MHz**: a cluster of {nword} isolated narrow components
  ({cluster_list} MHz), not a wideband block. 947.81 MHz is the strongest at
  +{n947['above']:.1f} dB and {n947['w3']:.2f} MHz wide. 926.91 MHz, outside the
  cluster, is ON-only (+{n926['above']:.1f} dB, {100*n926['pers']:.0f} % of ON
  passes, at the OFF floor); it is not attributed to a transmitter or to the
  feature bundle.

### 1800 MHz (GSM 1800 / LTE B3)

- Wideband structure from ~1811 to ~1879 MHz, detected as four threshold spans.
  The breaks at ~1820.5 MHz (0.9 MHz) and ~1840 MHz (1.7 MHz) are deep level
  gaps. The ~1831–1834 MHz ON gap is the ±1.5 MHz DC mask around the
  1832.70 MHz hop center, not an emission gap. The 1859.2–1862.2 MHz break
  coincides with the ON DC mask; OFF shows a shallow dip there, so the two
  sides are not claimed to be separated by a real emission gap on ON.
- The narrow component at 1840.89 MHz is +{n1840['above']:.1f} dB and
  {n1840['w3']:.2f} MHz wide; it sits inside the 1840.7–1859.0 MHz block.
  The 1849–1859 MHz plateau ripple is not counted as distinct carriers.
- The 1862–1879 MHz span is weaker. The ON detected span stops at 1876.06 MHz
  where a narrow dip crosses the extension threshold, while OFF reaches
  1878.94 MHz; this is a weak-tail threshold effect, not a verified
  differential fade. It is not claimed as a verified allocation.

### LTE 2100 (B1)

- One 18.10 MHz block at 2110.95–2129.05 MHz. ON detects 2114.00–2129.05 MHz;
  the missing lower 2110–2114 MHz edge is the ±1.5 MHz DC mask at 2112.5 MHz,
  not an emission difference. OFF shows the lower edge.
- The block has a sawtooth/ripple structure; only the block extent is claimed.
  No isolated narrow component survives the isolation test.

### LTE 2600 (B7)

- No cell reaches floor +6 dB in either variant. The ON peak median is
  +{p26o:.1f} dB above the 10th-percentile floor at {m26['peak_f']:.2f} MHz;
  OFF is +{p26f:.1f} dB.
  No wideband or narrow block is claimed. Absence of a threshold crossing is
  not proof that no signal exists below the threshold.

## ON/OFF comparison

- ON and OFF are sequential 300 s windows, not simultaneous. Differences
  include RF-environment change.
- Band floors (ON − OFF): LTE 800 {summ['lte800']['on']['floor']-summ['lte800']['off']['floor']:+.2f} dB,
  900 {summ['gsm900']['on']['floor']-summ['gsm900']['off']['floor']:+.2f} dB,
  1800 {summ['mhz1800']['on']['floor']-summ['mhz1800']['off']['floor']:+.2f} dB,
  LTE 2100 {summ['lte2100']['on']['floor']-summ['lte2100']['off']['floor']:+.2f} dB,
  LTE 2600 {summ['lte2600']['on']['floor']-summ['lte2600']['off']['floor']:+.2f} dB.
  The LTE 800 value comes from the elevated lower edge described above.
- Wideband block medians differ by at most 1.9 dB (the 1840.7–1859.0 MHz block);
  the LTE 2100 block differs by 1.7 dB. No correction is applied.
- Narrow components are present in both variants within 1.7 dB except 926.91 MHz,
  which is ON-only. The 1840.89 MHz component is 1.1 dB lower in ON.
- The 900 MHz 954 MHz block and the 1800 MHz 1862 MHz block have different
  ON/OFF extents. The 900 MHz difference is at a near-threshold edge; the
  1800 MHz upper edge is weak and method-sensitive, and its 1859–1862 MHz
  break coincides with the ON DC mask. These are not a uniform level shift.

## 150–160 MHz

The session's 150–160 MHz behaviour remains unresolved: the ON per-pass median
range is 18.78 dB, while OFF sits near −131.17 dB. It is outside the cellular
bands, is not used here, and is not notched, excluded or corrected. Resolving it
requires a controlled 50 Ω input and/or an alternating ON/OFF source.

## Limits

- Relative, uncalibrated levels only; antenna input; no dBm, noise figure,
  absolute floor or traceability.
- Sequential ON/OFF; single device, gain and antenna; 300 s per variant; not a
  soak test.
- Nominal band labels; no decoding, cell identity, ARFCN or operator attribution.
- No per-feature ablation; the ON/OFF hop plans differ.
- LTE 2600 has no threshold crossing; that is not proof of absence below the
  threshold.
- The 8 MHz capture-edge, ±1.5 MHz DC and <50 % coverage masks are
  measurement-quality masks, not signal notches. No level correction is applied.
- 150–160 MHz remains unresolved.

## Reproduce

```sh
env -u PYTHONPATH sessions/.venv/bin/python tools/report_cellular.py \\
    sessions/fullrange-20261118-111658 \\
    --out docs/results/fullrange-20261118-111658/cellular_lte_gsm.png \\
    --md docs/results/fullrange-20261118-111658/CELLULAR.md
```

Add `--use-cache` to reuse
`sessions/fullrange-20261118-111658/report/cellular_cache.npz`. Raw JSONL stays
in `sessions/` and is not committed.
"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text)
    return path
def session_meta(sess, cache_meta):
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
    return dict(
        session=sess.name, date=date, device=device, antenna=antenna, gain=gain,
        on_records=cache_meta.get('on_records', 0),
        on_passes=cache_meta.get('on_passes', 0),
        off_records=cache_meta.get('off_records', 0),
        off_passes=cache_meta.get('off_passes', 0),
    )


def load_data(sess, args):
    cache = Path(args.cache) if args.cache else sess / 'report' / 'cellular_cache.npz'
    if args.use_cache and cache.exists():
        z = np.load(cache)
        meta = json.loads(z['meta'].item()) if 'meta' in z.files else {}
        if (meta.get('edge_hz') == EDGE_HZ and meta.get('dc_hz') == DC_HZ
                and meta.get('bands') == [b['key'] for b in BANDS]):
            data = {k: z[k] for k in z.files if k != 'meta'}
            print(f'loaded cache {cache}')
            return data, meta
        print(f'cache {cache} does not match this analysis; re-extracting')
    on, on_records, on_passes = extract(sess / 'features-on.jsonl')
    off, off_records, off_passes = extract(sess / 'features-off.jsonl')
    data = {}
    for k, v in on.items():
        data[f'on_{k}'] = v
    for k, v in off.items():
        data[f'off_{k}'] = v
    meta = dict(edge_hz=EDGE_HZ, dc_hz=DC_HZ, bands=[b['key'] for b in BANDS],
                on_records=on_records, on_passes=on_passes,
                off_records=off_records, off_passes=off_passes,
                extracted_at=datetime.now(timezone.utc).isoformat())
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache, **data, meta=json.dumps(meta))
    print(f'wrote cache {cache}')
    return data, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('session')
    ap.add_argument('--out', default=None, help='dashboard PNG path')
    ap.add_argument('--md', default=None, help='markdown report path')
    ap.add_argument('--cache', default=None, help='npz cache path')
    ap.add_argument('--use-cache', action='store_true', help='load the npz cache if valid')
    a = ap.parse_args()
    sess = Path(a.session)
    outpath = Path(a.out) if a.out else sess / 'report' / 'cellular_lte_gsm.png'
    mdpath = Path(a.md) if a.md else sess / 'report' / 'CELLULAR.md'
    outpath.parent.mkdir(parents=True, exist_ok=True)

    data, cache_meta = load_data(sess, a)
    meta = session_meta(sess, cache_meta)

    summ = {}
    for b in BANDS:
        on = band_metrics(data[f'on_{b["key"]}'], b)
        off = band_metrics(data[f'off_{b["key"]}'], b)
        blocks_on = detect_blocks(on, b)
        blocks_off = detect_blocks(off, b)
        narrow_on = detect_narrow(on['f'], on['med'], data[f'on_{b["key"]}'],
                                  on['ok'], on['floor'], b['res']) if b['narrow'] else []
        narrow_off = detect_narrow(off['f'], off['med'], data[f'off_{b["key"]}'],
                                   off['ok'], off['floor'], b['res']) if b['narrow'] else []
        classify_blocks(blocks_on, narrow_on, on['floor'])
        classify_blocks(blocks_off, narrow_off, off['floor'])
        wb_on = [x for x in blocks_on if x['kind'] == 'wideband']
        wb_off = [x for x in blocks_off if x['kind'] == 'wideband']
        rows_b = match_blocks(wb_on, wb_off, b)
        summ[b['key']] = dict(
            on=on, off=off, blocks_on=blocks_on, blocks_off=blocks_off,
            narrow_on=narrow_on, narrow_off=narrow_off,
            n_wb=len(wb_on), n_narrow=len(narrow_on),
            n_cluster=sum(1 for x in blocks_on if x['kind'] == 'narrow cluster'),
            rows=rows_b,
        )

    rows_all = [r for b in BANDS for r in summ[b['key']]['rows']]
    narrows = {b['key']: summ[b['key']]['narrow_on'] for b in BANDS}
    blocks = {b['key']: summ[b['key']]['rows'] for b in BANDS}

    for b in BANDS:
        s = summ[b['key']]
        on, off = s['on'], s['off']
        print(f"\n{b['name']} {b['b0']:.0f}-{b['b1']:.0f} MHz")
        print(f"  ON  cover {100*on['cover']:.0f}% floor {on['floor']:.2f} "
              f"peak {on['peak']:.2f} ({on['peak']-on['floor']:+.2f}) @ {on['peak_f']:.2f} "
              f"persist {100*on['persist']:.1f}% peakhold {100*on['peakhold']:.1f}%")
        print(f"  OFF cover {100*off['cover']:.0f}% floor {off['floor']:.2f} "
              f"peak {off['peak']:.2f} ({off['peak']-off['floor']:+.2f}) @ {off['peak_f']:.2f} "
              f"persist {100*off['persist']:.1f}% peakhold {100*off['peakhold']:.1f}%")
        print(f"  {s['n_wb']} wideband ON, {s['n_narrow']} narrow ON, {s['n_cluster']} cluster ON")
        for r in s['rows']:
            print(f"    block ON {fmt_span(*r['on_span']) if r['on_span'] else '—'} "
                  f"OFF {fmt_span(*r['off_span']) if r['off_span'] else '—'} "
                  f"ONmed {r['on_med']:.2f} OFFmed {r['off_med']:.2f} "
                  f"d {r['on_med']-r['off_med']:+.2f} note {r['note']}")
        for c in s['narrow_on']:
            j = int(np.argmin(np.abs(s['off']['f'] - c['f'])))
            print(f"    narrow {c['f']:.2f} level {c['level']:.2f} above {c['above']:.2f} "
                  f"w3 {c['w3']:.2f} pers {100*c['pers']:.0f}% "
                  f"OFF {s['off']['med'][j]:.2f} d {c['level']-s['off']['med'][j]:+.2f}")

    wide_rows, narrow_rows = figure_tables(summ, rows_all)
    fig = make_figure(summ, data, blocks, narrows, rows_all,
                      wide_rows, narrow_rows, meta)
    fig.savefig(outpath, bbox_inches='tight')
    print('wrote', outpath)
    write_markdown(mdpath, meta, summ, rows_all)
    print('wrote', mdpath)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())





