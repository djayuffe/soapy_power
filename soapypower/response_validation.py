"""Independent source-on/off holdout validation of measured power responses."""
import hashlib
import json
import math
from pathlib import Path

from soapypower.calibration import PowerCalibration
from soapypower.calibration_workflows import _sha256, _check_coordinates, average_capture, band_summary
from soapypower.files import load_json
from soapypower.numerics import finite, integer


def response_sha256(calibration):
    profile = calibration.profile
    response = {key: profile.get(key) for key in ('points', 'baseband_hz', 'baseband_correction_db',
                 'reference_baseband_hz', 'context', 'processing')}
    return hashlib.sha256(json.dumps(response, sort_keys=True, allow_nan=False,
                                     separators=(',', ':')).encode()).hexdigest()


def validate_holdout(calibration, manifest_path):
    """Evaluate every supplied independent tone point; never train on holdouts."""
    manifest = load_json(manifest_path)
    allowed = {'version', 'reference', 'independent_measurements_confirmed', 'min_segments',
               'min_snr_db', 'max_abs_error_db', 'max_ripple_db', 'cells'}
    if (not isinstance(manifest, dict) or set(manifest) - allowed or
            type(manifest.get('version')) is not int or manifest['version'] != 1):
        raise ValueError('invalid holdout manifest version or fields')
    if not isinstance(manifest.get('reference'), str) or not manifest['reference'].strip():
        raise ValueError('holdout measurements must identify independent source/procedure')
    if manifest.get('independent_measurements_confirmed') is not True:
        raise ValueError('holdout independence must be explicitly confirmed')
    minimum = integer(manifest.get('min_segments', 32), 'minimum holdout segments', 1)
    min_snr = finite(manifest.get('min_snr_db', 10), 'minimum holdout SNR', nonnegative=True)
    tolerance = finite(manifest.get('max_abs_error_db', 0.2), 'holdout error tolerance', nonnegative=True)
    ripple_limit = finite(manifest.get('max_ripple_db', 0.2), 'holdout ripple tolerance', nonnegative=True)
    cells = manifest.get('cells')
    if not isinstance(cells, list) or not cells:
        raise ValueError('holdout validation requires measured cells')
    details = calibration.profile.get('method_details') or {}
    if not isinstance(details, dict):
        raise ValueError('invalid profile method details')
    training = details.get('capture_sha256', {})
    if not isinstance(training, dict):
        raise ValueError('invalid training capture provenance')
    # A smoother retains the original capture provenance as well.
    if any(not isinstance(value, str) for value in training.values()):
        raise ValueError('invalid training capture hash values')
    training_hashes = set(training.values())
    root = Path(manifest_path).resolve().parent
    results, hashes = [], {}
    for cell in cells:
        if not isinstance(cell, dict) or set(cell) != {'rf_hz', 'baseband_hz', 'start_hz', 'stop_hz', 'input_dbm', 'on', 'off'}:
            raise ValueError('invalid holdout cell fields')
        rf, bb = finite(cell['rf_hz'], 'holdout RF'), finite(cell['baseband_hz'], 'holdout baseband')
        start, stop = finite(cell['start_hz'], 'holdout band start'), finite(cell['stop_hz'], 'holdout band stop')
        if not start <= rf < stop:
            raise ValueError('holdout band must contain tone RF')
        summaries, pair_dsp = {}, None
        for key in ('on', 'off'):
            name = cell[key]
            if not isinstance(name, str) or not name:
                raise ValueError('holdout requires on/off capture paths')
            capture = average_capture(root / name, minimum, require_complete=True)
            _check_coordinates(capture, rf, bb)
            calibration.validate_context(capture['metadata']['context'])
            calibration.validate_processing(capture['metadata']['detrend'], capture['metadata']['dc_interpolated'])
            summary = band_summary(capture, start, stop)
            if pair_dsp is not None:
                from soapypower.calibration import _compare
                _compare(pair_dsp, summary['processing'], 'holdout paired DSP')
            pair_dsp = summary['processing']
            if capture['capture_sha256'] in training_hashes:
                raise ValueError('holdout capture was already used in profile training')
            summaries[key] = summary
            hashes[name] = capture['capture_sha256']
        net = summaries['on']['power'] - summaries['off']['power']
        off = summaries['off']['power']
        if net <= 0 or (off > 0 and 10 * (math.log10(net) - math.log10(off)) < min_snr):
            raise ValueError('holdout tone has insufficient net SNR')
        offset = float(calibration.offsets_db([rf], [bb])[0])
        predicted = 10 * math.log10(net) + offset
        known = finite(cell['input_dbm'], 'known reference-plane input dBm')
        error = predicted - known
        results.append({'rf_hz': rf, 'baseband_hz': bb, 'known_input_dbm': known,
                        'predicted_input_dbm': predicted, 'error_db': error,
                        'passed': abs(error) <= tolerance, 'captures': summaries})
    errors = [cell['error_db'] for cell in results]
    ripple = max(errors) - min(errors)
    return {'kind': 'independent_response_validation', 'version': 1,
            'profile_sha256': calibration.profile_id, 'response_sha256': response_sha256(calibration),
            'reference': manifest['reference'], 'manifest_sha256': _sha256(manifest_path),
            'capture_sha256': hashes, 'cells': results,
            'max_abs_error_db': max(abs(x) for x in errors),
            'mean_error_db': math.fsum(x / len(errors) for x in errors),
            'ripple_peak_to_peak_db': ripple,
            'acceptance': {'max_abs_error_db': tolerance, 'max_ripple_db': ripple_limit},
            'passed': all(cell['passed'] for cell in results) and ripple <= ripple_limit,
            'independence_status': 'capture_hashes_disjoint_from_training' if training_hashes else 'operator_declared_no_training_capture_hashes',
            'uncertainty_status': 'empirical holdout errors; no complete uncertainty budget inferred',
            'scope': 'only supplied tones, levels and integration bands; not a universal flatness or phase certificate'}
