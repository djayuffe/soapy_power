"""Offline scalar receiver/LNA characterization, separate from power profiles.

Known source temperatures are at each configuration's physical reference plane.
No digital full-scale, analog voltage gain, or internal gain topology is inferred.
"""
import copy
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from soapypower.calibration import _compare
from soapypower.calibration_workflows import (
    BOLTZMANN, REFERENCE_K, DSP_FIELDS, _sha256, _check_coordinates,
    average_capture, band_summary)
from soapypower.files import load_json
from soapypower.numerics import finite, integer, numeric_array


def _vector(value, name, minimum=3, positive=True):
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError):
        raise ValueError(name + ' must be a numeric vector') from None
    if array.ndim != 1 or len(array) < minimum or not np.isfinite(array).all():
        raise ValueError(name + ' must be a finite vector with enough observations')
    if positive and np.any(array <= 0):
        raise ValueError(name + ' must be strictly positive')
    return array


def fit_noise_temperature(temperatures_k, digital_density, density_std=None):
    """OLS or known-sigma WLS with centered/scaled SVD, never normal equations.

    Fit covariance is conditional on exact temperatures and independent density
    errors. Supplied sigma is standard error of each mean, not frame scatter.
    Without sigma, residual scatter estimates it (not a metrology budget).
    Three distinct source temperatures are required to expose curvature.
    """
    t = _vector(temperatures_k, 'temperatures')
    d = _vector(digital_density, 'digital density')
    if d.shape != t.shape or len(np.unique(t)) < 3:
        raise ValueError('need matching densities and at least three distinct temperatures')
    # Normalize before linear algebra to preserve very small raw PSD scales.
    ts, ds = float(np.max(t)), float(np.max(d))
    if float(np.ptp(d / ds)) <= 32 * np.finfo(float).eps:
        raise ValueError('digital density has no numerically resolvable source response')
    tn = t / ts
    origin = float(np.mean(tn))
    spread = float(np.ptp(tn))
    if spread <= 1e-10:
        raise ValueError('temperature span is too small for stable characterization')
    design = np.column_stack(((tn - origin) / spread, np.ones(len(t))))
    if density_std is None:
        weights = np.ones(len(t))
    else:
        sigma = _vector(density_std, 'density standard errors')
        if sigma.shape != d.shape:
            raise ValueError('density standard errors must match observations')
        weights = float(np.min(sigma)) / sigma
        if np.any(weights < 1e-12):
            raise ValueError('density weight dynamic range is too large')
    matrix = design * weights[:, None]
    u, singular, vt = np.linalg.svd(matrix, full_matrices=False)
    if singular[-1] <= singular[0] * 1e-10:
        raise ValueError('ill-conditioned source-temperature fit')
    inverse = (vt.T / singular) @ u.T
    beta = inverse @ (d / ds * weights)
    prediction = design @ beta
    if np.any(prediction <= 0):
        raise ValueError('fit predicts nonpositive measured density')
    slope_scaled = float(beta[0]) / spread
    intercept_scaled = float(beta[1]) - slope_scaled * origin
    if slope_scaled <= 0:
        raise ValueError('source-temperature slope must be positive')
    slope = slope_scaled * ds / ts
    intercept = intercept_scaled * ds
    te = intercept_scaled / slope_scaled * ts
    if not all(math.isfinite(x) for x in (slope, intercept, te)) or slope <= 0:
        raise ValueError('fit result outside float64 range')
    residual = d / ds - prediction
    if density_std is None:
        variance = float(np.sum((residual * weights)**2)) / (len(t) - 2)
    else:
        variance = (float(np.min(sigma)) / ds)**2
    cov_beta = (inverse @ inverse.T) * variance
    transform = np.array([[1 / (spread * ts), 0], [-origin / spread, 1]]) * ds
    covariance = transform @ cov_beta @ transform.T
    # Propagate using normalized coefficients to avoid dividing tiny PSD squares.
    grad_te = np.array([-float(beta[1]) / float(beta[0])**2, 1 / float(beta[0])]) * spread * ts
    te_std = math.sqrt(max(0, float(grad_te @ cov_beta @ grad_te)))
    delta_db = 10 * (np.log10(d / ds) - np.log10(prediction))
    noise_factor = 1 + te / REFERENCE_K
    result = {
        'slope_density_per_k': slope, 'intercept_density': intercept,
        'digital_conversion_per_watt': slope / BOLTZMANN,
        'input_power_offset_db': 10 * (math.log10(1000 * BOLTZMANN) - math.log10(slope)),
        'receiver_noise_temp_k': te,
        'receiver_noise_figure_db': 10 * math.log10(noise_factor) if noise_factor > 0 else None,
        'physical_noise_temperature': te >= 0,
        'temperatures_k': t.tolist(), 'digital_density': d.tolist(),
        'predicted_density': (prediction * ds).tolist(), 'residual_db': delta_db.tolist(),
        'max_abs_residual_db': float(np.max(abs(delta_db))),
        'measured_density_span_db': 10 * (math.log10(float(d.max())) - math.log10(float(d.min()))),
        'temperature_range_k': [float(t.min()), float(t.max())],
        'slope_intercept_covariance': covariance.tolist(),
        'conditional_noise_temp_std_k': te_std,
        'fit_covariance_scope': ('known independent density standard errors; exact source temperatures'
                                 if density_std is not None else
                                 'residual scatter only; exact source temperatures; not a total uncertainty'),
        'condition_number_scaled': float(singular[0] / singular[-1])}
    if not np.isfinite(covariance).all() or not math.isfinite(te_std):
        raise ValueError('fit uncertainty outside float64 range')
    return result


def separate_lna(receiver, cascade):
    """Friis separation; caller must establish identical downstream state/paths."""
    mr = finite(receiver['slope_density_per_k'], 'receiver slope', positive=True)
    mc = finite(cascade['slope_density_per_k'], 'cascade slope', positive=True)
    tr = finite(receiver['receiver_noise_temp_k'], 'receiver temperature')
    tc = finite(cascade['receiver_noise_temp_k'], 'cascade temperature')
    gain = mc / mr
    if not math.isfinite(gain) or gain <= 0:
        raise ValueError('LNA gain outside float64 range')
    te = tc - tr / gain
    if not math.isfinite(te):
        raise ValueError('LNA noise temperature outside float64 range')
    factor = 1 + te / REFERENCE_K
    return {'gain_linear': gain, 'gain_db': 10 * math.log10(gain),
            'noise_temp_k': te, 'noise_figure_db': 10 * math.log10(factor) if factor > 0 else None,
            'physical_noise_temperature': te >= 0,
            'interpretation': 'scalar matched external DUT; identical downstream chain assumed'}


def validate_cw(levels_dbm, net_raw_power, max_residual_db=0.1, slope_tolerance=0.01):
    """Validate the entire declared CW range; never select/drop convenient points."""
    levels = _vector(levels_dbm, 'CW input levels', positive=False)
    raw = _vector(net_raw_power, 'CW net powers')
    if raw.shape != levels.shape or len(np.unique(levels)) < 3 or float(np.ptp(levels)) < 1:
        raise ValueError('CW check needs three distinct levels spanning at least 1 dB')
    limit = finite(max_residual_db, 'CW residual limit', positive=True)
    tolerance = finite(slope_tolerance, 'CW slope tolerance', positive=True)
    output = 10 * np.log10(raw)
    x = levels - float(np.mean(levels))
    beta, _, rank, _ = np.linalg.lstsq(np.column_stack((x, np.ones(len(x)))), output, rcond=None)
    residual = output - (beta[0] * x + beta[1])
    if rank != 2 or not np.isfinite(beta).all():
        raise ValueError('invalid CW regression')
    deviation = float(np.max(abs(residual)))
    return {'slope_db_per_db': float(beta[0]), 'max_abs_residual_db': deviation,
            'residual_db': residual.tolist(), 'input_range_dbm': [float(levels.min()), float(levels.max())],
            'mean_input_power_offset_db': float(np.mean(levels - output)),
            'accepted': abs(float(beta[0]) - 1) <= tolerance and deviation <= limit,
            'scope': 'measured CW range only; does not establish P1dB or broadband overload margin'}


def propagate_joint_uncertainty(receiver_t, receiver_d, cascade_t, cascade_d, model):
    """Gaussian Monte Carlo with explicitly supplied joint standard uncertainties.

    Order: receiver temperatures, densities, cascade temperatures, densities.
    Source common-mode and between-configuration correlations are preserved.
    Invalid/negative physical draws invalidate intervals instead of being clipped
    or silently conditioned away. These are model intervals, not coverage proof.
    """
    if not isinstance(model, dict) or set(model) != {'standard_uncertainties', 'correlation', 'draws', 'seed'}:
        raise ValueError('joint uncertainty needs standard_uncertainties, correlation, draws and seed')
    center = np.concatenate([receiver_t, receiver_d, cascade_t, cascade_d]).astype(float)
    if len(center) > 512:
        raise ValueError('joint model supports at most 512 input quantities per cell')
    std = _vector(model['standard_uncertainties'], 'joint standard uncertainties', positive=False)
    correlation = numeric_array(model['correlation'], 'joint correlation')
    n = len(center)
    if std.shape != center.shape or np.any(std < 0):
        raise ValueError('joint standard uncertainties must match inputs and be nonnegative')
    if (correlation.shape != (n, n) or not np.isfinite(correlation).all()
            or np.any(abs(correlation) > 1) or not np.allclose(correlation, correlation.T, atol=1e-12, rtol=0)
            or not np.allclose(np.diag(correlation), 1, atol=1e-12, rtol=0)):
        raise ValueError('joint correlation must be symmetric with unit diagonal')
    eigenvalues, eigenvectors = np.linalg.eigh(correlation)
    if eigenvalues.min() < -1e-12:
        raise ValueError('joint correlation must be positive semidefinite')
    factor = eigenvectors * np.sqrt(np.maximum(eigenvalues, 0))
    draws = integer(model['draws'], 'Monte Carlo draws', 128)
    if draws > 20000:
        raise ValueError('Monte Carlo draws must not exceed 20000')
    seed = integer(model['seed'], 'Monte Carlo seed')
    rng = np.random.default_rng(seed)
    nr, nc = len(receiver_t), len(cascade_t)
    values, invalid = [], 0
    for _ in range(draws):
        trial = center + std * (factor @ rng.standard_normal(n))
        try:
            r = fit_noise_temperature(trial[:nr], trial[nr:2*nr])
            c = fit_noise_temperature(trial[2*nr:2*nr+nc], trial[2*nr+nc:])
            result = separate_lna(r, c)
            if not r['physical_noise_temperature'] or not c['physical_noise_temperature'] or not result['physical_noise_temperature']:
                raise ValueError('negative noise-temperature realization')
            values.append([result['gain_db'], result['noise_temp_k'], result['noise_figure_db']])
        except (ValueError, OverflowError, np.linalg.LinAlgError):
            invalid += 1
    summary = {'scope': 'supplied joint Gaussian temperature/density model only; omitted systematics remain unknown',
               'seed': seed, 'draws': draws, 'invalid_draws': invalid,
               'status': 'invalid_realizations' if invalid else 'conditional_model_intervals',
               'interval_probability': 0.95, 'statistics': None}
    if not invalid:
        data = np.asarray(values)
        summary['statistics'] = {name: {'standard_uncertainty': float(np.std(data[:, i], ddof=1)),
                                       'interval_95': np.quantile(data[:, i], [0.025, 0.975]).tolist()}
                                 for i, name in enumerate(('gain_db', 'noise_temp_k', 'noise_figure_db'))}
    return summary


class ReceiverCharacterization:
    """Build an exact-cell report from complete uncalibrated spectrum captures.

    This is intentionally not a PowerCalibration and offers no interpolation or
    live power-correction API. No implied evidence for unmeasured frequencies.
    """
    @classmethod
    def from_manifest(cls, path):
        manifest = load_json(path)
        if not isinstance(manifest, dict) or type(manifest.get('version')) is not int or manifest['version'] != 1:
            raise ValueError('expected characterization manifest version 1')
        allowed = {'version', 'interpretation', 'reference', 'reference_plane', 'downstream_chain',
                   'source_calibration', 'matched_scalar_model_confirmed', 'unchanged_downstream_confirmed',
                   'cells', 'min_segments', 'max_noise_residual_db', 'max_cw_noise_gain_delta_db',
                   'min_density_span_db', 'max_cw_residual_db', 'cw_slope_tolerance'}
        if set(manifest) - allowed:
            raise ValueError('unknown characterization manifest fields')
        interpretation = manifest.get('interpretation')
        if interpretation not in ('external-lna', 'configuration-comparison'):
            raise ValueError('choose external-lna or configuration-comparison interpretation')
        for field in ('reference', 'reference_plane', 'downstream_chain', 'source_calibration'):
            if not isinstance(manifest.get(field), str) or not manifest[field].strip():
                raise ValueError('manifest must identify ' + field)
        if manifest.get('matched_scalar_model_confirmed') is not True:
            raise ValueError('characterization requires explicit matched scalar model confirmation')
        if interpretation == 'external-lna' and manifest.get('unchanged_downstream_confirmed') is not True:
            raise ValueError('LNA extraction requires unchanged downstream confirmation')
        cells = manifest.get('cells')
        if not isinstance(cells, list) or not cells:
            raise ValueError('manifest needs measured characterization cells')
        root = Path(path).resolve().parent
        minimum = integer(manifest.get('min_segments', 32), 'minimum segments', 1)
        residual_limit = finite(manifest.get('max_noise_residual_db', 0.1), 'noise residual limit', positive=True)
        gain_limit = finite(manifest.get('max_cw_noise_gain_delta_db', 0.2), 'CW/noise gain limit', positive=True)
        contrast_limit = finite(manifest.get('min_density_span_db', 0.5), 'minimum measured density span', positive=True)
        output, contexts, coordinates, hashes = [], {}, set(), {}
        for cell in cells:
            if not isinstance(cell, dict):
                raise ValueError('characterization cells must be objects')
            rf, bb = finite(cell.get('rf_hz'), 'RF Hz'), finite(cell.get('baseband_hz'), 'baseband Hz')
            if (rf, bb) in coordinates:
                raise ValueError('duplicate characterization coordinate')
            allowed_cell = {'rf_hz', 'baseband_hz', 'start_hz', 'stop_hz', 'receiver', 'cascade',
                            'receiver_cw', 'cascade_cw', 'joint_uncertainty'}
            if set(cell) - allowed_cell:
                raise ValueError('unknown characterization cell fields')
            if interpretation != 'external-lna' and 'joint_uncertainty' in cell:
                raise ValueError('joint LNA uncertainty requires external-lna interpretation')
            coordinates.add((rf, bb))
            start, stop = finite(cell.get('start_hz'), 'band start'), finite(cell.get('stop_hz'), 'band stop')
            if not start <= rf < stop:
                raise ValueError('integration band must contain RF coordinate')
            fits, observations, all_dsp, all_geometry, cw = {}, {}, None, None, {}
            for config in ('receiver', 'cascade'):
                levels = cell.get(config)
                if not isinstance(levels, list) or len(levels) < 3:
                    raise ValueError('each configuration needs at least three noise levels')
                summaries, temps = [], []
                def capture_summary(name):
                    nonlocal all_dsp, all_geometry
                    if not isinstance(name, str) or not name:
                        raise ValueError('missing capture path')
                    capture = average_capture(root / name, minimum, require_complete=True)
                    _check_coordinates(capture, rf, bb)
                    context = capture['metadata']['context']
                    if config in contexts:
                        _compare(contexts[config], context, config + ' context')
                    else:
                        contexts[config] = copy.deepcopy(context)
                    if interpretation == 'external-lna' and len(contexts) == 2:
                        _compare(contexts['receiver'], contexts['cascade'], 'unchanged downstream')
                    dsp = {key: capture['metadata'][key] for key in DSP_FIELDS}
                    geometry = {key: capture.get(key, []) for key in ('start_hz', 'stop_hz', 'step_hz', 'masked_bins', 'estimated_bins')}
                    if all_dsp is not None:
                        _compare(all_dsp, dsp, 'characterization DSP')
                        _compare(all_geometry, geometry, 'characterization grid')
                    all_dsp, all_geometry = dsp, geometry
                    summary = band_summary(capture, start, stop)
                    hashes[name] = capture['capture_sha256']
                    return summary
                for level in levels:
                    if not isinstance(level, dict):
                        raise ValueError('noise levels must be objects')
                    if set(level) != {'temperature_k', 'capture'}:
                        raise ValueError('noise levels require only temperature_k and capture')
                    temps.append(finite(level.get('temperature_k'), 'reference-plane source temperature', positive=True))
                    summaries.append(capture_summary(level.get('capture')))
                densities = [s['mean_density'] for s in summaries]
                fits[config] = fit_noise_temperature(temps, densities)
                observations[config] = {'temperatures_k': temps, 'digital_density': densities, 'captures': summaries}
                tone_levels = cell.get(config + '_cw')
                if tone_levels is not None:
                    if not isinstance(tone_levels, list):
                        raise ValueError('CW evidence must be a list')
                    pins, powers, evidence = [], [], []
                    for level in tone_levels:
                        if not isinstance(level, dict):
                            raise ValueError('CW levels must be objects')
                        if set(level) != {'input_dbm', 'on', 'off'}:
                            raise ValueError('CW levels require only input_dbm, on and off')
                        on, off = capture_summary(level.get('on')), capture_summary(level.get('off'))
                        net = on['power'] - off['power']
                        if net <= 0 or (off['power'] > 0 and 10*(math.log10(net)-math.log10(off['power'])) < 10):
                            raise ValueError('CW net power must exceed baseline by at least 10 dB')
                        pins.append(finite(level.get('input_dbm'), 'known reference-plane CW dBm'))
                        powers.append(net)
                        evidence.append({'on': on, 'off': off})
                    cw[config] = validate_cw(pins, powers,
                        manifest.get('max_cw_residual_db', 0.1), manifest.get('cw_slope_tolerance', 0.01))
                    cw[config]['captures'] = evidence
            failures = []
            for config, fit in fits.items():
                if not fit['physical_noise_temperature']:
                    failures.append(config + '_negative_noise_temperature')
                if fit['measured_density_span_db'] < contrast_limit:
                    failures.append(config + '_insufficient_noise_contrast')
                if fit['max_abs_residual_db'] > residual_limit:
                    failures.append(config + '_noise_nonlinearity')
                if config not in cw:
                    failures.append(config + '_missing_cw_linearity')
                elif not cw[config]['accepted']:
                    failures.append(config + '_cw_nonlinearity')
            result = {'rf_hz': rf, 'baseband_hz': bb, 'band_hz': [start, stop],
                      'fits': fits, 'observations': observations, 'cw': cw, 'lna': None,
                      'uncertainty': {'status': 'not_evaluated'}, 'failure_reasons': failures}
            if interpretation == 'external-lna':
                lna = separate_lna(fits['receiver'], fits['cascade'])
                result['lna'] = lna
                if not lna['physical_noise_temperature']:
                    failures.append('negative_extracted_lna_temperature')
                if len(cw) == 2:
                    gain_cw = cw['receiver']['mean_input_power_offset_db'] - cw['cascade']['mean_input_power_offset_db']
                    result['cw_gain_db'] = gain_cw
                    result['cw_noise_gain_delta_db'] = gain_cw - lna['gain_db']
                    if abs(result['cw_noise_gain_delta_db']) > gain_limit:
                        failures.append('cw_noise_gain_disagreement')
                if 'joint_uncertainty' in cell:
                    r, c = observations['receiver'], observations['cascade']
                    result['uncertainty'] = propagate_joint_uncertainty(r['temperatures_k'], r['digital_density'],
                        c['temperatures_k'], c['digital_density'], cell['joint_uncertainty'])
                    if result['uncertainty']['status'] != 'conditional_model_intervals':
                        failures.append('uncertainty_invalid_realizations')
            result['metrology_status'] = ('conditional_joint_model_only' if result['uncertainty']['status'] == 'conditional_model_intervals' else 'incomplete_uncertainty')
            result['validation_status'] = 'failed_checks' if failures else 'passed_measured_checks'
            output.append(result)
        report = {'kind': 'receiver_noise_gain_characterization', 'version': 1,
                  'interpretation': interpretation, 't0_k': REFERENCE_K,
                  'reference': manifest['reference'], 'reference_plane': manifest['reference_plane'],
                  'downstream_chain': manifest['downstream_chain'], 'source_calibration': manifest['source_calibration'],
                  'contexts': contexts, 'cells': output, 'capture_sha256': hashes,
                  'manifest_sha256': _sha256(path), 'absolute_power_profile': False,
                  'physical_certification': False,
                  'limitations': ['Matched scalar source/path model; source impedance changes and S parameters are not deembedded.',
                                  'Source temperatures must already include path loss and passive thermal emission at the stated reference plane.',
                                  'Each cell is a band-averaged estimate, not an assertion of constant per-bin response.',
                                  'Finite-level CW/noise residual checks do not prove absence of all compression or ADC clipping.',
                                  'Conditional fit covariance and supplied Monte Carlo model do not establish a complete metrology uncertainty budget.',
                                  'No interpolation, extrapolation, automatic point deletion, or noise subtraction from signal power.']}
        report['report_sha256'] = hashlib.sha256(json.dumps(report, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()).hexdigest()
        instance = cls()
        instance.report = report
        return instance
