"""Offline profile construction, validation and band-power routines (no SDR)."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path

from soapypower.calibration import PowerCalibration, CONTEXT_FIELDS, _compare, require_fixed_gain
from soapypower.files import load_json, write_json as _write_json, EvidenceSnapshot
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
        if metadata.get('equalization') is not None or metadata.get('calibration') is not None or record['unit'] not in ('input power/Hz', 'dB(input power/Hz)'):
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
    equalize = commands.add_parser('equalize', help='optional bounded relative PSD shaping from a measured power profile')
    equalize.add_argument('records')
    equalize.add_argument('--profile', required=True)
    equalize.add_argument('--reference-rf-hz', type=float, required=True)
    equalize.add_argument('--reference-baseband-hz', type=float, required=True)
    equalize.add_argument('--regularization', type=float, default=None)
    equalize.add_argument('--mode', choices=['regularized', 'attenuation-only'], default='regularized')
    equalize.add_argument('--usable-policy', help='explicit JSON band selection policy; exclusions become masks')
    equalize.add_argument('--max-boost-db', type=float, default=12)
    equalize.add_argument('-O', '--output', required=True)
    equalize.add_argument('--force', action='store_true')
    smooth = commands.add_parser('smooth-profile', help='constrained uncertainty-weighted smoothing; independent holdouts required')
    smooth.add_argument('profile')
    smooth.add_argument('--model', required=True)
    smooth.add_argument('--holdouts', required=True)
    smooth.add_argument('-O', '--output', required=True)
    smooth.add_argument('--force', action='store_true')
    holdout = commands.add_parser('validate-response', help='validate a frozen profile against independent on/off tone captures')
    holdout.add_argument('profile')
    holdout.add_argument('--holdouts', required=True)
    holdout.add_argument('-O', '--output', required=True)
    holdout.add_argument('--force', action='store_true')
    holdout.add_argument('--require-checks', action='store_true')
    overlap = commands.add_parser('overlap-fit', help='relative IF tomography from repeated stable RF channels and validation captures')
    overlap.add_argument('manifest')
    overlap.add_argument('-O', '--output', required=True)
    overlap.add_argument('--force', action='store_true')
    gain = commands.add_parser('gain-compare', help='compare repeated same-RF observations at two fixed gain states')
    gain.add_argument('manifest')
    gain.add_argument('-O', '--output', required=True)
    gain.add_argument('--force', action='store_true')
    gain.add_argument('--require-checks', action='store_true')
    tone = commands.add_parser('tone-audit', help='isolated selected-IQ tone frequency/drift diagnostic; no protocol identification')
    tone.add_argument('iq')
    tone.add_argument('--sample-rate', type=float, required=True)
    tone.add_argument('--expected-baseband-hz', type=float, required=True)
    tone.add_argument('--reference-rf-hz', type=float, required=True)
    tone.add_argument('--frame-size', type=int, default=4096)
    tone.add_argument('--frames', type=int, default=128)
    tone.add_argument('--search-hz', type=float, default=500)
    tone.add_argument('--min-tone-fraction', type=float, default=0.8)
    tone.add_argument('-O', '--output', required=True)
    tone.add_argument('--force', action='store_true')
    intervals = commands.add_parser('interval-audit', help='operator-identified active/quiet IQ intervals; no calibrated noise figure')
    intervals.add_argument('iq')
    intervals.add_argument('--intervals', required=True)
    intervals.add_argument('-O', '--output', required=True)
    intervals.add_argument('--force', action='store_true')
    return root


def _input_snapshot(args):
    """One evidence policy for every file-producing calibration subcommand."""
    fields = {
        'context': ('records',), 'build': ('context','measurements'),
        'from-captures': ('manifest',), 'characterize': ('manifest',),
        'equalize': ('records','profile','usable_policy'),
        'smooth-profile': ('profile','model','holdouts'),
        'validate-response': ('profile','holdouts'),
        'overlap-fit': ('manifest',), 'gain-compare': ('manifest',),
        'tone-audit': ('iq',), 'interval-audit': ('iq','intervals'),
    }.get(args.command, ())
    snapshot = EvidenceSnapshot()
    documents = []
    for field in fields:
        name = getattr(args,field,None)
        if name is not None:
            snapshot.add(name)
            if field in ('manifest','holdouts'):
                documents.append(name)
    def captures(node, root):
        if isinstance(node,list):
            for child in node:captures(child,root)
        elif isinstance(node,dict):
            for key,value in node.items():
                if key in ('capture','on','off','cold','hot','reference','observed') and isinstance(value,str):
                    snapshot.add(root/value)
                elif isinstance(value,(dict,list)):
                    captures(value,root)
    for name in documents:
        document = load_json(name)
        if isinstance(document,dict):
            captures(document.get('cells',[]),Path(name).resolve().parent)
    output = getattr(args,'output',None)
    if output is not None:snapshot.protect_output(output)
    return snapshot


def main(argv=None):
    root = parser()
    args = root.parse_args(argv)
    if args.command is None:
        root.print_help()
        return 2
    try:
        snapshot = _input_snapshot(args)
        def write_json(path, value, overwrite=False):
            snapshot.protect_output(path)
            snapshot.verify()
            _write_json(path, value, overwrite=overwrite, before_publish=snapshot.verify)
        if args.command in ('overlap-fit','gain-compare','tone-audit','interval-audit'):
            protected = [getattr(args,'manifest',None),getattr(args,'iq',None),getattr(args,'intervals',None)]
            if getattr(args,'manifest',None):
                document=load_json(args.manifest)
                if isinstance(document,dict) and isinstance(document.get('cells'),list):
                    for cell in document['cells']:
                        if isinstance(cell,dict):
                            for key in ('capture','reference','observed'):
                                if isinstance(cell.get(key),str):
                                    protected.append(Path(args.manifest).resolve().parent/cell[key])
            if Path(args.output).resolve() in {Path(name).resolve() for name in protected if name is not None}:
                raise ValueError('passive analysis output must not replace source evidence')
            if args.command == 'overlap-fit':
                from soapypower.passive_response import build_overlap
                report=build_overlap(args.manifest).profile
            elif args.command == 'gain-compare':
                from soapypower.gain_comparison import compare_gain_states
                report=compare_gain_states(args.manifest)
            elif args.command == 'tone-audit':
                from soapypower.passive_iq import tone_audit
                report=tone_audit(args.iq,args.sample_rate,args.expected_baseband_hz,args.reference_rf_hz,
                                  args.frame_size,args.frames,args.search_hz,args.min_tone_fraction)
            else:
                from soapypower.passive_iq import interval_audit
                report=interval_audit(args.iq,args.intervals)
            write_json(args.output,report,overwrite=args.force)
            if args.command == 'gain-compare' and args.require_checks and not report['passed']:
                return 1
        elif args.command == 'equalize':
            from soapypower.equalization import equalize_file
            damping = args.regularization if args.regularization is not None else (0 if args.mode == 'attenuation-only' else 0.001)
            if args.usable_policy and Path(args.output).resolve() == Path(args.usable_policy).resolve():
                raise ValueError('equalization output must not replace its usable-band policy')
            policy = load_json(args.usable_policy) if args.usable_policy else None
            equalize_file(args.records, args.profile, args.output, args.reference_rf_hz,
                          args.reference_baseband_hz, damping, args.max_boost_db, args.force, args.mode, policy, evidence_snapshot=snapshot)
        elif args.command in ('smooth-profile', 'validate-response'):
            # Derived artifacts must never replace the measured source or any
            # manifest/model/capture evidence, even with explicit --force.
            from soapypower.response_validation import validate_holdout
            evidence = [args.profile, args.holdouts]
            if args.command == 'smooth-profile':
                evidence.append(args.model)
            manifest = load_json(args.holdouts)
            if isinstance(manifest, dict) and isinstance(manifest.get('cells'), list):
                for cell in manifest['cells']:
                    if isinstance(cell, dict):
                        for key in ('on', 'off'):
                            if isinstance(cell.get(key), str):
                                evidence.append(Path(args.holdouts).resolve().parent/cell[key])
            if Path(args.output).resolve() in {Path(name).resolve() for name in evidence}:
                raise ValueError('derived output must not replace source profile or measurement evidence')
            calibration = PowerCalibration.load(args.profile)
            if args.command == 'smooth-profile':
                from soapypower.response_smoothing import smooth_profile
                write_json(args.output, smooth_profile(calibration,args.model,args.holdouts).profile, overwrite=args.force)
            else:
                report = validate_holdout(calibration,args.holdouts)
                write_json(args.output, report, overwrite=args.force)
                if args.require_checks and not report['passed']:
                    return 1
        elif args.command == 'characterize':
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
            from soapypower.passive_response import load_response_profile
            calibration = load_response_profile(args.profile)
            output = calibration.metadata()
            output.update(rf_range_hz=list(calibration.domain['rf_hz']),
                          baseband_range_hz=list(calibration.domain['raw_baseband_hz']),
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
