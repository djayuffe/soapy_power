"""File-only telemetry inventory and bounded plots shared by automatic reports."""
import collections
import csv
import datetime as dt
import html
import json
import math
from pathlib import Path


def scalars(value, prefix='metadata', depth=0):
    if depth > 16:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from scalars(item, prefix + '.' + str(key), depth + 1)
    elif isinstance(value, (list, tuple)):
        # Large arrays remain complete in telemetry-records.jsonl, not thousands of curves.
        if len(value) <= 32:
            for i, item in enumerate(value):
                yield from scalars(item, f'{prefix}[{i}]', depth + 1)
    else:
        yield prefix, value


def generate(paths, output, max_points=1024):
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import pyplot as plt
    from matplotlib.dates import DateFormatter
    if max_points < 2:
        raise ValueError('At least two display points required')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    inventory, records, fragments = {}, 0, 0
    with (output / 'telemetry-records.jsonl').open('w') as export:
        for path in paths:
            with Path(path).open('rb') as source:
                for index, line in enumerate(source):
                    if not line.strip():
                        continue
                    if not line.endswith(b'\n'):
                        fragments += 1
                        continue
                    row = json.loads(line)
                    def finite_metadata(value):
                        if isinstance(value,float) and not math.isfinite(value):return None
                        if isinstance(value,dict):return {k:finite_metadata(v) for k,v in value.items()}
                        if isinstance(value,list):return [finite_metadata(v) for v in value]
                        return value
                    metadata = finite_metadata(row.get('metadata') or {})
                    export.write(json.dumps(dict(source=Path(path).parent.name+'/'+Path(path).name, record=index,
                        time_start=row.get('time_start'), time_stop=row.get('time_stop'), metadata=metadata), allow_nan=False)+'\n')
                    records += 1
                    try:
                        instant = dt.datetime.fromisoformat(row['time_start'].replace('Z', '+00:00'))
                        timestamp = instant.timestamp() if instant.tzinfo is not None else None
                    except (KeyError, TypeError, ValueError):
                        timestamp = None
                    for key, value in scalars(metadata):
                        field = inventory.setdefault(key, dict(present=0, missing=0, numeric=0, minimum=None,
                            maximum=None, mean=0.0, m2=0.0, values=collections.Counter(), other_values=0,
                            points=[], stride=1))
                        field['present'] += 1
                        if value is None:
                            field['missing'] += 1
                        elif isinstance(value, (float, int)) and math.isfinite(value):
                            number = float(value)
                            field['numeric'] += 1
                            delta = number - field['mean']
                            field['mean'] += delta / field['numeric']
                            field['m2'] += delta * (number - field['mean'])
                            field['minimum'] = number if field['minimum'] is None else min(field['minimum'], number)
                            field['maximum'] = number if field['maximum'] is None else max(field['maximum'], number)
                            if timestamp is not None and records % field['stride'] == 0:
                                field['points'].append((timestamp, number))
                                if len(field['points']) > max_points:
                                    field['points'] = field['points'][::2]
                                    field['stride'] *= 2
                        else:
                            label = str(value)
                            if label in field['values'] or len(field['values']) < 32:
                                field['values'][label] += 1
                            else:
                                field['other_values'] += 1
    figures = []
    varying = [key for key, field in inventory.items() if field['numeric'] and field['minimum'] != field['maximum'] and field['points']]
    with plt.rc_context({'figure.facecolor':'white','axes.facecolor':'white','text.color':'#25394a','axes.labelcolor':'#25394a','xtick.color':'#25394a','ytick.color':'#25394a','axes.edgecolor':'#637585'}):
        for page in range(0, len(varying), 6):
            fig, axs = plt.subplots(3, 2, figsize=(18, 12), layout='constrained')
            for ax, key in zip(axs.flat, varying[page:page+6]):
                field = inventory[key]
                times, values = np.asarray(field['points']).T
                dates = [dt.datetime.fromtimestamp(t, dt.timezone.utc) for t in times]
                ax.scatter(dates, values, s=4, color='#286b92')
                ax.set_title(key.removeprefix('metadata.'), fontsize=9, wrap=True)
                ax.xaxis.set_major_formatter(DateFormatter('%H:%M:%S', tz=dt.timezone.utc))
                ax.set_xlabel('UTC · sampled points; gaps and partitions are not connected')
                ax.grid(alpha=.2)
            for ax in list(axs.flat)[len(varying[page:page+6]):]:
                ax.set_visible(False)
            fig.suptitle('Recorded telemetry · all-record inventory, bounded display samples')
            filename=f'telemetry-{page//6+1:02d}.png'
            fig.savefig(output/filename, dpi=180)
            fig.savefig((output/filename).with_suffix('.pdf'))
            plt.close(fig)
            figures.append(filename)
    for field in inventory.values():
        field['absent_records'] = records - field['present']
        field['mean'] = field['mean'] if field['numeric'] else None
        field['standard_deviation'] = math.sqrt(max(0, field.pop('m2')) / field['numeric']) if field['numeric'] else None
        field['values'] = dict(field['values'])
        field.pop('points')
    report = dict(records=records, trailing_fragments=fragments, fields=inventory, figures=figures,
        scope='Every complete record exported. All scalar fields inventoried; varying numeric fields plotted with bounded samples. Constants and categories tabulated. Missing values remain unknown.',
        phase='PSD records cannot recover waveform phase. Only explicitly recorded phase fields are inventoried; retained IQ analysis is separate.')
    with (output/'inventory.csv').open('w',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['field','present','numeric','missing_or_absent','minimum','maximum','mean','standard_deviation','categories','other_category_records'])
        for key,v in sorted(inventory.items()):writer.writerow([key,v['present'],v['numeric'],v['missing']+v['absent_records'],v['minimum'],v['maximum'],v['mean'],v['standard_deviation'],json.dumps(v['values']),v['other_values']])
    (output/'inventory.json').write_text(json.dumps(report, indent=2, allow_nan=False)+'\n')
    esc=lambda x:html.escape(str(x))
    table=''.join(f'<tr><td>{esc(key)}</td><td>{v["numeric"]}</td><td>{v["absent_records"]+v["missing"]}</td><td>{esc(v["minimum"])}</td><td>{esc(v["maximum"])}</td><td>{esc(v["values"])}</td></tr>' for key,v in sorted(inventory.items()))
    (output/'index.html').write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Recorded telemetry</title><style>body{max-width:1400px;margin:24px auto;padding:16px;font:14px system-ui;color:#25394a;background:#f4f6f8}img{max-width:100%}td,th{padding:8px;border-bottom:1px solid #ccd4db;text-align:left;overflow-wrap:anywhere}table{width:100%;table-layout:fixed}</style><h1>Recorded telemetry and feature evidence</h1><p>'+esc(report['scope'])+'</p><p>'+esc(report['phase'])+'</p><p><a href="inventory.json">Complete inventory</a> · <a href="inventory.csv">Inventory CSV</a> · <a href="telemetry-records.jsonl">Every-record metadata export</a></p>'+''.join(f'<img loading="lazy" src="{name}" alt="Telemetry time series">' for name in figures)+'<table><thead><tr><th>Field</th><th>Numeric count</th><th>Missing / absent</th><th>Minimum</th><th>Maximum</th><th>Categories</th></tr></thead><tbody>'+table+'</tbody></table>')
    return report
