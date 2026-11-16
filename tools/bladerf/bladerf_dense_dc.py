#!/usr/bin/env python3
"""Measure a manual DC value at every survey center (local Jacobian, independent verification).

Nearest-anchor DC reuse fails between anchors: the LMS6002D DC offset and the control's effect
change within tens of MHz. Per center and at the exact stage gains the survey will use:

  1. base capture at the profile's DC (headroom guard lowers gain like the survey would);
  2. one capture each with the I and the Q control stepped (4 codes at RXVGA2 >= 18 dB, else 8);
  3. 2x2 Jacobian from the sample means, solve on the 1/64 code lattice;
  4. a fourth, independent capture at the solved value; keep it only if it reduces |mean|.

Receive only. Writes <output>/dc-table-dense.json (+ captures.jsonl); refuses a different LMS state.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from bladerf_profile import (DC_QUANTUM, accept_verified, clip_codes, load_json, perturbation_codes,
                             quantization_bound, refine_dc, sample_mean, select, solve_dc)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--device', required=True, help="e.g. 'driver=bladerf,serial=...,xb200=custom'")
    parser.add_argument('--profile-dir', type=Path, required=True,
                        help='receiver-profile-final.json, validated-full-grid-profile.json, schedule-selection.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=0, help='first N centers only (testing)')
    parser.add_argument('--iterations', type=int, default=2, choices=(1, 2),
                        help='2: one more verified Newton step where the residual exceeds 1.5x the DAC limit')
    options = parser.parse_args(argv)
    from bladerf_hw import HOP_SAMPLES, Receiver

    profile = load_json(options.profile_dir / 'receiver-profile-final.json')
    dense = load_json(options.profile_dir / 'validated-full-grid-profile.json')
    centers = [float(c) for c in load_json(options.profile_dir / 'schedule-selection.json')['selected_centers_hz']]
    if options.limit:
        centers = centers[:options.limit]
    options.output.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc)
    receiver = Receiver(options.device, profile['lms_state_at_fit'])
    rows = []
    t0 = time.monotonic()
    try:
        with open(options.output / 'captures.jsonl', 'w') as log:
            for index, center in enumerate(centers):
                gains, dc, anchor, validated = select(profile, dense, center)
                try:
                    _, _, iq, _, _, gains, base_dc, quality, reductions = receiver.guarded_hop(center, gains, dc)
                except RuntimeError as error:
                    rows.append(dict(center_hz=center, accepted=False, reason=str(error)))
                    continue
                row = calibrate_center(receiver, center, gains, base_dc, iq, quality, iterations=options.iterations, log=log)
                row.update(anchor_hz=anchor, anchor_validated=validated, headroom_reductions=len(reductions))
                m0, verified, ok = abs(complex(*row['base_mean'])), abs(complex(*row.get('verified_mean', row['base_mean']))), row['accepted']
                rows.append(row)
                if index % 50 == 0:
                    print('dense-dc %4d/%d %.1f MHz |m| %.4f -> %.4f %s  %.0f s' % (
                        index + 1, len(centers), center / 1e6, m0, verified, 'ok' if ok else 'kept',
                        time.monotonic() - t0), flush=True)
    finally:
        receiver.close()
    accepted = [r for r in rows if r.get('accepted')]
    table = dict(version=1, kind='bladerf1-dense-dc-table', device=options.device,
                 serial=profile.get('serial'), lms_state=receiver.lms_state,
                 started_utc=started.isoformat(), finished_utc=datetime.now(timezone.utc).isoformat(),
                 seconds=time.monotonic() - t0, dc_quantum=DC_QUANTUM, centers=len(rows), accepted=len(accepted),
                 hop_samples=HOP_SAMPLES,
                 iterations=options.iterations,
                 method='Per center at survey stage gains: base + I-step + Q-step captures, 2x2 Jacobian, lattice '
                        'solve, independent verification capture; accepted only if |mean| drops to <=0.7x '
                        '(or <=0.002) and headroom passes. With 2 iterations, a second verified Newton step '
                        'where the residual exceeds 1.5x the half-code DAC limit.',
                 scope='Normalized manual DC controls for this device, LMS state, gains and XB domain. '
                       'Not RF calibration; sample means include any signal within ~100 Hz of the LO.',
                 absolute_calibration=False, settling_retries=receiver.retries, points=rows)
    (options.output / 'dc-table-dense.json').write_text(json.dumps(table, indent=1))
    print('DENSE_DC_COMPLETE centers=%d accepted=%d seconds=%.0f' % (len(rows), len(accepted), table['seconds']), flush=True)
    return 0


def calibrate_center(receiver, center, gains, base_dc, iq, quality, iterations=2, log=None):
    """Jacobian DC calibration at one center and fixed gains, starting from a passed base capture."""

    def record(role, dc, q):
        if log is not None:
            log.write(json.dumps(dict(center_hz=center, role=role, gains=gains, dc=dc, mean=[q['mean_i'], q['mean_q']],
                                      max_component=q['max_component'])) + '\n')

    m0 = sample_mean(iq)
    base_codes = clip_codes(np.asarray(base_dc) / DC_QUANTUM)
    step_i, step_q = perturbation_codes(base_codes, 4 if gains['rxvga2'] >= 18 else 8)
    row = dict(center_hz=center, gains=gains, base_dc=[float(v) for v in base_dc], base_mean=[m0.real, m0.imag],
               base_max_component=quality['max_component'], probe_codes=[int(step_i), int(step_q)],
               normalized_dc=[float(v) for v in base_dc], accepted=False)
    means = {}
    for name, codes in (('i', base_codes + [step_i, 0]), ('q', base_codes + [0, step_q])):
        probe = (clip_codes(codes) * DC_QUANTUM).tolist()
        q = receiver_quality(receiver.hop(center, gains, probe)[2])
        record(name, probe, q)
        if not headroom_ok(q):
            row['reason'] = 'probe %s failed headroom' % name
            return row
        means[name] = complex(q['mean_i'], q['mean_q'])
    solved = solve_dc(base_dc, m0, means['i'], means['q'], step_i, step_q)
    if solved is None:
        row['reason'] = 'ill-conditioned Jacobian'
        return row
    vq = receiver_quality(receiver.hop(center, gains, solved['dc'])[2])
    record('verify', solved['dc'], vq)
    verified = complex(vq['mean_i'], vq['mean_q'])
    headroom = headroom_ok(vq)
    ok = headroom and accept_verified(abs(m0), abs(verified))
    row.update(solved, verified_mean=[verified.real, verified.imag], verified_max_component=vq['max_component'],
               accepted=bool(ok), quantization_bound=quantization_bound(solved['jacobian']))
    if not ok:
        row['reason'] = ('verification capture failed headroom' if not headroom
                         else 'verification did not confirm the solved value')
        return row
    row['normalized_dc'] = solved['dc']
    if iterations > 1 and abs(verified) > 1.5 * row['quantization_bound']:
        step = refine_dc(solved['dc'], verified, solved['jacobian'])
        if step['dc'] != solved['dc']:
            rq = receiver_quality(receiver.hop(center, gains, step['dc'])[2])
            record('refine', step['dc'], rq)
            refined = complex(rq['mean_i'], rq['mean_q'])
            better = headroom_ok(rq) and abs(refined) < abs(verified)
            row['refine'] = dict(dc=step['dc'], mean=[refined.real, refined.imag], accepted=bool(better))
            if better:
                verified = refined
                row.update(normalized_dc=step['dc'], verified_mean=[refined.real, refined.imag],
                           verified_max_component=rq['max_component'])
    row['improvement_db'] = float(20 * np.log10(max(abs(m0), 1e-9) / max(abs(verified), 1e-9)))
    return row


def receiver_quality(iq):
    from soapypower.receiver_quality import headroom_metrics
    return headroom_metrics(iq)


def headroom_ok(quality):
    from soapypower.receiver_quality import headroom_passes
    return headroom_passes(quality)


if __name__ == '__main__':
    sys.exit(main())
