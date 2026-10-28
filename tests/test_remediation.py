"""Independent numerical references and measurement-integrity fault injection."""
import concurrent.futures
from decimal import Decimal, localcontext
import datetime
import io
import json
import math
import threading
import time
import unittest
from unittest import mock

import numpy as np
from soapypower.psd import PSD
from soapypower.integrity import ContinuityTracker
from soapypower.iq import iq_from_interleaved
from soapypower.records import integrate_record
from soapypower.threadpool import QueueFullError
from soapypower.writer import SoapyPowerBinV3Writer, SoapyPowerBinV3Format
from soapypower.calibration import PowerCalibration
from soapypower.uncertainty import evaluate_budget, COMPONENTS
import test_audit as audit
import test_architecture as architecture


def v2(power=None):
    return dict(version=2, start_hz=100, stop_hz=104, step_hz=2, first_bin_center_hz=100,
                bin_width_hz=2, coverage_start_hz=99, coverage_stop_hz=103, samples=64,
                unit='mW/Hz', power=[2, 2] if power is None else power,
                masked_bins=[], zero_power_bins=[], estimated_bins=[])


class DSPRemediationTests(unittest.TestCase):
    def psd(self, **kwargs):
        instance = PSD(8, 1024, max_threads=1, **kwargs)
        self.addCleanup(instance.close)
        return instance

    def test_framer_submission_failure_is_transactional_and_retryable(self):
        for overlap in (0, 0.5, 0.875):
            psd = self.psd(fft_overlap=overlap)
            state = psd.set_center_freq(0)
            psd.update_contiguous_async(state, np.arange(3) * (1 + 1j)).result()
            before = state['tail'].copy()
            samples = np.arange(10) * (1 + 1j)
            with mock.patch.object(psd._executor, 'submit', side_effect=RuntimeError('injected')):
                with self.assertRaises(RuntimeError):
                    psd.update_contiguous_async(state, samples)
            np.testing.assert_array_equal(state['tail'], before)
            psd.update_contiguous_async(state, samples).result()
            self.assertGreater(state['repeats'], 0)

    def test_nonblocking_overload_preserves_tail(self):
        psd = self.psd(nonblocking=True, max_queue_size=1)
        state = psd.set_center_freq(0)
        entered, release = threading.Event(), threading.Event()
        original = psd._compute
        def slow(samples):
            entered.set()
            release.wait(5)
            return original(samples)
        try:
            with mock.patch.object(psd, '_compute', side_effect=slow):
                psd.update_contiguous_async(state, np.ones(8, complex))
                self.assertTrue(entered.wait(2))
                psd.update_contiguous_async(state, np.ones(8, complex))
                before = state['tail'].copy()
                started = time.monotonic()
                with self.assertRaises(QueueFullError):
                    psd.update_contiguous_async(state, np.ones(8, complex))
                self.assertLess(time.monotonic() - started, 0.1)
                np.testing.assert_array_equal(before, state['tail'])
        finally:
            release.set()

    def test_workspace_rejects_before_window_allocation(self):
        with mock.patch('soapypower.psd.periodic_window') as window:
            with self.assertRaisesRegex(ValueError, 'workspace'):
                PSD(4194304, 20e6, workspace_budget_bytes=256 << 20)
            window.assert_not_called()

    def test_explicit_iq_and_native_adapter(self):
        psd = self.psd(input_format='complex')
        for values in (np.arange(16, dtype=np.int16), np.ones(8), np.ones(8, dtype=np.uint8)):
            with self.assertRaisesRegex(ValueError, 'complex IQ'):
                psd.update(psd.set_center_freq(0), values)
        np.testing.assert_array_equal(iq_from_interleaved(np.array([128, 128, 255, 0], np.uint8), 128, 128), [0, 127/128-1j])
        with self.assertRaises(ValueError):
            iq_from_interleaved([1, 2, 3], 32768)

    def test_estimated_dc_and_calibration_transform_contract(self):
        psd = self.psd(fft_window='boxcar', remove_dc=True)
        state = psd.set_center_freq(0)
        psd.update(state, np.ones(8, complex))
        self.assertEqual(psd.measurement_metadata(state)['estimated_bins'], [4])
        calibration = PowerCalibration(architecture.profile())
        with self.assertRaisesRegex(ValueError, 'detrend'):
            PSD(8, 1024, calibration=calibration, detrend='constant')
        with self.assertRaisesRegex(ValueError, 'estimated'):
            PSD(8, 1024, calibration=calibration, remove_dc=True, fft_window='boxcar')

    def test_two_sum_weighted_merge_against_80_digit_reference(self):
        rng = np.random.default_rng(8395)
        values = 10**rng.uniform(-30, 0, 2000)
        counts = rng.integers(1, 1000, size=values.size)
        with localcontext() as context:
            context.prec = 80
            expected = float(sum(Decimal.from_float(float(x)) * int(n) for x, n in zip(values, counts)) / int(sum(counts)))
        for order in (np.arange(values.size), np.argsort(values), np.argsort(values)[::-1]):
            psd = self.psd()
            state = psd.set_center_freq(0)
            with mock.patch.object(psd, '_compute') as compute:
                for index in order:
                    compute.return_value = int(counts[index]), np.full(8, values[index])
                    psd._accumulate(state, None)
            self.assertLessEqual(abs(state['pwr_array'][0] - expected) / abs(np.spacing(expected)), 4)

    def test_small_direct_dft_against_decimal_reference(self):
        # Independent trig and DFT, 70 decimal digits, actual rounded window coefficients.
        pi = Decimal('3.141592653589793238462643383279502884197169399375105820974944592307816')
        def trig(x, cosine):
            x %= 2 * pi
            term = Decimal(1) if cosine else x
            result = term
            for k in range(1, 180):
                a = 2*k if cosine else 2*k+1
                term *= -x*x / (a*(a-1))
                result += term
                if abs(term) < Decimal('1e-65'):
                    break
            return result
        with localcontext() as context:
            context.prec = 70
            for bins in (7, 8, 15):
                psd = PSD(bins, 1024, fft_window='hann', log_scale=False, max_threads=1)
                self.addCleanup(psd.close)
                x = np.random.default_rng(bins).normal(size=bins) + 1j*np.random.default_rng(bins+1).normal(size=bins)
                state = psd.set_center_freq(0)
                psd.update(state, x)
                observed = psd.result(state)[1]
                win = [Decimal.from_float(float(w)) for w in psd._window]
                energy = sum(w*w for w in win)
                powers = []
                for k in range(bins):
                    real = imag = Decimal(0)
                    for n in range(bins):
                        angle = -2*pi*k*n/bins
                        c, s = trig(angle, True), trig(angle, False)
                        re, im = Decimal.from_float(float(x[n].real)), Decimal.from_float(float(x[n].imag))
                        real += (re*c-im*s)*win[n]
                        imag += (re*s+im*c)*win[n]
                    powers.append(float((real*real+imag*imag)/(1024*energy)))
                np.testing.assert_allclose(observed, np.fft.fftshift(powers), rtol=5e-12, atol=1e-20)


class TimingTests(unittest.TestCase):
    def test_timestamp_confidence_and_gap(self):
        for flags, expected in (([False]*3, 'unknown'), ([True, False, False], 'inferred'),
                                ([True, False, True], 'anchored'), ([True]*3, 'verified')):
            tracker = ContinuityTracker(10e6)
            for index, timed in enumerate(flags):
                tracker.observe(64, timed, 10**18 + index*6400)
            self.assertEqual(tracker.metadata()['continuity'], expected)
        tracker = ContinuityTracker(10e6)
        tracker.observe(64, True, 10**18)
        with self.assertRaisesRegex(RuntimeError, 'gap'):
            tracker.observe(64, True, 10**18 + 6500)


class AcquisitionFaultTests(unittest.TestCase):
    setUp = audit.AcquisitionTests.setUp
    capable_receiver = architecture.HardwareIntegrationTests.capable_receiver

    def test_exact_target_terminal_tail_and_clock_metadata(self):
        sdr, output = self.capable_receiver()
        original = sdr.device.read_stream
        def packet():
            result = original()
            result.flags = 2 | 4
            result.timeNs = 10**18
            return result
        sdr.device.read_stream = packet
        sdr.sweep(10000, 10000, 16, 1, runs=1, max_threads=1)
        record = json.loads(output.getvalue())
        meta = record['metadata']
        self.assertEqual(record['samples'], 16)
        self.assertEqual(meta['measurement_status'], 'complete')
        self.assertTrue(meta['end_burst_seen'])
        self.assertFalse(meta['full_burst_processed'])
        self.assertEqual(meta['burst_samples_unprocessed'], 48)
        self.assertEqual(meta['hardware_first_sample_ns'], 10**18)
        self.assertEqual(meta['hardware_last_sample_ns'], 10**18 + round(15e9/1024))
        self.assertFalse(meta['hardware_time_is_utc'])

    def test_calibrated_partial_is_rejected_and_raw_partial_identified(self):
        sdr, output = self.capable_receiver()
        sdr.sweep(10000, 10000, 64, 4, runs=1)
        calibration = PowerCalibration(architecture.profile(ctx=json.loads(output.getvalue())['metadata']['context']))
        original = sdr.device.read_stream
        def burst():
            result = original()
            result.flags = 2
            return result
        sdr.device.read_stream = burst
        output.seek(0); output.truncate()
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            sdr.sweep(10000, 10000, 64, 4, runs=1, calibration=calibration)
        self.assertEqual(output.getvalue(), b'')
        sdr.sweep(10000, 10000, 64, 4, runs=1)
        self.assertEqual(json.loads(output.getvalue())['metadata']['measurement_status'], 'partial_end_burst')

    def test_sample_settling_discards_transient(self):
        sdr, output = self.capable_receiver()
        original = sdr.device.read_stream
        def transient():
            result = original()
            sdr.device.buffer[:] = 100 if sdr.device.reads <= 2 else 1
            return result
        sdr.device.read_stream = transient
        sdr.sweep(10000, 10000, 64, 1, runs=1, settle_samples=128, log_scale=False)
        record = json.loads(output.getvalue())
        self.assertEqual(record['metadata']['settled_discarded_samples'], 128)
        self.assertAlmostEqual(sum(record['power']) * 16, 1)

    def test_missing_lock_and_timestamp_requirement_fail(self):
        for options, text in (({'lock_sensor': 'lo_locked'}, 'sensor'), ({'require_timestamps': True}, 'timestamp')):
            sdr, output = self.capable_receiver()
            with self.assertRaisesRegex((ValueError, RuntimeError), text):
                sdr.sweep(10000, 10000, 64, 4, runs=1, **options)
            self.assertEqual(output.getvalue(), b'')
            self.assertFalse(sdr.device.is_streaming)

    def test_acquisition_queue_overrun_never_publishes_hop(self):
        sdr, output = self.capable_receiver()
        release = threading.Event()
        timer = threading.Timer(0.05, release.set)
        from soapypower import psd as module
        original = module.simplespectral.fft
        def stalled(*args, **kwargs):
            release.wait(3)
            return original(*args, **kwargs)
        timer.start()
        try:
            with mock.patch.object(module.simplespectral, 'fft', side_effect=stalled):
                with self.assertRaisesRegex(QueueFullError, 'overrun'):
                    sdr.sweep(10000, 10000, 64, 100, runs=1, max_threads=1,
                              max_queue_size=1, max_buffer_size=64)
        finally:
            release.set()
            timer.cancel()
        self.assertEqual(output.getvalue(), b'')
        self.assertFalse(sdr.device.is_streaming)

    def test_binary_v3_acquisition_preserves_full_context(self):
        sdr, output = self.capable_receiver()
        sdr._output_format = 'soapy_power_bin_v3'
        sdr.sweep(10000, 10000, 64, 4, runs=1)
        record = SoapyPowerBinV3Format().read(io.BytesIO(output.getvalue()))
        self.assertEqual(record['metadata']['context']['hardware'], 'SimulatedReceiver')
        self.assertEqual(record['metadata']['measurement_status'], 'complete')

    def test_lock_timeout_is_explicit(self):
        sdr, output = self.capable_receiver()
        sdr.device.device.readSensor = lambda *args: 'false'
        with self.assertRaisesRegex(RuntimeError, 'settle timeout'):
            sdr.sweep(10000, 10000, 64, 1, runs=1, lock_sensor='lo_locked', lock_timeout=0.005)
        self.assertEqual(output.getvalue(), b'')

    def test_quantized_tuning_cannot_hide_coverage_gap(self):
        sdr, output = self.capable_receiver()
        parent = type(sdr.device)
        class Quantized(parent):
            @parent.freq.setter
            def freq(self, value):
                self._freq = round(value / 300) * 300
        sdr.device.__class__ = Quantized
        with self.assertRaisesRegex(ValueError, 'coverage'):
            sdr.sweep(10000, 14000, 64, 2, runs=1)


class OutputRemediationTests(unittest.TestCase):
    def test_fractional_flat_psd_and_estimated_masked_boundaries(self):
        data = v2()
        for lower, upper in ((99, 103), (99.1, 99.9), (100.25, 102.7), (101, 102),
                             (np.nextafter(101., -np.inf), np.nextafter(101., np.inf))):
            result = integrate_record(data, float(lower), float(upper))
            self.assertAlmostEqual(result['power'], 2*(upper-lower), delta=max(1e-25, 2*(upper-lower)*2e-14))
        data['estimated_bins'] = [0]
        with self.assertRaisesRegex(ValueError, 'estimated'):
            integrate_record(data)
        self.assertTrue(integrate_record(data, include_estimated=True)['contains_estimates'])
        data['estimated_bins'] = []
        data['masked_bins'] = [0]
        data['power'][0] = None
        result = integrate_record(data, 100, 102, omit_masked=True)
        self.assertAlmostEqual(result['excluded_bandwidth_hz'], 1)
        self.assertAlmostEqual(result['power'], 2)

    def test_binary_v3_float64_metadata_short_writes_and_corruption(self):
        class ShortWrites(io.BytesIO):
            def write(self, data): return super().write(data[:3])
        stream = ShortWrites()
        writer = SoapyPowerBinV3Writer(stream, log_scale=False)
        now = datetime.datetime.now(datetime.timezone.utc)
        values = np.array([1+2**-40, 2+2**-39])
        writer.write((np.array([100., 102.]), values), now, now, 64,
                     {'measurement_status': 'complete', 'estimated_bins': []})
        writer.close()
        raw = stream.getvalue()
        record = SoapyPowerBinV3Format().read(io.BytesIO(raw))
        np.testing.assert_array_equal(record['power'], values)
        self.assertEqual(record['metadata']['measurement_status'], 'complete')
        for end in (1, 10, len(raw)-1):
            with self.assertRaises(ValueError):
                SoapyPowerBinV3Format().read(io.BytesIO(raw[:end]))
        corrupt = bytearray(raw); corrupt[-1] ^= 1
        with self.assertRaisesRegex(ValueError, 'checksum'):
            SoapyPowerBinV3Format().read(io.BytesIO(corrupt))

    def test_uncertainty_requires_declared_covariance_and_marks_missing(self):
        budget = {'components': dict.fromkeys(COMPONENTS, 0.1), 'independent': True, 'coverage_factor': 2}
        result = evaluate_budget(budget)
        self.assertAlmostEqual(result['expanded_db'], 2*math.sqrt(5)*0.1)
        budget['components']['mismatch_db'] = None
        self.assertIsNone(evaluate_budget(budget)['combined_standard_db'])
        budget.pop('independent')
        with self.assertRaisesRegex(ValueError, 'correlation'):
            evaluate_budget(budget)
        budget['correlation'] = np.ones((5, 5)).tolist()
        budget['components']['mismatch_db'] = 0.1
        self.assertAlmostEqual(evaluate_budget(budget)['expanded_db'], 1)
        profile = architecture.profile(); profile['version'] = 2; profile['uncertainty'] = budget
        self.assertEqual(PowerCalibration(profile).metadata()['uncertainty_evaluation']['coverage_probability'], None)
