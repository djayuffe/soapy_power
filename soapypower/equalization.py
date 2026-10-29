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
from soapypower.numerics import finite, integer, numeric_array
from soapypower.records import read_records, validate_record


def relative_inverse(correction_db, reference_correction_db, regularization=0.001, max_boost_db=12,
                     mode='regularized', attenuation_target_db=None):
    """Design positive amplitude weights via W=(1+lambda)*H/(H**2+lambda).

    H**2=10**((Cref-C)/10) is the relative measured power response. Lambda is
    dimensionless relative power, not dB. Lambda=0 is the capped direct inverse.
    Computation stays logarithmic to avoid overflow in deep notches.
    """
    offsets = numeric_array(correction_db, 'correction offsets')
    if offsets.ndim != 1 or not len(offsets) or not np.isfinite(offsets).all():
        raise ValueError('correction offsets must be a nonempty finite vector')
    reference = finite(reference_correction_db, 'reference correction dB')
    damping = finite(regularization, 'dimensionless regularization', nonnegative=True)
    cap = finite(max_boost_db, 'maximum boost dB', nonnegative=True)
    if mode not in ('regularized', 'attenuation-only'):
        raise ValueError('unknown equalization mode')
    if mode == 'attenuation-only' and damping != 0:
        raise ValueError('attenuation-only mode requires zero regularization')
    if mode == 'regularized' and attenuation_target_db is not None:
        raise ValueError('attenuation target is only valid in attenuation-only mode')
    target = (float(offsets.max()) if attenuation_target_db is None else
              finite(attenuation_target_db, 'attenuation target correction dB'))
    if mode == 'attenuation-only' and target < float(offsets.max()):
        raise ValueError('attenuation target would require boosting a measured bin')
    with np.errstate(over='ignore', invalid='ignore'):
        response_db = reference - offsets
        log_h = response_db * (math.log(10) / 20)
        log_w = (-log_h if damping == 0 else
                 math.log1p(damping) + log_h - np.logaddexp(2 * log_h, math.log(damping)))
        requested_db = offsets - target if mode == 'attenuation-only' else log_w * (20 / math.log(10))
    if not np.isfinite(response_db).all() or not np.isfinite(requested_db).all():
        raise ValueError('equalizer response exceeds float64 range')
    applied_db = np.minimum(requested_db, 0 if mode == 'attenuation-only' else cap)
    target_level = reference - target if mode == 'attenuation-only' else 0.0
    return {'relative_chain_power_gain_db': response_db.tolist(),
            'amplitude_weight_db': applied_db.tolist(),
            'power_weight_db': applied_db.tolist(),
            'uncapped_weight_db': requested_db.tolist(),
            'boost_limited_bins': np.flatnonzero(requested_db > cap).tolist(),
            'predicted_residual_power_response_db': (response_db + applied_db - target_level).tolist(),
            'mode': mode, 'target_relative_power_level_db': target_level,
            'attenuation_target_correction_db': target if mode == 'attenuation-only' else None,
            'regularization_relative_power': damping, 'max_boost_db': 0 if mode == 'attenuation-only' else cap,
            'model': ('attenuation-only inverse to weakest selected response; no phase correction' if mode == 'attenuation-only' else
                      'anchor-normalized positive-amplitude Tikhonov inverse; no phase correction'),
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


def _prepare_record(record, calibration, reference_rf_hz, reference_baseband_hz, usable_policy=None):
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
    anchor = float(calibration.offsets_db([reference_rf], [reference_bb])[0])
    policy = {} if usable_policy is None else copy.deepcopy(usable_policy)
    if not isinstance(policy, dict) or set(policy) - {'edge_guard_hz', 'dc_exclusion_hz', 'dc_offset_hz',
                                                   'max_correction_db', 'max_residual_db'}:
        raise ValueError('invalid usable-band policy fields')
    edge = finite(policy.get('edge_guard_hz', 0), 'edge guard Hz', nonnegative=True)
    dc = finite(policy.get('dc_exclusion_hz', 0), 'DC exclusion width Hz', nonnegative=True)
    dc_offset = finite(policy.get('dc_offset_hz', 0), 'DC offset Hz')
    rate = context['sample_rate']
    if edge >= rate / 2 or dc >= rate or abs(dc_offset) > rate / 2 or (dc_offset and not dc):
        raise ValueError('invalid usable-band guard geometry')
    for key in ('max_correction_db', 'max_residual_db'):
        if key in policy:
            policy[key] = finite(policy[key], key, nonnegative=True)
    reasons = [[] for _ in rf]
    def exclude(mask, reason):
        for i in np.flatnonzero(mask):
            reasons[int(i)].append(reason)
    old = np.zeros(len(rf), dtype=bool);old[record['masked_bins']] = True
    exclude(old, 'input_mask')
    domain = calibration.domain
    outside = ((rf < domain['rf_hz'][0]) | (rf > domain['rf_hz'][1]) |
               (raw_bb < domain['raw_baseband_hz'][0]) | (raw_bb > domain['raw_baseband_hz'][1]))
    if usable_policy is None and np.any(outside & ~old):
        raise ValueError('spectrum lies outside calibration range; select a usable-band policy to exclude it')
    exclude(outside, 'outside_measured_calibration')
    if edge:
        # Exclude bin support that touches the selected edge guard.
        exclude(abs(raw_bb) + record['step_hz'] / 2 > rate / 2 - edge, 'passband_edge')
    if dc:
        distance = abs((raw_bb - dc_offset + rate / 2) % rate - rate / 2)
        exclude(distance <= dc / 2 + record['step_hz'] / 2, 'dc_or_spur_guard')
    candidate = np.array([not value for value in reasons])
    offsets = np.full(len(rf), np.nan)
    if candidate.any():
        offsets[candidate] = calibration.offsets_db(rf[candidate], raw_bb[candidate])
    if 'max_correction_db' in policy:
        exclude(candidate & (abs(offsets - anchor) > policy['max_correction_db']), 'excessive_correction')
    selected = np.array([not value for value in reasons])
    if np.count_nonzero(selected) < 2:
        raise ValueError('usable-band policy leaves fewer than two measured bins')
    return rf, raw_bb, offsets, anchor, selected, reasons, copy.deepcopy(policy)


def equalize_record(record, calibration, reference_rf_hz, reference_baseband_hz,
                    regularization=0.001, max_boost_db=12, mode='regularized',
                    usable_policy=None, attenuation_target_db=None):
    """Transform a copy; excluded support is explicitly masked, never fabricated."""
    rf, raw_bb, offsets, anchor, selected, reasons, policy = _prepare_record(
        record, calibration, reference_rf_hz, reference_baseband_hz, usable_policy)
    indices = np.flatnonzero(selected)
    design = relative_inverse(offsets[selected], anchor, regularization, max_boost_db, mode, attenuation_target_db)
    if 'max_residual_db' in policy:
        rejected = abs(np.asarray(design['predicted_residual_power_response_db'])) > policy['max_residual_db'] + 1e-12
        for i in indices[rejected]:
            reasons[int(i)].append('excessive_residual_bias')
        selected = np.array([not value for value in reasons])
        if np.count_nonzero(selected) < 2:
            raise ValueError('residual acceptance leaves fewer than two measured bins')
        # Freeze the target chosen before residual rejection, avoiding a moving
        # acceptance criterion in attenuation-only mode.
        target = design['attenuation_target_correction_db']
        indices = np.flatnonzero(selected)
        design = relative_inverse(offsets[selected], anchor, regularization, max_boost_db, mode, target)
    vector_keys = ('relative_chain_power_gain_db', 'amplitude_weight_db', 'power_weight_db',
                   'uncapped_weight_db', 'predicted_residual_power_response_db')
    for key in vector_keys:
        expanded = [None] * len(rf)
        for i, value in zip(indices, design[key]):
            expanded[int(i)] = value
        design[key] = expanded
    design['boost_limited_bins'] = [int(indices[i]) for i in design['boost_limited_bins']]
    result = copy.deepcopy(record)
    zeros = set(record['zero_power_bins'])
    logarithmic = record['unit'] == 'dB(input power/Hz)'
    for i, (value, weight) in enumerate(zip(record['power'], design['power_weight_db'])):
        if not selected[i]:
            result['power'][i] = None
            continue
        if i in zeros:
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
    excluded = np.flatnonzero(~selected).tolist()
    result['masked_bins'] = excluded
    result['zero_power_bins'] = sorted(zeros - set(excluded))
    result['metadata']['masked_bins'] = excluded
    runs = np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1)
    bands = [{'start_hz': float(rf[run[0]] - record['step_hz']/2),
              'stop_hz': float(rf[run[-1]] + record['step_hz']/2), 'bin_count': len(run)} for run in runs]
    design.update(profile=calibration.metadata(), reference_rf_hz=float(reference_rf_hz),
                  reference_raw_baseband_hz=float(reference_baseband_hz), reference_correction_db=anchor,
                  raw_baseband_hz=raw_bb.tolist(), usable_policy=policy,
                  exclusion_reasons=reasons, usable_bands=bands,
                  normalization_scope='record' if mode == 'attenuation-only' and attenuation_target_db is None else 'fixed_reference',
                  interpretation='relative display/analysis PSD; units retained; not input-referred dBm/Hz',
                  uncertainty_status='no transformed uncertainty budget evaluated')
    result['metadata']['equalization'] = design
    return validate_record(result)


def equalize_file(records, profile, output, reference_rf_hz, reference_baseband_hz,
                  regularization=0.001, max_boost_db=12, overwrite=False,
                  mode='regularized', usable_policy=None, evidence_snapshot=None):
    """Atomic bounded-memory JSONL transform; attenuation target spans all hops."""
    destination = Path(output).resolve()
    if destination in (Path(records).resolve(), Path(profile).resolve()):
        raise ValueError('equalization output must differ from source records and profile')
    from soapypower.files import EvidenceSnapshot
    snapshot = evidence_snapshot or EvidenceSnapshot([records,profile])
    snapshot.protect_output(output)
    from soapypower.passive_response import load_response_profile
    calibration = load_response_profile(profile)
    source_hash = _sha256(records)
    target = None
    if mode == 'attenuation-only':
        # Two bounded-memory passes establish one common weakest-response target,
        # preserving relative levels across hops instead of normalizing each hop.
        for record in read_records(records):
            _, _, offsets, _, selected, _, _ = _prepare_record(record, calibration, reference_rf_hz,
                                                              reference_baseband_hz, usable_policy)
            value = float(offsets[selected].max())
            target = value if target is None else max(target, value)
    def transformed():
        for record in read_records(records):
            result = equalize_record(record, calibration, reference_rf_hz, reference_baseband_hz,
                                     regularization, max_boost_db, mode, usable_policy, target)
            result['metadata']['equalization']['source_records_sha256'] = source_hash
            if mode == 'attenuation-only':
                result['metadata']['equalization']['normalization_scope'] = 'whole_file'
            yield result
        snapshot.verify()
        if _sha256(records) != source_hash:
            raise ValueError('source records changed during equalization')
    write_json_lines(output, transformed(), overwrite=overwrite, before_publish=snapshot.verify)
