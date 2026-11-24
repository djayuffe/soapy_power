"""Command-line entry point for the signal/spur analysis.

Examples
--------
    soapy_power_spurs demo --out docs/spurs-demo
    soapy_power_spurs analyze captures.json --out analysis
    soapy_power_spurs plan --center 260e6 --profile b200

The ``demo`` command uses only synthetic data.  It never opens a radio and
never starts a measurement session.
"""
import argparse
import json
import math
from pathlib import Path
import sys

import numpy as np

from soapypower import spur_design as sd
from soapypower import spur_pipeline as sp
from soapypower import spur_twin as st
from soapypower.floor_estimation import floor_temperature_kalman


def _json_write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, allow_nan=False) + '\n', encoding='utf-8')
    return str(path)


def _read_captures(path):
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if isinstance(payload, list):
        payload = {'captures': payload}
    if 'captures' not in payload:
        raise ValueError('input must contain a captures list')
    return payload


def _twin_report(scenario, twin, profile='b200'):
    captures = sp.captures_from_twin(twin, scenario['los'], tones=scenario['tones'])
    return sp.analyze_sweeps(captures, profile=profile)


def _score_report(report, expected, step_hz):
    found = []
    for track in report.get('tracks', []):
        kind = track.get('kind')
        if kind == 'lo_harmonic':
            kind = 'spur-{}'.format(track.get('order'))
        found.append({'if_hz': track.get('points', [{}])[0].get('if_hz', 0.0),
                      'kind': kind})
    return st.score(found, expected, step_hz)


def _demo_temperature(outdir):
    """A synthetic floor-vs-temperature data set for the Kalman/plot path."""
    rng = np.random.default_rng(7)
    times = np.arange(96, dtype=float) * 10.0
    temperature = 44.0 + 2.2 * np.sin(2.0 * math.pi * times / 900.0) + rng.normal(0, 0.08, times.size)
    floor = 0.03 * np.sin(2.0 * math.pi * times / 310.0) - 0.020 * (temperature - 45.0) + rng.normal(0, 0.035, times.size)
    groups = (times // 60).astype(int)
    fit = floor_temperature_kalman(times, temperature, floor, groups)
    payload = {'times_s': times.tolist(), 'temps_c': temperature.tolist(),
               'floor_db': floor.tolist(), 'fit': fit}
    _json_write(Path(outdir) / 'floor_vs_temperature.json', payload)
    return payload


def command_demo(args):
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    scenarios = st.scenarios()
    twin = st.Twin(bins=args.bins, rate_hz=args.rate, dof=args.dof, seed=args.seed)
    all_reports, scores = {}, {}
    for name, scenario in scenarios.items():
        report = _twin_report(scenario, twin, profile=args.profile)
        all_reports[name] = report
        _json_write(outdir / (name.replace(' ', '_').replace('/', '-') + '.json'), report)
        scores[name] = _score_report(report, scenario['expected'], twin.step)
    temperature = _demo_temperature(outdir)
    # A drive-order demonstration: a linear reference tone and a cubic
    # intermodulation product, each measured at three drive levels and three
    # LO settings.  The linear tone must follow 1 dB/dB, the cubic product
    # approximately 3 dB/dB.
    drive_captures = []
    for drive in (0.0, -5.0, -10.0):
        for lo in (256e6, 258e6, 260e6, 262e6, 264e6):
            drive_captures.append(sp.make_capture(
                lo, twin.capture(lo, tones=((266e6, 35.0),),
                                 imd=((268e6, 35.0, 3),), drive_db=drive),
                if_hz=twin.if_hz, rate_hz=twin.rate, dof=twin.dof,
                drive_db=drive, capture_id='drive-{:.0f}-{:.0f}'.format(drive, lo / 1e6)))
    drive_report = sp.analyze_sweeps(drive_captures, profile=args.profile)
    _json_write(outdir / 'drive_order.json', drive_report)
    # Use the first scenario with a classified harmonic response for the
    # before/after and slope figures; fall back to T3.
    figure_report = None
    for name in ('T3 800 MHz out-of-band tone',):
        if name in all_reports:
            figure_report = all_reports[name]
            break
    if figure_report is None:
        figure_report = all_reports.get(sorted(all_reports)[0])
    mixing = None
    if figure_report:
        try:
            figure = st.scenarios()['T3 800 MHz out-of-band tone']
            mixing = {'lo_hz': figure['los'][0], 'rf_sources': [260e6, 800e6],
                      'orders': (1, 3, 5, 7), 'rate_hz': twin.rate}
        except Exception:
            mixing = None
    figure_paths = {}
    try:
        from soapypower import spur_visuals as sv
        if figure_report:
            figure_paths = sv.save_report_figures(figure_report, outdir, mixing=mixing,
                                                  temperature=temperature)
    except RuntimeError as exc:
        figure_paths['error'] = str(exc)
    # A drive-order figure shows observed level versus input drive.
    try:
        from soapypower import spur_visuals as sv
        if drive_report.get('tracks'):
            figure_paths['drive_order'] = sv.plot_drive_order(drive_report, outdir / 'drive_order.png')
    except (RuntimeError, ValueError):
        pass
    summary = {
        'version': 1,
        'scope': 'synthetic demo; no hardware, no automatic exclusions',
        'scenario_scores': scores,
        'scenario_reports': {name: {'signals': len(r['signals']), 'intermod': len(r.get('intermod', [])),
                                    'spurs': len(r['spurs']),
                                    'unconfirmed': len(r['unconfirmed']),
                                    'floor_change_db': r['floor']['change_db']}
                             for name, r in all_reports.items()},
        'drive_order': {
            'diagnostics': drive_report['diagnostics'],
            'tracks': [{'attribution': t['attribution'], 'drive_kind': t['drive_kind'],
                        'slope_db_per_db': t['drive'].get('slope_db_per_db'),
                        'order': t['drive'].get('order'),
                        'source_hz': t['source_hz']}
                       for t in drive_report['tracks']],
        },
        'temperature_fit': temperature['fit'],
        'figures': figure_paths,
        'not_run': ['T1-T5 hardware tests', '50 ohm termination', 'signal generator'],
    }
    _json_write(outdir / 'summary.json', summary)
    lines = ['# Signal/spur analysis demo', '',
             'Synthetic only. No radio was opened; no automatic exclusion was produced.', '',
             '| scenario | tp | fp | missed | kind errors |', '|---|---:|---:|---:|---:|']
    for name, score in scores.items():
        lines.append('| {} | {} | {} | {} | {} |'.format(name, score['tp'], score['fp'],
                                                         score['missed'], score['kind_errors']))
    lines += ['', '## Drive diversity', '',
              '| track | attribution | drive kind | drive slope (dB/dB) | source (MHz) |',
              '|---|---|---|---:|---:|']
    for track in summary['drive_order']['tracks']:
        slope = track['slope_db_per_db']
        lines.append('| {} | {} | {} | {} | {:.3f} |'.format(
            'tone' if track['attribution'] == 'signal' else 'intermod product',
            track['attribution'], track['drive_kind'],
            '{:.2f}'.format(slope) if slope is not None else 'n/a',
            (track['source_hz'] or 0.0) / 1e6))
    lines += ['', '## Figures', '']
    for name, path in figure_paths.items():
        lines.append('- {}: `{}`'.format(name, path))
    lines += ['', '## Scope', '',
              'The scores are software verification against a known truth. They are not a',
              'claim of hardware performance. The controlled T1-T5 programme and the',
              'before/after hardware comparison still require a lab session.']
    (outdir / 'README.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('wrote {}'.format(outdir))
    return 0


def command_analyze(args):
    payload = _read_captures(args.input)
    options = dict(payload.get('options') or {})
    options.setdefault('profile', payload.get('profile', args.profile))
    options.setdefault('alpha', args.alpha)
    if args.orders:
        options['orders'] = tuple(args.orders)
    report = sp.analyze_sweeps(payload['captures'], **options)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    _json_write(outdir / 'report.json', report)
    figures = {}
    try:
        from soapypower import spur_visuals as sv
        figures = sv.save_report_figures(report, outdir)
    except RuntimeError as exc:
        figures['error'] = str(exc)
    print(json.dumps({'output': str(outdir / 'report.json'), 'figures': figures,
                      'signals': len(report['signals']), 'spurs': len(report['spurs']),
                      'unconfirmed': len(report['unconfirmed'])}, indent=1))
    return 0


def command_plan(args):
    report = sd.design_report(center_hz=args.center, rate_hz=args.rate,
                              profile=args.profile, count=args.count,
                              span_hz=args.span)
    path = Path(args.out)
    if path.suffix.lower() == '.json':
        _json_write(path, report)
    else:
        path.mkdir(parents=True, exist_ok=True)
        _json_write(path / 'plan.json', report)
    print(json.dumps(report['identifiability'], indent=1))
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog='soapy_power_spurs',
                                 description='Signal versus spur analysis for direct-conversion SDRs')
    sub = ap.add_subparsers(dest='command', required=True)
    demo = sub.add_parser('demo', help='synthetic T1-T5 and figure demo')
    demo.add_argument('--out', default='spurs-demo')
    demo.add_argument('--bins', type=int, default=4096)
    demo.add_argument('--rate', type=float, default=56e6)
    demo.add_argument('--dof', type=float, default=20.0)
    demo.add_argument('--seed', type=int, default=0)
    demo.add_argument('--profile', default='b200')
    demo.set_defaults(func=command_demo)

    analyze = sub.add_parser('analyze', help='analyze measured captures from JSON')
    analyze.add_argument('input')
    analyze.add_argument('--out', default='spur-analysis')
    analyze.add_argument('--profile', default='b200')
    analyze.add_argument('--alpha', type=float, default=1e-3)
    analyze.add_argument('--orders', type=int, nargs='+', default=None)
    analyze.set_defaults(func=command_analyze)

    plan = sub.add_parser('plan', help='D-optimal LO/gain/filter experiment design')
    plan.add_argument('--center', type=float, default=260e6)
    plan.add_argument('--rate', type=float, default=56e6)
    plan.add_argument('--profile', default='b200')
    plan.add_argument('--count', type=int, default=3)
    plan.add_argument('--span', type=float, default=None)
    plan.add_argument('--out', default='spur-plan.json')
    plan.set_defaults(func=command_plan)
    return ap


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
