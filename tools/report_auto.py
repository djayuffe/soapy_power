#!/usr/bin/env python3
"""Autogenerate the full report for a fullrange session directory (features-on.jsonl / features-off.jsonl).
Writes SESSION/report/: compare.json (if missing), waterfall.json (if missing), 10 true-3D waterfalls (PNG+PDF),
intermittency page, summary.json, one merged PDF (if pypdf is installed) and README.txt.
Never fails the caller: missing matplotlib/numpy or data is reported in report/status.txt and the exit code is 0.
usage: report_auto.py SESSION [--gif] [--suite dash_suite.json]"""
import json, subprocess, sys, traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent


def sh(args, out=None):
    r = subprocess.run([sys.executable] + [str(a) for a in args], capture_output=True, text=True, env={**__import__('os').environ, 'PYTHONPATH': str(HERE.parent)})
    if out is not None and r.returncode == 0:
        Path(out).write_text(r.stdout)
    return r


def intermittency(f, d, out):
    import numpy as np, matplotlib
    matplotlib.use('Agg'); import matplotlib.pyplot as plt
    BG, FG = '#070b14', '#c9d4e6'
    plt.rcParams.update({'figure.facecolor': BG, 'axes.facecolor': '#0b1222', 'text.color': FG, 'axes.labelcolor': FG, 'xtick.color': FG, 'ytick.color': FG, 'axes.edgecolor': '#2b3a55', 'axes.grid': True, 'grid.color': '#24324f', 'grid.alpha': .5, 'axes.spines.top': False, 'axes.spines.right': False, 'axes.titleweight': 'bold', 'axes.titlelocation': 'left'})
    ref = d.get('features-on') or d.get('features-off'); g, t = ref
    edges = np.arange(70, 6000, 10); meds, sds = [], []
    for lo in edges:
        m = (f >= lo) & (f < lo + 10); v = np.nanmedian(g[:, m], 1) if m.any() else np.full(g.shape[0], np.nan); meds.append(np.nanmedian(v)); sds.append(np.nanstd(v))
    meds, sds = np.array(meds), np.array(sds); chosen = []
    for i in np.argsort(-np.nan_to_num(sds)):
        if meds[i] > -134 and all(abs(edges[i] - edges[j]) >= 40 for j in chosen): chosen.append(i)
        if len(chosen) >= 3: break
    ctrl = [i for i in np.argsort(np.nan_to_num(sds, nan=1e9)) if meds[i] > -128]
    if ctrl: chosen.append(ctrl[0])
    fig, axs = plt.subplots(2, 2, figsize=(18, 10.5), dpi=100); fig.suptitle('Intermittens: nivå per pass i de mest varierende båndene (+ én stabil kontroll)', x=.05, ha='left', fontsize=18, fontweight='bold')
    for ax, i, role in zip(axs.flat, chosen, ['mest variabel', 'variabel 2', 'variabel 3', 'stabil kontroll']):
        lo = edges[i]; m = (f >= lo) & (f < lo + 10)
        for key, col, mk, lab in (('features-on', '#00d1ff', 'o-', 'ON'), ('features-off', '#ffb000', 's-', 'OFF')):
            if key in d:
                gg, tt = d[key]; v = np.nanmedian(gg[:, m], 1); ax.plot(tt, v, mk, color=col, label=f'{lab} (σ {np.nanstd(v):.1f} dB)')
        ax.set_title(f'{lo:.0f}–{lo + 10:.0f} MHz · {role}'); ax.set_xlabel('s fra start'); ax.set_ylabel('dB relativt'); ax.legend(frameon=False)
    fig.tight_layout(rect=(0, 0, 1, .95)); fig.savefig(out / 'intermittent.png', bbox_inches='tight'); fig.savefig(out / 'intermittent.pdf', bbox_inches='tight'); plt.close(fig)


def main():
    a = [x for x in sys.argv[1:] if not x.startswith('--')]
    if not a:
        print(__doc__); return 0
    sess = Path(a[0]); out = sess / 'report'; out.mkdir(exist_ok=True); status = []
    suite = None
    if '--suite' in sys.argv:
        suite = sys.argv[sys.argv.index('--suite') + 1]
    try:
        import numpy, matplotlib  # noqa
    except Exception as e:
        (out / 'status.txt').write_text(f'skipped: {e}\n'); print('report skipped:', e); return 0
    try:
        if not (sess / 'compare.json').exists() or (sess / 'compare.json').stat().st_size < 10:
            r = sh([HERE / 'fullrange_compare.py', sess], sess / 'compare.json'); status.append(f'compare exit {r.returncode}')
        if not (sess / 'waterfall.json').exists() or (sess / 'waterfall.json').stat().st_size < 10:
            r = sh([HERE / 'fullrange_waterfall.py', sess], sess / 'waterfall.json'); status.append(f'waterfall exit {r.returncode}{" " + r.stderr[-200:] if r.returncode else ""}')
        wf = sess / 'waterfall.json'
        if not wf.exists() or wf.stat().st_size < 10:
            raise RuntimeError('no waterfall.json (no features-on/off jsonl?)')
        args = [HERE / 'report_waterfalls3d.py', wf, out] + ([suite] if suite else ['']) + (['--gif'] if '--gif' in sys.argv else [])
        if not suite: args.pop(3)
        r = sh(args); status.append(f'3d exit {r.returncode}{" " + r.stderr[-300:] if r.returncode else ""}')
        sys.path.insert(0, str(HERE)); import report_waterfalls3d as w3
        f, d = w3.load(wf); intermittency(f, d, out); status.append('intermittency ok')
        cmp_ = sess / 'compare.json'
        if cmp_.exists() and cmp_.stat().st_size > 10:
            c = json.loads(cmp_.read_text()); (out / 'summary.json').write_text(json.dumps({'agreement': c.get('agreement'), **{k: {x: v.get(x) for x in ('records', 'hops', 'passes', 'seconds', 'records_per_s', 'temperature_records')} for k, v in c.get('variants', {}).items()}}, indent=1))
        try:
            from pypdf import PdfWriter, PdfReader
            wr = PdfWriter()
            for nm in ('on', 'off'):
                for bn in ('fullrange', '70-300MHz', '300-1000MHz', '1-3GHz', '3-6GHz'):
                    p = out / f'wf3d_{nm}_{bn}.pdf'
                    if p.exists():
                        i = len(wr.pages); [wr.add_page(pg) for pg in PdfReader(str(p)).pages]; wr.add_outline_item(f'{nm.upper()} {bn}', i)
            if (out / 'intermittent.pdf').exists():
                i = len(wr.pages); [wr.add_page(pg) for pg in PdfReader(str(out / 'intermittent.pdf')).pages]; wr.add_outline_item('Intermittens', i)
            if len(wr.pages):
                wr.write(str(out / 'report.pdf')); status.append(f'pdf {len(wr.pages)} pages')
        except ImportError:
            status.append('pdf merge skipped (pypdf missing)')
        (out / 'README.txt').write_text('Autogenerated by tools/report_auto.py. Relative, uncalibrated levels; antenna input mixes RF-environment change into ON/OFF differences.\nwf3d_*.png/pdf: true-3D waterfalls (linear x MHz, y s, z dB; identical z, colour and time scale).\nintermittent.png: per-pass level in the most variable 10 MHz windows.\nreport.pdf: all pages merged.\n')
    except Exception:
        status.append('error: ' + traceback.format_exc()[-600:])
    (out / 'status.txt').write_text('\n'.join(status) + '\n'); print('\n'.join(status)); return 0


if __name__ == '__main__':
    sys.exit(main())
