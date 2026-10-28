"""Offline profile construction, validation and band-power routines (no SDR)."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path

from soapypower.calibration import PowerCalibration, CONTEXT_FIELDS, _compare, require_fixed_gain
from soapypower.files import load_json, write_json
from soapypower.numerics import finite
from soapypower.records import read_records, integrate_record


def build_profile(context, csv_path, reference, reference_plane, reference_baseband_hz,
                  uncertainty_db=None, measured_at=None, max_spread_db=None):
    """Build the measured response grid from normalized, linear power responses.

    CSV powers must already be measured tone mean-square, with the intended
    signal/noise/integration procedure. The routine does not fabricate reference
    values or choose a receiver configuration.
    """
    data = Path(csv_path).read_bytes()
    table = csv.DictReader(io.StringIO(data.decode('utf-8-sig'), newline=''))
    columns = ['rf_hz', 'baseband_hz', 'input_dbm', 'raw_power']
    if table.fieldnames is None or len(table.fieldnames) != 4 or set(table.fieldnames) != set(columns):
        raise ValueError('measurement CSV must have exactly these columns: ' + ','.join(columns))
    if max_spread_db is not None:
        max_spread_db = finite(max_spread_db, 'maximum offset spread', nonnegative=True)
    cells = {}
    for number, row in enumerate(table, 2):
        try:
            if set(row) != set(columns) or any(row[key] is None for key in columns):
                raise ValueError('missing or extra columns')
            rf, baseband, input_dbm = (finite(row[key], key) for key in columns[:3])
            power = finite(row['raw_power'], 'raw_power', positive=True)
            offset = input_dbm - 10 * math.log10(power)
            if not math.isfinite(offset):
                raise ValueError('power offset exceeds representable range')
            cells.setdefault((rf, baseband), []).append(offset)
        except ValueError as error:
            raise ValueError('measurement CSV line {}: {}'.format(number, error)) from None
    rfs = sorted({point[0] for point in cells})
    basebands = sorted({point[1] for point in cells})
    reference_baseband_hz = finite(reference_baseband_hz, 'reference baseband')
    if len(rfs) < 2 or len(basebands) < 2 or len(cells) != len(rfs) * len(basebands):
        raise ValueError('measurements must form a complete grid with at least two RF and two baseband points')
    if reference_baseband_hz not in basebands:
        raise ValueError('reference baseband must be one of the measured offsets')
    response = []
    for rf in rfs:
        row = []
        for bb in basebands:
            offsets = cells[rf, bb]
            if len(offsets) > 1:
                if max_spread_db is None:
                    raise ValueError('repeated measurement cells require an explicit --max-spread-db acceptance limit')
                if max(offsets) - min(offsets) > max_spread_db:
                    raise ValueError('measurement spread exceeds acceptance limit at RF {}, baseband {}'.format(rf, bb))
            # Average raw/input linear power response, then invert to dB. This
            # does not confuse an average dB correction with an average gain.
            logs = [-value * (math.log(10) / 10) for value in offsets]
            maximum = max(logs)
            mean_log = maximum + math.log(math.fsum(math.exp(value - maximum) for value in logs) / len(logs))
            row.append(-mean_log * 10 / math.log(10))
        response.append(row)
    reference_index = basebands.index(reference_baseband_hz)
    points = [[rf, row[reference_index]] for rf, row in zip(rfs, response)]
    residual = [[value - row[reference_index] for value in row] for row in response]
    profile = {'version': 1, 'units': 'dBm', 'context': context, 'reference': reference,
               'reference_plane': reference_plane, 'reference_baseband_hz': reference_baseband_hz,
               'points': points, 'baseband_hz': basebands, 'baseband_correction_db': residual,
               'measurements_sha256': hashlib.sha256(data).hexdigest()}
    if uncertainty_db is not None:
        profile['uncertainty_db'] = uncertainty_db
    if measured_at is not None:
        profile['measured_at'] = measured_at
    return PowerCalibration(profile)


def extract_context(path):
    context = None
    for record in read_records(path):
        metadata = record.get('metadata')
        candidate = metadata.get('context') if isinstance(metadata, dict) else None
        if not isinstance(candidate, dict) or set(candidate) != CONTEXT_FIELDS:
            raise ValueError('record does not contain a complete acquisition context')
        if metadata.get('calibration') is not None or record['unit'] not in ('input power/Hz', 'dB(input power/Hz)'):
            raise ValueError('extract calibration context from uncalibrated records')
        require_fixed_gain(candidate)
        if context is not None:
            _compare(context, candidate)
        context = candidate
    return context


def parser():
    root = argparse.ArgumentParser(prog='soapy_power_calibration', description=__doc__)
    commands = root.add_subparsers(dest='command')
    validate = commands.add_parser('validate', help='validate a measured profile without opening a radio')
    validate.add_argument('profile')
    extract = commands.add_parser('context', help='extract one consistent context from uncalibrated JSON lines')
    extract.add_argument('records')
    extract.add_argument('-O', '--output', required=True)
    extract.add_argument('--force', action='store_true', help='atomically replace an existing output file')
    build = commands.add_parser('build', help='construct a profile from actual measured tone powers')
    build.add_argument('--context', required=True)
    build.add_argument('--measurements', required=True)
    build.add_argument('--reference', required=True)
    build.add_argument('--reference-plane', required=True)
    build.add_argument('--reference-baseband-hz', type=float, required=True)
    build.add_argument('--uncertainty-db', type=float)
    build.add_argument('--measured-at')
    build.add_argument('--max-spread-db', type=float)
    build.add_argument('-O', '--output', required=True)
    build.add_argument('--force', action='store_true')
    integrate = commands.add_parser('integrate', help='integrate each JSON hop in linear power with explicit missing data')
    integrate.add_argument('records')
    integrate.add_argument('--start-hz', type=float)
    integrate.add_argument('--stop-hz', type=float)
    integrate.add_argument('--method', choices=['fractional', 'legacy-centers'])
    integrate.add_argument('--include-estimated', action='store_true')
    integrate.add_argument('--omit-masked', action='store_true', help='report only measured bandwidth; never claim full-band power')
    baseline = commands.add_parser('baseline', help='measure terminated raw baseline; never infer absolute gain from one load')
    baseline.add_argument('records')
    baseline.add_argument('--temperature-k', type=float, required=True)
    baseline.add_argument('--source-ohms', type=float, required=True)
    baseline.add_argument('--receiver-ohms', type=float, required=True)
    for cmd in (baseline,):
        cmd.add_argument('--start-hz', type=float)
        cmd.add_argument('--stop-hz', type=float)
    compare = commands.add_parser('compare', help='compare antenna/observed capture to terminated/reference baseline')
    compare.add_argument('reference')
    compare.add_argument('observed')
    compare.add_argument('--start-hz', type=float)
    compare.add_argument('--stop-hz', type=float)
    measured = commands.add_parser('from-captures', help='build a measured tone, hot-cold or known-NF grid from a manifest')
    measured.add_argument('manifest')
    measured.add_argument('-O', '--output', required=True)
    measured.add_argument('--force', action='store_true')
    characterize = commands.add_parser('characterize', help='fit measured multi-temperature receiver/cascade characterization (not a power profile)')
    characterize.add_argument('manifest')
    characterize.add_argument('-O', '--output', required=True)
    characterize.add_argument('--force', action='store_true')
    characterize.add_argument('--require-checks', action='store_true', help='save report, then return failure if any measured checks fail')
    return root


def main(argv=None):
    root = parser()
    args = root.parse_args(argv)
    if args.command is None:
        root.print_help()
        return 2
    try:
        if args.command == 'characterize':
            from soapypower.characterization import ReceiverCharacterization
            report = ReceiverCharacterization.from_manifest(args.manifest).report
            write_json(args.output, report, overwrite=args.force)
            if args.require_checks and any(cell['validation_status'] != 'passed_measured_checks' for cell in report['cells']):
                return 1
        elif args.command in ('baseline', 'compare', 'from-captures'):
            from soapypower.calibration_workflows import (thermal_reference, average_capture,
                                                         band_summary, compare_captures, build_manifest)
            if args.command == 'baseline':
                output = thermal_reference(args.temperature_k, args.source_ohms, args.receiver_ohms)
                output['measured_baseline'] = band_summary(average_capture(args.records), args.start_hz, args.stop_hz)
                output['next_step'] = 'Known tone, two known noise temperatures, or independently known receiver noise figure required for absolute scale'
                print(json.dumps(output, allow_nan=False, sort_keys=True))
            elif args.command == 'compare':
                print(json.dumps(compare_captures(args.reference, args.observed, args.start_hz, args.stop_hz), allow_nan=False, sort_keys=True))
            else:
                write_json(args.output, build_manifest(args.manifest).profile, overwrite=args.force)
        elif args.command == 'validate':
            calibration = PowerCalibration.load(args.profile)
            profile = calibration.profile
            output = calibration.metadata()
            output.update(rf_range_hz=[profile['points'][0][0], profile['points'][-1][0]],
                          baseband_range_hz=[profile['baseband_hz'][0], profile['baseband_hz'][-1]],
                          context=calibration.context)
            print(json.dumps(output, allow_nan=False, sort_keys=True))
        elif args.command == 'context':
            write_json(args.output, extract_context(args.records), overwrite=args.force)
        elif args.command == 'build':
            calibration = build_profile(load_json(args.context), args.measurements, args.reference,
                                        args.reference_plane, args.reference_baseband_hz,
                                        args.uncertainty_db, args.measured_at, args.max_spread_db)
            write_json(args.output, calibration.profile, overwrite=args.force)
        elif args.command == 'integrate':
            for number, record in enumerate(read_records(args.records), 1):
                output = integrate_record(record, args.start_hz, args.stop_hz, args.omit_masked, args.method, args.include_estimated)
                output['record_number'] = number
                output['time_start'] = record.get('time_start')
                print(json.dumps(output, allow_nan=False, sort_keys=True))
    except (ValueError, OSError, OverflowError) as error:
        root.error(str(error))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
