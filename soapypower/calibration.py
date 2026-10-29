"""Explicit reference-plane power calibration, independent of receiver gain.

Offsets are measured as known input dBm minus 10*log10(integrated raw IQ power).
Interpolation is in dB against RF frequency, never against FFT-bin index.
"""
import copy
import hashlib
import json
import math

import numpy

from soapypower.numerics import finite, integer, numeric_array
from soapypower.files import load_json
from soapypower.uncertainty import evaluate_budget

CONTEXT_FIELDS = frozenset((
    'hardware', 'driver', 'serial', 'channel', 'antenna', 'sample_rate', 'bandwidth',
    'gains', 'agc', 'dc_correction', 'iq_correction', 'device_settings', 'tune_args',
    'lnb_lo', 'lnb_high_side', 'iq_conjugate', 'corr_ppm', 'sample_format',
    'clock_source', 'time_source', 'master_clock_rate', 'hardware_info'))


def _compare(expected, actual, path='context'):
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(expected) != set(actual):
            raise ValueError('calibration {} fields do not match'.format(path))
        for key, value in expected.items():
            _compare(value, actual[key], path + '.' + key)
    elif isinstance(expected, (list, tuple)):
        if type(expected) is not type(actual) or len(expected) != len(actual):
            raise ValueError('calibration mismatch: {}'.format(path))
        for index, (left, right) in enumerate(zip(expected, actual)):
            _compare(left, right, path + '[{}]'.format(index))
    elif isinstance(expected, bool) or isinstance(actual, bool):
        if type(expected) is not type(actual) or expected != actual:
            raise ValueError('calibration mismatch: {}'.format(path))
    elif isinstance(expected, (float, int)) and isinstance(actual, (float, int)):
        if not math.isclose(expected, actual, rel_tol=1e-10, abs_tol=1e-9):
            raise ValueError('calibration mismatch: {}'.format(path))
    elif expected != actual or type(expected) is not type(actual):
        raise ValueError('calibration mismatch: {}'.format(path))


def require_fixed_gain(context):
    if context.get('agc') is not False:
        raise ValueError('absolute calibration/reference comparison requires fixed gain (AGC off)')
    settings = context.get('device_settings')
    if not isinstance(settings, dict):
        raise ValueError('calibration device_settings must contain actual readback')
    for name in ('digital_agc', 'if_agc', 'testmode'):
        if str(settings.get(name, 'false')).strip().lower() not in ('false', '0', 'off'):
            raise ValueError('calibration requires ' + name + ' disabled with known readback')


def require_reference_context(context):
    """Known fixed-state frontend readbacks required for measured references."""
    if not isinstance(context, dict) or set(context) != CONTEXT_FIELDS:
        raise ValueError('reference requires complete acquisition context')
    require_fixed_gain(context)
    for field in ('hardware','driver','antenna','sample_rate','bandwidth','gains','channel','corr_ppm'):
        if context[field] is None:
            raise ValueError('reference requires known ' + field)
    finite(context['sample_rate'],'reference sample rate',positive=True)
    finite(context['bandwidth'],'reference bandwidth',nonnegative=True)
    finite(context['corr_ppm'],'reference frequency correction')
    integer(context['channel'],'reference RX channel')
    gains=context['gains']
    if context['sample_format'] != 'CF32' or not isinstance(gains,dict) or not gains:
        raise ValueError('reference requires CF32 and actual gain readbacks')
    for name,value in gains.items():
        if not isinstance(name,str) or not name:
            raise ValueError('gain readback names must be nonempty strings')
        finite(value,'actual gain ' + name)


class PowerCalibration:
    def __init__(self, profile):
        if not isinstance(profile, dict):
            raise ValueError('calibration profile must be a JSON object')
        required = {'version', 'units', 'reference', 'reference_plane', 'context', 'points',
                    'baseband_hz', 'baseband_correction_db', 'reference_baseband_hz'}
        if set(profile) - (required | {'uncertainty_db', 'measured_at', 'measurements_sha256', 'method', 'method_details', 'processing', 'uncertainty'}) or not required <= set(profile):
            raise ValueError('invalid calibration profile fields')
        if type(profile['version']) is not int or profile['version'] not in (1, 2) or profile['units'] != 'dBm':
            raise ValueError('expected calibration version 1 or 2 with dBm reference units')
        if profile['version'] == 2:
            self._uncertainty = evaluate_budget(profile.get('uncertainty'))
        elif 'uncertainty' in profile:
            raise ValueError('structured uncertainty requires profile version 2')
        else:
            self._uncertainty = {'status': 'not_evaluated', 'combined_standard_db': None,
                                 'measurement_statistics_included': False}
        for field in ('reference', 'reference_plane'):
            if not isinstance(profile[field], str) or not profile[field].strip():
                raise ValueError('calibration {} must identify the measured reference'.format(field))
        context = profile['context']
        if not isinstance(context, dict) or set(context) != CONTEXT_FIELDS:
            raise ValueError('calibration context must contain all acquisition context fields')
        for field in ('hardware', 'driver', 'antenna', 'sample_rate', 'bandwidth', 'gains', 'channel', 'corr_ppm'):
            if context[field] is None:
                raise ValueError('calibration requires a known {}'.format(field))
        rate = finite(context['sample_rate'], 'calibration sample rate', positive=True)
        finite(context['bandwidth'], 'calibration bandwidth', nonnegative=True)
        integer(context['channel'], 'calibration channel')
        if context['sample_format'] != 'CF32' or not isinstance(context['gains'], dict) or not context['gains']:
            raise ValueError('calibration requires CF32 input and actual gain readback')
        for value in context['gains'].values():
            finite(value, 'calibration gain')
        require_reference_context(context)
        if 'measured_at' in profile and (not isinstance(profile['measured_at'], str) or not profile['measured_at'].strip()):
            raise ValueError('measured_at must be a nonempty date/time string')
        if 'measurements_sha256' in profile:
            digest = profile['measurements_sha256']
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('measurements_sha256 must be a lowercase SHA-256 hex digest')
        if 'uncertainty_db' in profile:
            finite(profile['uncertainty_db'], 'calibration uncertainty', nonnegative=True)
        try:
            points = numpy.asarray(profile['points'], dtype=numpy.float64)
        except (TypeError, ValueError):
            raise ValueError('calibration points must be numeric [RF Hz, offset dB] pairs') from None
        if (points.ndim != 2 or points.shape[1] != 2 or points.shape[0] < 2 or
                not numpy.isfinite(points).all() or not numpy.all(numpy.diff(points[:, 0]) > 0)):
            raise ValueError('calibration points must be finite with strictly increasing RF frequencies')
        baseband = numeric_array(profile['baseband_hz'], 'baseband axis')
        corrections = numeric_array(profile['baseband_correction_db'], 'baseband corrections')
        reference_bb = finite(profile['reference_baseband_hz'], 'reference baseband frequency')
        if (baseband.ndim != 1 or baseband.size < 2 or not numpy.isfinite(baseband).all() or
                not numpy.all(numpy.diff(baseband) > 0) or
                corrections.shape != (points.shape[0], baseband.size) or
                not numpy.isfinite(corrections).all()):
            raise ValueError('baseband calibration must be a finite RF-by-baseband correction grid')
        if baseband[0] < -rate / 2 or baseband[-1] > rate / 2:
            raise ValueError('baseband calibration axes must lie within the sampled Nyquist interval')
        if not baseband[0] <= reference_bb <= baseband[-1]:
            raise ValueError('reference baseband lies outside calibration grid')
        for row in corrections:
            if not math.isclose(numpy.interp(reference_bb, baseband, row), 0, abs_tol=1e-9):
                raise ValueError('baseband correction must be zero at the reference offset for every RF point')
        self._baseband, self._bb_corrections, self._reference_bb = baseband.copy(), corrections.copy(), reference_bb
        processing = profile.get('processing', {'detrend': None})
        if not isinstance(processing, dict) or set(processing) != {'detrend'} or processing['detrend'] not in (None, 'constant'):
            raise ValueError('invalid calibration processing contract')
        # Validate every nested value as strict JSON, including the supplied context.
        encoded = json.dumps(profile, sort_keys=True, allow_nan=False, separators=(',', ':'))
        self._profile_id = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        self._profile = json.loads(encoded)
        self._context = self._profile['context']
        self._freqs, self._offsets = points[:, 0].copy(), points[:, 1].copy()

    @property
    def profile_id(self):
        return self._profile_id

    @property
    def profile(self):
        """A detached snapshot; edits cannot invalidate the profile identity."""
        return copy.deepcopy(self._profile)

    @property
    def context(self):
        return copy.deepcopy(self._context)

    @property
    def domain(self):
        return {'rf_hz': (float(self._freqs[0]), float(self._freqs[-1])),
                'raw_baseband_hz': (float(self._baseband[0]), float(self._baseband[-1]))}

    @classmethod
    def load(cls, path):
        return cls(load_json(path))

    def validate_context(self, context, before_tune=False):
        if before_tune:
            # Per-LO driver calibration can change these modes at tuning. Check
            # static settings now and the complete context immediately afterward.
            ignored = {'dc_correction', 'iq_correction'}
            _compare({k: v for k, v in self._context.items() if k not in ignored},
                     {k: v for k, v in context.items() if k not in ignored})
        else:
            _compare(self._context, context)
        if context['agc'] is not False:
            raise ValueError('AGC invalidates fixed power calibration')

    def validate_processing(self, detrend, remove_dc, allow_estimated_dc=False):
        expected = self._profile.get('processing', {'detrend': None})
        if not isinstance(expected, dict) or set(expected) != {'detrend'} or expected['detrend'] not in (None, 'constant'):
            raise ValueError('invalid calibration processing contract')
        if expected['detrend'] != detrend:
            raise ValueError('calibration detrend transformation does not match')
        if remove_dc and not allow_estimated_dc:
            raise ValueError('calibrated DC interpolation requires explicit allow_estimated_dc')

    def offsets_db(self, freqs, baseband_freqs=None):
        freqs = numpy.asarray(freqs, dtype=numpy.float64)
        if (not numpy.isfinite(freqs).all() or numpy.any(freqs < self._freqs[0]) or
                numpy.any(freqs > self._freqs[-1])):
            raise ValueError('RF spectrum lies outside the calibration frequency range; extrapolation disabled')
        # Missing coordinates explicitly evaluate the reference offset. PSD
        # callers always pass actual raw-IQ baseband coordinates for every bin.
        baseband = (numpy.full_like(freqs, self._reference_bb) if baseband_freqs is None else
                    numpy.asarray(baseband_freqs, dtype=numpy.float64))
        if (baseband.shape != freqs.shape or not numpy.isfinite(baseband).all() or
                numpy.any(baseband < self._baseband[0]) or numpy.any(baseband > self._baseband[-1])):
            raise ValueError('raw IQ baseband lies outside calibration grid; extrapolation disabled')
        rf = numpy.clip(numpy.searchsorted(self._freqs, freqs, side='right') - 1, 0, len(self._freqs) - 2)
        bb = numpy.clip(numpy.searchsorted(self._baseband, baseband, side='right') - 1, 0, len(self._baseband) - 2)
        a = (freqs - self._freqs[rf]) / (self._freqs[rf + 1] - self._freqs[rf])
        b = (baseband - self._baseband[bb]) / (self._baseband[bb + 1] - self._baseband[bb])
        grid = self._bb_corrections
        correction = ((1 - a) * ((1 - b) * grid[rf, bb] + b * grid[rf, bb + 1]) +
                      a * ((1 - b) * grid[rf + 1, bb] + b * grid[rf + 1, bb + 1]))
        return numpy.interp(freqs, self._freqs, self._offsets) + correction

    def apply(self, freqs, power, log_scale, baseband_freqs=None):
        offsets = self.offsets_db(freqs, baseband_freqs)
        power = numpy.asarray(power, dtype=numpy.float64)
        if power.shape != numpy.shape(freqs) or not numpy.isfinite(power).all() or numpy.any(power < 0):
            raise ValueError('calibration requires matching finite, nonnegative linear power')
        with numpy.errstate(divide='ignore'):
            log_power = 10 * numpy.log10(power) + offsets
        if log_scale:
            result = log_power
        else:
            with numpy.errstate(over='ignore', under='ignore'):
                result = numpy.power(10.0, log_power / 10)
            if numpy.any((power > 0) & (result == 0)):
                raise ValueError('calibrated linear power underflows float64; use logarithmic output')
        if numpy.any(numpy.isnan(result) | numpy.isposinf(result)):
            raise ValueError('calibrated power exceeds representable range')
        return result

    def metadata(self):
        result = {'uncertainty_evaluation': copy.deepcopy(self._uncertainty), 'profile_sha256': self.profile_id,
                  'reference': self._profile['reference'],
                  'reference_plane': self._profile['reference_plane'],
                  'model': 'rf_and_raw_baseband_grid',
                  'reference_baseband_hz': self._reference_bb}
        for field in ('uncertainty_db', 'measured_at', 'measurements_sha256', 'method', 'method_details'):
            if field in self._profile:
                result[field] = self.profile[field]
        return result
