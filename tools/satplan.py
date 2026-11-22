#!/usr/bin/env python3
"""DC-free hop plans for satellite bands (2 MS/s B200, 8192 bins).

  satplan.py PRESET|BANDS.json OUT_bands.json [--rate MSPS]   -> writes bands.json (bands + centres), prints the centres
  --rate: sample rate in MS/s (default 2). The trusted half-width scales with it (0.425 x rate: 0.85 MHz at 2 MS/s,
  measured flat to -1 dB at 0.91 MHz); the DC exclusion stays 100 kHz. Bins for 244 Hz resolution: 4096 x rate.

A hop at centre c is trusted on [c-usable, c-dc] and [c+dc, c+usable] (DC skirt excluded, passband flat).
A band of width <= usable-dc gets one hop with the band in its upper window. Wider bands get N >= 2 hops
spaced (usable-dc) apart starting at lo+usable: every hop's DC gap then falls in a neighbour's window,
so the band is covered contiguously and no bin of interest is ever read within dc of an LO."""
import json, math, sys
from pathlib import Path

USABLE, DC = 850e3, 100e3
PRESETS = {
    'sat': [('VHF 137-138 MHz (weather sats, Orbcomm, CubeSat)', 137.0e6, 138.0e6),
            ('VHF 145.8-146.0 MHz (amateur satellite)', 145.8e6, 146.0e6),
            ('UHF 435-438 MHz (amateur satellite)', 435.0e6, 438.0e6)],
    'sat-full': [('VHF 137-138 MHz (weather sats, Orbcomm, CubeSat)', 137.0e6, 138.0e6),
                 ('VHF 144-146 MHz (2 m incl. satellite sub-band)', 144.0e6, 146.0e6),
                 ('UHF 399.9-403 MHz (space operation / research, commercial CubeSats)', 399.9e6, 403.0e6),
                 ('UHF 430-440 MHz (70 cm incl. 435-438 satellite sub-band)', 430.0e6, 440.0e6)],
    'sat-s': [('S 2200-2290 MHz (space research / EESS downlinks)', 2200.0e6, 2290.0e6),
              ('S 2400-2450 MHz (amateur satellite S-band)', 2400.0e6, 2450.0e6)],
}


def hops_for(lo, hi, usable=USABLE, dc=DC):
    w, step = hi - lo, usable - dc
    if w <= step:
        return [lo - dc]
    n = max(2, math.ceil((w - 2 * usable) / step) + 1)
    return [lo + usable + k * step for k in range(n)]


def covered(f, centers, usable=USABLE, dc=DC):
    return any(dc <= abs(f - c) <= usable for c in centers)


def plan(bands, usable=USABLE, dc=DC):
    bands = sorted(bands, key=lambda b: b[1]); merged = []
    for name, lo, hi in bands:                      # merge overlapping bands
        if merged and lo <= merged[-1][2]:
            merged[-1] = (merged[-1][0] + ' + ' + name, merged[-1][1], max(hi, merged[-1][2]))
        else:
            merged.append((name, lo, hi))
    centers = []
    for _, lo, hi in merged:
        centers += [round(c / 1e3) * 1e3 for c in hops_for(lo, hi, usable, dc)]
    centers = sorted(set(centers))
    for name, lo, hi in merged:                     # self-check: every 1 kHz of every band covered
        k = int((hi - lo) // 1e3)
        bad = [lo + i * 1e3 for i in range(k + 1) if not covered(lo + i * 1e3, centers, usable, dc)]
        if bad:
            raise SystemExit(f'plan leaves {name} uncovered at {bad[0] / 1e6:.4f} MHz')
    return dict(dc_exclude_hz=dc, usable_halfwidth_hz=usable, centers_hz=centers,
                bands=[dict(name=n, lo_hz=lo, hi_hz=hi) for n, lo, hi in merged])


def main():
    a = sys.argv[1:]
    rate = float(a[a.index('--rate') + 1]) if '--rate' in a else 2.0
    a = [x for i, x in enumerate(a) if x != '--rate' and (i == 0 or a[i - 1] != '--rate')]
    if len(a) < 2:
        print(__doc__)
        for r in (2, 4, 8):
            print(f'{r} MS/s:', ', '.join(f'{k} ({len(plan(v, usable=0.425 * r * 1e6)["centers_hz"])} hops)' for k, v in PRESETS.items()))
        return 2
    sys.argv = [sys.argv[0]] + a
    src = sys.argv[1]
    bands = PRESETS[src] if src in PRESETS else [(b['name'], b['lo_hz'], b['hi_hz']) for b in json.loads(Path(src).read_text())['bands']]
    p = plan(bands, usable=0.425 * rate * 1e6); p['preset'] = src if src in PRESETS else 'custom'
    p['sample_rate_hz'] = rate * 1e6; p['bins'] = int(round(4096 * rate))
    Path(sys.argv[2]).write_text(json.dumps(p, indent=1))
    print(','.join(f'{c / 1e6:.6g}M' for c in p['centers_hz']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
