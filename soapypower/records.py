"""Validate JSON spectrum records and integrate measured bins in linear power."""
import math
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
    if not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] != 1:
        raise ValueError('expected spectrum JSON version 1')
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


def integrate_record(record, start_hz=None, stop_hz=None, omit_masked=False):
    """Sum selected bin densities times df, never summing dB values.

    Band bounds select bin centers in [start, stop). Reported boundaries use the
    record's half-open grid convention. Missing data is an error unless omission
    is explicit, and omissions are reported as incomplete measured bandwidth.
    """
    record = validate_record(record)
    lower = record['start_hz'] if start_hz is None else finite(start_hz, 'integration start')
    upper = record['stop_hz'] if stop_hz is None else finite(stop_hz, 'integration stop')
    if lower >= upper:
        raise ValueError('integration band must be ascending')
    if lower < record['start_hz'] or upper > record['stop_hz']:
        raise ValueError('integration band exceeds the measured frequency grid')
    step = record['step_hz']
    selected = [index for index in range(len(record['power']))
                if lower <= record['start_hz'] + index * step < upper]
    if not selected:
        raise ValueError('integration band contains no bin centers')
    masked = set(record['masked_bins']) & set(selected)
    if masked and not omit_masked:
        raise ValueError('integration band contains excluded bins; explicitly select a valid band or --omit-masked')
    valid = [index for index in selected if index not in masked]
    if not valid:
        raise ValueError('integration band contains no measured bins')
    logarithmic, unit, db_unit = UNITS[record['unit']]
    zeros = set(record['zero_power_bins'])
    logs = []
    for index in valid:
        if index in zeros:
            continue
        value = record['power'][index]
        logs.append(value * (math.log(10) / 10) if logarithmic else math.log(value))
    if logs:
        maximum = max(logs)
        log_power = maximum + math.log(math.fsum(math.exp(value - maximum) for value in logs)) + math.log(step)
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
    return {'power': linear, 'unit': unit, 'power_db': power_db, 'db_unit': db_unit,
            'zero_power': not bool(logs), 'linear_representation': representability,
            'complete': not bool(masked), 'selected_bins': len(selected), 'measured_bins': len(valid),
            'excluded_bins': len(masked), 'measured_bandwidth_hz': len(valid) * step,
            'excluded_bandwidth_hz': len(masked) * step,
            'start_hz': record['start_hz'] + selected[0] * step,
            'stop_hz': record['start_hz'] + (selected[-1] + 1) * step}
