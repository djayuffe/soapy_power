"""Bounded high-resolution previews and guard inventory for expert captures.

Plots use maximum-preserving display pooling, never calibrated rebinning. Each
attempt stays separate, and its row-dependent RF tuning is shown explicitly.
"""

import hashlib
import html
import json
from pathlib import Path


def summarize(path):
    digest = hashlib.sha256()
    result = dict(
        file=Path(path).name,
        records=0,
        complete=0,
        continuity_verified=0,
        headroom_accepted=0,
        rf_state_passed=0,
        all_guards_passed=0,
        trailing_fragment=False,
    )
    with Path(path).open('rb') as source:
        for line in source:
            digest.update(line)
            if not line.endswith(b'\n'):
                result['trailing_fragment'] = True
                continue
            row = json.loads(line)
            m = row.get('metadata') or {}
            flags = (
                m.get('measurement_status') == 'complete',
                m.get('continuity') == 'verified',
                m.get('headroom', {}).get('accepted') is True,
                m.get('rf_state', {}).get('after', {}).get('validity', {}).get('passed') is True,
            )
            result['records'] += 1
            for key, value in zip(('complete', 'continuity_verified', 'headroom_accepted', 'rf_state_passed'), flags):
                result[key] += int(value)
            result['all_guards_passed'] += int(all(flags))
    result['sha256'] = digest.hexdigest()
    return result


def generate(paths, output, max_rows=512, max_bins=1024):
    import numpy as np
    import matplotlib

    matplotlib.use('Agg')
    from matplotlib import pyplot as plt

    output = Path(output)
    output.mkdir(exist_ok=True)
    summaries = []
    figures = []
    for path_index, path in enumerate(paths, 1):
        summary = summarize(path)
        summaries.append(summary)
        if not summary['records']:
            continue
        stride = max(1, int(np.ceil(summary['records'] / max_rows)))
        rows, centers, first, steps = [], [], [], []
        unit = None
        with Path(path).open('rb') as source:
            for index, line in enumerate(source):
                if index % stride or not line.endswith(b'\n'):
                    continue
                record = json.loads(line)
                meta = record.get('metadata', {})
                p = np.asarray([np.nan if v is None else v for v in record['power']], float)
                # Display blocks crossing a mask stay masked. No interpolation.
                width = max(1, int(np.ceil(len(p) / max_bins)))
                reduced = np.array(
                    [
                        np.max(p[k : k + width]) if np.isfinite(p[k : k + width]).all() else np.nan
                        for k in range(0, len(p), width)
                    ]
                )
                unit = record.get('unit', '')
                if not unit.startswith('dB'):
                    reduced = 10 * np.log10(np.where(reduced > 0, reduced, np.nan))
                rows.append(reduced)
                centers.append(meta.get('tuned_center_hz', np.nan))
                first.append(record.get('first_bin_center_hz', record.get('start_hz', np.nan)))
                steps.append(record.get('step_hz', np.nan) * width)
        if not rows or len({len(row) for row in rows}) != 1:
            summary['preview'] = 'Unavailable: changing grid shape'
            continue
        data = np.stack(rows)
        finite = data[np.isfinite(data)]
        if not finite.size:
            summary['preview'] = 'Unavailable: no finite measured bins'
            continue
        fig, (ax, tune) = plt.subplots(
            2, 1, figsize=(18, 10), gridspec_kw={'height_ratios': [4, 1]}, constrained_layout=True
        )
        cmap = plt.get_cmap('inferno').copy()
        cmap.set_bad('#d8dde2')
        # Every row can tune to another RF. Using column index avoids inventing
        # a common RF/IF grid when DSP orientation or spacing changes.
        image = ax.imshow(
            np.ma.masked_invalid(data),
            aspect='auto',
            interpolation='nearest',
            origin='lower',
            cmap=cmap,
            vmin=np.percentile(finite, 5),
            vmax=np.percentile(finite, 99.5),
        )
        ax.set(
            title=Path(path).name + ' · raw measured density preview',
            xlabel='Display bin group (maximum per group)',
            ylabel='Selected measured row · gaps are not elapsed time',
        )
        fig.colorbar(image, ax=ax, label=unit if unit.startswith('dB') else '10 log10(' + unit + ')')
        tune.plot(np.arange(len(centers)), np.asarray(centers) / 1e6, lw=0.8, color='#285880')
        tune.set(xlabel='Selected measured row', ylabel='Tuned RF (MHz)')
        filename = (('%03d-' % path_index) if len(paths) > 1 else '') + Path(path).stem + '-waterfall.png'
        fig.savefig(output / filename, dpi=220)
        plt.close(fig)
        figures.append(filename)
    payload = dict(
        scope='Guard counts, not credited qualified time. Attempts and raw files remain separate.',
        display='Bounded representative rows; maximum display pooling; missing bins stay masked',
        attempts=summaries,
    )
    (output / 'capture-summary.json').write_text(json.dumps(payload, indent=2, allow_nan=False) + '\n')
    index = output / 'index.html'
    previous = (
        index.read_text()
        if index.exists()
        else '<!doctype html><meta charset="utf-8"><title>Expert capture report</title><h1>Expert capture report</h1>'
    )
    previous += '<h2>Capture guard inventory</h2><pre>' + html.escape(json.dumps(payload, indent=2)) + '</pre>'
    previous += ''.join(
        '<p><img style="max-width:100%;height:auto" src="' + html.escape(name) + '" alt="Measured waterfall"></p>'
        for name in figures
    )
    index.write_text(previous, encoding='utf-8')
    return payload


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', nargs='+', help='Recorded JSONL density files')
    parser.add_argument('--out', required=True, help='Fresh report directory')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return generate(args.input, args.out)


if __name__ == '__main__':
    main()
