#!/usr/bin/env python3
"""Timed bladeRF1 + XB-200 survey (42-3800 MHz, 1074 centers) with stage profiles and live headroom guard.

The acquisition policy is the 2026-11-16 v2 survey: 28 MS/s, FFT 4096 at 50 % segment overlap,
35 x 4096 samples per hop, stream reopened per hop, 20 ms tune delay + 32768 settle samples,
timestamp continuity required, partial hops rejected. --dc-table adds per-center manual DC
values from bladerf_dense_dc.py where their stage gains match. Optional IQ captures at fixed
centers follow the survey. Receive only.
"""
import argparse
import gzip
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from bladerf_profile import DenseDC, load_json, settings

IQ_CENTERS_MHZ = [70, 100, 400, 915, 1800, 2440, 3500, 910, 913, 915, 917, 923, 925]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--device', required=True)
    parser.add_argument('--profile-dir', type=Path, required=True)
    parser.add_argument('--dc-table', type=Path, help='dc-table-dense.json from bladerf_dense_dc.py')
    parser.add_argument('--duration', type=float, default=300)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--no-iq', action='store_true')
    options = parser.parse_args(argv)
    if not np.isfinite(options.duration) or options.duration <= 0:
        parser.error('duration must be positive')
    from bladerf_hw import HOP_SAMPLES, Receiver, sample_mean

    profile = load_json(options.profile_dir / 'receiver-profile-final.json')
    dense = load_json(options.profile_dir / 'validated-full-grid-profile.json')
    plan = [float(c) for c in load_json(options.profile_dir / 'schedule-selection.json')['selected_centers_hz']]
    dense_dc = DenseDC(load_json(options.dc_table)) if options.dc_table else None
    if dense_dc is not None and dense_dc.lms_state != profile['lms_state_at_fit']:
        parser.error('DC table LMS state differs from the profile')
    out = options.output
    out.mkdir(parents=True, exist_ok=False)
    for name in ('receiver-profile-final.json', 'validated-full-grid-profile.json', 'schedule-selection.json'):
        shutil.copy2(options.profile_dir / name, out / name)
    if options.dc_table:
        shutil.copy2(options.dc_table, out / 'dc-table-dense.json')
    shutil.copy2(__file__, out / 'acquisition-source.py')
    state = dict(status='starting', records=0, started_host_time=datetime.now(timezone.utc).isoformat(),
                 headroom_reductions=[], pipeline='v3-dense-dc' if dense_dc else 'v2-anchor-dc',
                 hops_per_sweep=len(plan), dc_sources={})

    def save():
        tmp = out / 'status.json.tmp'
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(out / 'status.json')

    live = {}

    def dc_for_gains(gains, center):
        value = dense_dc.lookup(center, gains) if dense_dc else None
        return value

    receiver = Receiver(options.device, profile['lms_state_at_fit'])
    first = None
    try:
        state['status'] = 'capturing'
        save()
        last_save = time.monotonic()
        with gzip.open(out / 'scan.jsonl.gz', 'wt', compresslevel=1) as raw:
            while state.get('captured_span_seconds', 0) < options.duration:
                center = plan[state['records'] % len(plan)]
                key = round(center)
                if key in live:
                    gains, dc, source = live[key]
                else:
                    gains, dc, anchor, validated = settings(profile, dense, center, dense_dc)
                    source = 'dense' if anchor == 'dense' else ('anchor' if validated else 'none')
                freq, psd_db, iq, start, stop, gains, dc, quality, reductions = receiver.guarded_hop(
                    center, gains, dc, dc_for_gains=lambda g, c=center: dc_for_gains(g, c))
                if reductions:
                    state['headroom_reductions'].extend(reductions)
                    source = 'dense' if dense_dc and dense_dc.lookup(center, gains) == list(dc) else 'none'
                live[key] = (gains, list(dc), source)
                state['dc_sources'][source] = state['dc_sources'].get(source, 0) + 1
                meta = receiver._psd.measurement_metadata(receiver._last_state)
                meta.update(receiver._last_integrity)
                meta['context'] = receiver._last_context
                meta['deadline_policy'] = 'finish-hop'
                meta['bladerf_quality'] = quality
                meta['bladerf_gain_nominal_db'] = float(sum(gains.values()) - 6)
                meta['bladerf_dc_source'] = source
                meta['bladerf_normalization_scope'] = ('Subtracting nominal stage gain does not calibrate RF power, '
                                                       'NF or per-band frequency response.')
                record = receiver._writer.make_record((freq, psd_db), start, stop, HOP_SAMPLES, metadata=meta)
                raw.write(json.dumps(record, allow_nan=False) + '\n')
                state['records'] += 1
                if first is None:
                    first = start
                    state['first_capture_host_time'] = start.isoformat()
                state['last_capture_host_time'] = stop.isoformat()
                state['captured_span_seconds'] = (stop - first).total_seconds()
                if time.monotonic() - last_save > 5:
                    raw.flush()
                    save()
                    last_save = time.monotonic()
        state.update(status='captured', full_sweeps=state['records'] // len(plan),
                     partial_final_sweep_hops=state['records'] % len(plan), settling_retries=receiver.retries)
        save()
        if not options.no_iq:
            state['iq_status'] = 'capturing'
            save()
            rows = []
            for index, mhz in enumerate(IQ_CENTERS_MHZ):
                center = mhz * 1e6
                gains, dc, _, _ = settings(profile, dense, center, dense_dc, iq_guard=True)
                freq, psd_db, iq, _, _, gains, dc, quality, _ = receiver.guarded_hop(center, gains, dc)
                name = 'iq-%dM%s' % (mhz, '-retune' if index >= 7 else '')
                np.savez_compressed(out / (name + '.npz'), iq=iq, frequency_hz=np.asarray(freq), psd_db=np.asarray(psd_db))
                mean = sample_mean(iq)
                rows.append(dict(name=name, center_hz=center, gains=gains, dc=list(dc), quality=quality,
                                 abs_mean=abs(mean), iq_samples=int(iq.size)))
            (out / 'iq-summary.json').write_text(json.dumps(rows, indent=1))
            state['iq_status'] = 'complete'
            state['iq_captures'] = len(rows)
    except BaseException as error:
        state.update(status='failed' if state['status'] != 'captured' else state['status'], error=repr(error))
        raise
    finally:
        save()
        receiver.close()
    print('SURVEY_COMPLETE', json.dumps({k: v for k, v in state.items() if k != 'headroom_reductions'}), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
