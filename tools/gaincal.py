#!/usr/bin/env python3
"""Automatic gain selection from a short gain sweep over the scan's own hop centres.

  gaincal.py OUT.json CAL_DIR [--bands bands.json] [--fallback DB] [--png OUT.png]
  CAL_DIR holds gain_<G>.jsonl (one sweep, soapy_power --headroom) and gain_<G>.rc (exit code) per tested gain.

Levels are digital (dB(input power/Hz)), so floor(G) - G is the input-referred noise. It falls while the ADC/IF
noise dominates and flattens once the front end dominates. Chosen gain = the smallest gain whose input-referred
noise is within 1 dB of the best safe gain, where safe means: the sweep completed, every hop's raw IQ stayed
below 0.5 of full scale (max component) and 0.2 at the 99.9th percentile, and no sample reached 0.95 (the
--headroom guard rejects the run otherwise). Prints the chosen gain (dB) on stdout."""
import json, math, sys
from pathlib import Path
import numpy as np

MAX_COMPONENT, P999_COMPONENT, WITHIN_DB = 0.5, 0.2, 1.0


def measure(path, dc=100e3, usable=850e3):
    hops = []
    with open(path) as fh:
        lines = fh.readlines()
    for line in lines:
        if not line.strip():
            continue
        r = json.loads(line); m = r['metadata']; p = np.array([np.nan if v is None else v for v in r['power']], float)
        n = len(p); off = (np.arange(n) - n // 2) * float(r['step_hz']); sel = (np.abs(off) >= dc) & (np.abs(off) <= usable) & np.isfinite(p)
        hr = m.get('headroom') or {}
        hops.append(dict(center_hz=float(m['tuned_center_hz']), floor_db=float(np.median(p[sel])), peak_db=float(np.max(p[sel])),
                         max_component=hr.get('max_component'), p999_component=hr.get('p999_component'), t095=hr.get('threshold_095_fraction'),
                         status=m.get('measurement_status')))
    return hops


def choose(cal_dir, bands=None, fallback=None, usable=850e3):
    cal = Path(cal_dir); rows = []
    expected = None
    for f in sorted(cal.glob('gain_*.jsonl'), key=lambda p: float(p.stem.split('_')[1])):
        g = float(f.stem.split('_')[1]); rcf = cal / f'gain_{f.stem.split("_")[1]}.rc'
        rc = int(rcf.read_text().strip()) if rcf.exists() and rcf.read_text().strip().lstrip('-').isdigit() else None
        hops = measure(f, usable=usable) if f.stat().st_size else []
        expected = max(expected or 0, len(hops))
        hr_ok = bool(hops) and all(h['max_component'] is not None and h['max_component'] < MAX_COMPONENT and h['p999_component'] < P999_COMPONENT and h['t095'] == 0 for h in hops)
        rows.append(dict(gain_db=g, rc=rc, hops=hops, n_hops=len(hops),
                         input_noise_db=float(np.median([h['floor_db'] - g for h in hops])) if hops else None,
                         floor_db=float(np.median([h['floor_db'] for h in hops])) if hops else None,
                         max_component=max((h['max_component'] or 0) for h in hops) if hops else None,
                         p999_component=max((h['p999_component'] or 0) for h in hops) if hops else None))
    for r in rows:
        r['safe'] = bool(r['rc'] == 0 and r['n_hops'] == expected and r['n_hops'] > 0 and r['max_component'] is not None
                         and r['max_component'] < MAX_COMPONENT and r['p999_component'] < P999_COMPONENT
                         and all(h['t095'] == 0 for h in r['hops']))
    safe = [r for r in rows if r['safe']]
    if not safe:
        return dict(chosen_gain_db=fallback, reason='no safe calibration point; fallback gain used', rows=rows)
    best = min(r['input_noise_db'] for r in safe)
    pick = min((r for r in safe if r['input_noise_db'] <= best + WITHIN_DB), key=lambda r: r['gain_db'])
    top = max(safe, key=lambda r: r['gain_db'])
    reason = (f"smallest safe gain within {WITHIN_DB:.1f} dB of the best input-referred noise ({best:.1f} dB); "
              f"highest safe gain tested {top['gain_db']:.0f} dB (max component {top['max_component']:.2f}, p99.9 {top['p999_component']:.3f})")
    out = dict(chosen_gain_db=pick['gain_db'], reason=reason, best_input_noise_db=best, rows=rows,
               limits=dict(max_component=MAX_COMPONENT, p999_component=P999_COMPONENT, within_db=WITHIN_DB))
    if bands:
        per = []
        for b in bands:
            ins = [r for r in rows if r['hops']]
            curve = [(r['gain_db'], float(np.median([h['floor_db'] - r['gain_db'] for h in r['hops'] if b['lo_hz'] - usable <= h['center_hz'] <= b['hi_hz'] + usable] or [np.nan])))
                     for r in ins]
            per.append(dict(name=b['name'], input_noise_db=curve))
        out['bands'] = per
    return out


def plot(res, png):
    import matplotlib
    matplotlib.use('Agg'); import matplotlib.pyplot as plt
    rows = [r for r in res['rows'] if r['hops']]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14, 5.2), dpi=110, facecolor='#070b14')
    for ax in (a1, a2):
        ax.set_facecolor('#0b1222'); ax.tick_params(colors='#c9d4e6'); [s.set_color('#2b3a55') for s in ax.spines.values()]
        ax.grid(color='#24324f', lw=.5)
    for b in res.get('bands', []):
        g = [x for x, y in b['input_noise_db'] if np.isfinite(y)]; y = [y for x, y in b['input_noise_db'] if np.isfinite(y)]
        a1.plot(g, y, 'o-', lw=1.4, label=b['name'][:34])
    a1.plot([r['gain_db'] for r in rows], [r['input_noise_db'] for r in rows], 'w--', lw=1, label='all hops (median)')
    if res.get('chosen_gain_db') is not None:
        a1.axvline(res['chosen_gain_db'], color='#ffb000', lw=1.2); a2.axvline(res['chosen_gain_db'], color='#ffb000', lw=1.2)
    a1.set_xlabel('gain (dB)', color='#c9d4e6'); a1.set_ylabel('input-referred noise (dB/Hz, digital − gain)', color='#c9d4e6')
    a1.legend(fontsize=8, facecolor='#0b1222', labelcolor='#c9d4e6', frameon=False)
    a2.semilogy([r['gain_db'] for r in rows], [max(r['max_component'], 1e-4) for r in rows], 'o-', color='#ff4d6d', label='max |I|,|Q| (fraction of full scale)')
    a2.semilogy([r['gain_db'] for r in rows], [max(r['p999_component'], 1e-4) for r in rows], 'o-', color='#00d1ff', label='99.9th percentile')
    a2.axhline(MAX_COMPONENT, color='#ff4d6d', ls=':'); a2.axhline(P999_COMPONENT, color='#00d1ff', ls=':')
    for r in res['rows']:
        if not r['safe']:
            a2.axvspan(r['gain_db'] - 2, r['gain_db'] + 2, color='#ff4d6d22')
    a2.set_xlabel('gain (dB)', color='#c9d4e6'); a2.set_ylabel('raw IQ headroom', color='#c9d4e6')
    a2.legend(fontsize=8, facecolor='#0b1222', labelcolor='#c9d4e6', frameon=False)
    fig.suptitle(f"Gain calibration → {res.get('chosen_gain_db')} dB · {res.get('reason', '')[:120]}", color='#c9d4e6', fontsize=10)
    fig.tight_layout(); fig.savefig(png, facecolor=fig.get_facecolor()); plt.close(fig)


def main():
    a = sys.argv[1:]
    if len(a) < 2:
        print(__doc__); return 2
    bj = json.loads(Path(a[a.index('--bands') + 1]).read_text()) if '--bands' in a else {}
    fb = float(a[a.index('--fallback') + 1]) if '--fallback' in a else None
    res = choose(a[1], bj.get('bands'), fb, usable=float(bj.get('usable_halfwidth_hz', 850e3)))
    Path(a[0]).write_text(json.dumps(res, indent=1, default=float))
    png = a[a.index('--png') + 1] if '--png' in a else str(Path(a[0]).with_suffix('.png'))
    try:
        plot(res, png)
    except Exception as e:
        print(f'gaincal plot failed: {e}', file=sys.stderr)
    if res['chosen_gain_db'] is None:
        return 1
    g = res['chosen_gain_db']; print(int(g) if float(g).is_integer() else g)
    return 0


if __name__ == '__main__':
    sys.exit(main())
