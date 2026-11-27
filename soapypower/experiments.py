"""Automated experiment suite for a 100-300 MHz survey band.

A suite is plain JSON: ordered trials, each with an explicit source state, a role
(fit / holdout / settling) and the exact soapy_power argument vector (no shell).
The runner executes trials, keeps a ledger (argv, timing, SHA-256, source
acknowledgments, every failed attempt) and always returns the reference source to
OFF. Analysis verifies the ledger hashes, fits the relative IF model on 'fit'
trials, refits it independently on 'holdout' trials, characterises tune settling
from a delay sweep and estimates IQ image rejection from the settling tone.

Nothing here is a calibration. Simulated sessions are labelled as such; acquired
sessions are hardware-observed, relative and uncalibrated.
"""
import argparse
import datetime
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import zlib
from pathlib import Path

from soapypower import model_validation as mv
from soapypower.files import load_json, write_json
from soapypower.numerics import crop_bins, finite
from soapypower.records import read_records

BAND_HZ = (100e6, 300e6)
RATE_HZ = 56e6
BINS = 8192
OVERLAP_PERCENT = 87.5244140625  # exactly 1022 of 8192 bins retained per hop
DEFAULT_DELAYS_S = (0.0, 0.002, 0.005, 0.01, 0.02, 0.04, 0.08, 0.2)
ACCEPTANCE = {'max_repeat_sigma_db': 0.5, 'max_if_disagreement_db': 0.25, 'settling_tolerance_db': 0.1}
INPUTS = ('antenna', 'terminated')
# Typed telemetry per hop (soapypower.telemetry): RFIC temperature read at most once per second, only
# while the stream is closed between hops (the suite reopens it per hop), plus sensors, processing-chain
# fingerprint and controller LO mapping. Recording only: no thermal correction is applied.
TELEMETRY_POLICY = {'version': 1, 'temperature_interval_s': 1.0, 'temperature_max_age_s': 3.0}


# ----------------------------------------------------------------- geometry/commands

def hop_plan(start_hz, stop_hz, rate_hz=RATE_HZ, bins=BINS, overlap_percent=OVERLAP_PERCENT):
    """Integer hop geometry identical to SoapyPower.freq_plan (frequency sign +1), without a device."""
    start, stop = finite(start_hz, 'start'), finite(stop_hz, 'stop')
    if stop < start:
        raise ValueError('frequency range must be ascending')
    removed = crop_bins(bins, finite(overlap_percent, 'overlap', nonnegative=True) / 100)
    step = finite(rate_hz, 'sample rate', positive=True) / bins
    hop = (bins - removed) * step
    span = stop - start
    hopping = span >= hop
    hops = math.ceil(span / hop) if hopping else 1
    first_offset = (-(bins // 2) + removed // 2) * step
    first = start - first_offset if hopping else ((start + stop) / 2 - first_offset - hop / 2 if span else start)
    return {'retained_bins': bins - removed, 'hop_hz': hop, 'hops': hops, 'bin_hz': step,
            'redundancy': bins / (bins - removed), 'centers_hz': [first + i * hop for i in range(hops)]}


def scan_argv(device, start_hz, stop_hz, seconds, output, antenna='RX2', gain_db=20.0,
              rate_hz=RATE_HZ, bins=BINS, overlap_percent=OVERLAP_PERCENT, integration_s=0.005,
              tune_delay_s=0.02, settle_samples=32768, executable='soapy_power', telemetry_policy=None):
    """Acquisition policy of examples/ettus/scan.sh as an argument vector.

    --skip-driver-teardown: the run's status is decided once the output is closed; UHD's
    macOS USB teardown can otherwise abort (SIGABRT) a complete measurement.
    """
    rate = '{:.6f}'.format(rate_hz)
    return [executable, '-d', device, '-A', antenna, '-C', '0',
            '-f', '{:.6f}:{:.6f}'.format(start_hz, stop_hz), '-e', '{:g}'.format(seconds),
            '-O', str(output), '-F', 'json', '-r', rate, '-w', rate, '--master-clock-rate', rate,
            '--force-rate', '-g', '{:g}'.format(gain_db), '-b', str(bins),
            '-o', repr(float(overlap_percent)), '-t', '{:g}'.format(integration_s),
            '--tune-delay', '{:g}'.format(tune_delay_s), '--settle-samples', str(int(settle_samples)),
            '--lock-sensor', 'lo_locked', '--reopen-stream', '--deadline-policy', 'finish-hop',
            '--require-timestamps', '--partial-policy', 'reject', '--no-pyfftw',
            '--precision', 'single', '--max-threads', '4', '--max-queue-size', '4', '--skip-driver-teardown'] + (
            ['--telemetry-policy', str(telemetry_policy)] if telemetry_policy is not None else [])


def calibration_config(device, levels_dbm, max_receiver_input_dbm, path_loss_db, reference,
                       reference_plane, rf_hz=None, baseband_hz=(-10e6, -5e6, 5e6, 10e6),
                       antenna='RX2', gain_db=20.0, rate_hz=RATE_HZ, bins=BINS):
    """Validated tone-method auto_calibration config for 100-300 MHz.

    Physical facts are mandatory arguments, never defaults, and
    physical_setup_confirmed stays False until the operator edits it.
    """
    from soapypower import auto_calibration
    rf_hz = list(rf_hz or [100e6 + 25e6 * i for i in range(9)])
    if len(rf_hz) < 4:
        raise ValueError('need at least four RF grid points')
    holdouts = [{'rf_hz': (rf_hz[i] + rf_hz[i + 1]) / 2, 'baseband_hz': bb}
                for i in (1, len(rf_hz) - 3) for bb in (baseband_hz[1], baseband_hz[2])]
    config = {'version': 1,
              'receiver': {'device': device, 'channel': 0, 'antenna': antenna, 'sample_rate': rate_hz,
                           'bandwidth': rate_hz, 'gain': gain_db, 'dc_correction': 'auto',
                           'iq_correction': 'auto', 'master_clock_rate': rate_hz},
              'rf_hz': rf_hz, 'baseband_hz': list(baseband_hz), 'reference_baseband_hz': baseband_hz[2],
              'holdouts': holdouts,
              'source': {'type': 'tone', 'levels_dbm': list(levels_dbm), 'path_loss_db': path_loss_db,
                         'max_receiver_input_dbm': max_receiver_input_dbm},
              'reference': reference, 'reference_plane': reference_plane,
              'physical_setup_confirmed': False,
              'acquisition': {'fft_bins': bins, 'segments': 64, 'integration_width_hz': 20e3,
                              'tune_delay': 0.1, 'settle_samples': 32768, 'lock_sensor': 'lo_locked',
                              'require_timestamps': True}}
    auto_calibration.configuration(config)  # raise before any hardware is touched
    return config


# ---------------------------------------------------------------------------- suite

def default_suite(device, tone_rf_hz=200e6, tone_dbm=None, max_receiver_input_dbm=None, path_loss_db=None,
                  delays_s=DEFAULT_DELAYS_S, scan_seconds=60.0, settle_seconds=20.0, antenna='RX2',
                  gain_db=20.0, overlap_percent=OVERLAP_PERCENT, band_hz=BAND_HZ, settle_samples=32768,
                  input_kind='antenna', telemetry=True, require_temperature=False):
    """Interleaved shifted hop grids (A, B), later holdout repeats, optional settling sweep.

    Grid B starts half a hop lower. A perfectly periodic grid identifies the IF
    response only up to one constant per IF coset; the second grid links them.
    Settling trials scan a window around the tone with --settle-samples 0, so the
    tune delay (plus the lock wait) is the only settling control being varied.
    input_kind is the operator's declaration of what is connected to the port
    ('antenna' or 'terminated'); the software cannot verify it. telemetry records the
    TELEMETRY_POLICY snapshots per hop; require_temperature makes a stale or missing RFIC
    temperature reject the hop.
    """
    if not isinstance(device, str) or not device:
        raise ValueError('device selector must be a nonempty string')
    if input_kind not in INPUTS:
        raise ValueError('input must be one of: ' + ', '.join(INPUTS))
    if input_kind == 'terminated' and tone_dbm is not None:
        raise ValueError('a terminated input cannot also receive a settling tone')
    start, stop = finite(band_hz[0], 'band start', positive=True), finite(band_hz[1], 'band stop', positive=True)
    if stop <= start:
        raise ValueError('band must be ascending')
    geometry = hop_plan(start, stop, overlap_percent=overlap_percent)
    shift = geometry['hop_hz'] / 2
    trials = []
    limits = None
    if tone_dbm is not None:
        tone_dbm = finite(tone_dbm, 'tone level')
        if max_receiver_input_dbm is None or path_loss_db is None:
            raise ValueError('a settling tone needs a declared receiver input ceiling and path loss')
        ceiling = finite(max_receiver_input_dbm, 'receiver input ceiling')
        loss = finite(path_loss_db, 'path loss', nonnegative=True)
        if tone_dbm - loss > ceiling:
            raise ValueError('tone at the receiver ({:.2f} dBm) exceeds the declared ceiling {:.2f} dBm'.format(tone_dbm - loss, ceiling))
        if not start <= tone_rf_hz <= stop:
            raise ValueError('settling tone must lie inside the band')
        delays = [finite(d, 'delay', nonnegative=True) for d in delays_s]
        if len(delays) < 4 or delays != sorted(set(delays)):
            raise ValueError('need at least four increasing unique delays')
        limits = {'tone_dbm': tone_dbm, 'path_loss_db': loss, 'receiver_input_dbm': tone_dbm - loss,
                  'max_receiver_input_dbm': ceiling}

    def scan(name, role, a, b, seconds, delay=0.02, samples=settle_samples, source=None, grid=None):
        trials.append({'id': name, 'kind': 'scan', 'role': role, 'grid': grid, 'start_hz': a, 'stop_hz': b,
                       'seconds': seconds, 'antenna': antenna, 'gain_db': gain_db,
                       'overlap_percent': overlap_percent, 'tune_delay_s': delay, 'settle_samples': samples,
                       'source': source or {'state': 'off'}})
    scan('grid-a-fit', 'fit', start, stop, scan_seconds, grid='A')
    scan('grid-b-fit', 'fit', start - shift, stop, scan_seconds, grid='B')
    scan('grid-a-holdout', 'holdout', start, stop, scan_seconds, grid='A')
    scan('grid-b-holdout', 'holdout', start - shift, stop, scan_seconds, grid='B')
    if limits:
        window = RATE_HZ / 2 + 2 * geometry['hop_hz']
        for d in delays:
            scan('settle-{:g}ms'.format(d * 1e3), 'settling', max(start - shift, tone_rf_hz - window),
                 min(stop, tone_rf_hz + window), settle_seconds, delay=d, samples=0, grid='A',
                 source={'state': 'on', 'rf_hz': tone_rf_hz, 'level_dbm': tone_dbm})
    return {'version': 1, 'kind': 'experiment_suite', 'device': device, 'band_hz': [start, stop],
            'geometry': {k: v for k, v in geometry.items() if k != 'centers_hz'}, 'tone_rf_hz': tone_rf_hz,
            'source_limits': limits, 'trials': trials, 'acceptance': dict(ACCEPTANCE),
            'input': {'kind': input_kind, 'port': antenna, 'declared_by': 'operator'},
            'telemetry': (dict(TELEMETRY_POLICY, **({'require_temperature': True} if require_temperature else {}))
                          if telemetry else None)}


def _sha256(path):
    digest = hashlib.sha256()
    with open(str(path), 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def subprocess_runner(trial, argv, output, timeout):
    started = time.monotonic()
    inherited = {}
    lease_text = os.environ.get('SPR_RADIO_LEASE_FD')
    if os.name != 'nt' and lease_text is not None:
        try:
            fd = int(lease_text)
            lease = Path(os.environ['SPR_SESSIONS']) / '.receiver.lock'
            actual, expected = os.fstat(fd), lease.stat()
            if fd < 3 or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError('receiver lease identity differs')
            inherited['pass_fds'] = (fd,)
        except (OSError, KeyError, ValueError) as error:
            raise ValueError('Invalid inherited receiver lease') from error
    done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False, **inherited)
    return {'returncode': done.returncode, 'seconds': time.monotonic() - started,
            'stderr_tail': done.stderr.decode('utf-8', 'replace')[-2000:]}


def run_suite(suite, directory, source=None, runner=subprocess_runner, retries=1, timeout_margin_s=120.0,
              executable='soapy_power', data_source='acquired',
              clock=lambda: datetime.datetime.now(datetime.timezone.utc)):
    """Execute trials in order.

    source(state, rf_hz, level_dbm) follows the auto_calibration SourceController
    contract. A failed attempt is kept and retried under a new file name; exhausted
    retries abort the session. The source is returned to OFF on every exit path.
    """
    if suite.get('kind') != 'experiment_suite' or suite.get('version') != 1 or not suite.get('trials'):
        raise ValueError('not a version 1 experiment suite')
    if data_source not in ('acquired', 'simulated'):
        raise ValueError('data_source must be acquired or simulated')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    ledger = {'kind': 'experiment_ledger', 'version': 1, 'status': 'running', 'data_source': data_source,
              'suite': suite, 'started_utc': clock().isoformat(), 'trials': [], 'calibrated': False}
    path = directory / 'ledger.json'
    policy_path = None
    if suite.get('telemetry') is not None:
        from soapypower.telemetry import TelemetryPolicy
        ledger['telemetry_policy_sha256'] = TelemetryPolicy(suite['telemetry']).sha256
        policy_path = directory / 'telemetry-policy.json'
        write_json(policy_path, suite['telemetry'])
    write_json(path, ledger)
    last = {'state': 'unknown', 'rf_hz': suite['trials'][0]['start_hz']}

    def drive(wanted):
        state = wanted['state']
        if source is None:
            if state != 'off':
                raise ValueError('trial needs a reference source but none was provided')
            return {'state': 'off', 'mode': 'no source connected'}
        last['state'], last['rf_hz'] = 'unknown', wanted.get('rf_hz', last['rf_hz'])
        ack = source(state, last['rf_hz'], wanted.get('level_dbm'))
        last['state'] = state
        return ack
    try:
        for trial in suite['trials']:
            entry = {'id': trial['id'], 'role': trial['role'], 'attempts': []}
            ledger['trials'].append(entry)
            for attempt in range(retries + 1):
                name = trial['id'] + ('' if attempt == 0 else '-retry{}'.format(attempt)) + '.jsonl'
                output = directory / name
                record = {'file': name, 'started_utc': clock().isoformat(), 'status': 'running'}
                entry['attempts'].append(record)
                try:
                    record['source'] = drive(trial.get('source', {'state': 'off'}))
                    argv = scan_argv(suite['device'], trial['start_hz'], trial['stop_hz'], trial['seconds'], output,
                                     trial['antenna'], trial['gain_db'], overlap_percent=trial['overlap_percent'],
                                     tune_delay_s=trial['tune_delay_s'], settle_samples=trial['settle_samples'],
                                     executable=executable, telemetry_policy=policy_path)
                    record['argv'] = argv
                    result = runner(trial, argv, output, trial['seconds'] + timeout_margin_s)
                    record.update(result)
                    if result['returncode'] != 0:
                        raise RuntimeError('acquisition exited with status {}'.format(result['returncode']))
                    if not output.is_file():
                        raise RuntimeError('acquisition produced no output file')
                    record.update(records=sum(1 for _ in read_records(output)), sha256=_sha256(output),
                                  status='accepted')
                    break
                except Exception as error:
                    record.update(status='failed', error='{}: {}'.format(type(error).__name__, error))
                finally:
                    record['finished_utc'] = clock().isoformat()
                    write_json(path, ledger, overwrite=True)
            if entry['attempts'][-1]['status'] != 'accepted':
                raise RuntimeError('trial {} failed after {} attempt(s)'.format(trial['id'], retries + 1))
        ledger['status'] = 'completed'
    except BaseException as error:
        ledger['status'] = 'failed'
        ledger['error'] = '{}: {}'.format(type(error).__name__, error)
        for entry in ledger['trials']:
            for record in entry['attempts']:
                if record.get('status') == 'running':
                    record['status'] = 'interrupted'
        raise
    finally:
        try:
            if source is not None and last['state'] != 'off':
                ledger['final_source'] = source('off', last['rf_hz'], None)
                last['state'] = 'off'
        except BaseException as error:
            ledger['source_cleanup_error'] = '{}: {}'.format(type(error).__name__, error)
            ledger['status'] = 'failed'
        ledger['final_source_state'] = last['state'] if source is not None else 'no source connected'
        if source is not None and last['state'] != 'off':
            print('Experiment stopped: reference source may be ON or unknown. Switch it OFF and verify.', file=sys.stderr)
        ledger['finished_utc'] = clock().isoformat()
        write_json(path, ledger, overwrite=True)
    return ledger


# -------------------------------------------------------------------------- analysis

def analyze(directory, cell_hz=100e3, dc_mask_hz=500e3, edge_fraction=0.12, tone_window_hz=40e3, input_kind=None):
    """Verify evidence, then fit -> independent holdout refit, settling and image checks.

    input_kind may declare the connected input for a suite planned before --input
    existed; it is recorded as declared at analysis and must match a planned value.
    """
    directory = Path(directory)
    ledger = load_json(directory / 'ledger.json')
    if ledger.get('kind') != 'experiment_ledger' or ledger.get('status') != 'completed':
        raise ValueError('analyse completed experiment ledgers only (status {!r})'.format(ledger.get('status')))
    suite = ledger['suite']
    files, trials = {}, {t['id']: t for t in suite['trials']}
    for entry in ledger['trials']:
        final = entry['attempts'][-1]
        if final['status'] != 'accepted':
            raise ValueError('trial {} has no accepted attempt'.format(entry['id']))
        path = directory / final['file']
        if _sha256(path) != final['sha256']:
            raise ValueError('evidence file changed after acquisition: ' + final['file'])
        files[entry['id']] = path
    start, stop = suite['band_hz']
    acc = dict(ACCEPTANCE, **suite.get('acceptance', {}))
    options = dict(edge_fraction=edge_fraction, dc_mask_hz=dc_mask_hz)
    by_role = lambda role: [files[i] for i in files if trials[i]['role'] == role]
    simulated = ledger['data_source'] == 'simulated'
    planned = suite.get('input', {}).get('kind')
    if input_kind is not None and input_kind not in INPUTS:
        raise ValueError('input must be one of: ' + ', '.join(INPUTS))
    if planned and input_kind and input_kind != planned:
        raise ValueError('suite was planned with input {!r}, not {!r}'.format(planned, input_kind))
    input_source = 'planned' if planned else ('declared at analysis' if input_kind else 'unspecified')
    input_kind = planned or input_kind or 'unspecified'
    report = {'kind': 'experiment_analysis', 'version': 1, 'data_source': ledger['data_source'], 'input': input_kind,
              'input_source': input_source,
              'evidence_level': 'simulation: software verification only' if simulated
              else 'hardware-observed: relative, uncalibrated', 'calibrated': False,
              'ledger_sha256': _sha256(directory / 'ledger.json'), 'checks': {}, 'required': []}
    checks = report['checks']
    fit_store = mv.HopStore.from_files(by_role('fit'), start, stop, cell_hz, **options)
    checks['repeatability'] = mv.repeatability(fit_store, acc['max_repeat_sigma_db'])
    fit = mv.fit_if_response(fit_store, if_bin_hz=cell_hz)
    checks['if_fit'] = {k: v for k, v in fit.items() if k not in ('if_axis_hz', 'response_db', 'response_se_db')}
    checks['if_fit']['passed'] = bool(fit.get('status') == 'fit' and fit.get('fully_identified'))
    holdout_files = by_role('holdout')
    if holdout_files:
        test_store = mv.HopStore.from_files(holdout_files, start, stop, cell_hz, **options)
        check = mv.holdout_validation(fit_store, test_store, if_bin_hz=cell_hz,
                                      max_disagreement_db=acc['max_if_disagreement_db'])
        checks['if_holdout'] = {k: v for k, v in check.items() if k != 'fit'}
    else:
        checks['if_holdout'] = {'status': 'missing', 'passed': False}
    report['required'] = ['repeatability', 'if_fit', 'if_holdout']
    if fit.get('status') == 'fit':
        write_json(directory / 'if_response.json', {
            'kind': 'relative_if_response', 'version': 1, 'calibrated': False, 'data_source': ledger['data_source'],
            'if_axis_hz': fit['if_axis_hz'], 'response_db': fit['response_db'],
            'response_se_db': fit['response_se_db'], 'anchor_if_hz': fit['anchor_if_hz'],
            'receiver_context_signature': fit_store.signature,
            'validated': bool(checks['if_holdout'].get('passed'))}, overwrite=True)
        levels = mv.cell_levels(fit_store, fit['response_db'])
        spurs, masked = mv.spurious_responses(fit_store, fit, levels)
        checks['spurious_responses'] = dict(spurs, gating=False)
        extra = {}
        if masked is not None:
            extra = {'mixing_masked_' + k: masked[k] for k in ('median_db', 'p10_db', 'p90_db')}
        write_json(directory / 'cell_levels.json', dict(
            levels, kind='cell_levels', version=1, calibrated=False, data_source=ledger['data_source'],
            input=input_kind, receiver_context_signature=fit_store.signature,
            holdout_median_db=mv.cell_levels(test_store, fit['response_db'])['median_db'] if holdout_files else None,
            mixing_identified=[{k: h[k] for k in ('order', 'sign', 'kind', 'source_bands_hz')} for h in spurs.get('identified', [])],
            **extra), overwrite=True)
        if input_kind == 'terminated':
            floor = mv.floor_summary(levels)
            if masked is not None:
                after = mv.floor_summary(masked)
                floor['after_mixing_mask'] = {k: after.get(k) for k in ('floor_median_db', 'flatness_p5_p95_db', 'intermittent_cells')}
                floor['after_mixing_mask']['internal_features'] = len(after.get('internal_features', []))
            checks['terminated_floor'] = dict(floor, gating=False)
    settle = {trials[i]['tune_delay_s']: files[i] for i in files if trials[i]['role'] == 'settling'}
    if settle:
        tone = suite['tone_rf_hz']
        delays, levels, error = mv.settling_from_scans(settle, tone, tone_window_hz, dc_mask_hz=dc_mask_hz)
        choice = mv.choose_delay(delays, error, tolerance_db=acc['settling_tolerance_db'])
        checks['settling'] = dict(choice, delays_s=delays, tone_db=levels, error_db=error,
                                  scope='7-MHz survey steps with stream reopen; other retune sizes not characterised')
        report['required'].append('settling')
        checks['image_rejection'] = dict(mv.image_rejection_from_scan(settle[delays[-1]], tone, tone_window_hz,
                                                                      dc_mask_hz=dc_mask_hz), gating=False)
    stores = [fit_store] + ([test_store] if holdout_files else [])
    if any(store.telemetry for store in stores):
        checks['telemetry'] = dict(mv.telemetry_summary(stores, dc_mask_hz, controlled_input=input_kind == 'terminated'),
                                   gating=False)
    report['passed'] = all(checks[name].get('passed') is True for name in report['required'])
    write_json(directory / 'analysis.json', report, overwrite=True)
    return report


def _session(directory):
    directory = Path(directory)
    analysis = load_json(directory / 'analysis.json')
    levels = load_json(directory / 'cell_levels.json')
    response = load_json(directory / 'if_response.json')
    if analysis.get('kind') != 'experiment_analysis' or levels.get('kind') != 'cell_levels':
        raise ValueError('not an analysed experiment session: {}'.format(directory))
    return analysis, levels, response


def compare(terminated_dir, antenna_dir, output=None, threshold_db=3.0):
    """Classify features as external (arrive at the port) or internal (present when terminated).

    Both sessions must be analysed, from the same data source and the same receiver
    context (driver, serial, gains, rate, port ...); their declared inputs must be
    'terminated' and 'antenna'. The IF responses of the two sessions are compared too.
    """
    ta, tl, tr = _session(terminated_dir)
    aa, al, ar = _session(antenna_dir)
    if (ta['input'], aa['input']) != ('terminated', 'antenna'):
        raise ValueError('declared inputs are {!r} and {!r}; expected terminated and antenna'.format(ta['input'], aa['input']))
    if ta['data_source'] != aa['data_source']:
        raise ValueError('cannot compare simulated with acquired sessions')
    if tl['receiver_context_signature'] != al['receiver_context_signature']:
        raise ValueError('receiver context differs between the sessions')
    for name, analysis in (('terminated', ta), ('antenna', aa)):
        if not analysis['passed']:
            raise ValueError('{} session did not pass its own checks'.format(name))
    result = mv.compare_inputs({'levels': tl, 'response': tr}, {'levels': al, 'response': ar}, threshold_db)
    if tl.get('mixing_masked_median_db') or al.get('mixing_masked_median_db'):
        def masked(levels):
            if not levels.get('mixing_masked_median_db'):
                return levels
            return dict(levels, **{k: levels['mixing_masked_' + k] for k in ('median_db', 'p10_db', 'p90_db')})
        after = mv.compare_inputs({'levels': masked(tl)}, {'levels': masked(al)}, threshold_db)
        result['after_mixing_mask'] = {k: after[k] for k in ('external', 'internal', 'internal_intermittent',
                                                             'antenna_minus_terminated_median_db')}
        result['after_mixing_mask']['intermittent_cells'] = after['terminated_floor'].get('intermittent_cells')
        result['mixing_identified'] = {'terminated': tl.get('mixing_identified', []), 'antenna': al.get('mixing_identified', [])}
    result.update(kind='input_comparison', version=1, calibrated=False, data_source=ta['data_source'],
                  terminated_session=str(terminated_dir), antenna_session=str(antenna_dir))
    output = Path(output) if output else Path(terminated_dir) / 'comparison-{}.json'.format(Path(antenna_dir).name)
    write_json(output, result, overwrite=True)
    return result


# ----------------------------------------------------------------------- dry-run rig

class SimulatedRig:
    """Receiver + CW source simulator with known truth for a full pipeline dry run."""

    def __init__(self, truth_if=None, tau_s=0.008, amplitude_db=1.5, image_rejection_db=35.0,
                 tone_above_floor_db=50.0, fail_first=False, environment=(), seed=0, harmonic_sources=(),
                 temperature=None, thermal_db_per_c=0.0, lo_offset_hz=0.0):
        import numpy as np
        self.truth = truth_if or (lambda f: -1.2 * (f / 14e6) ** 2 + .25 * np.sin(2 * np.pi * f / 5e6))
        self.tau, self.amplitude, self.irr, self.tone_db = tau_s, amplitude_db, image_rejection_db, tone_above_floor_db
        self.fail_first = fail_first
        self.environment = list(environment)  # always-present RF-fixed (rf_hz, dB above floor)
        self.seed = int(seed)
        self.harmonic_sources = list(harmonic_sources)  # (source_hz, order, sign, dB): LO-harmonic responses
        self.temperature, self.thermal_db_per_c, self.lo_offset_hz = temperature, thermal_db_per_c, lo_offset_hz
        self.clock_s = 0.0  # session time across trials, for the temperature model
        self.source_state = {'state': 'off'}
        self.calls = []

    def source(self, state, rf_hz, level=None):
        self.source_state = {'state': state, 'rf_hz': rf_hz, 'level': level}
        self.calls.append((state, rf_hz, level))
        return {'state': state, 'rf_hz': rf_hz, 'input_dbm': level, 'confirmed': True, 'mode': 'simulated'}

    def runner(self, trial, argv, output, timeout):
        if self.fail_first:
            self.fail_first = False
            return {'returncode': 3, 'seconds': 0.0, 'stderr_tail': 'simulated failure'}
        plan = hop_plan(trial['start_hz'], trial['stop_hz'], overlap_percent=trial['overlap_percent'])
        tones, scale, bins, passes = list(self.environment), 1.0, 1024, 4
        if self.source_state['state'] == 'on':
            tones = [(self.source_state['rf_hz'], self.tone_db)]  # settling trials: the tone alone
            scale = 10 ** (-self.amplitude * math.exp(-trial['tune_delay_s'] / self.tau) / 10)
            bins, passes = 4096, 2
        centers = mv.synthetic_scan(output, trial['start_hz'], trial['stop_hz'], hop_hz=plan['hop_hz'], bins=bins,
                          passes=passes, if_response=self.truth, tones=tones, tone_scale=scale,
                          image_rejection_db=self.irr, seed=zlib.crc32('{}/{}'.format(self.seed, trial['id']).encode()),
                          harmonic_sources=self.harmonic_sources,
                          offset_hz=plan['centers_hz'][0] - trial['start_hz'] - plan['hop_hz'] / 2,
                          temperature=self.temperature, thermal_db_per_c=self.thermal_db_per_c,
                          lo_offset_hz=self.lo_offset_hz, time_origin_s=self.clock_s)
        self.clock_s += .05 * passes * len(centers)
        return {'returncode': 0, 'seconds': 0.0, 'stderr_tail': ''}


# ------------------------------------------------------------------------------- CLI

def _summary(report):
    lines = ['{} ({}; input: {})'.format('PASSED' if report['passed'] else 'FAILED', report['evidence_level'],
                                        report.get('input', 'unspecified'))]
    for name, check in report['checks'].items():
        mark = 'required' if name in report['required'] else 'info'
        keys = ('sigma_db', 'identifiable_components', 'median_redundancy', 'agreement', 'improvement_db',
                'delay_s', 'status', 'median_image_rejection_db', 'min_lower_bound_db',
                'floor_median_db', 'flatness_p5_p95_db', 'intermittent_cells', 'records_with_fresh_temperature',
                'temperature_min_c', 'temperature_max_c', 'max_abs_trial_slope_c_per_min', 'chain_stable')
        detail = {k: check[k] for k in keys if k in check}
        if isinstance(check.get('floor_vs_temperature'), dict) and check['floor_vs_temperature'].get('status') == 'fit':
            detail['floor_db_per_c'] = check['floor_vs_temperature']['slope_db_per_c']
        if isinstance(check.get('lo_mapping'), dict):
            detail['lo_outside_dc_mask'] = check['lo_mapping']['records_lo_outside_dc_mask']
        if 'agreement' in detail:
            detail['agreement'] = detail['agreement'].get('rms_db')
        if check.get('internal_features') is not None:
            detail['internal_features'] = len(check['internal_features'])
        if 'after_mixing_mask' in check:
            detail['after_mixing_mask'] = check['after_mixing_mask']
        if name == 'spurious_responses':
            detail = {'rounds': [r['excess_observations'] for r in check.get('rounds', [])],
                      'masked_observations': check.get('masked_observations'),
                      'identified': ['n={order} s={sign:+d} events={attributed_events} src {src}'.format(
                          src='/'.join('{:.1f}'.format(f / 1e6) for f in h['top_sources_hz'][:3]), **h)
                          for h in check.get('identified', [])]}
        lines.append('  {:<16} {:<8} {:<5} {}'.format(name, mark, str(check.get('passed', '-')), json.dumps(detail)))
    return '\n'.join(lines)


def _comparison_summary(result):
    mhz = lambda hz: '{:.2f}'.format(hz / 1e6)
    lines = ['antenna - terminated: median {:+.2f} dB over {} cells; IF responses differ {:.3f} dB RMS'.format(
        result['antenna_minus_terminated_median_db'], result['cells_compared'],
        result.get('if_response_agreement', {}).get('rms_db') or float('nan')),
        'external (arrive at the port, > {:g} dB above the terminated level):'.format(result['threshold_db'])]
    lines += ['  {}-{} MHz  peak {} MHz  +{:.1f} dB'.format(mhz(r['start_hz']), mhz(r['stop_hz']), mhz(r['peak_hz']),
                                                         r['peak_excess_db']) for r in result['external'][:15]]
    lines.append('internal (present with the termination):')
    lines += ['  {}-{} MHz  peak {} MHz  +{:.1f} dB over floor; antenna-terminated {}'.format(
        mhz(r['start_hz']), mhz(r['stop_hz']), mhz(r['peak_hz']), r['peak_excess_db'],
        'n/a' if r['antenna_minus_terminated_db'] is None else '{:+.1f} dB'.format(r['antenna_minus_terminated_db']))
        for r in result['internal'][:15]] or ['  none']
    if 'after_mixing_mask' in result:
        after = result['after_mixing_mask']
        lines.append('after masking identified mixing responses: {} external, {} intermittent cells; external:'.format(
            len(after['external']), after['intermittent_cells']))
        lines += ['  {}-{} MHz  peak {} MHz  +{:.1f} dB'.format(mhz(r['start_hz']), mhz(r['stop_hz']), mhz(r['peak_hz']),
                                                             r['peak_excess_db']) for r in after['external'][:10]]
    floor = result['terminated_floor']
    lines.append('intermittent with the termination (p90-median spread > typical + {:g} dB): {} cells'.format(
        result['threshold_db'], floor.get('intermittent_cells', 'n/a')))
    lines += ['  {}-{} MHz  peak {} MHz  +{:.1f} dB'.format(mhz(r['start_hz']), mhz(r['stop_hz']), mhz(r['peak_hz']),
                                                         r['peak_excess_db']) for r in result.get('internal_intermittent', [])[:8]]
    return '\n'.join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(prog='soapy_power_experiment', description=__doc__.split('\n')[0])
    sub = p.add_subparsers(dest='cmd')
    sub.required = True
    a = sub.add_parser('plan', help='write a 100-300 MHz experiment suite (JSON)')
    a.add_argument('device')
    a.add_argument('-O', '--output', required=True)
    a.add_argument('--start-mhz', type=float, default=BAND_HZ[0] / 1e6)
    a.add_argument('--stop-mhz', type=float, default=BAND_HZ[1] / 1e6)
    a.add_argument('--scan-seconds', type=float, default=60.0)
    a.add_argument('--tone-dbm', type=float, help='generator level; enables the settling sweep')
    a.add_argument('--tone-mhz', type=float, default=200.0)
    a.add_argument('--max-receiver-input-dbm', type=float, help='declared safe receiver input (required with --tone-dbm)')
    a.add_argument('--path-loss-db', type=float, help='generator-to-receiver loss (required with --tone-dbm)')
    a.add_argument('--antenna', default='RX2')
    a.add_argument('--gain', type=float, default=20.0)
    a.add_argument('--input', choices=INPUTS, default='antenna',
                   help='what is connected to the port (operator declaration)')
    a.add_argument('--no-telemetry', action='store_true', help='do not record per-hop telemetry snapshots')
    a.add_argument('--require-temperature', action='store_true',
                   help='reject a hop whose RFIC temperature is missing or older than 3 s')
    b = sub.add_parser('run', help='execute a suite with real hardware')
    b.add_argument('suite')
    b.add_argument('directory', help='new session directory')
    b.add_argument('--source-hook', help='executable implementing the SourceController protocol')
    b.add_argument('--retries', type=int, default=1)
    b.add_argument('--soapy-power', default=os.environ.get('SOAPY_POWER', 'soapy_power'))
    c = sub.add_parser('analyze', help='verify hashes; fit, independent holdout refit, settling, image')
    c.add_argument('directory')
    c.add_argument('--input', choices=INPUTS, help='declare the input of a suite planned without --input')
    d = sub.add_parser('calibration-config', help='write a validated tone auto-calibration config')
    d.add_argument('device')
    d.add_argument('-O', '--output', required=True)
    d.add_argument('--levels-dbm', type=float, nargs='+', required=True)
    d.add_argument('--max-receiver-input-dbm', type=float, required=True)
    d.add_argument('--path-loss-db', type=float, required=True)
    d.add_argument('--reference', required=True, help='source certificate / serials')
    d.add_argument('--reference-plane', required=True)
    d.add_argument('--antenna', default='RX2')
    d.add_argument('--gain', type=float, default=20.0)
    f = sub.add_parser('compare', help='terminated vs antenna session: internal vs external features')
    f.add_argument('terminated')
    f.add_argument('antenna')
    f.add_argument('-O', '--output')
    f.add_argument('--threshold-db', type=float, default=3.0)
    e = sub.add_parser('dry-run', help='full pipeline against a simulated rig (no hardware)')
    e.add_argument('directory', help='new session directory')
    args = p.parse_args(argv)
    try:
        if args.cmd == 'plan':
            suite = default_suite(args.device, args.tone_mhz * 1e6, args.tone_dbm, args.max_receiver_input_dbm,
                                  args.path_loss_db, scan_seconds=args.scan_seconds, antenna=args.antenna,
                                  gain_db=args.gain, band_hz=(args.start_mhz * 1e6, args.stop_mhz * 1e6),
                                  input_kind=args.input, telemetry=not args.no_telemetry,
                                  require_temperature=args.require_temperature)
            write_json(args.output, suite)
            return 0
        if args.cmd == 'calibration-config':
            write_json(args.output, calibration_config(args.device, args.levels_dbm, args.max_receiver_input_dbm,
                                                       args.path_loss_db, args.reference, args.reference_plane,
                                                       antenna=args.antenna, gain_db=args.gain))
            return 0
        if args.cmd == 'run':
            from soapypower.auto_calibration import SourceController, install_signal_handlers
            suite = load_json(args.suite)
            if shutil.which(args.soapy_power) is None:
                raise ValueError('acquisition executable not found: ' + args.soapy_power)
            needs = any(t.get('source', {}).get('state', 'off') != 'off' for t in suite['trials'])
            controller = SourceController(args.source_hook) if (needs or args.source_hook) else None
            install_signal_handlers()
            ledger = run_suite(suite, args.directory, controller, retries=args.retries, executable=args.soapy_power)
            return 0 if ledger['status'] == 'completed' else 1
        if args.cmd == 'compare':
            result = compare(args.terminated, args.antenna, args.output, args.threshold_db)
            print(_comparison_summary(result))
            return 0
        if args.cmd == 'analyze':
            report = analyze(args.directory, input_kind=args.input)
            print(_summary(report))
            return 0 if report['passed'] else 1
        rig = SimulatedRig()
        suite = default_suite('driver=simulated', tone_dbm=-40.0, max_receiver_input_dbm=-20.0, path_loss_db=1.0)
        run_suite(suite, args.directory, rig.source, rig.runner, data_source='simulated')
        report = analyze(args.directory)
        print(_summary(report))
        return 0 if report['passed'] else 1
    except KeyboardInterrupt:
        print('interrupted; inspect ledger.json', file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError) as error:
        print('soapy_power_experiment: {}'.format(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
