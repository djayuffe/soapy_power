"""Experiment design for LO-diversity and gain-diversity tests.

The two knobs that turn an ambiguous peak into an identified mixing product
are (1) a wide enough LO offset and (2) a known drive/gain change.  This module
selects LO offsets by D-optimality for the line fit ``IF = a + b*LO`` and
builds an executable sweep plan.  It does not touch hardware; a caller can
feed the plan to an existing runner or to the digital twin.
"""
import itertools
import math

import numpy as np

from soapypower import spur_inference as si
from soapypower.spur_model import receiver_profile


def _comb(n, k):
    try:
        return math.comb(n, k)
    except AttributeError:  # pragma: no cover - Python < 3.8
        return math.factorial(n) // (math.factorial(k) * math.factorial(n - k))


def _fim(lo_hz, sigma_hz):
    x = np.asarray(lo_hz, dtype=float)
    sigma = float(sigma_hz)
    if sigma <= 0:
        raise ValueError('sigma_hz must be positive')
    X = np.column_stack([np.ones(x.size), x - x.mean()])
    return X.T @ X / sigma ** 2


def d_optimal_offsets(center_hz, candidates_hz, count=3, sigma_hz=None,
                      min_span_hz=0.0, max_subsets=20000):
    """Choose ``count`` LO settings that maximize ``log det(X'X/sigma^2)``.

    The model is a line in LO; the determinant criterion is the standard
    D-optimality for the pair (intercept, slope).  A minimum span prevents a
    nominally optimal set that is too narrow to separate mixing orders.
    """
    center_hz = float(center_hz)
    candidates = np.unique(np.asarray(candidates_hz, dtype=float))
    count = int(count)
    if count < 2 or candidates.size < count:
        raise ValueError('need at least two candidates and count >= 2')
    sigma = float(sigma_hz) if sigma_hz else 1.0
    if sigma <= 0:
        raise ValueError('sigma_hz must be positive')
    min_span_hz = float(min_span_hz)
    if min_span_hz < 0:
        raise ValueError('min_span_hz must be nonnegative')

    def score(subset):
        subset = np.asarray(subset, dtype=float)
        if np.ptp(subset) < min_span_hz:
            return -float('inf')
        sign, logdet = np.linalg.slogdet(_fim(subset, sigma))
        return float(logdet) if sign > 0 else -float('inf')

    best, best_score = None, -float('inf')
    if _comb(candidates.size, count) <= max_subsets:
        for subset in itertools.combinations(candidates, count):
            s = score(subset)
            if s > best_score:
                best, best_score = subset, s
    else:
        # Greedy farthest-point initialization, then local swap improvement.
        order = np.argsort(np.abs(candidates - center_hz))[::-1]
        chosen = list(candidates[order[:count]])
        if np.ptp(chosen) < min_span_hz:
            idx = np.argsort(candidates)
            chosen = list(candidates[np.linspace(0, candidates.size - 1, count).astype(int)])
        chosen = list(np.unique(chosen))
        while len(chosen) < count:
            rest = [c for c in candidates if c not in chosen]
            chosen.append(rest[int(np.argmax(np.abs(np.asarray(rest) - center_hz)))])
        best = np.asarray(chosen, dtype=float)
        best_score = score(best)
        for _ in range(5):
            improved = False
            for i in range(count):
                for candidate in candidates:
                    trial = best.copy()
                    trial[i] = candidate
                    if len(np.unique(trial)) < count:
                        continue
                    s = score(trial)
                    if s > best_score + 1e-12:
                        best, best_score, improved = trial, s, True
            if not improved:
                break
    if best is None or not math.isfinite(best_score):
        raise ValueError('no candidate set satisfies the minimum span')
    best = np.sort(best)
    return {
        'offsets_hz': [float(x) for x in best],
        'logdet_fim': float(best_score),
        'span_hz': float(np.ptp(best)),
        'sigma_hz': sigma,
        'count': count,
        'criterion': 'D-optimal intercept+slope, constrained minimum span',
    }


def default_lo_plan(center_hz, rate_hz=56e6, count=3, span_hz=None, sigma_hz=None,
                    orders=(1, 3, 5, 7)):
    """A practical symmetric LO plan around ``center_hz``.

    The default span is 10 % of the sample rate, or five resolution bins,
    whichever is larger.  The returned separation is computed for a 1st- vs
    3rd-order slope.
    """
    center_hz, rate_hz = float(center_hz), float(rate_hz)
    if span_hz is None:
        span_hz = max(0.10 * rate_hz, 5.0 * rate_hz / 4096.0)
    span_hz = float(span_hz)
    if count < 3:
        raise ValueError('LO-diversity needs at least three settings')
    offsets = np.linspace(center_hz - span_hz / 2.0, center_hz + span_hz / 2.0, count)
    sigma = float(sigma_hz) if sigma_hz else max(rate_hz / 4096.0 / math.sqrt(12.0), 1e-9)
    return {
        'center_hz': center_hz,
        'offsets_hz': [float(x) for x in offsets],
        'span_hz': float(np.ptp(offsets)),
        'sigma_hz': sigma,
        'separation_1_vs_3': si.slope_separation(offsets, sigma, 1, 3),
        'orders': list(orders),
        'note': 'symmetric plan; use d_optimal_offsets when the tuning range is constrained',
    }


def sweep_plan(center_hz, offsets_hz, gains_db=(0.0,), filter_states=('filter-off',),
               seconds=30.0, purpose='LO diversity'):
    """A JSON-ready list of sweeps, one per LO × gain × filter state."""
    if not offsets_hz:
        raise ValueError('offsets_hz must not be empty')
    plan = []
    for offset in offsets_hz:
        for gain in gains_db:
            for filter_state in filter_states:
                plan.append({
                    'lo_hz': float(offset), 'center_hz': float(center_hz),
                    'gain_db': float(gain), 'filter': str(filter_state),
                    'seconds': float(seconds), 'purpose': purpose,
                })
    return plan


def suboctave_filter_bank(rf_min_hz=70e6, rf_max_hz=6e9, max_ratio=2.0,
                          insertion_loss_db=0.5, stopband_db=30.0):
    """Advisory sub-octave preselector bands.

    A harmonic response can only be suppressed physically.  This function
    returns a filter bank plan for an external switchable preselector; it does
    not claim that the current receiver has one.
    """
    rf_min_hz, rf_max_hz = float(rf_min_hz), float(rf_max_hz)
    if rf_min_hz <= 0 or rf_max_hz <= rf_min_hz:
        raise ValueError('invalid RF range')
    if max_ratio <= 1:
        raise ValueError('max_ratio must exceed 1')
    bands, low = [], rf_min_hz
    while low < rf_max_hz:
        high = min(low * max_ratio, rf_max_hz)
        bands.append({'low_hz': float(low), 'high_hz': float(high),
                      'center_hz': float(math.sqrt(low * high)),
                      'insertion_loss_db': float(insertion_loss_db),
                      'stopband_db': float(stopband_db),
                      'filter': 'LPF-{:.0f}-{:.0f}MHz'.format(low / 1e6, high / 1e6)})
        low = high
    return {
        'rf_min_hz': rf_min_hz, 'rf_max_hz': rf_max_hz,
        'max_ratio': float(max_ratio), 'bands': bands,
        'scope': 'advisory hardware plan; not an automatic switch driver',
        'note': 'select the band containing the RF source, never the LO; verify filter response with a known tone',
    }


def filter_for_rf(filter_bank, rf_hz):
    """Return the band that contains ``rf_hz`` or ``None``."""
    for band in filter_bank['bands']:
        if band['low_hz'] <= rf_hz <= band['high_hz']:
            return dict(band)
    return None


def lab_matrix():
    """The T1-T5 controlled tests from the plan, with explicit predictions."""
    lo = 260e6
    return {
        'T1': {
            'name': '50 ohm termination, no external RF',
            'setup': 'terminated input, LNA on, TX off, three LO settings',
            'expected': 'flat floor; no persistent peak; any peak must be classified as internal or unconfirmed',
            'tests': ['floor bias', 'FDR false-alarm rate', 'DC mask'],
        },
        'T2': {
            'name': 'single in-band tone',
            'setup': 'RF tone at LO+9 MHz, three LO settings',
            'expected': 'one signal, no harmonic classification',
            'tests': ['slope -1', 'source recovery', 'image rejection'],
        },
        'T3': {
            'name': 'out-of-band tone via 3rd harmonic',
            'setup': 'RF tone near 800 MHz with LO near 260 MHz',
            'expected': 'signal at the fundamental if in band; LO-harmonic response at f-3*LO',
            'tests': ['slope -3', 'filter-on/filter-off', 'source in 762-821 MHz'],
        },
        'T4': {
            'name': 'two in-band tones and intermodulation',
            'setup': 'two tones at LO-6 MHz and LO+11 MHz, drive sweep',
            'expected': 'two signals; intermod products either absent or ordered by drive slope',
            'tests': ['drive order', 'two-tone IMD', 'no false spur from a real tone'],
        },
        'T5': {
            'name': 'strong near-band neighbour (FM surrogate)',
            'setup': 'strong tone at LO+18 MHz, then filter on/off',
            'expected': 'signal for the fundamental; harmonic/filter response documented, not silently notched',
            'tests': ['preselector', 'image', 'floor preservation'],
        },
    }


def design_report(center_hz=260e6, rate_hz=56e6, profile='b200', count=3,
                  span_hz=None, gains_db=(0.0, -10.0, -20.0)):
    """Complete JSON-ready design bundle for one band."""
    prof = receiver_profile(profile)
    lo_plan = default_lo_plan(center_hz, rate_hz=rate_hz, count=count, span_hz=span_hz)
    bank = suboctave_filter_bank(prof.rf_min_hz, prof.rf_max_hz)
    rf = center_hz + 800e6  # a source that can reach the mixer by the 3rd harmonic
    return {
        'profile': prof.to_dict(),
        'lo_plan': lo_plan,
        'sweeps': sweep_plan(center_hz, lo_plan['offsets_hz'], gains_db=gains_db,
                             filter_states=('filter-off', 'filter-on')),
        'filter_bank': bank,
        'filter_for_example_source': filter_for_rf(bank, rf),
        'lab_matrix': lab_matrix(),
        'identifiability': {
            'crlb': si.crlb_slope(lo_plan['offsets_hz'], lo_plan['sigma_hz']),
            'separation_1_vs_3': lo_plan['separation_1_vs_3'],
        },
        'scope': 'relative experiment design; no hardware command is executed',
    }
