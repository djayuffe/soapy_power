"""Digital twin of a direct-conversion receiver's spectrum, and the spur test matrix T1-T5.

The twin draws PSD bins directly (Gamma-distributed about their mean, like a Welch average of
dof/2 segments) for one LO setting at a time:

* floor: mean sigma^2 on every IF bin;
* DC leakage: IF-fixed line at 0 Hz;
* RF-fixed signals: IF = f - LO (order 1);
* LO-harmonic responses of a square-wave LO: IF = f - n*LO for odd n, amplitude (power) 1/n^2;
* IQ image: IF = -(f - LO) at -image_rejection_db;
* n-th order intermodulation product: power slope n against the input drive.

`analyze` is the pipeline under test: bias-corrected floor, FDR peak detection, LO-diversity
classification (+ optional drive scaling). `legacy_analyze` is what the suite did before:
dB-median floor, fixed threshold, no classification. Everything is relative.
"""
import math
import numpy as np
from soapypower import floor_estimation as fe

RATE_HZ = 56e6


class Twin:
    def __init__(self, bins=4096, rate_hz=RATE_HZ, dof=20.0, floor=1.0, seed=0, dc_db=15.0,
                 image_rejection_db=35.0, harmonics=(3, 5, 7)):
        self.bins, self.rate, self.dof, self.floor = bins, rate_hz, dof, floor
        self.step = rate_hz / bins
        self.if_hz = (np.arange(bins) - bins // 2) * self.step
        self.rng = np.random.default_rng(seed)
        self.dc_db, self.irr, self.harmonics = dc_db, image_rejection_db, harmonics

    def _add(self, mean, if_target, power_db_above_floor):
        i = int(round(if_target / self.step)) + self.bins // 2
        if 1 <= i < self.bins - 1:
            mean[i] += self.floor * 10 ** (power_db_above_floor / 10)

    def capture(self, lo_hz, tones=(), drive_db=0.0, imd=()):
        """One PSD (linear). tones: (rf_hz, db above floor at drive 0). imd: (rf_hz, db at drive 0, order)."""
        mean = np.full(self.bins, self.floor)
        self._add(mean, 0.0, self.dc_db)
        for rf, level in tones:
            self._add(mean, rf - lo_hz, level + drive_db)
            self._add(mean, -(rf - lo_hz), level + drive_db - self.irr)
            for n in self.harmonics:
                self._add(mean, rf - n * lo_hz, level + drive_db - 20 * math.log10(n) - 6.0)
        for rf, level, order in imd:
            self._add(mean, rf - lo_hz, level + order * drive_db)
        k = self.dof / 2.0
        return mean * self.rng.gamma(k, 1.0 / k, self.bins)


# ------------------------------------------------------------------------- analyzers

def _candidates(power, floor, dof, method, alpha):
    det = fe.detect_peaks(power, floor, dof, alpha=alpha, method=method)
    peaks = []
    for grp in fe.group_peaks(det['index']):
        top = grp[np.argmax(power[grp])]
        peaks.append(int(top))
    return peaks


def analyze(twin, lo_list, tones=(), imd=(), alpha=0.001, dc_mask_hz=0.5e6, tol_bins=1):
    """Floor + FDR peaks + LO-diversity classification over captures at lo_list (first = reference)."""
    caps = [twin.capture(lo, tones=tones, imd=imd) for lo in lo_list]
    ref = caps[0]
    masks = []
    for c in caps:
        keep = np.abs(twin.if_hz) >= dc_mask_hz
        masks.append(keep)
    est = fe.floor_estimate(np.concatenate([c[m] for c, m in zip(caps, masks)]), dof=twin.dof, correlation_bins=1.0)
    floor = est['mean_linear']
    dets = []
    for c, m in zip(caps, masks):
        pw = np.where(m, c, np.nan)
        dets.append(np.array(sorted(_candidates(pw, floor, twin.dof, 'bh', alpha)), dtype=int))
    found = []
    for i0 in dets[0]:
        f0 = twin.if_hz[i0]
        tracks = {}
        for n, s in [(1, 1)] + [(n, s) for n in (1, 3, 5, 7) for s in (-1, 1) if (n, s) != (1, 1)]:
            slope = -s * n
            hits = [(lo_list[0], f0)]
            for lo, d in zip(lo_list[1:], dets[1:]):
                pred = f0 + slope * (lo - lo_list[0])
                j = int(round(pred / twin.step)) + twin.bins // 2
                near = d[np.abs(d - j) <= tol_bins]
                if near.size:
                    hits.append((lo, twin.if_hz[near[np.argmin(np.abs(near - j))]]))
            tracks[(n, s)] = hits
        full = [k for k, h in tracks.items() if len(h) == len(lo_list)]
        if not full:
            found.append({'if_hz': float(f0), 'kind': 'unconfirmed', 'order': None})
            continue
        full.sort(key=lambda k: (0 if k == (1, 1) else 1, k[0], -k[1]))
        n, s = full[0]
        hits = tracks[(n, s)]
        verdict = fe.classify_lo_slope([h[0] for h in hits], [h[1] for h in hits], resolution_hz=twin.step)
        kind = 'signal' if (n, s) == (1, 1) else ('image' if n == 1 else 'spur-%d' % n)
        found.append({'if_hz': float(f0), 'kind': kind, 'order': n, 'sign': s, 'status': verdict.get('status'),
                      'source_hz': verdict.get('source_hz'), 'confirmed': True})
    return {'floor': est, 'peaks': found}


def legacy_analyze(twin, lo_list, tones=(), imd=(), fixed_db=6.0, dc_mask_hz=0.5e6):
    """Previous behaviour: dB-median floor, fixed threshold, every peak reported as a signal."""
    ref = twin.capture(lo_list[0], tones=tones, imd=imd)
    keep = np.abs(twin.if_hz) >= dc_mask_hz
    floor = float(np.median(ref[keep]))
    det = fe.detect_peaks(np.where(keep, ref, np.nan), floor, twin.dof, method='fixed', fixed_db=fixed_db)
    peaks = [{'if_hz': float(twin.if_hz[int(g[np.argmax(ref[g])])]), 'kind': 'signal'} for g in fe.group_peaks(det['index'])]
    return {'floor_db': 10 * math.log10(floor), 'peaks': peaks}


# ---------------------------------------------------------------------- test matrix

def _expected(tones, lo, irr_db=35.0, min_db=8.0, window_hz=RATE_HZ / 2 * .95, optional_db=3.0):
    """Truth list: (kind, IF Hz[, 'optional']). Required above min_db; real but marginal features
    (optional_db..min_db) may be found or missed without counting as hit or false alarm."""
    out = []
    for rf, level in tones:
        if abs(rf - lo) < window_hz:
            out.append(('signal', rf - lo))
            if level - irr_db >= optional_db and abs(rf - lo) > 0.5e6:
                out.append(('image', -(rf - lo)) if level - irr_db >= min_db else ('image', -(rf - lo), 'optional'))
        for n in (3, 5, 7):
            level_n = level - 20 * math.log10(n) - 6.0
            f = rf - n * lo
            if level_n >= optional_db and abs(f) < window_hz and abs(f) > 0.5e6:
                out.append(('spur-%d' % n, f) if level_n >= min_db else ('spur-%d' % n, f, 'optional'))
    return out


def scenarios():
    """T1-T5 of the plan, frequencies in Hz. expected: (kind, if_hz at the reference LO)."""
    lo = 260e6
    los = [lo, lo + 2e6, lo - 2e6]
    fm_lo = 191e6
    fm_los = [fm_lo, fm_lo + 2e6, fm_lo - 2e6]
    matrix = {
        'T1 no signal (50 ohm)': ((), los),
        'T2 200 MHz-band in-band tone': (((fm_lo + 9e6, 40.0),), fm_los),
        'T3 800 MHz out-of-band tone': (((800e6, 45.0),), los),
        'T4 two in-band tones': (((lo - 6e6, 40.0), (lo + 11e6, 40.0)), los),
        'T5 strong near-band neighbour (FM surrogate)': (((lo + 18e6, 55.0),), los),
    }
    return {name: dict(tones=t, los=l, expected=_expected(t, l[0]))
            for name, (t, l) in matrix.items()}


def score(found, expected, step_hz):
    """tp / fp / missed / kind_errors of found peaks against expected (optional features are neutral)."""
    exp = list(expected)
    required = [i for i, e in enumerate(exp) if len(e) == 2]
    used, tp, kind_err, neutral = set(), 0, 0, 0
    for p in found:
        match = None
        for i, e in enumerate(exp):
            if i not in used and abs(p['if_hz'] - e[1]) <= 2 * step_hz:
                match = i
                break
        if match is None:
            continue
        used.add(match)
        if len(exp[match]) == 3:
            neutral += 1
            continue
        tp += 1
        if p['kind'] != exp[match][0]:
            kind_err += 1
    fp = len(found) - len(used)
    return {'tp': tp, 'fp': fp, 'missed': len(required) - tp, 'kind_errors': kind_err}
