"""Deterministic timing, failure propagation and FFT range regressions."""
import unittest
import json
import threading
from unittest import mock
import numpy as np
from soapypower.psd import PSD
import test_audit as audit
import test_architecture as architecture
import test_calibration_workflows as workflows


class TimingOrderTests(unittest.TestCase):
    setUp = audit.AcquisitionTests.setUp
    capable_receiver = architecture.HardwareIntegrationTests.capable_receiver

    def test_settle_clock_starts_after_all_tune_operations(self):
        sdr, output = self.capable_receiver()
        sdr.setup(64, 1)
        clock = [10.0]
        original = sdr._apply_gain
        def slow_gain():
            original()
            clock[0] += 3.0
        starts = []
        def settle(start):
            starts.append(start)
            return 0
        with mock.patch.object(self.power.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(sdr, '_apply_gain', side_effect=slow_gain), \
                mock.patch.object(sdr, '_settle', side_effect=settle):
            future, _, _ = sdr.psd(10000)
            future.result(timeout=2)
        self.assertEqual(starts, [13.0])
        sdr.stop()

    def test_failed_output_stops_acquisition_before_another_packet(self):
        sdr, output = self.capable_receiver()
        sdr.setup(64, 20, max_buffer_size=64)
        recorded = threading.Event()
        def fail():
            raise OSError('injected output failure')
        future = sdr._writer._submit(fail)
        future.add_done_callback(lambda done: recorded.set())
        self.assertTrue(recorded.wait(2))
        before = sdr.device.reads
        try:
            with self.assertRaisesRegex(OSError, 'injected output'):
                sdr.psd(10000)
            self.assertEqual(sdr.device.reads, before)
        finally:
            sdr.stop(cancel=True)


class FFTRangeTests(unittest.TestCase):
    def test_large_finite_single_input_keeps_representable_density(self):
        psd = PSD(64, 1024, fft_window='boxcar', fft_overlap=0,
                  precision='single', log_scale=False, max_threads=1)
        self.addCleanup(psd.close)
        samples = np.full(64, 1e38+1e38j, dtype=np.complex64)
        state = psd.set_center_freq(0)
        psd.update(state, samples)
        _, density = psd.result(state)
        expected = 2 * float(samples.real[0])**2 * 64 / 1024
        self.assertTrue(np.isfinite(density).all())
        self.assertAlmostEqual(float(density.max()) / expected, 1, places=6)

    def test_large_constant_detrend_does_not_overflow_mean(self):
        psd = PSD(64, 1024, fft_window='boxcar', fft_overlap=0,
                  precision='single', detrend='constant', log_scale=False, max_threads=1)
        self.addCleanup(psd.close)
        state = psd.set_center_freq(0)
        psd.update(state, np.full(64, 1e38+1e38j, dtype=np.complex64))
        np.testing.assert_array_equal(psd.result(state)[1], np.zeros(64))


    def test_large_windowed_overlap_matches_power_of_two_scaled_reference(self):
        rng = np.random.default_rng(93)
        samples = (rng.uniform(-1, 1, 1024) + 1j*rng.uniform(-1, 1, 1024)).astype('complex64')
        psd = PSD(64, 1024, precision='single', detrend='constant',
                  log_scale=False, max_threads=1)
        self.addCleanup(psd.close)
        small, large = psd.set_center_freq(0), psd.set_center_freq(0)
        psd.update(small, samples)
        psd.update(large, samples * np.float32(2.0**125))
        np.testing.assert_allclose(psd.result(large)[1] / 2.0**250,
                                   psd.result(small)[1], rtol=2e-6, atol=1e-15)

    def test_double_precision_extreme_constant_detrends_without_overflow(self):
        psd = PSD(64, 1024, fft_window='boxcar', detrend='constant',
                  log_scale=False, max_threads=1)
        self.addCleanup(psd.close)
        state = psd.set_center_freq(0)
        psd.update(state, np.full(64, 2.0**1020, dtype='complex128'))
        np.testing.assert_array_equal(psd.result(state)[1], np.zeros(64))


    def test_unused_extreme_tail_does_not_rescale_the_complete_segment(self):
        psd = PSD(64, 1024, fft_window='boxcar', fft_overlap=0,
                  log_scale=False, max_threads=1)
        self.addCleanup(psd.close)
        state = psd.set_center_freq(0)
        psd.update(state, np.r_[np.ones(64, dtype='complex128'), 2.0**1020])
        self.assertAlmostEqual(float(psd.result(state)[1].max()), 64/1024)

    def test_extreme_detrended_segment_preserves_another_small_segment(self):
        psd = PSD(64, 1024, fft_window='boxcar', fft_overlap=0,
                  detrend='constant', log_scale=False, max_threads=1)
        self.addCleanup(psd.close)
        tone = 1e-100 * np.exp(2j*np.pi*8*np.arange(64)/64)
        state = psd.set_center_freq(0)
        psd.update(state, np.r_[np.full(64, 2.0**1020, dtype='complex128'), tone])
        self.assertAlmostEqual(float(psd.result(state)[1].max()) / (1e-200*64/2048), 1, places=12)


class ReferenceRangeTests(unittest.TestCase):
    setUp = workflows.WorkflowTests.setUp
    capture = workflows.WorkflowTests.capture
    manifest = workflows.WorkflowTests.manifest
    build = workflows.WorkflowTests.build

    def test_tone_snr_stays_finite_when_linear_ratio_overflows(self):
        manifest = self.manifest()
        for cell in manifest['cells']:
            self.capture(cell['on'], cell['rf_hz'], cell['baseband_hz'], density=1e-300, tone=1e300)
            self.capture(cell['off'], cell['rf_hz'], cell['baseband_hz'], density=1e-300)
        profile = self.build(manifest)
        json.dumps(profile.profile, allow_nan=False)
        for cell in profile.profile['method_details']['cells']:
            self.assertGreater(cell['snr_db'], 5900)

    def test_holdout_snr_handles_the_same_finite_power_range(self):
        from soapypower.response_validation import validate_holdout
        manifest = self.manifest()
        for cell in manifest['cells']:
            self.capture(cell['on'], cell['rf_hz'], cell['baseband_hz'], density=1e-300, tone=1e300)
            self.capture(cell['off'], cell['rf_hz'], cell['baseband_hz'], density=1e-300)
        profile = self.build(manifest).profile
        # Synthetic independent calibration with training hashes removed.
        # This checks arithmetic, not empirical independence or RF accuracy.
        profile.pop('method_details')
        holdout = dict(version=1, reference='synthetic range test', independent_measurements_confirmed=True,
                       cells=[{key: value for key, value in cell.items() if key != 'path_loss_db'}
                              for cell in manifest['cells']])
        for cell in holdout['cells']: cell['input_dbm'] -= 10
        path = self.root/'holdouts.json'
        path.write_text(json.dumps(holdout))
        from soapypower.calibration import PowerCalibration
        self.assertTrue(validate_holdout(PowerCalibration(profile), path)['passed'])
