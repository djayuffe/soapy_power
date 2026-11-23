"""End-to-end signal-versus-spur pipeline.

Input is a set of captures at different LO settings (and optionally different
drive levels), all representing the same receiver state.  The pipeline:

1. estimates each capture's noise floor and normalizes it away;
2. estimates the common floor with a Gamma mixture, so spurs are modelled
   instead of silently deleting the upper tail;
3. detects peaks with Benjamini-Hochberg FDR control;
4. associates peaks across LO settings and tests the square-wave laws
   ``IF = s*(RF - n*LO)``;
5. classifies each track with a Bayesian posterior, robust residuals and an
   optional drive-order test;
6. reports signals, images, LO-harmonic responses and unconfirmed peaks, with
   an explicit before/after floor estimate.

No automatic notch or exclusion list is produced.  The output is evidence for
a human or a later, separately validated rule.
"""
import math

import numpy as np

from soapypower import floor_estimation as fe
from soapypower import spur_inference as si
from soapypower.spur_model import receiver_profile


BAND_LABELS = (
    (87.5e6, 108e6, 'FM broadcast'),
    (174e6, 240e6, 'DAB/VHF'),
    (470e6, 790e6, 'DVB-T/UHF'),
    (791e6, 862e6, 'LTE 800 downlink'),
    (880e6, 960e6, 'GSM 900'),
    (1710e6, 1785e6, 'LTE 1800 uplink'),
    (1805e6, 1880e6, 'LTE 1800 downlink'),
    (1920e6, 1980e6, 'LTE 1900/2100 uplink'),
    (2110e6, 2170e6, 'LTE 2100 downlink'),
    (2400e6, 2500e6, '2.4 GHz ISM'),
    (5150e6, 5850e6, '5 GHz Wi-Fi'),
)


def band_label(rf_hz):
    """Human-readable context for a source frequency, or ``None``."""
    for low, high, label in BAND_LABELS:
        if low <= rf_hz <= high:
            return label
    return None


def _finite(value, name):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('{} must be finite'.format(name))
    return value


def make_capture(lo_hz, power, if_hz=None, rate_hz=None, dof=None,
                 drive_db=0.0, gain_db=None, temperature_c=None,
                 capture_id=None, partition='train', unit='linear',
                 metadata=None):
    """Validate and normalize one spectrum capture.

    ``power`` is linear PSD per bin by default; ``unit='db'`` accepts dB and
    converts to linear.  If ``if_hz`` is omitted, ``rate_hz`` and the array
    length define a centered, fft-shifted axis (the convention of
    :func:`soapypower.spur_model.welch_psd`).
    """
    lo_hz = _finite(lo_hz, 'lo_hz')
    p = np.asarray(power, dtype=float)
    if p.ndim != 1 or p.size < 8:
        raise ValueError('power must be a one-dimensional array of at least eight bins')
    if np.any(~np.isfinite(p)):
        raise ValueError('power contains non-finite values')
    if unit.lower() in ('db', 'db/hz', 'dbm'):
        p = 10.0 ** (p / 10.0)
    elif unit.lower() not in ('linear', 'power'):
        raise ValueError('unit must be linear or db')
    if not np.isfinite(p).all() or np.any(p <= 0):
        raise ValueError('power must be positive after unit conversion')
    if rate_hz is not None and _finite(rate_hz,'rate_hz')<=0:raise ValueError('rate_hz must be positive')
    if if_hz is None:
        if rate_hz is None:
            raise ValueError('if_hz or rate_hz is required')
        rate_hz = _finite(rate_hz, 'rate_hz')
        if rate_hz <= 0:
            raise ValueError('rate_hz must be positive')
        if_hz = (np.arange(p.size, dtype=float) - p.size // 2) * (rate_hz / p.size)
    if_hz = np.asarray(if_hz, dtype=float)
    if if_hz.ndim != 1 or if_hz.size != p.size or np.any(~np.isfinite(if_hz)):
        raise ValueError('if_hz must be finite, one-dimensional and the same length as power')
    if np.any(np.diff(if_hz) <= 0):
        raise ValueError('if_hz must be strictly increasing')
    if dof is not None:
        dof = _finite(dof, 'dof')
        if dof <= 0:
            raise ValueError('dof must be positive')
    if partition not in ('train', 'validation'):
        raise ValueError('partition must be train or validation')
    return {
        'lo_hz': lo_hz,
        'if_hz': if_hz,
        'power': p,
        'rate_hz': float(rate_hz) if rate_hz is not None else float(np.ptp(if_hz) + np.median(np.diff(if_hz))),
        'dof': dof,
        'drive_db': _finite(drive_db, 'drive_db'),
        'gain_db': None if gain_db is None else _finite(gain_db, 'gain_db'),
        'temperature_c': None if temperature_c is None else _finite(temperature_c, 'temperature_c'),
        'capture_id': str(capture_id) if capture_id is not None else None,
        'partition': partition,
        'metadata': dict(metadata or {}),
    }


def captures_from_twin(twin, lo_list, tones=(), imd=(), drive_db=0.0, partition='train'):
    """Build pipeline captures from :class:`soapypower.spur_twin.Twin`."""
    out = []
    for i, lo in enumerate(lo_list):
        out.append(make_capture(lo, twin.capture(lo, tones=tones, imd=imd, drive_db=drive_db),
                                if_hz=twin.if_hz, rate_hz=twin.rate, dof=twin.dof,
                                drive_db=drive_db, capture_id='twin-{:02d}'.format(i),
                                partition=partition))
    return out


def _clean(value):
    """Recursively convert numpy scalars/arrays and reject JSON NaN/Infinity."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_clean(v) for v in value.tolist()]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _capture_floor(capture, dc_mask_hz, alpha):
    keep = np.abs(capture['if_hz']) >= float(dc_mask_hz)
    if keep.sum() < 8:
        raise ValueError('fewer than eight bins outside the DC mask')
    est = fe.floor_estimate(capture['power'][keep], dof=capture['dof'], spur_alpha=alpha)
    return est, keep


def _peaks(capture, floor_mean, dof, alpha, dc_mask_hz, normalization):
    """Detected peaks of one capture, in normalized units."""
    keep = np.abs(capture['if_hz']) >= float(dc_mask_hz)
    norm = capture['power'] * normalization
    test = np.where(keep, norm, np.nan)
    det = fe.detect_peaks(test, floor_mean, dof, alpha=alpha, method='bh')
    peaks = []
    for group in fe.group_peaks(det['index']):
        top = int(group[np.argmax(test[group])])
        q = det.get('q_value')
        q_index = np.searchsorted(det['index'], top)
        q_value = float(q[q_index]) if q is not None and q_index < len(q) else None
        peaks.append({
            'index': top,
            'if_hz': float(capture['if_hz'][top]),
            'power_linear': float(norm[top]),
            'power_db': 10.0 * math.log10(float(norm[top])),
            'surprise_db': 10.0 * math.log10(float(norm[top]) / floor_mean),
            'q_value': q_value,
            'group_indices': [int(i) for i in group],
            'group_width_bins': int(group.size),
        })
    return peaks


def _track_one(seed_capture, seed_peak, captures, peaks, law, tol_bins):
    n, s, kind = law
    lo0, if0 = captures[seed_capture]['lo_hz'], seed_peak['if_hz']
    points = []
    for i, (capture, found) in enumerate(zip(captures, peaks)):
        if i == seed_capture:
            predicted = if0
        else:
            predicted = if0 - s * n * (capture['lo_hz'] - lo0)
        if abs(predicted) > float(np.ptp(capture['if_hz'])) / 2.0:
            continue
        if not found:
            continue
        candidates = [p for p in found if abs(p['if_hz'] - predicted) <= tol_bins * float(np.median(np.diff(capture['if_hz'])))]
        if not candidates:
            continue
        peak = min(candidates, key=lambda p: abs(p['if_hz'] - predicted))
        points.append({
            'capture': i,
            'peak': peak,
            'lo_hz': float(capture['lo_hz']),
            'if_hz': float(peak['if_hz']),
            'predicted_if_hz': float(predicted),
            'power_db': float(peak['power_db']),
            'drive_db': float(capture['drive_db']),
            'gain_db': capture['gain_db'],
            'temperature_c': capture['temperature_c'],
        })
    if seed_capture not in {p['capture'] for p in points}:
        points.append({
            'capture': seed_capture, 'peak': seed_peak, 'lo_hz': float(lo0),
            'if_hz': float(if0), 'predicted_if_hz': float(if0),
            'power_db': float(seed_peak['power_db']), 'drive_db': float(captures[seed_capture]['drive_db']),
            'gain_db': captures[seed_capture]['gain_db'],
            'temperature_c': captures[seed_capture]['temperature_c'],
        })
    lo = np.array([p['lo_hz'] for p in points])
    f = np.array([p['if_hz'] for p in points])
    predicted = np.array([p['predicted_if_hz'] for p in points])
    return {
        'law': (n, s, kind), 'points': points,
        'hits': len(points),
        'lo_span_hz': float(np.ptp(lo)) if lo.size else 0.0,
        'rms_residual_hz': float(np.sqrt(np.mean((f - predicted) ** 2))) if points else float('inf'),
        'seed_capture': int(seed_capture),
        'seed_if_hz': float(if0),
    }


def _select_tracks(captures, peaks, orders, tol_bins, min_hits):
    """Build candidate tracks from every seed and greedily select non-overlapping ones."""
    candidates = []
    laws = [(n, s, 'signal' if (n, s) == (1, 1) else 'image' if n == 1 else 'lo_harmonic')
            for n in orders for s in (1, -1)]
    for ci, found in enumerate(peaks):
        for peak in found:
            for law in laws:
                track = _track_one(ci, peak, captures, peaks, law, tol_bins)
                if track['hits'] >= min_hits:
                    candidates.append(track)
    # Deduplicate identical point sets (same physical track seeded from many captures).
    unique = {}
    for track in candidates:
        key = tuple(sorted((p['capture'], p['peak']['index']) for p in track['points']))
        old = unique.get(key)
        score = (-track['hits'], track['rms_residual_hz'], -track['lo_span_hz'])
        if old is None or score < old[0]:
            unique[key] = (score, track)
    ordered = sorted((item[1] for item in unique.values()),
                     key=lambda t: (-t['hits'], t['rms_residual_hz'], -t['lo_span_hz']))
    used, selected = set(), []
    for track in ordered:
        points = {(p['capture'], p['peak']['index']) for p in track['points']}
        if points & used:
            continue
        # A real signal and its IQ image are different peaks; shared points are
        # not.  This rule prevents the same evidence from being counted twice.
        used |= points
        selected.append(track)
    return selected


def _track_result(track, captures, orders, resolution_hz, profile):
    lo = np.array([p['lo_hz'] for p in track['points']])
    f = np.array([p['if_hz'] for p in track['points']])
    class_lo = fe.classify_lo_slope(lo, f, orders=orders, resolution_hz=resolution_hz)
    bayes = si.bayes_classify_track(lo, f, orders=orders, resolution_hz=resolution_hz,
                                    sigma_hz=max(resolution_hz, 1e-9) / math.sqrt(12.0))
    if lo.size >= 2:
        robust = si.robust_line_fit(lo, f, sigma=np.full(lo.size, max(resolution_hz, 1e-9) / math.sqrt(12.0)))
    else:
        robust = {'status': 'unidentifiable', 'reason': 'needs at least two LO points'}
    # Drive order uses level above each capture's own floor, so gain changes do
    # not masquerade as nonlinearity.
    levels = np.array([p['power_db'] for p in track['points']])
    drives = np.array([p['drive_db'] for p in track['points']])
    drive = fe.drive_order(drives, levels) if np.ptp(drives) >= 1.0 and drives.size >= 2 else {
        'status': 'not_available', 'reason': 'needs >= 2 drive levels spanning >= 1 dB'}
    best = bayes.get('best') or {}
    source = best.get('source_hz')
    in_band = None
    if source is not None and profile is not None:
        in_band = bool(profile.rf_min_hz <= source <= profile.rf_max_hz)
    kind = bayes.get('kind') or class_lo.get('kind')
    # A drive-order slope near 1 dB/dB is a linear response; a slope near an
    # integer >= 2 is the signature of an n-th order nonlinear product.  Only
    # a clearly measured slope changes the attribution, so an absent or noisy
    # drive test leaves "signal" untouched.
    attribution = kind
    drive_kind = 'not_available'
    if drive.get('status') == 'measured':
        slope = float(drive['slope_db_per_db'])
        se = drive.get('slope_se')
        order = drive.get('order')
        near_linear = abs(slope - 1.0) <= max(0.25, 3.0 * float(se)) if se is not None else abs(slope - 1.0) <= 0.25
        if near_linear:
            drive_kind = 'linear'
        elif order is not None and order >= 2 and slope >= 1.6:
            drive_kind = 'nonlinear-{}'.format(int(order))
            if kind == 'signal':
                attribution = 'intermod'
    return {
        'status': bayes.get('status', class_lo.get('status')),
        'kind': kind,
        'attribution': attribution,
        'drive_kind': drive_kind,
        'order': bayes.get('order', class_lo.get('order')),
        'sign': bayes.get('sign', class_lo.get('sign')),
        'source_hz': source,
        'band': band_label(source) if source is not None else None,
        'in_receiver_band': in_band,
        'signal_probability': bayes.get('signal_probability'),
        'spur_probability': bayes.get('spur_probability'),
        'posterior': bayes.get('posterior', {}),
        'bayes': bayes,
        'lo_slope': class_lo,
        'robust_line': robust,
        'drive': drive,
        'hits': track['hits'],
        'lo_points': int(lo.size),
        'lo_span_hz': track['lo_span_hz'],
        'rms_residual_hz': track['rms_residual_hz'],
        'points': [{'capture': p['capture'], 'lo_hz': p['lo_hz'], 'if_hz': p['if_hz'],
                    'power_db': p['power_db'], 'drive_db': p['drive_db'],
                    'gain_db': p['gain_db'], 'temperature_c': p['temperature_c']}
                   for p in track['points']],
        'scope': 'posterior and classification are relative, not calibrated',
    }


def analyze_sweeps(captures, orders=(1, 3, 5, 7), alpha=1e-3, dc_mask_hz=0.5e6,
                   tol_bins=1.0, min_hits=None, profile=None, confidence=0.95):
    """Run the complete signal/spur pipeline over captures.

    ``min_hits`` defaults to at least three captures and 60 % of the captures.
    ``profile`` may be a :class:`ReceiverProfile`, a profile name or ``None``.
    """
    if not captures:
        raise ValueError('at least one capture is required')
    if not all(isinstance(c, dict) for c in captures):
        raise ValueError('every capture must be a dictionary of capture fields')
    # Re-validate every capture, including ones loaded from JSON where power
    # and if_hz are plain lists.  make_capture is idempotent for its own
    # output dicts and converts lists to arrays.
    captures = [make_capture(**c) for c in captures]
    if any(len(c['power']) != len(captures[0]['power']) for c in captures):
        raise ValueError('all captures must have the same number of bins')
    if len({c['partition'] for c in captures})>1:
        raise ValueError('training and validation captures cannot be fitted together')
    if any(c['rate_hz']!=captures[0]['rate_hz'] or not np.array_equal(c['if_hz'],captures[0]['if_hz']) or c['dof']!=captures[0]['dof'] for c in captures):
        raise ValueError('pooled spur inference requires matching rate, IF grid and degrees of freedom')
    if profile is not None:
        profile = receiver_profile(profile)
    # A capture carrying a non-finite dof is not usable; infer from the data.
    floors, keeps = [], []
    for capture in captures:
        est, keep = _capture_floor(capture, dc_mask_hz, alpha)
        floors.append(est)
        keeps.append(keep)
    if min_hits is None:
        min_hits = max(3, int(math.ceil(0.6 * len(captures)))) if len(captures) >= 3 else len(captures)
    min_hits = min(int(min_hits), len(captures))
    # Normalize each capture to its own measured floor.  This makes peaks and
    # floors comparable across gain settings and removes the receiver's gain
    # from every subsequent ratio.
    norms = [1.0 / est['mean_linear'] for est in floors]
    all_norm = np.concatenate([c['power'][k] * s for c, k, s in zip(captures, keeps, norms)])
    dof = captures[0]['dof']
    if dof is None:
        dof = fe.estimate_dof(all_norm)
    floor_before = fe.floor_estimate(all_norm, dof=dof, confidence=confidence, spur_alpha=alpha)
    floor_after = si.mixture_floor_estimate(all_norm, dof=dof)
    # The mixture returns arrays; keep them out of the JSON report.
    floor_after_summary = {k: v for k, v in floor_after.items()
                           if k not in ('floor_responsibility', 'spur_probability')}
    floor_after_summary['spur_fraction'] = float(np.mean(floor_after['spur_probability'] > 0.5))
    floor_mean = float(floor_after['mean_linear'])
    peaks = [_peaks(c, floor_mean, dof, alpha, dc_mask_hz, s)
             for c, s in zip(captures, norms)]
    resolution = float(np.median([np.median(np.diff(c['if_hz'])) for c in captures]))
    tracks = _select_tracks(captures, peaks, orders, tol_bins, min_hits)
    results = [_track_result(t, captures, orders, resolution, profile) for t in tracks]
    # Mark spur bins in the reference capture for visualization.  A bin is
    # marked only when a classified image/harmonic track actually covers it.
    spur_bins = set()
    signal_bins = set()
    for track, result in zip(tracks, results):
        for point in track['points']:
            indices = point['peak']['group_indices']
            if result['attribution'] == 'signal':
                signal_bins.update((point['capture'], i) for i in indices)
            elif result['attribution'] in ('image', 'lo_harmonic', 'intermod'):
                spur_bins.update((point['capture'], i) for i in indices)
    ref = captures[0]
    spur_mask = np.zeros(len(ref['power']), dtype=bool)
    signal_mask = np.zeros(len(ref['power']), dtype=bool)
    for capture, index in spur_bins:
        if capture == 0:
            spur_mask[index] = True
    for capture, index in signal_bins:
        if capture == 0:
            signal_mask[index] = True
    lo_span = float(np.ptp([c['lo_hz'] for c in captures]))
    separation = si.slope_separation([c['lo_hz'] for c in captures], resolution / math.sqrt(12.0), 1, 3) \
        if len(captures) >= 2 else None
    identifiable = bool(fe.distinct_lo_count([c['lo_hz'] for c in captures],resolution)>=3 and separation and separation['separable_3sigma'])
    report = {
        'version': 1,
        'validation_performed': False,
        'analysis_partition': captures[0]['partition'],
        'scope': 'relative uncalibrated power; no automatic exclusions or notches',
        'config': {
            'captures': len(captures), 'orders': list(orders), 'alpha': float(alpha),
            'dc_mask_hz': float(dc_mask_hz), 'tol_bins': float(tol_bins),
            'min_hits': int(min_hits), 'confidence': float(confidence),
            'dof': float(dof), 'profile': profile.to_dict() if profile else None,
        },
        'captures': [{
            'capture_id': c['capture_id'], 'lo_hz': c['lo_hz'], 'rate_hz': c['rate_hz'],
            'dof': c['dof'], 'drive_db': c['drive_db'], 'gain_db': c['gain_db'],
            'temperature_c': c['temperature_c'], 'partition': c['partition'],
            'bins': int(len(c['power'])), 'floor_mean_db': floors[i]['mean_db'],
            'floor_ci_db': floors[i]['ci_db'], 'floor_median_uncorrected_db': floors[i]['median_db_uncorrected'],
        } for i, c in enumerate(captures)],
        'floor': {
            'before': floor_before,
            'after': floor_after_summary,
            'change_db': float(floor_after['mean_db'] - floor_before['mean_db']),
            'median_bias_db': float(floor_before['bias_db']),
            'min_hold_bias_3_db': float(fe.min_hold_to_mean_db(3, dof)),
            'min_hold_bias_8_db': float(fe.min_hold_to_mean_db(8, dof)),
            'scope': 'before = bias-corrected median, after = Gamma mixture; both relative',
        },
        'identifiability': {
            'lo_span_hz': lo_span,
            'resolution_hz': resolution,
            'separation_1_vs_3': separation,
            'identifiable_for_3rd_order': identifiable,
            'reason': 'needs >= 3 LO settings spanning >= 4 resolution bins; otherwise peaks are unconfirmed',
        },
        'tracks': results,
        'signals': [r for r in results if r['attribution'] == 'signal'],
        'intermod': [r for r in results if r['attribution'] == 'intermod'],
        'spurs': [r for r in results if r['attribution'] in ('image', 'lo_harmonic')],
        'unconfirmed': [r for r in results
                        if r['attribution'] not in ('signal', 'intermod', 'image', 'lo_harmonic')],
        'before_after': {
            'if_hz': [float(x) for x in ref['if_hz']],
            'power_db': [float(10.0 * math.log10(x)) for x in ref['power'] * norms[0]],
            'floor_before_db': float(floor_before['mean_db']),
            'floor_after_db': float(floor_after['mean_db']),
            'median_before_db': float(floor_before['median_db_uncorrected']),
            'spur_mask': [bool(x) for x in spur_mask],
            'signal_mask': [bool(x) for x in signal_mask],
        },
        'diagnostics': {
            'peaks_per_capture': [len(p) for p in peaks],
            'candidate_tracks': len(tracks),
            'signals': len([r for r in results if r['attribution'] == 'signal']),
            'intermod': len([r for r in results if r['attribution'] == 'intermod']),
            'spurs': len([r for r in results if r['attribution'] in ('image', 'lo_harmonic')]),
            'unconfirmed': len([r for r in results
                                if r['attribution'] not in ('signal', 'intermod', 'image', 'lo_harmonic')]),
        },
        'automatic_exclusions': [],
        'recommendations': _recommendations(results, identifiable, profile),
    }
    return _clean(report)


def _recommendations(results, identifiable, profile):
    out = []
    if not identifiable:
        out.append('Use at least three LO settings spanning four resolution bins before claiming a spur identity.')
    if any(r['attribution'] == 'lo_harmonic' for r in results):
        out.append('An LO-harmonic response was identified; an external low-pass/preselector filter or a lower-harmonic receiver path is the physical remedy.')
    if any(r['attribution'] == 'image' for r in results):
        out.append('An IQ image was identified; estimate and apply the receiver IQ correction for this state, and verify it with a known tone.')
    if any(r['attribution'] == 'intermod' for r in results):
        out.append('A nonlinear intermodulation signature was measured; repeat at lower input drive before attributing the feature to the environment.')
    elif any(r['drive'].get('order') in (2, 3, 4, 5) for r in results):
        out.append('A nonlinear drive-order signature was measured; repeat at lower input drive before attributing the feature to the environment.')
    if profile is not None and profile.rf_max_hz < 6e9:
        out.append('{} covers {}–{} MHz; sources above the RF range can still reach the mixer through harmonics.'.format(
            profile.name, profile.rf_min_hz / 1e6, profile.rf_max_hz / 1e6))
    if not out:
        out.append('No action implied: classifications are relative and no automatic exclusion was made.')
    return out
