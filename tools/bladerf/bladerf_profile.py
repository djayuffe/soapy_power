"""bladeRF1 (x40/x115) + XB-200 receive profile logic: stage gains, manual DC, dense DC tables.

Pure functions only (no SoapySDR import), so they are unit-tested without hardware.

Normalized DC values are the driver's manual DC controls (sign + 6 magnitude bits on the
LMS6002D, readback quantum 1/64), not IQ sample means. A DC value is only valid for the
stage gains, XB-200 domain and LMS calibration state it was measured with.
"""
import json
import math

import numpy as np

DC_QUANTUM = 1 / 64
DC_MAX_CODE = 63
XB200_MAX_HZ = 237.5e6          # bladeRF1 tunes natively from 237.5 MHz; below, the XB-200 mixes
STAGES = ('lna', 'rxvga1', 'rxvga2')


def gains_key(gains):
    return tuple(float(gains[k]) for k in STAGES)


def same_domain(a_hz, b_hz):
    return (a_hz < XB200_MAX_HZ) == (b_hz < XB200_MAX_HZ)


def load_json(path):
    with open(path) as handle:
        return json.load(handle)


def matching_dc_anchor(anchors, gains, center_hz, max_distance_hz=150e6):
    """Nearest validated anchor with identical stage gains in the same XB domain, else None."""
    if not math.isfinite(center_hz) or not math.isfinite(max_distance_hz) or max_distance_hz < 0:
        raise ValueError('invalid DC anchor coordinate')
    key = gains_key(gains)
    choices = [a for a in anchors if a.get('validated') is True and gains_key(a['gains']) == key
               and same_domain(a['center_hz'], center_hz) and abs(a['center_hz'] - center_hz) <= max_distance_hz]
    return min(choices, key=lambda a: abs(a['center_hz'] - center_hz)) if choices else None


def select(profile, dense, center_hz, iq_guard=False):
    """Stage gains and anchor DC for one center (the 2026-11-16 v2 policy).

    Gain: the lowest gain needed at this full-grid center (dense profile), or within
    +/-14 MHz for IQ captures (iq_guard), else the band gain. DC: nearest matching anchor.
    Returns (gains, normalized_dc, anchor_hz or None, anchor_validated).
    """
    points = (dense or {}).get('points', [])
    width = 14e6 if iq_guard else 2
    near = [p for p in points if abs(p['center_hz'] - center_hz) <= width]
    if near:
        gains = dict(min(near, key=lambda p: (p['total_gain_db'], abs(p['center_hz'] - center_hz)))['gains'])
    else:
        band = next(b for b in profile['bands'] if b['start_hz'] <= center_hz < b['stop_hz']
                    or (center_hz == b['stop_hz'] == profile['bands'][-1]['stop_hz']))
        gains = dict(band['gains'])
    gains = {k: float(gains[k]) for k in STAGES}
    anchors = [a for b in profile['bands'] for a in b['dc_anchors']]
    anchor = matching_dc_anchor(anchors, gains, center_hz, profile.get('dc_max_anchor_distance_hz', 150e6))
    if anchor is None:
        return gains, [0.0, 0.0], None, False
    return gains, [float(v) for v in anchor['normalized_dc']], anchor['center_hz'], True


class DenseDC:
    """Per-center DC values measured at the exact stage gains the survey uses."""

    def __init__(self, table):
        if table.get('kind') != 'bladerf1-dense-dc-table':
            raise ValueError('not a bladeRF1 dense DC table')
        self.lms_state = table['lms_state']
        self.rows = {}
        for row in table['points']:
            if row.get('accepted') is True:
                self.rows[(round(row['center_hz']), gains_key(row['gains']))] = [float(v) for v in row['normalized_dc']]

    def lookup(self, center_hz, gains):
        return self.rows.get((round(center_hz), gains_key(gains)))

    def add(self, center_hz, gains, dc):
        self.rows[(round(center_hz), gains_key(gains))] = [float(v) for v in dc]


def settings(profile, dense, center_hz, dense_dc=None, iq_guard=False):
    """select() with a dense per-center DC value overriding the anchor when gains match."""
    gains, dc, anchor, validated = select(profile, dense, center_hz, iq_guard=iq_guard)
    if dense_dc is not None and not iq_guard:
        value = dense_dc.lookup(center_hz, gains)
        if value is not None:
            return gains, value, 'dense', True
    return gains, dc, anchor, validated


def clip_codes(codes):
    return np.clip(np.rint(np.asarray(codes, float)), -DC_MAX_CODE, DC_MAX_CODE).astype(int)


def perturbation_codes(base_codes, delta):
    """Probe steps that stay in range and move toward the interior."""
    out = []
    for value in base_codes:
        step = -delta if value > 0 else delta
        if abs(value + step) > DC_MAX_CODE:
            step = -step
        out.append(step)
    return out


def solve_dc(base_dc, mean0, mean_i, mean_q, step_i, step_q, search=2, max_condition=50.0):
    """Local 2x2 Jacobian DC solve on the 1/64 code lattice.

    base_dc: normalized controls of the base capture; mean0/mean_i/mean_q: complex sample means at
    base, base+step_i on I control, base+step_q on Q control (steps in codes). The full Jacobian
    absorbs I/Q swaps (XB-200 mix path) and cross-coupling.
    Returns dict(dc, predicted_abs_mean, jacobian, condition) or None if ill-conditioned.
    """
    m0 = np.array([mean0.real, mean0.imag])
    di, dq = step_i * DC_QUANTUM, step_q * DC_QUANTUM
    if di == 0 or dq == 0:
        raise ValueError('zero DC probe step')
    jac = np.column_stack([(np.array([mean_i.real, mean_i.imag]) - m0) / di,
                           (np.array([mean_q.real, mean_q.imag]) - m0) / dq])
    sv = np.linalg.svd(jac, compute_uv=False)
    if not np.isfinite(sv).all() or sv[-1] <= 0 or sv[0] / sv[-1] > max_condition:
        return None
    best = _lattice(base_dc, m0, jac, search)
    return dict(dc=best[1], predicted_abs_mean=best[0], jacobian=jac.tolist(), condition=float(sv[0] / sv[-1]))


def _lattice(base_dc, mean, jac, search):
    """Newton step from base_dc, then the best code within +/-search under the linear model."""
    base = np.asarray(base_dc, float)
    m0 = np.asarray(mean, float)
    centre = clip_codes((base - np.linalg.solve(jac, m0)) / DC_QUANTUM)
    best = None
    for a in range(-search, search + 1):
        for b in range(-search, search + 1):
            dc = clip_codes(centre + [a, b]) * DC_QUANTUM
            residual = float(np.linalg.norm(m0 + jac @ (dc - base)))
            if best is None or residual < best[0] - 1e-15:
                best = (residual, [float(v) for v in dc])
    return best


def refine_dc(dc, mean, jacobian, search=2):
    """Second Newton step from a verified capture, reusing the Jacobian (the control is mildly nonlinear)."""
    residual, value = _lattice(dc, [mean.real, mean.imag], np.asarray(jacobian, float), search)
    return dict(dc=value, predicted_abs_mean=residual)


def quantization_bound(jacobian):
    """|mean| reachable with half a code of error on each control: the 6-bit DAC limit at these gains."""
    return float(np.linalg.norm(np.asarray(jacobian, float), axis=0).sum() * DC_QUANTUM / 2)


def sample_mean(iq):
    return complex(np.mean(np.asarray(iq), dtype=np.complex128))


def accept_verified(abs_base, abs_verified, ratio=0.7, floor=0.002):
    """Keep a solved value only if an independent capture confirms it helps."""
    return bool(abs_verified <= max(ratio * abs_base, floor) and abs_verified < abs_base)
