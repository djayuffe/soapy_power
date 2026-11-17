#!/usr/bin/env python3
"""True-3D waterfalls from fullrange_waterfall.json: x = RF (MHz, linear), y = time (s, linear), z = level (dB, linear).
Every figure uses the same z range, colour scale and time axis, so figures are directly comparable. Relative, uncalibrated levels.
usage: report_waterfalls3d.py WATERFALL.json OUTDIR"""
import json, sys
from pathlib import Path
import numpy as np, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import colors

ZMIN, ZMAX = -136.0, -100.0
BANDS = [(70, 6000, 'full range'), (70, 300, '70-300 MHz'), (300, 1000, '300-1000 MHz'), (1000, 3000, '1-3 GHz'), (3000, 6000, '3-6 GHz')]
MARK = [('FM', 98), ('DAB-III', 225), ('LTE800', 805), ('GSM900', 935), ('GSM1800', 1840), ('UMTS', 2120), ('2.4 GHz ISM', 2410)]
BG, FG = '#070b14', '#c9d4e6'
plt.rcParams.update({'figure.facecolor': BG, 'axes.facecolor': BG, 'text.color': FG, 'axes.labelcolor': FG, 'xtick.color': FG, 'ytick.color': FG, 'font.size': 11})


def load(path):
    W = json.load(open(path)); f = W['start_mhz'] + W['cell_mhz'] * (np.arange(len(W['features-on']['grid'][0])) + .5); out = {}
    for k in ('features-on', 'features-off'):
        g = np.array([[np.nan if x is None else x for x in r] for r in W[k]['grid']], float); t = np.array([np.nan if x is None else x for x in W[k]['t0_s']], float)
        ok = np.isfinite(g).mean(1) > .5; out[k] = (g[ok], t[ok])
    return f, out


def render(f, g, t, lo, hi, title, fname, outdir):
    m = (f >= lo) & (f <= hi); x = f[m]; z = np.clip(g[:, m], ZMIN, ZMAX)
    X, Y = np.meshgrid(x, t); norm = colors.Normalize(ZMIN, ZMAX); cmap = plt.get_cmap('turbo')
    fig = plt.figure(figsize=(20, 12), dpi=110); ax = fig.add_subplot(111, projection='3d')
    for a in (ax.xaxis, ax.yaxis, ax.zaxis):
        a.set_pane_color((0.04, 0.06, 0.11, 1)); a.line.set_color('#2b3a55'); a._axinfo['grid']['color'] = (0.3, 0.4, 0.6, 0.25)
    ax.plot_surface(X, Y, np.where(np.isfinite(z), z, ZMIN), facecolors=cmap(norm(np.nan_to_num(z, nan=ZMIN))), rstride=1, cstride=1, linewidth=0, antialiased=True, shade=False)
    ax.set_xlim(lo, hi); ax.set_ylim(0, 300); ax.set_zlim(ZMIN, ZMAX)
    ax.set_xlabel('RF (MHz, lineær)', labelpad=14); ax.set_ylabel('tid (s, lineær)', labelpad=12); ax.set_zlabel('nivå (dB relativt, lineær)', labelpad=10)
    span = hi - lo; ax.set_box_aspect((float(np.clip(span / 400, 1.8, 3.6)), 1.5, 1.0), zoom=1.3); ax.view_init(elev=27, azim=-58)
    for n, fq in MARK:
        if lo <= fq <= hi:
            ax.text(fq, -10, ZMAX + 1, n, color='#ffd166', fontsize=9, ha='center')
    sm = plt.cm.ScalarMappable(norm=norm, cmap=cmap); cb = fig.colorbar(sm, ax=ax, shrink=.5, pad=.02); cb.set_label('dB relativt (samme skala i alle figurer)')
    fig.suptitle(title, fontsize=19, fontweight='bold', x=.06, ha='left', y=.95)
    fig.text(.06, .905, f'B200mini · antenne · 2 MHz-celler · {g.shape[0]} passer · z {ZMIN:.0f}…{ZMAX:.0f} dB, tid 0–300 s og fargeskala identisk på alle · relativt, ukalibrert', color='#8fa3c4', fontsize=11)
    fig.savefig(outdir / f'{fname}.png', bbox_inches='tight'); fig.savefig(outdir / f'{fname}.pdf', bbox_inches='tight'); plt.close(fig)
    return fname


def main():
    path, outdir = sys.argv[1], Path(sys.argv[2]); outdir.mkdir(parents=True, exist_ok=True)
    f, d = load(path); made = []
    for key, nm in (('features-on', 'ON'), ('features-off', 'OFF')):
        g, t = d[key]
        for lo, hi, bn in BANDS:
            made.append(render(f, g, t, lo, hi, f'Vannfall 3D — features {nm} — {bn}', f'wf3d_{nm.lower()}_{bn.replace(" ", "").replace("/", "-").replace("–", "-")}', outdir))
    print('\n'.join(made))


if __name__ == '__main__':
    main()
