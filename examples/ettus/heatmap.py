#!/usr/bin/env python3
"""Plot uncalibrated JSONL spectra as peaks per frequency/time cell.

Requires NumPy, Matplotlib and the installed soapy_power package. Input is
streamed; memory scales with time cells × display bins, not raw file size.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import numpy as np
from soapypower.records import read_records


def aggregate(path, start_hz, stop_hz, bins=4096, time_bin=1.0, edge_fraction=.12):
    if (not np.isfinite([start_hz, stop_hz, time_bin, edge_fraction]).all()
            or stop_hz <= start_hz or bins < 2 or time_bin <= 0
            or not 0 <= edge_fraction < .5):
        raise ValueError('invalid display geometry')
    cells = {}
    first = last = previous = None
    records = sweeps = 0
    step = (stop_hz-start_hz)/bins
    for r in read_records(path):
        if r['unit'] != 'dB(input power/Hz)':
            raise ValueError('this example requires uncalibrated logarithmic PSD')
        m = r.get('metadata', {})
        if m.get('measurement_status') != 'complete':
            raise ValueError('incomplete acquisition in input')
        if m.get('continuity') != 'verified':
            raise ValueError('input lacks verified hardware continuity')
        t, end = datetime.fromisoformat(r['time_start']), datetime.fromisoformat(r['time_stop'])
        if t.tzinfo is None or end.tzinfo is None or end < t or (last is not None and t < previous_time):
            raise ValueError('invalid or unordered host timestamps')
        if first is None:
            first = t
        previous_time, last = t, end
        center = m.get('tuned_center_hz', (r['start_hz']+r['stop_hz'])/2)
        if previous is None or center < previous:
            sweeps += 1
        previous = center
        values = np.asarray(r['power'], dtype=float)
        freq = r['start_hz']+np.arange(values.size)*r['step_hz']
        edge = int(values.size*edge_fraction)
        index = np.arange(values.size)
        keep = ((index >= edge) & (index < values.size-edge)
                & (freq >= start_hz) & (freq < stop_hz) & np.isfinite(values))
        bucket = int((t-first).total_seconds()/time_bin)
        if bucket not in cells:
            cells[bucket] = np.full(bins, -np.inf, dtype=np.float32)
        dest = cells[bucket]
        np.maximum.at(dest, ((freq[keep]-start_hz)/step).astype(int), values[keep])
        records += 1
    if first is None:
        raise ValueError('no input spectra')
    result = np.full((max(cells)+1, bins), np.nan, dtype=np.float32)
    for bucket, values in cells.items():
        result[bucket] = np.where(np.isfinite(values), values, np.nan)
    if not np.isfinite(result).any():
        raise ValueError('no finite measurements inside the requested band')
    duration = (last-first).total_seconds()
    time_edges = np.r_[np.arange(result.shape[0])*time_bin,
                       max(duration, (result.shape[0]-1)*time_bin+1e-9)]
    summary = dict(start_utc=first.isoformat(), stop_utc=last.isoformat(),
                   elapsed_seconds=duration, records=records, sweeps_including_partial=sweeps,
                   frequency_cell_hz=step, time_cell_seconds=time_bin,
                   aggregation='maximum PSD per cell, assigned by hop host start time',
                   units='dB(input power/Hz), uncalibrated')
    return result, np.linspace(start_hz, stop_hz, bins+1), time_edges, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--start-mhz', type=float, required=True)
    parser.add_argument('--stop-mhz', type=float, required=True)
    parser.add_argument('--bins', type=int, default=4096)
    parser.add_argument('--time-bin', type=float, default=1.0)
    parser.add_argument('--edge-fraction', type=float, default=.12)
    parser.add_argument('-O', '--output-prefix', type=Path, required=True)
    args = parser.parse_args()
    paths = [Path(str(args.output_prefix)+suffix) for suffix in ('.png', '.npz', '.json')]
    if any(p.exists() or p.resolve() == args.input.resolve() for p in paths):
        parser.error('output paths must be new and separate from the input')
    try:
        data, frequencies, times, summary = aggregate(args.input, args.start_mhz*1e6,
            args.stop_mhz*1e6, args.bins, args.time_bin, args.edge_fraction)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    limits = np.nanpercentile(data, [2, 99.7])
    limits[1] = max(limits[1], limits[0]+10)
    fig, ax = plt.subplots(figsize=(14, 7))
    mesh = ax.pcolormesh(frequencies/1e6, times/60, np.ma.masked_invalid(data),
                         shading='flat', cmap='magma', vmin=limits[0], vmax=limits[1], rasterized=True)
    ax.invert_yaxis()
    ax.set(xlabel='Frequency (MHz)', ylabel='Elapsed time (minutes)',
           title='RF survey: {}–{} MHz'.format(args.start_mhz, args.stop_mhz))
    fig.colorbar(mesh, ax=ax, label='Peak dB(input power/Hz), uncalibrated')
    fig.text(.08, .02, 'Peak per display cell; sequential hops, not simultaneous reception. '
             'Blank = unobserved. Color limits may saturate strong peaks.', fontsize=9)
    fig.tight_layout(rect=(0, .05, 1, 1))
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(paths[0], dpi=180)
    plt.close(fig)
    np.savez_compressed(paths[1], peak_density_db=data,
                        frequency_edges_hz=frequencies, elapsed_edges_seconds=times)
    paths[2].write_text(json.dumps(summary, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
