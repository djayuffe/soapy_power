"""Bounded, non-destructive spur diagnostics on measured JSONL scan records.

Only contiguous common measured IF support is pooled. Context changes, tuning
orientation, units, gain and DSP settings are separated. Sequential LO tracks
are hypotheses: this adapter never generates notches or invents phase.
"""

import hashlib
import html
import json
from pathlib import Path


def analyze_files(paths, output, max_records=192, max_groups=12, max_bins=8192):
    import numpy as np
    from soapypower.spur_pipeline import make_capture, analyze_sweeps

    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    groups, skipped, sources = {}, {}, []
    seen = 0
    examined = 0
    group_budget = min(max_groups, max(1, max_records // 8))

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    for source in paths:
        digest = hashlib.sha256()
        with Path(source).open('rb') as stream:
            for line in stream:
                digest.update(line)
                examined += 1
                if len(line) > 4 * 1024**2:
                    skip('oversized record')
                    continue
                try:
                    r = json.loads(line)
                    m = r['metadata']
                    ctx = m['context']
                    if m.get('measurement_status') != 'complete' or m.get('continuity') != 'verified':
                        raise ValueError('incomplete or unverified acquisition')
                    if m.get('frequency_sign', 1) != 1 or m.get('iq_conjugate') or ctx.get('lnb_lo', 0):
                        raise ValueError('transformed frequency axis needs an explicit capture manifest')
                    if ctx.get('agc') is True:
                        raise ValueError('AGC context is not a fixed-gain comparison')
                    if (
                        m.get('headroom', {}).get('accepted') is False
                        or m.get('rf_state', {}).get('after', {}).get('validity', {}).get('passed') is False
                    ):
                        raise ValueError('explicit receive guard failure')
                    grid = m['frequency_grid']
                    if type(grid['first_bin_index']) is not int:
                        raise ValueError('IF grid index must be an integer')
                    rate, step, center = float(m['sample_rate_hz']), float(grid['step_hz']), float(m['tuned_center_hz'])
                    p = np.array([np.nan if v is None else v for v in r['power']], float)
                    if (
                        len(p) > max_bins
                        or len(p) < 16
                        or not np.isfinite([rate, step, center]).all()
                        or min(rate, step) <= 0
                    ):
                        raise ValueError('unsupported grid size or frequency metadata')
                    if grid['bin_count'] != len(p) or abs(float(grid['rf_origin_hz']) - center) > step * 1e-5:
                        raise ValueError('grid origin does not match tuned center')
                    analog = m.get('rf_state', {}).get('after', {}).get('tuning_components_hz', {}).get('RF')
                    if analog is not None and abs(float(analog) - center) > step / 2:
                        raise ValueError('nonzero analog/digital tuning split needs explicit LO manifest')
                    unit = r['unit']
                    if unit not in ('input power/Hz', 'dB(input power/Hz)', 'mW/Hz', 'dBm/Hz'):
                        raise ValueError('unsupported density unit')
                    if unit.startswith('dB'):
                        p = np.power(10.0, p / 10.0)
                    for k in set(r.get('estimated_bins',[]))|set(r.get('masked_bins',[])):
                        if type(k) is not int or not 0<=k<len(p):raise ValueError('Invalid masked/estimated bin')
                        p[k]=np.nan
                    fixed = {
                        k: v
                        for k, v in ctx.items()
                        if k not in ('frequency', 'freq', 'center_hz', 'frequency_hz', 'tuned_center_hz')
                    }
                    # Adjacent LO groups share an RF neighbourhood; distant bands
                    # cannot be attributed using the same noise/signal environment.
                    key = json.dumps(
                        [
                            fixed,
                            rate,
                            step,
                            len(p),
                            grid['first_bin_index'],
                            unit,
                            m.get('window'),
                            m.get('segment_count'),
                            m.get('overlap_samples'),
                            m.get('detrend'),
                            m.get('dc_mask_width_hz'),
                            m.get('dc_offset_hz'),
                            int(center // rate),
                        ],
                        sort_keys=True,
                    )
                    if key not in groups and len(groups) >= group_budget:
                        worst = max(groups, key=lambda k: hashlib.sha256(k.encode()).hexdigest())
                        if hashlib.sha256(key.encode()).hexdigest() >= hashlib.sha256(worst.encode()).hexdigest():
                            skip('deterministic context sampling budget')
                            continue
                        del groups[worst]
                    group = groups.setdefault(key, {})
                    if center not in group and len(group) >= 8:
                        skip('LO views per context budget')
                        continue
                    # Earliest complete view per LO; repeated rows aren't independent LO evidence.
                    group.setdefault(
                        center,
                        dict(
                            power=p,
                            center=center,
                            rate=rate,
                            step=step,
                            first=int(grid['first_bin_index']),
                            metadata=m,
                            source=str(source),
                            time=r.get('time_start'),
                        ),
                    )
                    seen += 1
                except (ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
                    skip(str(error))
        sources.append(dict(path=str(source), sha256=digest.hexdigest()))
    reports = []
    for group in groups.values():
        rows = list(group.values())[:8]
        if len(rows) < 3:
            skip('fewer than three distinct LO views in a matched local context')
            continue
        common = np.logical_and.reduce([np.isfinite(row['power']) & (row['power'] > 0) for row in rows])
        # Split at every unmeasured/estimated bin: never bridge a masked DC hole.
        edges = np.flatnonzero(np.diff(np.r_[False, common, False]))
        for a, b in zip(edges[::2], edges[1::2]):
            if b - a < 16:
                continue
            if len(reports) >= max_groups:
                skip('analysis group budget reached')
                break
            axis = (np.arange(a, b) + rows[0]['first']) * rows[0]['step']
            captures = [
                make_capture(
                    row['center'],
                    row['power'][a:b],
                    if_hz=axis,
                    rate_hz=row['rate'],
                    metadata={'source': row['source'], 'time': row['time']},
                    capture_id=str(i),
                )
                for i, row in enumerate(rows)
            ]
            try:
                report = analyze_sweeps(captures, dc_mask_hz=0, orders=(1, 3, 5, 7), alpha=1e-3)
                name = 'group-%02d.json' % (len(reports) + 1)
                (output / name).write_text(json.dumps(report, indent=1, allow_nan=False) + '\n')
                reports.append(
                    dict(
                        file=name,
                        views=len(rows),
                        if_start_hz=float(axis[0]),
                        if_stop_hz=float(axis[-1]),
                        tracks=len(report['tracks']),
                        signals=len(report['signals']),
                        spurs=len(report['spurs']),
                    )
                )
            except (ValueError, FloatingPointError, OverflowError) as error:
                skip('inference unavailable: ' + str(error))
    summary = dict(
        version=1,
        scope='Conditional sequential-LO hypotheses; no automatic exclusions',
        phase='Unavailable from PSD; use retained complex IQ with analysis:phase or noise --coherent-features',
        methods=[
            'Measured common IF support',
            'Gamma floor / inferred DOF',
            'BH false discovery control',
            'Robust LO slopes',
            'Bayesian signal/image/harmonic attribution',
            'Orders 1,3,5,7',
        ],
        limitations=[
            'Sequential RF variability can mimic tracks',
            'DOF is estimated, not independently measured',
            'Tuned center is the LO proxy; known nonzero tuning splits are excluded',
            'Deterministic context sampling across all source rows; bounded LO views and support groups',
            'No absolute calibration, phase recovery, or guarantee of interference origin',
        ],
        sources=sources,
        examined_records=examined,
        selected_records=sum(len(g) for g in groups.values()),
        groups=reports,
        skipped=skipped,
    )
    (output / 'summary.json').write_text(json.dumps(summary, indent=2, allow_nan=False) + '\n')
    cards = ''.join(
        '<tr>'
        + ''.join('<td>' + html.escape(str(g[k])) + '</td>' for k in ('file', 'views', 'tracks', 'signals', 'spurs'))
        + '</tr>'
        for g in reports
    )
    document = '<!doctype html><meta charset="utf-8"><title>Measured spur audit</title>'
    document += '<style>body{font:15px system-ui;max-width:1100px;margin:50px auto;padding:20px;color:#243446}table{border-collapse:collapse;width:100%}td,th{padding:12px;border-bottom:1px solid #ccd3dc;text-align:left}pre{white-space:pre-wrap}</style>'
    document += '<h1>Measured spur audit</h1><p>' + html.escape(summary['scope']) + '</p>'
    document += (
        '<table><tr><th>Group</th><th>LO views</th><th>Tracks</th><th>Signal hypotheses</th><th>Spur hypotheses</th></tr>'
        + cards
        + '</table>'
    )
    document += '<h2>Evidence and limits</h2><pre>' + html.escape(json.dumps(summary, indent=2)) + '</pre>'
    (output / 'index.html').write_text(document, encoding='utf-8')
    return summary


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', nargs='+', help='Measured JSONL scan files')
    parser.add_argument('--out', required=True, help='Fresh output directory')
    parser.add_argument('--max-records', type=int, default=192, help='Representative row memory budget, 24–1536')
    parser.add_argument('--max-groups', type=int, default=12, help='Matched support group limit, 1–96')
    parser.add_argument(
        '--max-bins', type=int, default=8192, help='Maximum exact bins/view, 16–65536; no interpolation'
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 24 <= args.max_records <= 1536 or not 1 <= args.max_groups <= 96 or not 16 <= args.max_bins <= 65536:
        parser.error('Analysis budget is outside supported bounds')
    return analyze_files(args.input, args.out, args.max_records, args.max_groups, args.max_bins)


if __name__ == '__main__':
    main()
