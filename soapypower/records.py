"""Validate JSON spectrum records and integrate measured bins in linear power."""
import math
from fractions import Fraction
from pathlib import Path

import numpy

from soapypower.files import loads_json
from soapypower.numerics import finite, integer

UNITS = {'input power/Hz': (False, 'input power', 'dB(input power)'),
         'dB(input power/Hz)': (True, 'input power', 'dB(input power)'),
         'mW/Hz': (False, 'mW', 'dBm'), 'dBm/Hz': (True, 'mW', 'dBm')}


def _indices(values, size, name):
    if not isinstance(values, list):
        raise ValueError('{} must be an index list'.format(name))
    checked = [integer(value, name) for value in values]
    if checked != sorted(set(checked)) or any(value >= size for value in checked):
        raise ValueError('{} must be sorted unique bin indices'.format(name))
    return set(checked)


def validate_record(record):
    if not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] not in (1, 2):
        raise ValueError('expected spectrum JSON version 1 or 2')
    required = {'start_hz', 'stop_hz', 'step_hz', 'samples', 'unit', 'power', 'masked_bins', 'zero_power_bins'}
    if not required <= set(record) or not isinstance(record['unit'], str) or record['unit'] not in UNITS:
        raise ValueError('missing spectrum fields or unsupported density units')
    power = record['power']
    if not isinstance(power, list) or len(power) < 2:
        raise ValueError('spectrum needs at least two power bins')
    for field in ('start_hz', 'stop_hz', 'step_hz'):
        if type(record[field]) not in (int, float):
            raise ValueError('{} must be a JSON number'.format(field))
    start = finite(record['start_hz'], 'start_hz')
    stop = finite(record['stop_hz'], 'stop_hz')
    step = finite(record['step_hz'], 'step_hz', positive=True)
    integer(record['samples'], 'samples', 1)
    expected_stop = start + len(power) * step
    tolerance = max(abs(numpy.spacing(max(abs(start), abs(stop)))) * 8, step * 1e-8)
    if stop <= start or not math.isfinite(expected_stop) or not math.isclose(stop, expected_stop, rel_tol=0, abs_tol=tolerance):
        raise ValueError('spectrum frequency geometry does not match its power bins')
    masked = _indices(record['masked_bins'], len(power), 'masked_bins')
    zeros = _indices(record['zero_power_bins'], len(power), 'zero_power_bins')
    if masked & zeros:
        raise ValueError('a bin cannot be both excluded and measured zero')
    estimated = _indices(record.get('estimated_bins', record.get('metadata', {}).get('estimated_bins', []) if isinstance(record.get('metadata'), dict) else []), len(power), 'estimated_bins')
    if estimated & masked:
        raise ValueError('estimated and masked bins cannot overlap')
    if record['version'] == 2:
        for key, expected in (('first_bin_center_hz', start), ('bin_width_hz', step),
                              ('coverage_start_hz', start - step / 2),
                              ('coverage_stop_hz', start + (len(power) - 0.5) * step)):
            if type(record.get(key)) not in (int, float) or not math.isclose(record[key], expected, rel_tol=0, abs_tol=tolerance):
                raise ValueError('invalid explicit bin support: ' + key)
    logarithmic = UNITS[record['unit']][0]
    for index, value in enumerate(power):
        if value is None:
            if index not in masked and not (logarithmic and index in zeros):
                raise ValueError('unclassified null power bin')
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('power bins must be finite JSON numbers or classified nulls')
            if index in masked or (logarithmic and index in zeros):
                raise ValueError('missing/zero-log bins must use null')
            if not logarithmic and (value < 0 or ((value == 0) != (index in zeros))):
                raise ValueError('linear powers and zero-power markers disagree')
    return record


def read_records(path):
    with Path(path).open(encoding='utf-8') as stream:
        found = False
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = validate_record(loads_json(line))
            except (ValueError, TypeError) as error:
                raise ValueError('{} line {}: {}'.format(path, line_number, error)) from None
            found = True
            yield record
        if not found:
            raise ValueError('spectrum file contains no records')


def integrate_record(record, start_hz=None, stop_hz=None, omit_masked=False,
                     method=None, include_estimated=False):
    """Integrate density over bin support (v2), or explicit legacy centers (v1).

    Fractional integration is a piecewise-constant bin-support approximation,
    not reconstruction of a sub-bin signal or inversion of the window kernel.
    Boundary offsets use exact rational representations of the supplied floats.
    """
    record = validate_record(record)
    method = method or ('fractional' if record['version'] == 2 else 'legacy-centers')
    if method not in ('fractional', 'legacy-centers'):
        raise ValueError('integration method must be fractional or legacy-centers')
    first, step, size = record['start_hz'], record['step_hz'], len(record['power'])
    edge_lo, edge_hi = first - step / 2, first + (size - 0.5) * step
    default_lo, default_hi = (edge_lo, edge_hi) if method == 'fractional' else (first, record['stop_hz'])
    lower = default_lo if start_hz is None else finite(start_hz, 'integration start')
    upper = default_hi if stop_hz is None else finite(stop_hz, 'integration stop')
    if lower >= upper or lower < default_lo or upper > default_hi:
        raise ValueError('integration band is empty or exceeds measured support')
    f, df = Fraction(first), Fraction(step)
    selected = []
    if method == 'fractional':
        lo = Fraction(0) if start_hz is None or lower == edge_lo else (Fraction(lower) - f) / df + Fraction(1, 2)
        hi = Fraction(size) if stop_hz is None or upper == edge_hi else (Fraction(upper) - f) / df + Fraction(1, 2)
        begin, end = max(0, math.floor(lo)), min(size, math.ceil(hi))
        for index in range(begin, end):
            width = step if lo <= index and hi >= index + 1 else float(max(Fraction(0), min(hi, index + 1) - max(lo, index)) * df)
            if width > 0:
                selected.append((index, width))
    else:
        lo, hi = (Fraction(lower) - f) / df, (Fraction(upper) - f) / df
        selected = [(index, step) for index in range(max(0, math.ceil(lo)), min(size, math.ceil(hi)))]
    if not selected:
        raise ValueError('integration band contains no bins')
    masked = set(record['masked_bins'])
    metadata = record.get('metadata') or {}
    estimated = set(record.get('estimated_bins', metadata.get('estimated_bins', []) if isinstance(metadata, dict) else []))
    excluded_width = math.fsum(width for index, width in selected if index in masked)
    estimated_width = math.fsum(width for index, width in selected if index in estimated)
    if excluded_width and not omit_masked:
        raise ValueError('integration band contains excluded bins; explicitly choose --omit-masked')
    if estimated_width and not include_estimated:
        raise ValueError('integration contains estimated bins; explicitly choose --include-estimated')
    valid = [(index, width) for index, width in selected if index not in masked]
    if not valid:
        raise ValueError('integration band contains no measured bins')
    logarithmic, unit, db_unit = UNITS[record['unit']]
    zeros, logs = set(record['zero_power_bins']), []
    for index, width in valid:
        if index in zeros:
            continue
        value = record['power'][index]
        logs.append((value * (math.log(10) / 10) if logarithmic else math.log(value)) + math.log(width))
    if logs:
        maximum = max(logs)
        log_power = maximum + math.log(math.fsum(math.exp(value - maximum) for value in logs))
        power_db = log_power * (10 / math.log(10))
        try:
            linear = math.exp(log_power)
        except OverflowError:
            linear = None
        representability = 'overflow' if linear is None else 'underflow' if linear == 0 else 'finite'
        if linear == 0:
            linear = None
    else:
        linear, power_db, representability = 0.0, None, 'zero'
    if not logarithmic and logs:
        # Preserve ordinary linear arithmetic accuracy without log/exp round trips.
        # Use the log-domain result when products/sum overflow or underflow.
        terms = [record['power'][index] * width for index, width in valid]
        if all(math.isfinite(term) and (term > 0 or record['power'][index] == 0)
               for term, (index, width) in zip(terms, valid)):
            try:
                direct = math.fsum(terms)
            except OverflowError:
                direct = None
            if direct is not None and math.isfinite(direct) and direct > 0:
                linear = direct
                power_db = math.log(direct) * (10 / math.log(10))
                representability = 'finite'
    return {'power': linear, 'unit': unit, 'power_db': power_db, 'db_unit': db_unit,
            'zero_power': not bool(logs), 'linear_representation': representability,
            'integration_method': method, 'complete': not bool(excluded_width),
            'contains_estimates': bool(estimated_width), 'estimated_bandwidth_hz': estimated_width,
            'selected_bins': len(selected), 'measured_bins': len(valid),
            'excluded_bins': sum(index in masked for index, width in selected),
            'requested_bandwidth_hz': upper - lower,
            'measured_bandwidth_hz': math.fsum(width for index, width in valid),
            'excluded_bandwidth_hz': excluded_width, 'start_hz': lower, 'stop_hz': upper}
