#!/usr/bin/env python3
"""Plot uncalibrated JSONL spectra using peaks or weighted linear-power means.

Requires NumPy, Matplotlib and the installed soapy_power package. Input is
streamed; memory scales with time cells × display bins, not raw file size.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import numpy as np
from soapypower.records import read_records


def aggregate(path, start_hz, stop_hz, bins=4096, time_bin=1.0, edge_fraction=.12,
              mode='peak', dc_mask_width_hz=0.0):
    if (not np.isfinite([start_hz, stop_hz, time_bin, edge_fraction, dc_mask_width_hz]).all()
            or stop_hz <= start_hz or bins < 2 or time_bin <= 0
            or not 0 <= edge_fraction < .5 or dc_mask_width_hz < 0):
        raise ValueError('invalid display geometry')
    if mode not in ('peak', 'weighted-mean'):
        raise ValueError('unknown aggregation mode')
    context_signature = None
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
        context = m.get('context', {})
        if ((mode == 'weighted-mean' or dc_mask_width_hz)
                and isinstance(context, dict) and context.get('tune_args')):
            raise ValueError('fusion/center exclusion with tuning arguments requires a measured LO/DSP mapping; this example supports default zero-offset tuning')
        if mode == 'weighted-mean':
            context = m.get('context')
            gains = context.get('gains') if isinstance(context, dict) else None
            if (not isinstance(context, dict) or context.get('agc') is not False
                    or not isinstance(gains, dict) or not gains
                    or any(not isinstance(g, (int, float)) or not np.isfinite(g) for g in gains.values())):
                raise ValueError('weighted fusion requires an observable fixed-gain context')
            keys = ('driver', 'hardware', 'serial', 'channel', 'antenna', 'gains', 'agc',
                    'sample_rate', 'bandwidth', 'clock_source', 'time_source',
                    'master_clock_rate', 'dc_correction', 'iq_correction',
                    'manual_dc', 'manual_iq', 'lnb_lo', 'lnb_high_side',
                    'iq_conjugate', 'sample_format', 'hardware_info', 'device_settings', 'tune_args')
            signature = json.dumps({key: context.get(key) for key in keys}, sort_keys=True)
            if context_signature is not None and signature != context_signature:
                raise ValueError('weighted fusion receiver context changed')
            context_signature = signature

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
        rf_center = m.get('frequency_grid', {}).get('rf_origin_hz', center)
        rate = m.get('sample_rate_hz', r['step_hz']*m.get('fft_bins', values.size))
        if not np.isfinite([rf_center, rate]).all() or rate <= 0:
            raise ValueError('invalid receiver frequency/rate metadata')
        offset = freq-rf_center
        half_width = rate*(.5-edge_fraction)
        keep = ((np.abs(offset) < half_width) & (freq >= start_hz)
                & (freq < stop_hz) & np.isfinite(values))
        # Retain the historical left Nyquist endpoint in unweighted mode.
        if mode == 'peak':
            keep |= ((offset == -half_width) & (freq >= start_hz)
                     & (freq < stop_hz) & np.isfinite(values))
        if dc_mask_width_hz:
            keep &= np.abs(offset) >= dc_mask_width_hz/2
        bucket = int((t-first).total_seconds()/time_bin)
        indices = ((freq[keep]-start_hz)/step).astype(int)
        if bucket not in cells:
            shape = (2, bins) if mode == 'weighted-mean' else (bins,)
            cells[bucket] = np.full(shape, -np.inf, dtype=np.float64)
        dest = cells[bucket]
        if mode == 'peak':
            np.maximum.at(dest, indices, values[keep])
        else:
            # Quartic taper suppresses edge contributions. Integration duration
            # weights repeated acquisitions; it does not imply independence.
            weight = (1-(offset[keep]/half_width)**2)**2 * r['samples']/rate
            log_weight = np.log(weight)
            # Log-sum-exp is equivalent to a linear PSD mean, without overflow
            # or underflow from exponentiating very large/small dB values.
            np.logaddexp.at(dest[0], indices, log_weight+values[keep]*(np.log(10)/10))
            np.logaddexp.at(dest[1], indices, log_weight)
        records += 1
    if first is None:
        raise ValueError('no input spectra')
    result = np.full((max(cells)+1, bins), np.nan, dtype=np.float32)
    for bucket, values in cells.items():
        if mode == 'weighted-mean':
            present = np.isfinite(values[1])
            result[bucket, present] = ((values[0, present]-values[1, present])*(10/np.log(10)))
        else:
            result[bucket] = np.where(np.isfinite(values), values, np.nan)
    if not np.isfinite(result).any():
        raise ValueError('no finite measurements inside the requested band')
    duration = (last-first).total_seconds()
    time_edges = np.r_[np.arange(result.shape[0])*time_bin,
                       max(duration, (result.shape[0]-1)*time_bin+1e-9)]
    summary = dict(start_utc=first.isoformat(), stop_utc=last.isoformat(),
                   elapsed_seconds=duration, records=records, sweeps_including_partial=sweeps,
                   frequency_cell_hz=step, time_cell_seconds=time_bin,
                   aggregation=('maximum PSD' if mode == 'peak' else 'taper/duration weighted linear PSD mean')+' per cell, assigned by hop host start time',
                   mode=mode, dc_mask_width_hz=dc_mask_width_hz, edge_fraction=edge_fraction,
                   exclusion_reference='displayed RF origin; not physical LO readback',
                   observed_fraction=float(np.isfinite(result).mean()),
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
    parser.add_argument('--mode', choices=['peak', 'weighted-mean'], default='peak')
    parser.add_argument('--dc-mask-width-hz', type=float, default=0,
                        help='exclude this full RF width around the displayed center in a validated zero-offset setup; requires overlap')
    parser.add_argument('-O', '--output-prefix', type=Path, required=True)
    args = parser.parse_args()
    paths = [Path(str(args.output_prefix)+suffix) for suffix in ('.png', '.npz', '.json')]
    if any(p.exists() or p.resolve() == args.input.resolve() for p in paths):
        parser.error('output paths must be new and separate from the input')
    try:
        data, frequencies, times, summary = aggregate(args.input, args.start_mhz*1e6,
            args.stop_mhz*1e6, args.bins, args.time_bin, args.edge_fraction,
            args.mode, args.dc_mask_width_hz)
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
    kind = 'Peak' if args.mode == 'peak' else 'Weighted mean'
    fig.colorbar(mesh, ax=ax, label=kind+' dB(input power/Hz), uncalibrated')
    fig.text(.08, .02, kind+' per display cell; sequential hops, not simultaneous reception. '
             'Blank = unobserved. Color limits may saturate strong peaks.', fontsize=9)
    fig.tight_layout(rect=(0, .05, 1, 1))
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(paths[0], dpi=180)
    plt.close(fig)
    matrix_name = 'peak_density_db' if args.mode == 'peak' else 'mean_density_db'
    np.savez_compressed(paths[1], **{matrix_name: data,
                        'frequency_edges_hz': frequencies, 'elapsed_edges_seconds': times})
    paths[2].write_text(json.dumps(summary, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
