"""Numerical, artifact and offline workflow regression tests."""
import io
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from soapypower.psd import PSD
from soapypower.calibration import PowerCalibration
from soapypower.calibration_cli import build_profile, extract_context, main
from soapypower.files import loads_json, write_json, load_json
from soapypower.records import integrate_record, read_records, validate_record
from soapypower.writer import SoapyPowerBinFormat
import test_architecture as architecture
import test_audit as audit


def record(unit='mW/Hz', power=None):
    return dict(version=1, start_hz=100, stop_hz=104, step_hz=2, samples=64,
                unit=unit, power=[1, 3] if power is None else power,
                masked_bins=[], zero_power_bins=[])


class NumericalMaintenanceTests(unittest.TestCase):
    def test_representable_density_does_not_overflow_intermediate_squares(self):
        psd = PSD(64, 1024, fft_window='boxcar', log_scale=False, max_threads=1)
        self.addCleanup(psd.close)
        state = psd.set_center_freq(10000)
        for _ in range(8):
            psd.update(state, np.full(256, 1e153, dtype=np.complex128))
        _, powers = psd.result(state)
        self.assertTrue(np.isfinite(powers).all())
        self.assertAlmostEqual(np.sum(powers) * 16 / 1e306, 1, places=13)

    def test_unequal_record_means_match_independent_reference(self):
        psd = PSD(64, 1024, fft_window='boxcar', fft_overlap=0, log_scale=False, max_threads=1)
        self.addCleanup(psd.close)
        state = psd.set_center_freq(10000)
        psd.update(state, np.full(64, 2, dtype=complex))
        psd.update(state, np.full(192, 4, dtype=complex))
        _, powers = psd.result(state)
        self.assertAlmostEqual(np.sum(powers) * 16, 13, places=12)

    def test_calibration_lookup_does_not_require_excluded_nyquist_bin(self):
        data = architecture.profile()
        data['baseband_hz'] = [-496, 496]
        calibration = PowerCalibration(data)
        psd = PSD(64, 1024, dc_offset=512, dc_mask_width=1,
                  calibration=calibration, max_threads=1)
        self.addCleanup(psd.close)
        state = psd.set_center_freq(10000)
        psd.update(state, np.ones(128, dtype=complex))
        _, powers = psd.result(state)
        self.assertTrue(np.isnan(powers[0]))
        self.assertFalse(np.isnan(powers[1:]).any())

    def test_binary_short_writes_round_trip(self):
        class ShortWrites(io.BytesIO):
            def write(self, data):
                return super().write(data[:2])
        stream = ShortWrites()
        formatter = SoapyPowerBinFormat()
        formatter.write(stream, 0, 1, 100, 104, 2, 64, np.array([1., 3.]))
        stream.seek(0)
        np.testing.assert_array_equal(formatter.read(stream)[1], [1, 3])

    def test_binary_nonprogress_and_payload_limit_rejected(self):
        for count in (0, None, -1, True, 100000):
            stream = mock.Mock()
            stream.write.return_value = count
            with self.assertRaises(OSError):
                SoapyPowerBinFormat().write(stream, 0, 1, 100, 104, 2, 64, np.ones(2))
        for limit in (True, 7, 1.5):
            with self.assertRaises(ValueError):
                SoapyPowerBinFormat(max_payload_bytes=limit)

    def test_profile_identity_and_context_cannot_be_mutated_by_callers(self):
        calibration = PowerCalibration(architecture.profile())
        identity = calibration.profile_id
        calibration.context['gains']['overall'] = 999
        calibration.profile['points'][0][1] = 999
        calibration.validate_context(architecture.context())
        self.assertEqual(calibration.profile_id, identity)
        self.assertEqual(calibration.profile['points'][0][1], -30)


class AcquisitionMaintenanceTests(unittest.TestCase):
    setUp = audit.AcquisitionTests.setUp
    capable_receiver = architecture.HardwareIntegrationTests.capable_receiver

    def test_gain_change_during_read_cannot_publish_calibrated_power(self):
        sdr, output = self.capable_receiver()
        sdr.sweep(10000, 10000, 64, 4, runs=1, max_threads=1)
        calibration = PowerCalibration(architecture.profile(ctx=json.loads(output.getvalue())['metadata']['context']))
        output.seek(0)
        output.truncate()
        original = sdr.device.read_stream
        def changed():
            result = original()
            sdr.device.gain += 1
            return result
        with mock.patch.object(sdr.device, 'read_stream', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'gains'):
                sdr.sweep(10000, 10000, 64, 4, runs=1, max_threads=1, calibration=calibration)
        self.assertEqual(output.getvalue(), b'')
        self.assertFalse(sdr.device.is_streaming)


class ArtifactTests(unittest.TestCase):
    def test_strict_json_rejects_duplicate_keys_and_constants(self):
        for text in ('{"a":1,"a":2}', '{"x":{"a":1,"a":2}}', '{"a":NaN}', '{"a":Infinity}', '{"a":1e999}'):
            with self.assertRaises(ValueError):
                loads_json(text)

    def test_atomic_no_clobber_and_explicit_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'result.json'
            write_json(path, {'first': 1})
            with self.assertRaises(FileExistsError):
                write_json(path, {'second': 2})
            self.assertEqual(load_json(path), {'first': 1})
            write_json(path, {'second': 2}, overwrite=True)
            self.assertEqual(load_json(path), {'second': 2})
            with self.assertRaises(ValueError):
                write_json(path, {'bad': float('nan')}, overwrite=True)
            self.assertEqual(load_json(path), {'second': 2})
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_failed_publication_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch('soapypower.files.os.link', side_effect=OSError('unsupported')):
                with self.assertRaises(OSError):
                    write_json(Path(directory) / 'output.json', {})
            self.assertEqual(list(Path(directory).iterdir()), [])


class IntegrationTests(unittest.TestCase):
    def test_linear_and_log_integration_have_same_physical_power(self):
        linear = integrate_record(record())
        logarithmic = integrate_record(record('dBm/Hz', [0, 10 * math.log10(3)]))
        self.assertAlmostEqual(linear['power'], 8)
        self.assertAlmostEqual(logarithmic['power'], 8)
        self.assertAlmostEqual(linear['power_db'], 10 * math.log10(8))
        self.assertEqual(linear['db_unit'], 'dBm')
        self.assertEqual(integrate_record(record(), 102, 104)['measured_bins'], 1)

    def test_missing_power_requires_explicit_omission(self):
        data = record(power=[None, 3])
        data['masked_bins'] = [0]
        with self.assertRaisesRegex(ValueError, 'excluded'):
            integrate_record(data)
        result = integrate_record(data, omit_masked=True)
        self.assertFalse(result['complete'])
        self.assertEqual(result['excluded_bandwidth_hz'], 2)
        self.assertAlmostEqual(result['power'], 6)
        with self.assertRaises(ValueError):
            integrate_record(data, 100, 102, omit_masked=True)

    def test_true_zero_and_unrepresentable_linear_power_are_distinct(self):
        data = record('dBm/Hz', [None, None])
        data['zero_power_bins'] = [0, 1]
        self.assertTrue(integrate_record(data)['zero_power'])
        for value, status in ((4000, 'overflow'), (-4000, 'underflow'), (1e308, 'overflow')):
            result = integrate_record(record('dBm/Hz', [value, value]))
            self.assertIsNone(result['power'])
            self.assertFalse(result['zero_power'])
            self.assertEqual(result['linear_representation'], status)
            self.assertTrue(math.isclose(result['power_db'], value + 10 * math.log10(4), rel_tol=1e-14, abs_tol=1e-10))

    def test_invalid_record_geometry_and_null_classification(self):
        for changes in ({'stop_hz': 105}, {'samples': True}, {'power': [None, 1]},
                        {'masked_bins': [-1]}, {'zero_power_bins': [0]},
                        {'power': [float('nan'), 1]}, {'power': [-1, 1]}, {'unit': []}, {'start_hz': '100'}):
            data = record()
            data.update(changes)
            with self.assertRaises(ValueError):
                validate_record(data)

    def test_jsonl_errors_identify_line_and_empty_inputs_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'records.jsonl'
            path.write_text(json.dumps(record()) + '\n{}\n')
            with self.assertRaisesRegex(ValueError, 'line 2'):
                list(read_records(path))
            path.write_text('\n')
            with self.assertRaisesRegex(ValueError, 'no records'):
                list(read_records(path))


class OfflineWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.csv = self.root / 'measurements.csv'
        self.csv.write_text('rf_hz,baseband_hz,input_dbm,raw_power\n1000,-512,-30,1\n1000,512,-30,0.5\n2000,-512,-30,1\n2000,512,-30,0.5\n')

    def build(self, **kwargs):
        return build_profile(architecture.context(), self.csv, 'test reference', 'test input', -512, **kwargs)

    def test_measured_grid_normalization_and_provenance(self):
        calibration = self.build()
        np.testing.assert_allclose(calibration.offsets_db([1000, 2000], [512, -512]), [-30 + 10 * math.log10(2), -30])
        self.assertEqual(len(calibration.profile['measurements_sha256']), 64)

    def test_incomplete_grid_and_unmeasured_reference_rejected(self):
        with self.assertRaisesRegex(ValueError, 'reference baseband'):
            build_profile(architecture.context(), self.csv, 'test', 'input', 0)
        self.csv.write_text(self.csv.read_text().rsplit('2000,512', 1)[0])
        with self.assertRaisesRegex(ValueError, 'complete grid'):
            self.build()

    def test_repeated_cells_need_acceptance_limit_and_average_linear_response(self):
        with self.csv.open('a') as stream:
            stream.write('1000,-512,-30,2\n')
        with self.assertRaisesRegex(ValueError, 'explicit'):
            self.build()
        with self.assertRaisesRegex(ValueError, 'spread'):
            self.build(max_spread_db=1)
        self.assertAlmostEqual(self.build(max_spread_db=4).offsets_db([1000], [-512])[0], -30 - 10 * math.log10(1.5))

    def test_context_build_validate_integrate_cli_round_trip(self):
        data = record('input power/Hz')
        data['metadata'] = {'context': architecture.context(), 'calibration': None}
        capture = self.root / 'capture.jsonl'
        capture.write_text(json.dumps(data) + '\n')
        context_path, profile_path = self.root / 'context.json', self.root / 'profile.json'
        self.assertEqual(main(['context', str(capture), '-O', str(context_path)]), 0)
        self.assertEqual(main(['build', '--context', str(context_path), '--measurements', str(self.csv),
                               '--reference', 'test', '--reference-plane', 'input',
                               '--reference-baseband-hz=-512', '-O', str(profile_path)]), 0)
        with mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['validate', str(profile_path)]), 0)
            self.assertEqual(json.loads(output.getvalue())['context'], architecture.context())
        result = subprocess.run([sys.executable, '-m', 'soapypower.calibration_cli', 'integrate', str(capture)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertAlmostEqual(json.loads(result.stdout)['power'], 8)
        data['metadata']['context']['agc'] = True
        capture.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'AGC'):
            extract_context(capture)
