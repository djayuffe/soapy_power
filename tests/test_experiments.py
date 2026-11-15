"""Instrument control, experiment orchestration and model validation (no hardware)."""
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np

from soapypower import experiments as ex
from soapypower import model_validation as mv
from soapypower.instruments import InstrumentError, ScpiLink, SignalGenerator, SimulatedLink
import test_audit as audit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
H = lambda f: -1.5 * (f / 14e6) ** 2 + .3 * np.sin(2 * np.pi * f / 5e6)
FLAT = lambda f: 0 * f


def generator(**link_options):
    link = SimulatedLink(**link_options)
    return SignalGenerator(link, max_dbm=-10, min_hz=1e6, max_hz=3e9, sleep=lambda s: None), link


def scans(root, response=H, interleave=True, passes=3, seeds=(1, 2), **options):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    files = [root / 'a.jsonl']
    mv.synthetic_scan(files[0], 100e6, 200e6, 7e6, retained_hz=28e6, bins=1024, if_response=response,
                      passes=passes, seed=seeds[0], **options)
    if interleave:
        files.append(root / 'b.jsonl')
        mv.synthetic_scan(files[1], 100e6, 200e6, 7e6, retained_hz=28e6, bins=1024, if_response=response,
                          passes=passes, seed=seeds[1], offset_hz=3.3e6, **options)
    return files


def store(files, **options):
    return mv.HopStore.from_files(files, 100e6, 200e6, 100e3, edge_fraction=options.pop('edge_fraction', 0),
                                  dc_mask_hz=options.pop('dc_mask_hz', 500e3), **options)


class GeneratorTests(unittest.TestCase):
    def test_tone_sequence_is_clear_off_set_verify_on(self):
        g, link = generator(stale_error='-113,"Undefined header"')
        result = g.apply('on', 200e6, -40.0)
        self.assertEqual((link.state['OUTP'], result['output']), (1, 'on'))
        writes = [c for c in link.log if not c.endswith('?')]
        self.assertEqual(writes[0], '*CLS')  # stale queue cleared before this session's checks
        self.assertLess(writes.index('OUTP OFF'), next(i for i, c in enumerate(writes) if c.startswith('FREQ')))
        self.assertLess(next(i for i, c in enumerate(writes) if c.startswith('POW')), writes.index('OUTP ON'))

    def test_limits_reject_before_any_rf(self):
        g, link = generator()
        for hz, dbm in ((200e6, -5.0), (1e5, -40.0), (4e9, -40.0), (float('nan'), -40.0)):
            with self.assertRaises(ValueError):
                g.apply('on', hz, dbm)
        self.assertEqual(link.state['OUTP'], 0)
        self.assertNotIn('OUTP ON', link.log)

    def test_readback_mismatch_and_error_queue_leave_output_off(self):
        for options, message in (({'drift_db': 0.5}, 'readback'), ({'error': '-221,"Settings conflict"'}, 'conflict')):
            g, link = generator(**options)
            with self.assertRaisesRegex(InstrumentError, message):
                g.apply('on', 200e6, -40.0)
            self.assertEqual(link.state['OUTP'], 0)

    def test_failed_cleanup_is_reported_with_the_original_error(self):
        class Sticky(SimulatedLink):
            def write(self, command):
                if command.startswith('POW '):  # relay welds after the level change
                    self.stuck_on, self.state['OUTP'] = True, 1
                super().write(command)
        link = Sticky(drift_db=0.5)
        g = SignalGenerator(link, -10, 1e6, 3e9, sleep=lambda s: None)
        with self.assertRaisesRegex(InstrumentError, 'readback mismatch.*RF OFF could not be verified'):
            g.apply('on', 200e6, -40.0)

    def test_output_that_will_not_switch_off_is_reported(self):
        with self.assertRaisesRegex(InstrumentError, 'off'):
            SignalGenerator(SimulatedLink(stuck_on=True, output=1), -10, 1e6, 3e9, sleep=lambda s: None)

    def test_thermal_states_identity_and_off_level_are_refused(self):
        g, _ = generator()
        for state in ('hot', 'cold', 'terminated'):
            with self.assertRaisesRegex(ValueError, 'cannot realise'):
                g.apply(state, 1e8)
        with self.assertRaisesRegex(ValueError, 'no level'):
            g.apply('off', 1e8, -40.0)
        with self.assertRaisesRegex(InstrumentError, 'identity'):
            SignalGenerator(SimulatedLink(), -10, 1e6, 3e9, expected_idn='Keysight', sleep=lambda s: None)

    def test_hook_protocol_reply_and_safe_refusal(self):
        import scpi_source_hook as hook
        link = SimulatedLink()
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'c.json'
            config.write_text(json.dumps({'host': 'x', 'max_dbm': -10, 'min_hz': 1e6, 'max_hz': 3e9, 'settle_s': 0.001}))
            request = {'version': 1, 'request_id': 'r1', 'state': 'on', 'rf_hz': 150e6, 'input_dbm': -35.0}
            out = io.StringIO()
            with mock.patch.dict(os.environ, {'SCPI_SOURCE_CONFIG': str(config)}):
                self.assertEqual(hook.main(io.StringIO(json.dumps(request) + '\n'), out, lambda *a: link), 0)
            reply = json.loads(out.getvalue())
            self.assertTrue(reply['confirmed'])
            self.assertEqual({k: reply[k] for k in request}, request)
            self.assertEqual(len(reply['config_sha256']), 64)
            self.assertAlmostEqual(reply['readback']['level_dbm'], -35.0)
            self.assertEqual(link.state['OUTP'], 1)
            with mock.patch.dict(os.environ, {'SCPI_SOURCE_CONFIG': str(config)}), mock.patch('sys.stderr'):
                bad = dict(request, request_id='r2', input_dbm=-5.0)
                self.assertEqual(hook.main(io.StringIO(json.dumps(bad) + '\n'), io.StringIO(), lambda *a: link), 2)
                self.assertEqual(link.state['OUTP'], 0)
                config.write_text(json.dumps({'host': 'x', 'max_dbm': -10, 'min_hz': 1e6}))
                self.assertEqual(hook.main(io.StringIO(json.dumps(request) + '\n'), io.StringIO(), lambda *a: link), 2)


class _Server:
    """Raw-socket SCPI server around a SimulatedLink."""

    def __init__(self, link):
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self.loop, args=(link,), daemon=True).start()

    def loop(self, link):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection:
                buffer = b''
                while True:
                    data = connection.recv(4096)
                    if not data:
                        break
                    buffer += data
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        link.write(line.decode())
                        while link._reply:
                            connection.sendall(link.read().encode() + b'\n')

    def close(self):
        self.server.close()


class SocketTests(unittest.TestCase):
    """Real TCP, and the real hook subprocess behind the production SourceController."""

    def setUp(self):
        self.link = SimulatedLink()
        self.server = _Server(self.link)
        self.addCleanup(self.server.close)

    def test_scpi_over_tcp(self):
        g = SignalGenerator(ScpiLink('127.0.0.1', self.server.port, timeout=2), -10, 1e6, 3e9, settle_s=0.001)
        g.apply('on', 120e6, -50.0)
        self.assertEqual(self.link.state['OUTP'], 1)
        g.close(off=True)
        self.assertEqual(self.link.state['OUTP'], 0)

    @unittest.skipIf(os.name == 'nt', 'POSIX executable hook wrapper')
    def test_hook_subprocess_through_source_controller(self):
        from soapypower.auto_calibration import SourceController
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'c.json'
            config.write_text(json.dumps({'host': '127.0.0.1', 'port': self.server.port, 'max_dbm': -10,
                                          'min_hz': 1e6, 'max_hz': 3e9, 'settle_s': 0.001, 'timeout': 2}))
            wrapper = Path(temp) / 'hook'
            wrapper.write_text('#!/bin/sh\nexec "{}" "{}"\n'.format(sys.executable, ROOT / 'tools' / 'scpi_source_hook.py'))
            wrapper.chmod(0o755)
            with mock.patch.dict(os.environ, {'SCPI_SOURCE_CONFIG': str(config)}):
                controller = SourceController(wrapper, timeout=30)
                ack = controller('on', 150e6, -40.0)
                self.assertTrue(ack['confirmed'])
                self.assertEqual(ack['reply']['readback']['output'], 'on')  # evidence kept by the controller
                self.assertEqual((self.link.state['OUTP'], self.link.state['POW']), (1, -40.0))
                controller('off', 150e6)
                self.assertEqual(self.link.state['OUTP'], 0)
                with self.assertRaises(ValueError):
                    controller('on', 150e6, -5.0)
                self.assertEqual(self.link.state['OUTP'], 0)


class SolverTests(unittest.TestCase):
    def test_additive_fit_is_exact_on_gapped_noiseless_data(self):
        rng = np.random.default_rng(3)
        rows, cols = rng.normal(size=60), rng.normal(size=25)
        m = np.add.outer(rows, cols)
        m[rng.random(m.shape) < .3] = np.nan
        r, c, residual = mv.additive_fit(m)
        self.assertLess(np.nanmax(np.abs(residual)), 1e-10)
        np.testing.assert_allclose(c - c[0], cols - cols[0], atol=1e-10)

    def test_robust_fit_ignores_a_strong_localized_outlier(self):
        rng = np.random.default_rng(4)
        cols = np.linspace(-1, 1, 20)
        m = np.add.outer(rng.normal(size=80), cols) + rng.normal(0, .01, (80, 20))
        m[5, 7] += 30
        _, c, _ = mv.additive_fit(m)
        self.assertLess(np.max(np.abs((c - c[0]) - (cols - cols[0]))), .01)

    def test_components_are_counted(self):
        m = np.full((4, 4), np.nan)
        m[0, :2] = m[1, :2] = 1
        m[2, 2:] = m[3, 2:] = 1
        self.assertEqual(sorted(set(mv.component_labels(m))), [0, 1])


class ModelTests(unittest.TestCase):
    def test_interleaved_grids_identify_the_response_a_periodic_grid_does_not(self):
        with tempfile.TemporaryDirectory() as temp:
            periodic = mv.fit_if_response(store(scans(Path(temp) / 'p', interleave=False)))
            self.assertFalse(periodic['fully_identified'])
            self.assertLess(periodic['identified_slots'], periodic['observed_slots'])
            # Unlinked slots are not reported as numbers relative to the anchor.
            self.assertEqual(sum(v is not None for v in periodic['response_db']), periodic['identified_slots'])
            fit = mv.fit_if_response(store(scans(Path(temp) / 'i')))
            self.assertTrue(fit['fully_identified'])
            axis, got = np.array(fit['if_axis_hz']), mv._array(fit['response_db'])
            truth = H(axis) - H(fit['anchor_if_hz'])
            ok = np.isfinite(got)
            self.assertEqual(ok.sum(), fit['observed_slots'])
            self.assertLess(np.max(np.abs(got[ok] - truth[ok])), .1)

    def test_recorded_frequency_grid_is_used_and_legacy_records_fall_back(self):
        with tempfile.TemporaryDirectory() as temp:
            modern = store(scans(Path(temp) / 'm', interleave=False, passes=1))
            legacy = store(scans(Path(temp) / 'l', interleave=False, passes=1, frequency_grid=False))
            self.assertEqual(modern.origin_sources, {'frequency_grid'})
            self.assertEqual(legacy.origin_sources, {'coverage_midpoint'})
            step = 28e6 / 512
            for (key, a), b in zip(sorted(modern.hops.items()), sorted(h['center'] for h in legacy.hops.values())):
                self.assertAlmostEqual(a['center'] - b, step / 2)

    def test_estimated_dc_bin_is_not_treated_as_a_measurement(self):
        with tempfile.TemporaryDirectory() as temp:
            path = scans(temp, interleave=False, passes=1)[0]
            s = mv.HopStore.from_files(path, 100e6, 200e6, 10e3, edge_fraction=0)
            centers = s.cell_center(np.arange(s.cells))
            for hop in s.hops.values():
                cell = int(np.argmin(np.abs(centers - hop['center'])))
                level = hop['passes'][0][cell]
                self.assertTrue(np.isnan(level) or level > -140)  # spur present, floor-valued estimate excluded

    def test_holdout_passes_for_truth_and_for_a_flat_receiver(self):
        with tempfile.TemporaryDirectory() as temp:
            for name, response in (('ripple', H), ('flat', FLAT)):
                fit = store(scans(Path(temp) / (name + '-fit'), response))
                hold = store(scans(Path(temp) / (name + '-hold'), response, seeds=(7, 8)))
                check = mv.holdout_validation(fit, hold)
                self.assertTrue(check['passed'], name)
                self.assertLess(check['agreement']['rms_db'], .1)
                self.assertLessEqual(check['spread_after_db'], check['max_allowed_spread_db'])
            self.assertLess(check['improvement_db'], .05)  # flat: valid, nothing to correct

    def test_holdout_fails_when_the_receiver_response_changed(self):
        with tempfile.TemporaryDirectory() as temp:
            fit = store(scans(Path(temp) / 'fit', H))
            hold = store(scans(Path(temp) / 'hold', lambda f: H(f) + 1.0 * (f / 14e6), seeds=(7, 8)))
            check = mv.holdout_validation(fit, hold)
            self.assertFalse(check['passed'])
            self.assertGreater(check['agreement']['rms_db'], .25)

    def test_single_session_report(self):
        with tempfile.TemporaryDirectory() as temp:
            report = mv.validate_scan(scans(temp, passes=4), 100e6, 200e6, edge_fraction=0, dc_mask_hz=500e3)
            self.assertTrue(report['passed'])
            self.assertFalse(report['calibrated'])
            self.assertTrue(report['repeatability']['passed'])

    def test_context_change_agc_and_tune_args_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'a.jsonl'
            mv.synthetic_scan(path, 100e6, 110e6, 7e6, retained_hz=28e6, bins=256, passes=1)
            lines = path.read_text().splitlines()

            def variant(index, **context):
                record = json.loads(lines[index])
                record['metadata']['context'].update(context)
                return json.dumps(record)
            for content, message in ((variant(0, agc=True), 'fixed-gain'),
                                     (lines[0] + '\n' + variant(1, gains={'PGA': 30.0}), 'context changed'),
                                     (variant(0, tune_args='OFFSET=1e6'), 'tuning arguments')):
                path.write_text(content + '\n')
                with self.assertRaisesRegex(ValueError, message):
                    mv.HopStore.from_files(path, 100e6, 110e6, 100e3, dc_mask_hz=500e3)

    def test_settling_choice_is_verified_on_unseen_delays(self):
        delays = [0, .002, .005, .01, .02, .04, .08, .2]
        chosen = mv.choose_delay(delays, [-1.5 * np.exp(-d / .008) for d in delays], tolerance_db=.1)
        self.assertTrue(chosen['passed'])
        self.assertAlmostEqual(chosen['model']['tau_s'], .008, delta=5e-4)
        self.assertFalse(mv.choose_delay(delays, [-1.5 + .001 * i for i in range(8)], tolerance_db=.1)['passed'])
        slow = mv.choose_delay(delays, [-3 * np.exp(-d / .1) for d in delays], tolerance_db=.1)
        self.assertEqual(slow['status'], 'reference_not_settled')
        self.assertFalse(slow['passed'])
        self.assertEqual(mv.choose_delay(delays, [0.0] * 8)['status'], 'chosen')

    def test_iq_model_recovers_image_and_dc(self):
        rng = np.random.default_rng(0)
        n, w = np.arange(8192), 0.1
        d = .02 - .01j
        y = np.exp(.3j) * np.exp(2j * np.pi * w * n) + .01 * np.exp(-1j) * np.exp(-2j * np.pi * w * n) + d
        y = y + 1e-4 * (rng.normal(size=n.size) + 1j * rng.normal(size=n.size))
        fit = mv.fit_iq_model(y, w)
        self.assertAlmostEqual(fit['image_rejection_db'], 40.0, delta=.1)
        self.assertAlmostEqual(abs(complex(fit['dc_real'], fit['dc_imag']) - d), 0, delta=1e-4)

    def test_image_rejection_from_a_scan_and_floor_limited_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            for irr, tone, measured in ((30.0, 50.0, True), (60.0, 40.0, False)):
                path = Path(temp) / '{}.jsonl'.format(irr)
                mv.synthetic_scan(path, 150e6, 250e6, 7e6, bins=4096, passes=1, tones=[(200.3e6, tone)],
                                  image_rejection_db=irr)
                result = mv.image_rejection_from_scan(path, 200.3e6, 40e3)
                if measured:
                    self.assertGreater(result['measured_hops'], 3)
                    self.assertAlmostEqual(result['median_image_rejection_db'], irr, delta=1.0)
                else:
                    self.assertEqual(result['measured_hops'], 0)
                    self.assertLess(result['min_lower_bound_db'], irr)  # a bound, never an overstatement
                    self.assertGreater(result['min_lower_bound_db'], 30)


class OrchestrationTests(unittest.TestCase):
    setUp = audit.AcquisitionTests.setUp

    def test_hop_plan_matches_the_acquisition_planner_exactly(self):
        self.sdr.device.sample_rate = 56e6
        for start, stop in ((100e6, 300e6), (100e6 - 3493164.0625, 300e6), (150e6, 154e6)):
            plan = ex.hop_plan(start, stop)
            overlap = self.sdr.nearest_overlap(ex.OVERLAP_PERCENT / 100, 8192)
            self.assertEqual(plan['centers_hz'], self.sdr.freq_plan(start, stop, 8192, overlap, quiet=True))
        self.assertEqual((ex.hop_plan(100e6, 300e6)['retained_bins'], ex.hop_plan(100e6, 300e6)['hops']), (1022, 29))

    def test_argv_is_accepted_by_the_real_cli_parser(self):
        from soapypower.__main__ import setup_argument_parser
        argv = ex.scan_argv('driver=uhd,type=b200', 96.5e6, 300e6, 60, 'x.jsonl', tune_delay_s=0.005, settle_samples=0)
        args = setup_argument_parser().parse_args(argv[1:])
        self.assertEqual((args.freq[0], args.freq[1]), (96.5e6, 300e6))
        self.assertEqual((args.tune_delay, args.settle_samples, args.overlap), (0.005, 0, ex.OVERLAP_PERCENT))
        for flag in ('--require-timestamps', '--partial-policy', '--lock-sensor', '--reopen-stream'):
            self.assertIn(flag, argv)


class SuiteTests(unittest.TestCase):
    def test_suite_interleaves_shifted_grids_and_isolates_tune_delay(self):
        suite = ex.default_suite('driver=uhd', tone_dbm=-40.0, max_receiver_input_dbm=-20.0, path_loss_db=1.0)
        starts = {t['grid']: t['start_hz'] for t in suite['trials'] if t['role'] == 'fit'}
        self.assertAlmostEqual(starts['A'] - starts['B'], suite['geometry']['hop_hz'] / 2)
        settling = [t for t in suite['trials'] if t['role'] == 'settling']
        self.assertEqual([t['tune_delay_s'] for t in settling], list(ex.DEFAULT_DELAYS_S))
        self.assertTrue(all(t['settle_samples'] == 0 and t['start_hz'] < 200e6 < t['stop_hz'] for t in settling))
        self.assertEqual(suite['source_limits']['receiver_input_dbm'], -41.0)

    def test_tone_requires_declared_limits_and_respects_the_ceiling(self):
        with self.assertRaisesRegex(ValueError, 'ceiling and path loss'):
            ex.default_suite('d', tone_dbm=-40.0)
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            ex.default_suite('d', tone_dbm=-10.0, max_receiver_input_dbm=-20.0, path_loss_db=1.0)
        with self.assertRaisesRegex(ValueError, 'inside the band'):
            ex.default_suite('d', tone_rf_hz=400e6, tone_dbm=-40.0, max_receiver_input_dbm=-20.0, path_loss_db=0)

    def test_calibration_config_validates_and_demands_physical_facts(self):
        config = ex.calibration_config('driver=uhd', [-60, -50, -40], -20, 0.5, 'cert 123', 'RX2 connector')
        self.assertFalse(config['physical_setup_confirmed'])
        with self.assertRaises(ValueError):
            ex.calibration_config('driver=uhd', [-60, -50, -10], -30, 0.0, 'cert', 'RX2')


class RunTests(unittest.TestCase):
    def run_rig(self, rig, suite, **options):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name) / 's'
        options.setdefault('data_source', 'simulated')
        return ex.run_suite(suite, directory, rig.source, rig.runner, **options), directory

    def test_dry_run_recovers_truth_and_leaves_source_off(self):
        rig = ex.SimulatedRig()
        suite = ex.default_suite('driver=simulated', tone_dbm=-40.0, max_receiver_input_dbm=-20.0, path_loss_db=1.0)
        ledger, directory = self.run_rig(rig, suite)
        self.assertEqual((ledger['status'], rig.source_state['state'], ledger['final_source_state']), ('completed', 'off', 'off'))
        report = ex.analyze(directory)
        self.assertTrue(report['passed'], json.dumps(report['checks'], default=str)[:1500])
        self.assertEqual(report['data_source'], 'simulated')
        self.assertIn('simulation', report['evidence_level'])
        self.assertAlmostEqual(report['checks']['settling']['model']['tau_s'], .008, delta=2e-4)
        self.assertAlmostEqual(report['checks']['image_rejection']['median_image_rejection_db'], 35.0, delta=1.0)
        self.assertTrue(json.loads((directory / 'if_response.json').read_text())['validated'])

    def test_tampered_evidence_is_refused(self):
        suite = ex.default_suite('driver=simulated')
        ledger, directory = self.run_rig(ex.SimulatedRig(), suite)
        path = directory / ledger['trials'][0]['attempts'][-1]['file']
        path.write_text(path.read_text().replace('"samples": 65536', '"samples": 65535', 1))
        with self.assertRaisesRegex(ValueError, 'changed after acquisition'):
            ex.analyze(directory)

    def test_failed_trial_is_retried_and_recorded_not_hidden(self):
        suite = ex.default_suite('driver=simulated')
        suite['trials'] = suite['trials'][:1]
        ledger, _ = self.run_rig(ex.SimulatedRig(fail_first=True), suite, retries=1)
        self.assertEqual([a['status'] for a in ledger['trials'][0]['attempts']], ['failed', 'accepted'])

    def test_exhausted_retries_abort_and_source_is_turned_off(self):
        rig = ex.SimulatedRig()
        suite = ex.default_suite('driver=simulated', tone_dbm=-40.0, max_receiver_input_dbm=-20.0, path_loss_db=1.0)
        suite['trials'] = [t for t in suite['trials'] if t['role'] == 'settling'][:2]

        def explode(trial, argv, output, timeout):
            raise RuntimeError('USB disconnect')
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(RuntimeError):
                ex.run_suite(suite, Path(temp) / 's', rig.source, explode, retries=0)
            self.assertEqual(rig.source_state['state'], 'off')
            ledger = json.loads((Path(temp) / 's' / 'ledger.json').read_text())
            self.assertEqual((ledger['status'], ledger['final_source_state']), ('failed', 'off'))
            with self.assertRaisesRegex(ValueError, 'completed'):
                ex.analyze(Path(temp) / 's')

    def test_interrupt_marks_attempt_and_turns_source_off(self):
        rig = ex.SimulatedRig()
        suite = ex.default_suite('driver=simulated', tone_dbm=-40.0, max_receiver_input_dbm=-20.0, path_loss_db=1.0)
        suite['trials'] = [t for t in suite['trials'] if t['role'] == 'settling'][:1]

        def interrupt(trial, argv, output, timeout):
            raise KeyboardInterrupt
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(KeyboardInterrupt):
                ex.run_suite(suite, Path(temp) / 's', rig.source, interrupt)
            ledger = json.loads((Path(temp) / 's' / 'ledger.json').read_text())
            self.assertEqual(ledger['trials'][0]['attempts'][0]['status'], 'interrupted')
            self.assertEqual(rig.source_state['state'], 'off')

    def test_source_required_but_missing_fails_closed(self):
        suite = ex.default_suite('driver=simulated', tone_dbm=-40.0, max_receiver_input_dbm=-20.0, path_loss_db=1.0)
        suite['trials'] = [t for t in suite['trials'] if t['role'] == 'settling'][:1]
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(RuntimeError):
            ex.run_suite(suite, Path(temp) / 's', None, ex.SimulatedRig().runner, retries=0)

    def test_session_directory_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(FileExistsError):
            ex.run_suite(ex.default_suite('d'), temp, None)


class CliTests(unittest.TestCase):
    def test_plan_calibration_config_and_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            suite = Path(temp) / 'suite.json'
            self.assertEqual(ex.main(['plan', 'driver=uhd', '-O', str(suite), '--tone-dbm', '-40',
                                      '--max-receiver-input-dbm', '-20', '--path-loss-db', '1']), 0)
            self.assertEqual(json.loads(suite.read_text())['kind'], 'experiment_suite')
            config = Path(temp) / 'cal.json'
            self.assertEqual(ex.main(['calibration-config', 'driver=uhd', '-O', str(config), '--levels-dbm', '-60',
                                      '-50', '-40', '--max-receiver-input-dbm', '-20', '--path-loss-db', '0.5',
                                      '--reference', 'cert', '--reference-plane', 'RX2']), 0)
            with mock.patch('sys.stderr'):
                self.assertEqual(ex.main(['plan', 'driver=uhd', '-O', str(Path(temp) / 'x.json'), '--tone-dbm', '-40']), 2)
                self.assertEqual(ex.main(['run', str(suite), str(Path(temp) / 'r'), '--soapy-power', 'no-such-binary']), 2)
            self.assertFalse((Path(temp) / 'r').exists())


if __name__ == '__main__':
    unittest.main()
