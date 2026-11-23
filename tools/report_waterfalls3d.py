#!/usr/bin/env python3
"""True-3D waterfalls (v2) from fullrange_waterfall.json.
x = RF (MHz, linear), y = time (s, linear, 0-300), z = level (dB relative, linear, -136..-100 on every figure).
Same z range, colour scale and time axis on every figure -> directly comparable. Relative, uncalibrated levels.
Effects: relief shading, floor heat-map projection, back-wall max-hold, glowing max-hold ridge, labelled peak stems,
nice tick spacing, scale box. 70-300 MHz also shows the 0.1 MHz suite p10/median/p90 band (needs SUITE.json).
usage: report_waterfalls3d.py WATERFALL.json OUTDIR [SUITE.json] [--gif]"""
import json, sys
from pathlib import Path
import numpy as np, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import colors
from matplotlib.collections import PolyCollection
from matplotlib.colors import LightSource

ZMIN, ZMAX, ZFLOOR, TMAX = -136.0, -100.0, -142.0, 300.0
BANDS = [(70, 6000, 'fullrange'), (70, 300, '70-300MHz'), (300, 1000, '300-1000MHz'), (1000, 3000, '1-3GHz'), (3000, 6000, '3-6GHz')]
MARK = [('FM', 98), ('DAB-III', 225), ('LTE800', 805), ('GSM900', 935), ('GSM1800', 1840), ('UMTS', 2120), ('2.4G ISM', 2410)]
BG, FG, GLOW = '#05080f', '#c9d4e6', '#ffd166'
plt.rcParams.update({'figure.facecolor': BG, 'axes.facecolor': BG, 'text.color': FG, 'axes.labelcolor': FG, 'xtick.color': FG, 'ytick.color': FG, 'font.size': 11})
CMAP = plt.get_cmap('turbo'); NORM = colors.Normalize(ZMIN, ZMAX)


def load(path):
    W = json.load(open(path)); first = next(k for k in ('features-on', 'features-off') if k in W)
    f = W['start_mhz'] + W['cell_mhz'] * (np.arange(len(W[first]['grid'][0])) + .5); out = {}
    for k in ('features-on', 'features-off'):
        if k not in W: continue
        g = np.array([[np.nan if x is None else x for x in r] for r in W[k]['grid']], float); t = np.array([np.nan if x is None else x for x in W[k]['t0_s']], float)
        ok = (np.isfinite(g).mean(1) > .5) & np.isfinite(t); out[k] = (g[ok], t[ok])
    return f, out


def time_extent(data):
    """One shared display axis containing every observed pass timestamp."""
    times=[float(v) for _,t in data.values() for v in t if np.isfinite(v)]
    if not times:raise ValueError('no observed waterfall timestamps')
    return max(1.0,float(np.ceil(max(times)/50)*50))


def nice_step(span, n=8):
    raw = span / n; p = 10 ** np.floor(np.log10(raw)); return p * min((1, 2, 5, 10), key=lambda m: abs(m * p - raw))


def peaks(x, mx, k=6, sep=0.02):
    idx, out = np.argsort(-mx), []
    for i in idx:
        if np.isfinite(mx[i]) and mx[i] > -125 and all(abs(x[i] - x[j]) >= sep * (x[-1] - x[0]) for j in out): out.append(i)
        if len(out) >= k: break
    return out


def build(f, g, t, lo, hi, title, nm, suite=None, step=1):
    m = (f >= lo) & (f <= hi); x = f[m][::step]; z = np.clip(g[:, m][:, ::step], ZMIN, ZMAX); zf = np.where(np.isfinite(z), z, ZMIN)
    X, Y = np.meshgrid(x, t)
    ls = LightSource(azdeg=300, altdeg=38)
    rgb=ls.shade(zf,cmap=CMAP,norm=NORM,blend_mode='soft',vert_exag=1.6,dx=max(x[1]-x[0],1e-9),dy=np.median(np.diff(t))) if len(t)>1 and len(x)>1 else CMAP(NORM(zf))
    fig = plt.figure(figsize=(20, 12.5), dpi=110); ax = fig.add_axes([-.07, -.02, 1.1, 1.0], projection='3d')
    for a in (ax.xaxis, ax.yaxis, ax.zaxis):
        a.set_pane_color((0.03, 0.05, 0.09, 1)); a.line.set_color('#2b3a55'); a._axinfo['grid']['color'] = (0.3, 0.4, 0.6, 0.22)
    ax.set_xlim(lo, hi); ax.set_ylim(0, TMAX); ax.set_zlim(ZFLOOR, ZMAX)
    if z.shape[0]>1 and z.shape[1]>1:ax.contourf(X, Y, np.ma.masked_invalid(z), zdir='z', offset=ZFLOOR, levels=np.linspace(ZMIN, ZMAX, 25), cmap=CMAP, norm=NORM, alpha=.95)      # floor heat-map projection
    ax.plot_surface(X, Y, np.ma.masked_invalid(z), facecolors=rgb, rstride=1, cstride=1, linewidth=0, antialiased=True, shade=False)
    mx = np.nanmax(z, 0); tm = t[np.nanargmax(np.where(np.isfinite(z), z, -1e9), 0)]
    verts = [list(zip(x, np.maximum(mx, ZMIN))) + [(x[-1], ZFLOOR), (x[0], ZFLOOR)]]
    wall = PolyCollection(verts, facecolors='#00d1ff', alpha=.16, edgecolors='none'); ax.add_collection3d(wall, zs=TMAX, zdir='y')   # back wall: max-hold
    ax.plot(x, np.full_like(x, TMAX), np.maximum(mx, ZMIN), color='#00d1ff', lw=1.0, alpha=.9)
    for lw, al in ((8, .07), (4, .16), (1.6, .95)): ax.plot(x, np.full_like(x, TMAX), np.maximum(mx, ZMIN) + .1, color=GLOW, lw=lw, alpha=al)   # glow on the max-hold wall
    for i in peaks(x, mx, k=5 if hi - lo > 2000 else 6, sep=0.05 if hi - lo > 2000 else 0.03):
        ax.plot([x[i], x[i]], [tm[i], tm[i]], [ZFLOOR, mx[i]], color='#ffffff', lw=.7, alpha=.55, ls=':')
        ax.scatter([x[i]], [tm[i]], [mx[i]], color='#ffffff', s=22, depthshade=False)
        ax.text(x[i], tm[i], mx[i] + 2.2, f'{x[i]:.0f} MHz\n{mx[i]:.0f} dB', color='#ffffff', fontsize=8.5, ha='center', va='bottom')
    if suite is not None and hi <= 400:
        rf = np.array([np.nan if v is None else v for v in suite['rf_mhz']]); p10, md, p90 = (np.array([np.nan if v is None else v for v in suite[k]]) for k in ('p10', 'median_db', 'p90'))
        k = np.isfinite(rf) & np.isfinite(p10) & np.isfinite(p90)
        ax.add_collection3d(PolyCollection([list(zip(rf[k], np.clip(p90[k], ZFLOOR, ZMAX))) + list(zip(rf[k][::-1], np.clip(p10[k][::-1], ZFLOOR, ZMAX)))], facecolors='#ff3d81', alpha=.22), zs=0, zdir='y')
        ax.plot(rf[k], np.zeros(k.sum()), np.clip(md[k], ZFLOOR, ZMAX), color='#ff3d81', lw=1.0)
    ax.set_xlabel('RF (MHz, lineær)', labelpad=16); ax.set_ylabel('tid (s, lineær)', labelpad=12); ax.set_zlabel('nivå (dB relativt, lineær)', labelpad=10)
    ax.set_xticks(np.arange(np.ceil(lo / nice_step(hi - lo)) * nice_step(hi - lo), hi + 1e-6, nice_step(hi - lo))); ax.set_yticks(np.arange(0, TMAX + 1, 50)); ax.set_zticks(np.arange(-135, ZMAX + 1, 5))
    span = hi - lo; ax.set_box_aspect((float(np.clip(1.6 + 2.0 * np.log10(span / 230 + 1), 1.9, 4.2)), 1.55, 1.0), zoom=1.28)
    for n, fq in MARK:
        if lo <= fq <= hi and (hi - lo) < 3000 or (n in ('GSM900', '2.4G ISM') and hi - lo >= 3000 and lo <= fq <= hi):
            ax.text(fq, -14, ZFLOOR, n, color=GLOW, fontsize=8.5, ha='center', va='top')
    cb = fig.colorbar(plt.cm.ScalarMappable(norm=NORM, cmap=CMAP), ax=ax, shrink=.42, pad=.09, aspect=26); cb.set_label('dB relativt · samme skala i alle figurer'); cb.outline.set_edgecolor('#2b3a55')
    fig.suptitle(title, fontsize=21, fontweight='bold', x=.05, ha='left', y=.985)
    fig.text(.05, .945, f'B200mini · antenne · 2 MHz-celler · {g.shape[0]} passer · alle akser lineære · z {ZMIN:.0f}…{ZMAX:.0f} dB, tid 0–{TMAX:.0f} s og fargeskala identisk på alle figurer · relativt, ukalibrert', color='#8fa3c4', fontsize=11)
    floor = np.nanmedian(z); fig.text(.05, .045, f'Gulv (median) {floor:.1f} dB · maks {np.nanmax(z):.1f} dB · dynamikk {np.nanmax(z) - floor:.1f} dB\n'
        f'Rutenett: x {nice_step(hi - lo):g} MHz · y 50 s · z 5 dB   |   gul glød + cyan vegg = maks-hold over alle passer (bakvegg) · hvite stolper = sterkeste topper på tidspunktet de inntraff · gulvet = varmekart sett ovenfra' + ('\nrosa bakerst = suite (0,1 MHz) p10–p90 + median' if suite is not None and hi <= 400 else ''), color='#8fa3c4', fontsize=10, va='top')
    return fig, ax


def main():
    a = [x for x in sys.argv[1:] if not x.startswith('--')]; gif = '--gif' in sys.argv
    path, outdir = a[0], Path(a[1]); outdir.mkdir(parents=True, exist_ok=True); suite = json.load(open(a[2]))['telemetry_on'] if len(a) > 2 and Path(a[2]).exists() else None
    f, d = load(path); made = []
    global TMAX
    TMAX=time_extent(d)
    for key, nm in (('features-on', 'ON'), ('features-off', 'OFF')):
        if key not in d: continue
        g, t = d[key]
        for lo, hi, bn in BANDS:
            fig, ax = build(f, g, t, lo, hi, f'Vannfall 3D — features {nm} — {bn}', nm, suite if nm == 'ON' else None)
            fn = f'wf3d_{nm.lower()}_{bn}'; fig.savefig(outdir / f'{fn}.png', bbox_inches='tight'); fig.savefig(outdir / f'{fn}.pdf', bbox_inches='tight'); plt.close(fig); made.append(fn)
    if gif:
        from PIL import Image
        for key, nm, lo, hi, bn in (('features-on', 'ON', 70, 6000, 'fullrange'), ('features-on', 'ON', 70, 300, '70-300MHz')):
            if key not in d: continue
            g, t = d[key]; fig, ax = build(f, g, t, lo, hi, f'Vannfall 3D — features {nm} — {bn}', nm, suite if (lo == 70 and hi == 300) else None, step=2 if hi > 1000 else 1); fig.set_size_inches(14, 9); fig.set_dpi(70); frames = []
            for k in range(48):
                ph = 2 * np.pi * k / 48; ax.view_init(elev=26 + 6 * np.cos(ph), azim=-58 + 48 * np.sin(ph)); fig.canvas.draw(); frames.append(Image.fromarray(np.asarray(fig.canvas.buffer_rgba())[..., :3]).convert('P', palette=Image.ADAPTIVE, colors=200))
            frames[0].save(outdir / f'wf3d_{nm.lower()}_{bn}.gif', save_all=True, append_images=frames[1:], duration=80, loop=0, optimize=True); plt.close(fig); made.append(f'wf3d_{nm.lower()}_{bn}.gif')
    print('\n'.join(made))


if __name__ == '__main__':
    main()
