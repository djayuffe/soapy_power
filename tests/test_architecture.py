"""Architecture, DC, calibration and hardware-control regression tests."""
import copy
import datetime
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from soapypower.calibration import PowerCalibration, CONTEXT_FIELDS
from soapypower.psd import PSD
from soapypower import writer
from soapypower.frontend import Frontend
from soapypower.__main__ import setup_argument_parser, validate_arguments
import test_audit as audit


def context():
    result = dict(hardware='SimulatedReceiver', driver='test', serial='TEST-001', channel=0,
                  antenna='RX', sample_rate=1024.0, bandwidth=1024.0, gains={'overall': 20.7},
                  agc=False, dc_correction=True, iq_correction=True, device_settings={},
                  tune_args={}, lnb_lo=0.0, lnb_high_side=False, iq_conjugate=False,
                  corr_ppm=0.0, sample_format='CF32', clock_source=None, time_source=None,
                  master_clock_rate=None, hardware_info={'serial': 'TEST-001'})
    assert set(result) == CONTEXT_FIELDS
    return result


def profile(offset=-30.0, ctx=None, points=None):
    return dict(version=1, units='dBm', reference='Synthetic known-tone reference, for tests only',
                reference_plane='simulated RF input', context=ctx or context(),
                points=points or [[0, offset], [20000, offset]], uncertainty_db=0.5,
                reference_baseband_hz=0.0, baseband_hz=[-512.0, 512.0],
                baseband_correction_db=[[0.0, 0.0], [0.0, 0.0]])


class CalibrationTests(unittest.TestCase):
    def test_known_tone_integral_in_milliwatts_and_dbm(self):
        calibration = PowerCalibration(profile())
        for log in (False, True):
            psd = PSD(64, 1024, fft_window='boxcar', calibration=calibration, log_scale=log, max_threads=1)
            try:
                state = psd.set_center_freq(10000)
                x = 0.5 * np.exp(2j * np.pi * 5 * np.arange(1024) / 64)
                psd.update(state, x)
                freqs, power = psd.result(state)
                linear = 10**(power / 10) if log else power
                expected_mw = 0.25 * 10**(-30 / 10)
                self.assertAlmostEqual(np.sum(linear) * 16, expected_mw, places=16)
                self.assertAlmostEqual(10 * np.log10(np.sum(linear) * 16), -36.020599913279625, places=12)
                self.assertEqual(psd.measurement_metadata(state)['calibration']['reference_plane'], 'simulated RF input')
            finally:
                psd.close()

    def test_frequency_correction_is_interpolated_in_db_and_rf_coordinates(self):
        calibration = PowerCalibration(profile(points=[[100, -40], [200, -20]]))
        np.testing.assert_allclose(calibration.offsets_db([100, 150, 200]), [-40, -30, -20])
        np.testing.assert_allclose(calibration.apply([100, 150, 200], np.ones(3), False),
                                   [1e-4, 1e-3, 1e-2])
        with self.assertRaisesRegex(ValueError, 'extrapolation'):
            calibration.offsets_db([99, 150])
        # Zero remains exactly zero / -infinity; no fictitious noise floor.
        self.assertEqual(calibration.apply([150], np.zeros(1), False)[0], 0)
        self.assertTrue(np.isneginf(calibration.apply([150], np.zeros(1), True)[0]))

    def test_rf_dependent_baseband_response_uses_bilinear_db_interpolation(self):
        data = profile(points=[[1000, -30], [2000, -20]])
        data['baseband_hz'] = [-512, 0, 512]
        data['baseband_correction_db'] = [[2, 0, 4], [6, 0, 8]]
        calibration = PowerCalibration(data)
        # At RF midpoint, interpolate both row responses, then evaluate BB.
        np.testing.assert_allclose(calibration.offsets_db([1500, 1500], [-256, 256]), [-23, -22])
        with self.assertRaisesRegex(ValueError, 'baseband'):
            calibration.offsets_db([1500], [513])

    def test_calibration_baseband_coordinates_follow_raw_iq_through_mirroring(self):
        data = profile(points=[[0, -30], [20000, -30]])
        data['baseband_hz'] = [-512, 0, 512]
        data['baseband_correction_db'] = [[0, 0, 16], [0, 0, 16]]
        calibration = PowerCalibration(data)
        for conjugate in (False, True):
            for sign in (-1, 1):
                psd = PSD(64, 1024, fft_window='boxcar', calibration=calibration, iq_conjugate=conjugate,
                          frequency_sign=sign, lnb_lo=10000, log_scale=True, max_threads=1)
                try:
                    state = psd.set_center_freq(1000)
                    psd.update(state, np.exp(2j * np.pi * 5 * np.arange(128) / 64))
                    _, power = psd.result(state)
                    # Raw tone is always +80 Hz: +2.5 dB correction, regardless
                    # of IQ conjugation or the external converter's RF side.
                    self.assertAlmostEqual(np.max(power), 10 * np.log10(1 / 16) - 30 + 2.5, places=12)
                finally:
                    psd.close()

    def test_context_change_in_any_acquisition_field_is_rejected(self):
        calibration = PowerCalibration(profile())
        calibration.validate_context(context())
        changes = {'serial': 'OTHER', 'channel': 1, 'sample_rate': 2048,
                   'bandwidth': 512, 'gains': {'overall': 21}, 'agc': True,
                   'antenna': 'OTHER', 'dc_correction': False, 'iq_correction': False,
                   'tune_args': {'OFFSET': '128'}, 'lnb_high_side': True,
                   'lnb_lo': 1e9, 'iq_conjugate': True, 'corr_ppm': 1,
                   'device_settings': {'biastee': 'true'}, 'driver': 'different'}
        for key, value in changes.items():
            candidate = context()
            candidate[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                calibration.validate_context(candidate)

    def test_profile_schema_and_numeric_domain(self):
        for changes in ({'version': 2}, {'version': True}, {'units': 'dBFS'},
                        {'points': [[0, 0], [0, 1]]}, {'points': [[0, 0], [2, math.inf]]},
                        {'points': [[0, 0]]}, {'reference': ''}, {'uncertainty_db': -1}):
            candidate = profile()
            candidate.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                PowerCalibration(candidate)
        candidate = profile()
        candidate['context']['agc'] = True
        with self.assertRaises(ValueError):
            PowerCalibration(candidate)
        candidate = profile()
        del candidate['context']['gains']
        with self.assertRaises(ValueError):
            PowerCalibration(candidate)

    def test_profile_load_is_strict_and_hash_is_stable(self):
        data = profile()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cal.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            calibration = PowerCalibration.load(path)
            self.assertEqual(calibration.profile_id, PowerCalibration(data).profile_id)
            path.write_text('{"version": NaN}', encoding='utf-8')
            with self.assertRaises(ValueError):
                PowerCalibration.load(path)

    def test_optional_fftw_multiple_threads_preserve_parseval(self):
        import simplespectral
        if not getattr(simplespectral, 'fft_pyfftw', False) or not simplespectral.use_pyfftw:
            self.skipTest('optional active pyFFTW backend unavailable')
        psd = PSD(127, 1024, fft_threads=2, max_threads=1, log_scale=False)
        try:
            state = psd.set_center_freq(0)
            signal = np.exp(2j * np.pi * 7.4 * np.arange(1024) / 127)
            psd.update(state, signal)
            self.assertAlmostEqual(np.sum(psd.result(state)[1]) * psd.bin_width, 1, places=13)
        finally:
            psd.close()

    def test_fft_threads_are_per_instance_not_a_global_override(self):
        import simplespectral
        calls = []
        def fft(data, **kwargs):
            calls.append(kwargs.pop('threads'))
            return np.fft.fft(data, **kwargs)
        original = simplespectral.fft_threads
        with mock.patch.object(simplespectral, 'fft_pyfftw', True), \
                mock.patch.object(simplespectral, 'use_pyfftw', True), \
                mock.patch.object(simplespectral, 'fft', side_effect=fft):
            for threads in (1, 2):
                psd = PSD(64, 1024, max_threads=1, fft_threads=threads)
                try:
                    psd.update(psd.set_center_freq(0), np.ones(64))
                finally:
                    psd.close()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(simplespectral.fft_threads, original)

    def test_single_precision_window_normalization_matches_used_coefficients(self):
        psd = PSD(63, 1024, precision='single', fft_window=('kaiser', 7), log_scale=False, max_threads=1)
        try:
            state = psd.set_center_freq(0)
            psd.update(state, np.ones(63, dtype=np.complex64))
            self.assertAlmostEqual(np.sum(psd.result(state)[1]) * psd.bin_width, 1, places=6)
        finally:
            psd.close()


class DcArchitectureTests(unittest.TestCase):
    def test_hann_dc_leakage_is_excluded_without_inventing_power(self):
        psd = PSD(64, 1024, dc_mask_width=32, log_scale=False, max_threads=1)
        try:
            state = psd.set_center_freq(10000)
            psd.update(state, np.ones(1024, dtype=np.complex64))
            freqs, power = psd.result(state)
            np.testing.assert_array_equal(freqs[np.isnan(power)], [9984, 10000, 10016])
            self.assertLess(np.nansum(power), 1e-30)
            self.assertFalse(psd.measurement_metadata(state)['dc_interpolated'])
        finally:
            psd.close()

    def test_low_if_spur_exclusion_respects_conjugation_and_mixer_sign(self):
        for conjugate in (False, True):
            for sign in (-1, 1):
                psd = PSD(64, 1024, dc_offset=80, dc_mask_width=8, iq_conjugate=conjugate,
                          frequency_sign=sign, lnb_lo=20000, log_scale=False, max_threads=1)
                try:
                    state = psd.set_center_freq(1000)
                    x = np.exp(2j * np.pi * 5 * np.arange(128) / 64)
                    psd.update(state, x)
                    freqs, power = psd.result(state)
                    expected = 20000 + sign * (1000 + (-80 if conjugate else 80))
                    np.testing.assert_array_equal(freqs[np.isnan(power)], [expected])
                finally:
                    psd.close()

    def test_exclusion_does_not_modify_wanted_bins(self):
        rng = np.random.default_rng(171)
        x = rng.normal(size=512) + 1j * rng.normal(size=512)
        values = []
        for width in (0, 32):
            psd = PSD(64, 1024, dc_mask_width=width, log_scale=False, max_threads=1)
            try:
                state = psd.set_center_freq(0)
                psd.update(state, x)
                values.append(psd.result(state)[1])
            finally:
                psd.close()
        valid = np.isfinite(values[1])
        np.testing.assert_array_equal(values[0][valid], values[1][valid])

    def test_unsupported_or_ambiguous_dc_options_are_rejected(self):
        for options in ({'remove_dc': True}, {'remove_dc': True, 'fft_window': 'boxcar', 'bins': 2},
                        {'dc_mask_width': 1024}, {'dc_offset': 80},
                        {'dc_offset': 600, 'dc_mask_width': 10},
                        {'dc_offset': 8, 'dc_mask_width': 1},
                        {'remove_dc': True, 'fft_window': 'boxcar', 'dc_mask_width': 10}):
            params = dict(bins=64, sample_rate=1024, max_threads=1)
            params.update(options)
            with self.subTest(options=options), self.assertRaises(ValueError):
                PSD(**params)

    def test_nyquist_exclusion_wraps_the_fft_boundary(self):
        psd = PSD(64, 1024, dc_offset=512, dc_mask_width=32, max_threads=1, log_scale=False)
        try:
            state = psd.set_center_freq(0)
            psd.update(state, np.ones(64))
            freqs, power = psd.result(state)
            np.testing.assert_array_equal(freqs[np.isnan(power)], [-512, -496, 496])
        finally:
            psd.close()


class OutputMetadataTests(unittest.TestCase):
    def test_json_distinguishes_excluded_bins_zero_power_and_units(self):
        stream = io.StringIO()
        output = writer.JsonWriter(stream, allow_nan=True, power_unit='mW')
        now = datetime.datetime.now(datetime.timezone.utc)
        output.write_async(([0, 1, 2], [np.nan, -np.inf, -40]), now, now, 64,
                           metadata=lambda: {'reference_plane': 'test connector'})
        output.close()
        record = json.loads(stream.getvalue(), parse_constant=lambda value: self.fail(value))
        self.assertEqual(record['unit'], 'dBm/Hz')
        self.assertEqual(record['power'], [None, None, -40])
        self.assertEqual(record['masked_bins'], [0])
        self.assertEqual(record['zero_power_bins'], [1])
        self.assertEqual(record['metadata']['reference_plane'], 'test connector')

    def test_nan_is_only_accepted_when_explicitly_enabled(self):
        with self.assertRaises(ValueError):
            writer.spectrum_data(([0, 1], [np.nan, 1]), True)
        writer.spectrum_data(([0, 1], [np.nan, 1]), True, allow_nan=True)
        with self.assertRaises(ValueError):
            writer.spectrum_data(([0, 1], [np.inf, 1]), True, allow_nan=True)

    def test_calibrated_linear_and_logarithmic_text_units(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        for log, unit in ((False, 'mW/Hz'), (True, 'dBm/Hz')):
            stream = io.StringIO()
            output = writer.RtlPowerFftwWriter(stream, log_scale=log, power_unit='mW')
            output.write(([0, 1], [1, 2]), now, now, 64)
            output.close()
            self.assertIn('[' + unit + ']', stream.getvalue())

    def test_cli_rejects_conflicting_scientific_options(self):
        parser = setup_argument_parser()
        for arguments in (['--remove-dc'], ['--dc-offset', '80'],
                          ['--calibration', 'test.json', '--agc'],
                          ['--remove-dc', '--fft-window', 'boxcar', '--dc-mask-width', '16']):
            with mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
                validate_arguments(parser, parser.parse_args(arguments))


class HardwareIntegrationTests(unittest.TestCase):
    setUp = audit.AcquisitionTests.setUp

    def capable_receiver(self, **options):
        base = type(self.sdr.device)
        class Raw:
            def __init__(self, wrapper):
                self.wrapper = wrapper
                self.dc, self.iq, self.agc = True, True, True
                self.dc_writes = self.iq_writes = 0
                self.frequency_args = []
                self.ppm = 0
            def hasGainMode(self, *args): return True
            def getGainMode(self, *args): return self.agc
            def setGainMode(self, direction, channel, value): self.agc = value
            def getGain(self, *args): return self.wrapper.gain
            def getDriverKey(self): return 'test'
            def getHardwareInfo(self): return {'serial': 'TEST-001'}
            def hasDCOffsetMode(self, *args): return True
            def hasIQBalanceMode(self, *args): return True
            def getDCOffsetMode(self, *args): return self.dc
            def getIQBalanceMode(self, *args): return self.iq
            def setDCOffsetMode(self, direction, channel, value):
                self.dc = value
                self.dc_writes += 1
            def setIQBalanceMode(self, direction, channel, value):
                self.iq = value
                self.iq_writes += 1
            def hasFrequencyCorrection(self, *args): return True
            def setFrequencyCorrection(self, direction, channel, value): self.ppm = value
            def getFrequencyCorrection(self, *args): return self.ppm
            def setFrequency(self, direction, channel, value, args):
                self.frequency_args.append(dict(args))
                self.wrapper.freq = value
        class Device(base):
            hardware = 'SimulatedReceiver'
            antenna = 'RX'
            bandwidth = 1024.0
            channel = 0
            gain = 20.7
            _freq = 0
            def __init__(self, **kwargs): self.device = Raw(self)
            @property
            def freq(self): return self._freq
            @freq.setter
            def freq(self, value):
                self._freq = value
                self.device.dc = self.device.iq = False  # emulate per-LO driver calibration
            def list_gains(self): return []
        self.power.simplesoapy.SoapyDevice = Device
        output = io.BytesIO()
        sdr = self.power.SoapyPower(output=output, output_format='json', **options)
        self.addCleanup(sdr.stop)
        return sdr, output

    def test_fixed_gain_disables_existing_agc_before_measurement(self):
        sdr, output = self.capable_receiver()
        self.assertFalse(sdr.device.device.agc)
        sdr.sweep(10000, 10000, 64, 4, runs=1, max_threads=1)
        record = json.loads(output.getvalue())
        self.assertFalse(record['metadata']['context']['agc'])
        self.assertEqual(record['metadata']['context']['gains'], {'overall': 20.7})
        self.assertEqual(record['metadata']['segment_count'], 7)

    def test_explicit_corrections_reapplied_after_every_tune(self):
        sdr, output = self.capable_receiver(dc_correction='on', iq_correction='on')
        sdr.sweep(10000, 13000, 64, 4, runs=1, max_threads=1)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertGreater(len(records), 1)
        for record in records:
            self.assertTrue(record['metadata']['context']['dc_correction'])
            self.assertTrue(record['metadata']['context']['iq_correction'])
        self.assertEqual(sdr.device.device.dc_writes, len(records) + 1)

    def test_default_corrections_are_never_overridden(self):
        sdr, _ = self.capable_receiver()
        sdr.sweep(10000, 10000, 64, 4, runs=1, max_threads=1)
        self.assertEqual(sdr.device.device.dc_writes, 0)
        self.assertEqual(sdr.device.device.iq_writes, 0)

    def test_unsupported_explicit_modes_and_agc_fail(self):
        frontend = Frontend(SimpleNamespace(), 1, 0, dc_correction='on')
        with self.assertRaises(ValueError): frontend.apply_corrections()
        with self.assertRaises(ValueError): frontend.gain_mode(True)
        with self.assertRaises(ValueError): frontend.frequency_correction(1)

    def test_tuning_arguments_reach_driver_and_modern_ppm_api(self):
        sdr, output = self.capable_receiver(tune_args={'OFFSET': '128'}, corr=1.5)
        sdr.sweep(10000, 10000, 64, 4, runs=1, max_threads=1)
        self.assertEqual(sdr.device.device.frequency_args, [{'OFFSET': '128'}])
        self.assertEqual(sdr.device.device.ppm, 1.5)
        self.assertEqual(json.loads(output.getvalue())['metadata']['context']['corr_ppm'], 1.5)

    def test_calibration_profile_applies_in_complete_acquisition_pipeline(self):
        sdr, output = self.capable_receiver(dc_correction='on', iq_correction='on')
        sdr.sweep(10000, 10000, 64, 4, runs=1, max_threads=1)
        raw_record = json.loads(output.getvalue())
        calibration = PowerCalibration(profile(ctx=raw_record['metadata']['context']))
        output.seek(0)
        output.truncate()
        sdr.sweep(10000, 10000, 64, 4, runs=1, max_threads=1, calibration=calibration)
        calibrated = json.loads(output.getvalue())
        self.assertEqual(calibrated['unit'], 'dBm/Hz')
        for before, after in zip(raw_record['power'], calibrated['power']):
            if before is not None:
                self.assertAlmostEqual(after, before - 30, places=11)
        self.assertEqual(calibrated['metadata']['calibration']['profile_sha256'], calibration.profile_id)

    def test_driver_retune_cannot_silently_restore_agc_in_fixed_gain_mode(self):
        sdr, output = self.capable_receiver()
        original_type = type(sdr.device)
        original = original_type.freq.fset
        def retune(device, value):
            original(device, value)
            device.device.agc = True
        original_type.freq = property(original_type.freq.fget, retune)
        sdr.sweep(10000, 13000, 64, 4, runs=1, max_threads=1)
        for record in map(json.loads, output.getvalue().splitlines()):
            self.assertFalse(record['metadata']['context']['agc'])
            self.assertEqual(record['metadata']['context']['gains'], {'overall': 20.7})

    def test_cli_json_and_calibration_round_trip_with_simulated_radio(self):
        from soapypower.__main__ import main
        self.capable_receiver()
        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / 'raw.jsonl'
            calibrated_path = Path(directory) / 'calibrated.jsonl'
            profile_path = Path(directory) / 'calibration.json'
            base = ['soapy_power', '-f', '10000', '-b', '64', '-n', '4', '-F', 'json',
                    '--dc-correction', 'on', '--iq-correction', 'on', '--max-threads', '1']
            with mock.patch.object(self.power, 'install_signal_handlers'), \
                    mock.patch('sys.argv', base + ['-O', str(raw_path)]):
                main()
            raw = json.loads(raw_path.read_text())
            profile_path.write_text(json.dumps(profile(ctx=raw['metadata']['context'])), encoding='utf-8')
            with mock.patch.object(self.power, 'install_signal_handlers'), \
                    mock.patch('sys.argv', base + ['-O', str(calibrated_path),
                                                  '--calibration', str(profile_path), '--dc-mask-width', '32']):
                main()
            calibrated = json.loads(calibrated_path.read_text())
            self.assertEqual(calibrated['unit'], 'dBm/Hz')
            self.assertEqual(calibrated['masked_bins'], [31, 32, 33])
            self.assertEqual(calibrated['samples'], 256)
            self.assertEqual(calibrated['metadata']['segment_count'], 7)

    def test_completed_hardware_burst_never_joins_another_burst(self):
        sdr, output = self.capable_receiver()
        original = sdr.device.read_stream
        def packet():
            result = original()
            result.flags = 2
            return result
        sdr.device.read_stream = packet
        sdr.sweep(10000, 13000, 64, 4, runs=5, max_threads=1)
        records = output.getvalue().splitlines()
        self.assertEqual(len(records), 1)
        record = json.loads(records[0])
        self.assertEqual(record['samples'], 64)
        self.assertTrue(record['metadata']['end_burst'])
        self.assertEqual(sdr.device.reads, 1)

    def test_calibration_checks_frontend_state_after_driver_retune(self):
        sdr, output = self.capable_receiver()
        # Initial factory modes are true. This driver resets them to false at
        # tuning; a profile made after a tune is still usable on a fresh device.
        sdr.sweep(10000, 10000, 64, 4, runs=1, max_threads=1)
        measured_context = json.loads(output.getvalue())['metadata']['context']
        self.assertFalse(measured_context['dc_correction'])
        calibration = PowerCalibration(profile(ctx=measured_context))
        sdr.device.device.dc = sdr.device.device.iq = True
        sdr.device._freq = 0
        output.seek(0)
        output.truncate()
        sdr.sweep(10000, 10000, 64, 4, runs=1, max_threads=1, calibration=calibration)
        self.assertEqual(json.loads(output.getvalue())['unit'], 'dBm/Hz')

    def test_gain_change_prevents_calibrated_output_and_cleans_up(self):
        sdr, output = self.capable_receiver(dc_correction='on', iq_correction='on')
        sdr.sweep(10000, 10000, 64, 4, runs=1, max_threads=1)
        calibration = PowerCalibration(profile(ctx=json.loads(output.getvalue())['metadata']['context']))
        output.seek(0)
        output.truncate()
        sdr.device.gain = 21.7
        with self.assertRaisesRegex(ValueError, 'gains'):
            sdr.sweep(10000, 10000, 64, 4, runs=1, max_threads=1, calibration=calibration)
        self.assertEqual(output.getvalue(), b'')
        self.assertFalse(sdr.device.is_streaming)
