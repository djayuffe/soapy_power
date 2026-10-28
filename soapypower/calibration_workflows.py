"""Measured calibration workflows, from termination baseline to reference grids."""
import copy
import csv
import hashlib
import math
from pathlib import Path
import tempfile

import numpy as np

from soapypower.calibration import PowerCalibration, CONTEXT_FIELDS, _compare, require_fixed_gain
from soapypower.files import load_json
from soapypower.numerics import finite, integer
from soapypower.records import read_records, integrate_record

def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


BOLTZMANN = 1.380649e-23  # exact SI, J/K
REFERENCE_K = 290.0
DSP_FIELDS = ('fft_bins', 'sample_rate_hz', 'window', 'overlap_samples', 'detrend',
              'dc_interpolated', 'dc_mask_width_hz', 'dc_offset_hz', 'precision',
              'iq_conjugate', 'frequency_sign')


def thermal_reference(temperature_k, source_ohms, receiver_ohms):
    temperature = finite(temperature_k, 'source temperature K', positive=True)
    source = finite(source_ohms, 'source ohms', positive=True)
    receiver = finite(receiver_ohms, 'receiver ohms', positive=True)
    ratio = min(source, receiver) / max(source, receiver)
    eta = 4 * ratio / (1 + ratio)**2
    return {'temperature_k': temperature, 'source_ohms': source, 'receiver_ohms': receiver,
            'ideal_resistive_transfer': eta, 'ideal_mismatch_loss_db': -10 * math.log10(eta),
            'available_thermal_density_dbm_hz': 10 * math.log10(BOLTZMANN * temperature * 1000),
            'ideal_delivered_thermal_density_dbm_hz': 10 * math.log10(BOLTZMANN * temperature * eta * 1000),
            'model': 'pure resistances; no measured S parameters or source-dependent receiver noise',
            'absolute_receiver_calibration': False}


def effective_temperature(temperature, loss_db=0, path_temperature_k=290):
    temperature = finite(temperature, 'source temperature K', positive=True)
    loss_db = finite(loss_db, 'passive path loss dB', nonnegative=True)
    path_temperature = finite(path_temperature_k, 'path temperature K', positive=True)
    transmission = 10**(-loss_db / 10)
    return temperature * transmission + path_temperature * (1 - transmission)


def noise_solution(cold_density, hot_density, cold_k, hot_k, min_y_db=0.5):
    cold = finite(cold_density, 'cold density', positive=True)
    hot = finite(hot_density, 'hot density', positive=True)
    tc, th = (finite(x, 'effective source temperature', positive=True) for x in (cold_k, hot_k))
    threshold = finite(min_y_db, 'minimum Y dB', positive=True)
    if th <= tc or hot <= cold:
        raise ValueError('hot temperature and measured density must exceed cold')
    delta = hot - cold
    y_db = 10 * math.log1p(delta / cold) / math.log(10)
    if y_db < threshold:
        raise ValueError('Y factor is below the explicit acceptance threshold')
    te = (th - tc) * (cold / delta) - tc
    if not math.isfinite(te) or te < 0:
        raise ValueError('negative/nonfinite equivalent receiver temperature: check linearity and source data')
    return {'offset_db': 10 * math.log10(1000 * BOLTZMANN * (th - tc)) - 10 * math.log10(delta),
            'receiver_temperature_k': te, 'noise_figure_db': 10 * math.log1p(te / REFERENCE_K) / math.log(10),
            'y_db': y_db, 'reference_temperature_k': REFERENCE_K}


def average_capture(path, min_segments=1, require_complete=False):
    """Segment-weighted linear mean; only a single consistent tuned grid per file."""
    count, mean, first, context, dsp = 0, None, None, None, None
    minimum = integer(min_segments, 'minimum segments', 1)
    for record in read_records(path):
        if record['unit'] not in ('input power/Hz', 'dB(input power/Hz)'):
            raise ValueError('workflow requires uncalibrated IQ density')
        metadata = record.get('metadata')
        candidate = metadata.get('context') if isinstance(metadata, dict) else None
        if not isinstance(candidate, dict) or set(candidate) != CONTEXT_FIELDS or candidate['agc'] is not False:
            raise ValueError('workflow requires complete fixed-gain receiver context')
        require_fixed_gain(candidate)
        if metadata.get('equalization') is not None:
            raise ValueError('reference capture is equalized; use original raw measurements')
        if metadata.get('calibration') is not None:
            raise ValueError('capture is already calibrated')
        if require_complete and metadata.get('measurement_status') != 'complete':
            raise ValueError('reference capture must explicitly report complete acquisition')
        if metadata.get('measurement_status', 'complete') != 'complete':
            raise ValueError('partial reference capture is not accepted')
        current_dsp = {key: metadata.get(key) for key in DSP_FIELDS}
        if any(key not in metadata for key in DSP_FIELDS):
            raise ValueError('capture lacks the required DSP metadata')
        segments = integer(metadata.get('segment_count'), 'segment count', 1)
        if first is None:
            first, context, dsp = copy.deepcopy(record), candidate, current_dsp
        else:
            _compare(context, candidate)
            _compare(dsp, current_dsp, 'DSP')
            for key in ('start_hz', 'stop_hz', 'step_hz', 'masked_bins', 'estimated_bins'):
                if record.get(key, []) != first.get(key, []):
                    raise ValueError('capture contains different tuned grids or bin states')
            if len(record['power']) != len(first['power']):
                raise ValueError('capture bin count changed')
        values = np.array([np.nan if x is None else x for x in record['power']], dtype=float)
        if record['unit'].startswith('dB('):
            for index in record['zero_power_bins']:
                values[index] = -np.inf
            with np.errstate(over='ignore', under='ignore'):
                values = 10**(values / 10)
            if np.isinf(values).any() or any(values[i] == 0 for i in range(values.size) if i not in record['zero_power_bins'] and i not in record['masked_bins']):
                raise ValueError('raw logarithmic density is outside linear range; capture with -l')
        if mean is None:
            mean = values
        else:
            mean += (values - mean) * (segments / (count + segments))
        count += segments
    if count < minimum:
        raise ValueError('capture has fewer complete Welch segments than required')
    first['power'] = [None if np.isnan(x) else float(x) for x in mean]
    first['unit'] = 'input power/Hz'
    first['zero_power_bins'] = np.flatnonzero(mean == 0).tolist()
    first['metadata']['segment_count'] = count
    first['capture_sha256'] = _sha256(path)
    return first


def band_summary(record, start_hz=None, stop_hz=None):
    result = integrate_record(record, start_hz, stop_hz, method='fractional')
    if result['linear_representation'] not in ('finite', 'zero'):
        raise ValueError('reference power is outside linear range')
    result['mean_density'] = result['power'] / result['measured_bandwidth_hz']
    result['segment_count'] = record['metadata']['segment_count']
    result['capture_sha256'] = record['capture_sha256']
    result['processing'] = {key: record['metadata'][key] for key in DSP_FIELDS}
    result['settle_policy'] = record['metadata'].get('settle_policy')
    return result


def compare_captures(reference, observed, start_hz=None, stop_hz=None):
    left, right = average_capture(reference), average_capture(observed)
    _compare(left['metadata']['context'], right['metadata']['context'])
    _compare({k: left['metadata'].get(k) for k in DSP_FIELDS}, {k: right['metadata'].get(k) for k in DSP_FIELDS}, 'DSP')
    for field in ('start_hz', 'stop_hz', 'step_hz', 'masked_bins', 'estimated_bins'):
        if left.get(field, []) != right.get(field, []):
            raise ValueError('comparison requires identical tuning and bin states')
    a, b = band_summary(left, start_hz, stop_hz), band_summary(right, start_hz, stop_hz)
    if a['power'] <= 0:
        raise ValueError('reference must have positive measured power')
    delta = b['power'] - a['power']
    return {'reference': a, 'observed': b, 'ratio_db': 10 * (math.log10(b['power']) - math.log10(a['power'])) if b['power'] > 0 else None,
            'excess_raw_power': delta, 'positive_excess': delta > 0,
            'absolute_calibration': False, 'interpretation': 'difference includes antenna, mismatch, environment and receiver noise changes'}


def _check_coordinates(record, rf, baseband):
    metadata, context = record['metadata'], record['metadata']['context']
    bins = integer(metadata['fft_bins'], 'FFT bins', 2)
    rate = finite(context['sample_rate'], 'sample rate', positive=True)
    if metadata.get('detrend') is not None or metadata.get('dc_interpolated'):
        raise ValueError('reference workflows require no detrending or DC interpolation')
    center = metadata.get('tuned_center_hz')
    if center is None:
        raise ValueError('reference capture needs tuned_center_hz metadata; recapture with this version')
    sign = -1 if context['lnb_high_side'] else 1
    actual = sign * (rf - context['lnb_lo']) - center
    if context['iq_conjugate']:
        actual = (-actual + rate / 2) % rate - rate / 2
    if not math.isclose(actual, baseband, rel_tol=0, abs_tol=max(rate / bins * 1e-6, 1e-6)):
        raise ValueError('manifest RF/baseband coordinates do not match actual tuned capture')
    if not -rate / 2 <= baseband <= rate / 2:
        raise ValueError('baseband coordinate is outside the Nyquist interval')


def build_manifest(path):
    """Build a complete measured RF-by-baseband grid; all paths are manifest-relative."""
    from soapypower.calibration_cli import build_profile
    manifest = load_json(path)
    if not isinstance(manifest, dict) or manifest.get('version') != 1 or type(manifest['version']) is not int:
        raise ValueError('expected workflow manifest version 1')
    method = manifest.get('method')
    if method not in ('tone', 'hot-cold', 'known-nf'):
        raise ValueError('method must be tone, hot-cold or known-nf')
    if method != 'tone' and manifest.get('assumptions_confirmed') is not True:
        raise ValueError('thermal methods require assumptions_confirmed=true after checking source and receiver model')
    cells = manifest.get('cells')
    if not isinstance(cells, list) or not cells:
        raise ValueError('manifest needs measured cells')
    root, context, rows, results, hashes = Path(path).resolve().parent, None, [], [], {}
    minimum = integer(manifest.get('min_segments', 32), 'minimum segments', 1)
    for cell in cells:
        if not isinstance(cell, dict):
            raise ValueError('each cell must be an object')
        rf, bb = finite(cell.get('rf_hz'), 'RF Hz'), finite(cell.get('baseband_hz'), 'baseband Hz')
        start, stop = cell.get('start_hz'), cell.get('stop_hz')
        if start is None or stop is None or not finite(start, 'band start') <= rf < finite(stop, 'band stop'):
            raise ValueError('each cell needs explicit integration bounds containing rf_hz')
        summaries = {}
        cell_dsp = None
        keys = ('on', 'off') if method == 'tone' else ('cold', 'hot') if method == 'hot-cold' else ('cold',)
        for key in keys:
            name = cell.get(key)
            if not isinstance(name, str) or not name:
                raise ValueError('cell needs a capture path for ' + key)
            capture = average_capture(root / name, minimum, require_complete=True)
            _check_coordinates(capture, rf, bb)
            current_dsp = {key: capture['metadata'][key] for key in DSP_FIELDS}
            if cell_dsp is not None:
                _compare(cell_dsp, current_dsp, 'paired DSP')
            cell_dsp = current_dsp
            current = capture['metadata']['context']
            if context is None:
                context = current
            else:
                _compare(context, current)
            summaries[key] = band_summary(capture, start, stop)
            hashes[name] = capture['capture_sha256']
        if method == 'tone':
            on, off = summaries['on']['power'], summaries['off']['power']
            power = on - off
            threshold = finite(manifest.get('min_snr_db', 10), 'minimum tone SNR dB', nonnegative=True)
            if power <= 0 or (off > 0 and 10 * math.log10(power / off) < threshold):
                raise ValueError('net tone power is nonpositive or below required SNR')
            input_dbm = finite(cell.get('input_dbm'), 'known source input dBm') - finite(cell.get('path_loss_db', 0), 'tone path loss', nonnegative=True)
            result = {'net_raw_power': power, 'snr_db': 10 * math.log10(power / off) if off else None}
        else:
            model = thermal_reference(cell.get('cold_temperature_k'), cell.get('source_ohms'), cell.get('receiver_ohms'))
            eta = model['ideal_resistive_transfer']
            if eta != 1 and manifest.get('accept_ideal_mismatch') is not True:
                raise ValueError('mismatched thermal source requires accept_ideal_mismatch=true; this is only a conditional resistive model')
            loss, path_k = cell.get('path_loss_db', 0), cell.get('path_temperature_k', 290)
            tc = effective_temperature(cell['cold_temperature_k'], loss, path_k) * eta
            cold = summaries['cold']['mean_density']
            if method == 'hot-cold':
                hot_k = cell.get('hot_temperature_k')
                if hot_k is not None and cell.get('enr_db') is not None:
                    raise ValueError('choose hot_temperature_k or ENR, not both')
                if hot_k is None:
                    enr = finite(cell.get('enr_db'), 'source excess noise ratio dB')
                    hot_k = cell['cold_temperature_k'] + REFERENCE_K * 10**(enr / 10)
                th = effective_temperature(hot_k, loss, path_k) * eta
                result = noise_solution(cold, summaries['hot']['mean_density'], tc, th, manifest.get('min_y_db', 0.5))
                power, input_dbm = 1.0, result['offset_db']
            else:
                nf = finite(cell.get('noise_figure_db'), 'independently known noise figure', nonnegative=True)
                te = REFERENCE_K * math.expm1(nf * math.log(10) / 10)
                if cold <= 0:
                    raise ValueError('termination capture must have positive density')
                power, input_dbm = cold, 10 * math.log10(1000 * BOLTZMANN * (tc + te))
                result = {'assumed_noise_figure_db': nf, 'assumed_receiver_temperature_k': te}
            result['source_model'] = model
            result['conditional_on_noise_and_impedance_model'] = True
        rows.append([rf, bb, input_dbm, power])
        results.append(dict(result, rf_hz=rf, baseband_hz=bb, captures=summaries))
    with tempfile.TemporaryDirectory() as directory:
        csv_path = Path(directory) / 'derived.csv'
        with csv_path.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['rf_hz', 'baseband_hz', 'input_dbm', 'raw_power'])
            writer.writerows(rows)
        calibration = build_profile(context, csv_path, manifest.get('reference'), manifest.get('reference_plane'),
                                    manifest.get('reference_baseband_hz'), manifest.get('uncertainty_db'),
                                    manifest.get('measured_at'), manifest.get('max_spread_db'))
    profile = calibration.profile
    if 'uncertainty' in manifest:
        profile['version'] = 2
        profile['uncertainty'] = manifest['uncertainty']
    profile['method'] = method
    profile['method_details'] = {'manifest_sha256': _sha256(path),
                                 'capture_sha256': hashes, 'cells': results,
                                 'assumptions_confirmed': manifest.get('assumptions_confirmed', False),
                                 'uncertainty_status': 'not_automatically_evaluated'}
    return PowerCalibration(profile)
