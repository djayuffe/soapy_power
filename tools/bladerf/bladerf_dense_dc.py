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
                             select, solve_dc)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--device', required=True, help="e.g. 'driver=bladerf,serial=...,xb200=custom'")
    parser.add_argument('--profile-dir', type=Path, required=True,
                        help='receiver-profile-final.json, validated-full-grid-profile.json, schedule-selection.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=0, help='first N centers only (testing)')
    options = parser.parse_args(argv)
    from bladerf_hw import HOP_SAMPLES, Receiver, sample_mean

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
                m0 = sample_mean(iq)
                base_codes = clip_codes(np.asarray(base_dc) / DC_QUANTUM)
                delta = 4 if gains['rxvga2'] >= 18 else 8
                step_i, step_q = perturbation_codes(base_codes, delta)
                means = {}
                failed = None
                for name, codes in (('i', base_codes + [step_i, 0]), ('q', base_codes + [0, step_q])):
                    probe = (clip_codes(codes) * DC_QUANTUM).tolist()
                    _, _, piq, _, _ = receiver.hop(center, gains, probe)
                    pq = receiver_quality(piq)
                    log.write(json.dumps(dict(center_hz=center, role=name, gains=gains, dc=probe, mean=[pq['mean_i'], pq['mean_q']],
                                              max_component=pq['max_component'])) + '\n')
                    if not headroom_ok(pq):
                        failed = 'probe %s failed headroom' % name
                        break
                    means[name] = complex(pq['mean_i'], pq['mean_q'])
                row = dict(center_hz=center, gains=gains, base_dc=[float(v) for v in base_dc], anchor_hz=anchor,
                           anchor_validated=validated, base_mean=[m0.real, m0.imag], base_max_component=quality['max_component'],
                           headroom_reductions=len(reductions), probe_codes=[int(step_i), int(step_q)])
                if failed:
                    row.update(accepted=False, reason=failed, normalized_dc=row['base_dc'])
                    rows.append(row)
                    continue
                solved = solve_dc(base_dc, m0, means['i'], means['q'], step_i, step_q)
                if solved is None:
                    row.update(accepted=False, reason='ill-conditioned Jacobian', normalized_dc=row['base_dc'])
                    rows.append(row)
                    continue
                _, _, viq, _, _ = receiver.hop(center, gains, solved['dc'])
                vq = receiver_quality(viq)
                verified = complex(vq['mean_i'], vq['mean_q'])
                ok = headroom_ok(vq) and accept_verified(abs(m0), abs(verified))
                row.update(solved, verified_mean=[verified.real, verified.imag], verified_max_component=vq['max_component'],
                           accepted=bool(ok), normalized_dc=solved['dc'] if ok else row['base_dc'],
                           improvement_db=float(20 * np.log10(max(abs(m0), 1e-9) / max(abs(verified), 1e-9))))
                if not ok:
                    row['reason'] = 'verification did not confirm the solved value'
                log.write(json.dumps(dict(center_hz=center, role='verify', gains=gains, dc=solved['dc'],
                                          mean=[vq['mean_i'], vq['mean_q']], max_component=vq['max_component'])) + '\n')
                rows.append(row)
                if index % 50 == 0:
                    print('dense-dc %4d/%d %.1f MHz |m| %.4f -> %.4f %s  %.0f s' % (
                        index + 1, len(centers), center / 1e6, abs(m0), abs(verified), 'ok' if ok else 'kept',
                        time.monotonic() - t0), flush=True)
    finally:
        receiver.close()
    accepted = [r for r in rows if r.get('accepted')]
    table = dict(version=1, kind='bladerf1-dense-dc-table', device=options.device,
                 serial=profile.get('serial'), lms_state=receiver.lms_state,
                 started_utc=started.isoformat(), finished_utc=datetime.now(timezone.utc).isoformat(),
                 seconds=time.monotonic() - t0, dc_quantum=DC_QUANTUM, centers=len(rows), accepted=len(accepted),
                 hop_samples=HOP_SAMPLES,
                 method='Per center at survey stage gains: base + I-step + Q-step captures, 2x2 Jacobian, lattice '
                        'solve, independent verification capture; accepted only if |mean| drops to <=0.7x '
                        '(or <=0.002) and headroom passes.',
                 scope='Normalized manual DC controls for this device, LMS state, gains and XB domain. '
                       'Not RF calibration; sample means include any signal within ~100 Hz of the LO.',
                 absolute_calibration=False, settling_retries=receiver.retries, points=rows)
    (options.output / 'dc-table-dense.json').write_text(json.dumps(table, indent=1))
    print('DENSE_DC_COMPLETE centers=%d accepted=%d seconds=%.0f' % (len(rows), len(accepted), table['seconds']), flush=True)
    return 0


def receiver_quality(iq):
    from soapypower.receiver_quality import headroom_metrics
    return headroom_metrics(iq)


def headroom_ok(quality):
    from soapypower.receiver_quality import headroom_passes
    return headroom_passes(quality)


if __name__ == '__main__':
    sys.exit(main())
