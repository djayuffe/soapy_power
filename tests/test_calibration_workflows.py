"""Synthetic thermal/tone workflows; coefficients are never physical fixtures."""
import copy
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np
from soapypower.calibration_workflows import (BOLTZMANN, thermal_reference, effective_temperature,
    noise_solution, average_capture, compare_captures, build_manifest)
from soapypower.calibration_cli import main
from soapypower.calibration import PowerCalibration
import test_architecture as architecture


class ThermalTests(unittest.TestCase):
    def test_75_ohm_termination_is_conditional_not_absolute(self):
        model = thermal_reference(290, 75, 50)
        self.assertAlmostEqual(model['ideal_resistive_transfer'], 0.96)
        self.assertAlmostEqual(model['ideal_mismatch_loss_db'], 0.1772876696)
        self.assertAlmostEqual(model['available_thermal_density_dbm_hz'], -173.9751871942)
        self.assertFalse(model['absolute_receiver_calibration'])
        self.assertAlmostEqual(effective_temperature(290, 20, 290), 290)
        self.assertAlmostEqual(effective_temperature(1000, 10, 300), 370)

    def test_y_factor_gain_and_temperature_with_actual_cold_temperature(self):
        gain, tc, th, te = 1e15, 303.15, 3203.15, 580
        solution = noise_solution(BOLTZMANN*(tc+te)*gain, BOLTZMANN*(th+te)*gain, tc, th)
        self.assertAlmostEqual(solution['offset_db'], -120)
        self.assertAlmostEqual(solution['receiver_temperature_k'], te)
        self.assertAlmostEqual(solution['noise_figure_db'], 10*math.log10(3))
        for args in ((1, 1, 290, 1000), (1, 1.001, 290, 1000), (1, 100, 290, 1000)):
            with self.assertRaises(ValueError):
                noise_solution(*args)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def capture(self, name, rf=10000, bb=128, density=1e-6, tone=0, **changes):
        center = rf-bb
        first = center-512
        powers = np.full(64, density)
        powers[int(round((rf-first)/16))] += tone/16
        metadata = dict(fft_bins=64, sample_rate_hz=1024, window='boxcar', overlap_samples=0,
                        detrend=None, dc_interpolated=False, dc_mask_width_hz=0, dc_offset_hz=0,
                        precision='double', iq_conjugate=False, frequency_sign=1,
                        segment_count=100, measurement_status='complete', tuned_center_hz=center,
                        calibration=None, context=architecture.context())
        metadata.update(changes)
        data = dict(version=2, start_hz=first, stop_hz=first+1024, step_hz=16,
                    first_bin_center_hz=first, bin_width_hz=16, coverage_start_hz=first-8,
                    coverage_stop_hz=first+1016, samples=6400, unit='input power/Hz',
                    power=powers.tolist(), masked_bins=[], zero_power_bins=[], estimated_bins=[], metadata=metadata)
        (self.root/name).write_text(json.dumps(data)+'\n')
        return data

    def manifest(self, method='tone'):
        result = dict(version=1, method=method, reference='SYNTHETIC TEST ONLY', reference_plane='simulated',
                      reference_baseband_hz=128, assumptions_confirmed=True, cells=[])
        for rf in (10000, 20000):
            for bb in (-128, 128):
                tag = '{}_{}'.format(rf, bb)
                cell = dict(rf_hz=rf, baseband_hz=bb, start_hz=rf-8, stop_hz=rf+8)
                if method == 'tone':
                    cell.update(on=tag+'_on.jsonl', off=tag+'_off.jsonl', input_dbm=-20, path_loss_db=10)
                    self.capture(cell['on'], rf, bb, tone=1)
                    self.capture(cell['off'], rf, bb)
                else:
                    cell.update(cold=tag+'_cold.jsonl', cold_temperature_k=300, source_ohms=50, receiver_ohms=50)
                    self.capture(cell['cold'], rf, bb, density=BOLTZMANN*(300+580)*1e15)
                    if method == 'hot-cold':
                        cell.update(hot=tag+'_hot.jsonl', enr_db=10)
                        self.capture(cell['hot'], rf, bb, density=BOLTZMANN*(3200+580)*1e15)
                    else:
                        cell['noise_figure_db'] = 10*math.log10(3)
                result['cells'].append(cell)
        return result

    def build(self, manifest):
        path = self.root/'manifest.json'
        path.write_text(json.dumps(manifest))
        return build_manifest(path)

    def test_all_three_methods_produce_settings_matched_profiles(self):
        for method, offset in (('tone', -30), ('hot-cold', -120), ('known-nf', -120)):
            profile = self.build(self.manifest(method))
            np.testing.assert_allclose(profile.offsets_db([10000, 20000], [-128, 128]), offset)
            self.assertEqual(profile.profile['method'], method)
            self.assertEqual(len(profile.profile['method_details']['capture_sha256']), 8 if method != 'known-nf' else 4)
            profile.validate_context(architecture.context())

    def test_partial_unknown_integrity_and_changed_processing_are_rejected(self):
        manifest = self.manifest()
        name = manifest['cells'][0]['on']
        for changes in ({'measurement_status': 'partial_deadline'}, {'detrend': 'constant'}, {'tuned_center_hz': 1}):
            cell = manifest['cells'][0]
            self.capture(name, cell['rf_hz'], cell['baseband_hz'], tone=1, **changes)
            with self.assertRaises(ValueError):
                self.build(manifest)
        self.capture(name, 10000, -128, tone=1, window='hann')
        with self.assertRaisesRegex(ValueError, 'DSP'):
            self.build(manifest)

    def test_low_snr_and_incomplete_grid_rejected(self):
        manifest = self.manifest()
        manifest['min_snr_db'] = 100
        with self.assertRaisesRegex(ValueError, 'SNR'):
            self.build(manifest)
        manifest.pop('min_snr_db')
        manifest['cells'].pop()
        with self.assertRaisesRegex(ValueError, 'complete grid'):
            self.build(manifest)

    def test_mismatch_and_noise_assumptions_are_explicit(self):
        manifest = self.manifest('known-nf')
        manifest['assumptions_confirmed'] = False
        with self.assertRaisesRegex(ValueError, 'assumptions'):
            self.build(manifest)
        manifest['assumptions_confirmed'] = True
        manifest['cells'][0]['source_ohms'] = 75
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            self.build(manifest)
        manifest['accept_ideal_mismatch'] = True
        self.assertEqual(self.build(manifest).profile['method'], 'known-nf')

    def test_baseline_compare_and_cli_build_without_sdr(self):
        self.capture('terminated.jsonl')
        self.capture('antenna.jsonl', density=2e-6)
        comparison = compare_captures(self.root/'terminated.jsonl', self.root/'antenna.jsonl')
        self.assertAlmostEqual(comparison['ratio_db'], 10*math.log10(2))
        self.assertFalse(comparison['absolute_calibration'])
        with mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(main(['baseline', str(self.root/'terminated.jsonl'), '--temperature-k', '293.15',
                                   '--source-ohms', '75', '--receiver-ohms', '50']), 0)
            self.assertFalse(json.loads(out.getvalue())['absolute_receiver_calibration'])
        manifest = self.manifest('tone')
        self.build(manifest)
        path = self.root/'result.json'
        self.assertEqual(main(['from-captures', str(self.root/'manifest.json'), '-O', str(path)]), 0)
        self.assertEqual(PowerCalibration.load(path).profile['method'], 'tone')

    def test_segment_weighted_linear_average_not_average_db(self):
        data = self.capture('average.jsonl', density=1)
        second = copy.deepcopy(data)
        second['power'] = [3.0]*64
        second['metadata']['segment_count'] = 300
        with (self.root/'average.jsonl').open('a') as stream:
            stream.write(json.dumps(second)+'\n')
        result = average_capture(self.root/'average.jsonl')
        np.testing.assert_allclose(result['power'], 2.5)


class RTLSettingsTests(unittest.TestCase):
    def test_hidden_digital_agc_and_testmode_cannot_calibrate(self):
        for name in ('digital_agc', 'if_agc', 'testmode'):
            data = architecture.profile()
            data['context']['device_settings'][name] = 'true'
            with self.assertRaisesRegex(ValueError, name):
                PowerCalibration(data)

    def test_all_advertised_settings_are_snapshotted(self):
        from types import SimpleNamespace
        from soapypower.frontend import Frontend
        raw = SimpleNamespace(getSettingInfo=lambda: [SimpleNamespace(key='digital_agc'), SimpleNamespace(key='biastee')],
                              readSetting=lambda key: 'false')
        wrapper = SimpleNamespace(device=raw)
        self.assertEqual(Frontend(wrapper, 1, 0).context({}, {}, 0, False, False)['device_settings'],
                         {'digital_agc': 'false', 'biastee': 'false'})
