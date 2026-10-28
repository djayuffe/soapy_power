"""Opt-in relative PSD shaping from measured gain, never noise-floor inversion.

A bounded, anchor-normalized Tikhonov inverse changes signal and noise together.
Magnitude measurements do not determine phase, causal FIR taps, or higher SNR.
"""
import copy
import math
from pathlib import Path

import numpy as np

from soapypower.calibration import PowerCalibration, require_fixed_gain
from soapypower.calibration_workflows import _sha256
from soapypower.files import write_json_lines
from soapypower.numerics import finite, integer
from soapypower.records import read_records, validate_record


def relative_inverse(correction_db, reference_correction_db, regularization=0.001, max_boost_db=12):
    """Design positive amplitude weights via W=(1+lambda)*H/(H**2+lambda).

    H**2=10**((Cref-C)/10) is the relative measured power response. Lambda is
    dimensionless relative power, not dB. Lambda=0 is the capped direct inverse.
    Computation stays logarithmic to avoid overflow in deep notches.
    """
    offsets = np.asarray(correction_db, dtype=float)
    if offsets.ndim != 1 or not len(offsets) or not np.isfinite(offsets).all():
        raise ValueError('correction offsets must be a nonempty finite vector')
    reference = finite(reference_correction_db, 'reference correction dB')
    damping = finite(regularization, 'dimensionless regularization', nonnegative=True)
    cap = finite(max_boost_db, 'maximum boost dB', nonnegative=True)
    with np.errstate(over='ignore', invalid='ignore'):
        response_db = reference - offsets
        log_h = response_db * (math.log(10) / 20)
        log_w = (-log_h if damping == 0 else
                 math.log1p(damping) + log_h - np.logaddexp(2 * log_h, math.log(damping)))
        requested_db = log_w * (20 / math.log(10))
    if not np.isfinite(response_db).all() or not np.isfinite(requested_db).all():
        raise ValueError('equalizer response exceeds float64 range')
    applied_db = np.minimum(requested_db, cap)
    return {'relative_chain_power_gain_db': response_db.tolist(),
            'amplitude_weight_db': applied_db.tolist(),
            'power_weight_db': applied_db.tolist(),
            'uncapped_weight_db': requested_db.tolist(),
            'boost_limited_bins': np.flatnonzero(requested_db > cap).tolist(),
            'predicted_residual_power_response_db': (response_db + applied_db).tolist(),
            'regularization_relative_power': damping, 'max_boost_db': cap,
            'model': 'anchor-normalized positive-amplitude Tikhonov inverse; no phase correction',
            'absolute_power_calibration': False, 'noise_subtracted': False,
            'phase_measured': False, 'snr_improvement_claimed': False}


def _raw_baseband(rf, metadata, context):
    center = finite(metadata.get('tuned_center_hz'), 'actual tuned center')
    rate = finite(context.get('sample_rate'), 'sample rate', positive=True)
    lo = finite(context.get('lnb_lo'), 'external LO')
    sign = -1 if context['lnb_high_side'] else 1
    if metadata.get('frequency_sign') != sign or metadata.get('iq_conjugate') is not context['iq_conjugate']:
        raise ValueError('record spectral orientation disagrees with context')
    if metadata.get('sample_rate_hz') != context['sample_rate']:
        raise ValueError('record sample rate disagrees with context')
    grid = metadata.get('frequency_grid')
    if grid is not None:
        if not isinstance(grid, dict):
            raise ValueError('invalid exact frequency grid')
        bins = integer(metadata.get('fft_bins'), 'FFT bins', 2)
        first = integer(grid.get('first_bin_index'), 'first bin index', -bins)
        count = integer(grid.get('bin_count'), 'grid bin count', 1)
        step = finite(grid.get('step_hz'), 'grid spacing', positive=True)
        origin = finite(grid.get('rf_origin_hz'), 'RF grid origin')
        tolerance = max(abs(float(np.spacing(max(abs(origin), 1)))) * 8, step * 1e-8)
        if (count != len(rf) or not math.isclose(step, rate / bins, rel_tol=1e-10, abs_tol=1e-12)
                or not math.isclose(origin, math.fsum((lo, sign * center)), rel_tol=0, abs_tol=tolerance)):
            raise ValueError('exact frequency grid disagrees with acquisition')
        indices = first + np.arange(count)
        if not np.allclose(rf, origin + indices * step, rtol=0, atol=tolerance):
            raise ValueError('record frequencies disagree with exact grid')
        raw = sign * indices * step
    else:
        raw = sign * (rf - lo) - center
    # RF output coordinates are always within the delivered complex Nyquist band.
    # Only conjugation wraps the negative Nyquist endpoint to its original bin.
    tolerance = rate * 1e-10
    if np.any(raw < -rate / 2 - tolerance) or np.any(raw > rate / 2 + tolerance):
        raise ValueError('record grid lies outside actual tuned Nyquist support')
    if context['iq_conjugate']:
        raw = (-raw + rate / 2) % rate - rate / 2
    return raw


def equalize_record(record, calibration, reference_rf_hz, reference_baseband_hz,
                    regularization=0.001, max_boost_db=12):
    """Return a transformed copy with raw units and full per-bin shaping metadata."""
    record = validate_record(record)
    metadata = record.get('metadata')
    if not isinstance(metadata, dict) or not isinstance(metadata.get('context'), dict):
        raise ValueError('equalization needs complete acquisition context')
    if record['unit'] not in ('input power/Hz', 'dB(input power/Hz)') or metadata.get('calibration') is not None:
        raise ValueError('relative equalization requires raw uncalibrated density; no double correction')
    if metadata.get('equalization') is not None:
        raise ValueError('record is already equalized')
    if metadata.get('measurement_status') != 'complete':
        raise ValueError('equalization requires an explicitly complete acquisition')
    if record.get('estimated_bins', metadata.get('estimated_bins', [])) or metadata.get('dc_interpolated'):
        raise ValueError('equalization does not accept estimated/interpolated bins')
    context = metadata['context']
    calibration.validate_context(context)
    require_fixed_gain(context)
    calibration.validate_processing(metadata.get('detrend'), False)
    rf = record['start_hz'] + np.arange(len(record['power'])) * record['step_hz']
    raw_bb = _raw_baseband(rf, metadata, context)
    reference_rf = finite(reference_rf_hz, 'reference RF Hz')
    reference_bb = finite(reference_baseband_hz, 'reference raw baseband Hz')
    offset = calibration.offsets_db(rf, raw_bb)
    anchor = float(calibration.offsets_db([reference_rf], [reference_bb])[0])
    design = relative_inverse(offset, anchor, regularization, max_boost_db)
    result = copy.deepcopy(record)
    masked, zeros = set(record['masked_bins']), set(record['zero_power_bins'])
    logarithmic = record['unit'] == 'dB(input power/Hz)'
    for i, (value, weight) in enumerate(zip(record['power'], design['power_weight_db'])):
        if i in masked or i in zeros:
            continue
        log_power = value + weight if logarithmic else 10 * math.log10(value) + weight
        if not math.isfinite(log_power):
            raise ValueError('equalized power exceeds float64 range')
        if logarithmic:
            result['power'][i] = log_power
        else:
            try:
                transformed = 10**(log_power / 10)
            except OverflowError:
                raise ValueError('equalized linear power overflows; use raw logarithmic input') from None
            if not math.isfinite(transformed) or transformed == 0:
                raise ValueError('equalized linear power outside float64 range; use logarithmic input')
            result['power'][i] = transformed
    design.update(profile=calibration.metadata(), reference_rf_hz=reference_rf,
                  reference_raw_baseband_hz=reference_bb, reference_correction_db=anchor,
                  raw_baseband_hz=raw_bb.tolist(),
                  interpretation='relative display/analysis PSD; units retained; not input-referred dBm/Hz',
                  uncertainty_status='no transformed uncertainty budget evaluated')
    result['metadata']['equalization'] = design
    return validate_record(result)


def equalize_file(records, profile, output, reference_rf_hz, reference_baseband_hz,
                  regularization=0.001, max_boost_db=12, overwrite=False):
    """Atomically transform JSONL without replacing the input or calibration file."""
    destination = Path(output).resolve()
    if destination in (Path(records).resolve(), Path(profile).resolve()):
        raise ValueError('equalization output must differ from source records and profile')
    calibration = PowerCalibration.load(profile)
    source_hash = _sha256(records)
    def transformed():
        for record in read_records(records):
            result = equalize_record(record, calibration, reference_rf_hz, reference_baseband_hz,
                                     regularization, max_boost_db)
            result['metadata']['equalization']['source_records_sha256'] = source_hash
            yield result
        if _sha256(records) != source_hash:
            raise ValueError('source records changed during equalization')
    write_json_lines(output, transformed(), overwrite=overwrite)
