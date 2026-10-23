"""Hardware-free regression tests for numerical and I/O contracts."""
import concurrent.futures
import datetime
import io
import os
import signal
import struct
import subprocess
import sys
import threading
import types
import unittest
from unittest import mock

import numpy as np

from soapypower.psd import PSD
from soapypower import writer
from soapypower.threadpool import ThreadPoolExecutor
from soapypower.__main__ import (float_with_multiplier, freq_or_freq_range,
                                 setup_argument_parser, validate_arguments)


def reference_welch(x, bins, rate, overlap=0.5):
    # Independent, explicit two-sided periodic Hann reference.
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(bins) / bins)
    step = bins - int(bins * overlap)
    spectra = [np.abs(np.fft.fft(x[i:i + bins] * window)) ** 2
               for i in range(0, len(x) - bins + 1, step)]
    return np.mean(spectra, axis=0) / (rate * np.sum(window ** 2))


class SpectrumTests(unittest.TestCase):
    def make_psd(self, bins=64, **kwargs):
        psd = PSD(bins, 1024, max_threads=2, max_queue_size=1, **kwargs)
        self.addCleanup(psd.close)
        return psd

    def test_tone_frequency_and_integrated_power(self):
        psd = self.make_psd(log_scale=False, lnb_lo=1000)
        x = np.exp(2j * np.pi * 7 * np.arange(1024) / 64).astype(np.complex64)
        state = psd.set_center_freq(10000)
        psd.update(state, x)
        freqs, power = psd.result(state)
        self.assertEqual(freqs[np.argmax(power)], 11112)
        self.assertAlmostEqual(np.sum(power) * 16, 1, places=6)
        np.testing.assert_allclose(power, np.fft.fftshift(reference_welch(x, 64, 1024)),
                                   rtol=2e-6, atol=1e-12)

    def test_noise_and_unequal_segment_weighting(self):
        rng = np.random.default_rng(37)
        psd = self.make_psd(log_scale=False)
        chunks = [(rng.normal(size=n) + 1j * rng.normal(size=n)).astype(np.complex64)
                  for n in (128, 512)]
        state = psd.set_center_freq(0)
        for chunk in chunks:
            psd.update_async(state, chunk)
        freqs, power = psd.wait_for_result(state)
        expected = sum(reference_welch(x, 64, 1024) * w
                       for x, w in zip(chunks, (3, 15))) / 18
        np.testing.assert_allclose(power, np.fft.fftshift(expected), rtol=2e-6)
        self.assertEqual(power.dtype, np.float64)

    def test_detrend_and_scipy_reference(self):
        try:
            from scipy.signal import welch
        except ImportError:
            self.skipTest('optional SciPy reference is unavailable')
        rng = np.random.default_rng(73)
        x = (rng.normal(size=1024) + 1j * rng.normal(size=1024) + 5).astype(np.complex64)
        for bins, window in ((n, w) for n in (63, 64) for w in
                             ('hann', 'hamming', 'blackman', 'bartlett', 'boxcar',
                              ('kaiser', 5), ('tukey', 0), ('tukey', 0.25), ('tukey', 1))):
            psd = self.make_psd(bins=bins, log_scale=False, fft_window=window, detrend='constant')
            state = psd.set_center_freq(0)
            psd.update(state, x)
            _, expected = welch(x.astype(np.complex128), fs=1024, nperseg=bins, noverlap=bins // 2, window=window,
                                detrend='constant', return_onesided=False)
            np.testing.assert_allclose(psd.result(state)[1], np.fft.fftshift(expected), rtol=3e-12, atol=1e-25)

    def test_small_crop_and_zero_power(self):
        psd = self.make_psd(crop_factor=0.001)
        state = psd.set_center_freq(0)
        psd.update(state, np.zeros(128, dtype=np.complex64))
        freqs, power = psd.result(state)
        self.assertEqual(len(freqs), 64)
        self.assertTrue(np.all(np.isneginf(power)))

    def test_batched_fft_and_strided_input(self):
        psd = self.make_psd(log_scale=False, fft_window='boxcar')
        state = psd.set_center_freq(0)
        data = np.ones(1200000, dtype=np.complex64)[::2]
        psd.update(state, data)
        power = psd.result(state)[1]
        self.assertAlmostEqual(np.sum(power) * 16, 1, places=6)

    def test_crop_and_dc(self):
        psd = self.make_psd(crop_factor=0.25, remove_dc=True, log_scale=False,
                            fft_window='boxcar')
        state = psd.set_center_freq(0)
        psd.update(state, np.ones(128, dtype=np.complex64))
        freqs, power = psd.result(state)
        self.assertEqual(len(freqs), 48)
        self.assertEqual(np.max(power), 0)

    def test_worker_failures_propagate_for_multiple_updates(self):
        psd = self.make_psd()
        state = psd.set_center_freq(0)
        release = threading.Event()
        self.addCleanup(release.set)
        def failed_fft(*args, **kwargs):
            release.wait(5)
            raise RuntimeError('FFT backend failed')
        with mock.patch('soapypower.psd.simplespectral.fft', side_effect=failed_fft):
            psd.update_async(state, np.zeros(128))
            psd.update_async(state, np.zeros(128))
            future = psd.result_async(state)
            release.set()
            with self.assertRaisesRegex(RuntimeError, 'FFT backend failed'):
                future.result(timeout=5)

    def test_invalid_configuration_and_empty_state(self):
        for kwargs in ({'bins': 1}, {'fft_overlap': 1}, {'crop_factor': 0.999}):
            with self.assertRaises(ValueError):
                self.make_psd(**kwargs)
        psd = self.make_psd()
        with self.assertRaises(ValueError):
            psd.result(psd.set_center_freq(0))


class WriterTests(unittest.TestCase):
    def test_binary_float64_and_endianness(self):
        formatter = writer.SoapyPowerBinFormat()
        stream = io.BytesIO()
        formatter.write(stream, 0, 1, 100, 102, 1, 64, np.array([1, 2], dtype='>f8'))
        self.assertEqual(len(stream.getvalue()), formatter.header_size() + 8)
        stream.seek(0)
        header, power = formatter.read(stream)
        self.assertEqual(header.size, 8)
        np.testing.assert_array_equal(power, [1, 2])
        self.assertIsNone(formatter.read(stream))
        stream.seek(0)
        valid = stream.read()
        for end in (1, 4, 6, len(valid) - 1):
            with self.assertRaises(ValueError):
                formatter.read(io.BytesIO(valid[:end]))

    def test_partial_reads(self):
        class ShortReads(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 2))
        formatter = writer.SoapyPowerBinFormat()
        stream = ShortReads()
        formatter.write(stream, 0, 1, 0, 2, 1, 2, np.ones(2))
        stream.seek(0)
        self.assertEqual(len(formatter.read(stream)[1]), 2)

    def test_writer_close_drains_and_preserves_stream(self):
        stream = io.BytesIO()
        output = writer.RtlPowerWriter(stream)
        now = datetime.datetime.now(datetime.timezone.utc)
        for _ in range(10):
            output.write_async((np.array([0, 1]), np.array([1, 2])), now, now, 64)
        output.close()
        self.assertEqual(len(stream.getvalue().splitlines()), 10)
        output.close()
        stream.write(b'still open')

    def test_text_stream(self):
        stream = io.StringIO()
        output = writer.RtlPowerFftwWriter(stream)
        now = datetime.datetime.now(datetime.timezone.utc)
        output.write_async((np.array([0, 1]), np.array([1, 2])), now, now, 64)
        output.close()
        self.assertIn('0 1', stream.getvalue())

    def test_prior_write_failure_survives_later_marker(self):
        output = writer.RtlPowerWriter(io.BytesIO())
        failed = concurrent.futures.Future()
        failed.set_exception(RuntimeError('PSD failed'))
        result = output.write_async(failed, None, None, 0)
        with self.assertRaises(RuntimeError):
            result.result(timeout=5)
        with self.assertRaisesRegex(RuntimeError, 'PSD failed'):
            output.close()


class InputTests(unittest.TestCase):
    def test_strict_numbers_and_ranges(self):
        self.assertEqual(float_with_multiplier('-1.5M'), -1500000)
        for value in ('abc12', '1MHz', '1e999', '12 junk'):
            with self.assertRaises(ValueError):
                float_with_multiplier(value)
        for value in ('1:2:3', '2:1', '1:'):
            with self.assertRaises(ValueError):
                freq_or_freq_range(value)

    def test_invalid_cli_options(self):
        parser = setup_argument_parser()
        for arguments in (['-b', '1'], ['-n', '0'], ['--fft-overlap', '100'],
                          ['-t', 'nan'], ['--max-threads', '-1'], ['--fft-window', 'kaiser']):
            with self.subTest(arguments=arguments), mock.patch('sys.stderr', io.StringIO()):
                with self.assertRaises(SystemExit):
                    validate_arguments(parser, parser.parse_args(arguments))

    def test_help_without_sdr_binding(self):
        result = subprocess.run([sys.executable, '-m', 'soapypower', '--help'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class PoolTests(unittest.TestCase):
    def test_bounded_backpressure_and_shutdown(self):
        pool = ThreadPoolExecutor(max_workers=1, max_queue_size=1)
        gate = threading.Event()
        try:
            first = pool.submit(gate.wait, 2)
            second = pool.submit(lambda: 2)
            submitted = threading.Event()
            def producer():
                pool.submit(lambda: 3)
                submitted.set()
            thread = threading.Thread(target=producer)
            thread.start()
            self.assertFalse(submitted.wait(0.05))
            gate.set()
            thread.join(5)
            self.assertTrue(submitted.is_set())
            self.assertEqual(second.result(timeout=5), 2)
        finally:
            gate.set()
            pool.shutdown()
        with self.assertRaises(RuntimeError):
            pool.submit(lambda: None)


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        # Only the hardware boundary is replaced; DSP, pools and writers are real.
        class FakeDevice:
            sample_rate = 1024
            is_streaming = False
            freq = 0
            buffer_overflow_count = 0
            reads = 0
            fail_read = False
            def __init__(self, **kwargs):
                pass
            def start_stream(self, buffer_size=0):
                self.is_streaming = True
                self.buffer = np.zeros(buffer_size or 64, dtype=np.complex64)
                return self.buffer
            def stop_stream(self):
                self.is_streaming = False
            def read_stream(self):
                self.reads += 1
                if self.fail_read:
                    raise RuntimeError('device disconnected')
                self.buffer[:] = 1
                return types.SimpleNamespace(ret=len(self.buffer), flags=0, timeNs=0)
        self.modules = mock.patch.dict(sys.modules, {'simplesoapy': types.SimpleNamespace(SoapyDevice=FakeDevice, SoapySDR=types.SimpleNamespace(
            SOAPY_SDR_OVERFLOW=-4, SOAPY_SDR_END_ABRUPT=8, SOAPY_SDR_HAS_TIME=4))})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        import importlib
        power = importlib.import_module('soapypower.power')
        self.power = power
        power._shutdown = False
        self.addCleanup(setattr, power, '_shutdown', False)
        self.output = io.BytesIO()
        self.sdr = power.SoapyPower(output=self.output, output_format='soapy_power_bin')
        self.addCleanup(self.sdr.stop)

    def test_sweep_end_to_end_and_reuse(self):
        for _ in range(2):
            self.sdr.sweep(10000, 11024, 64, 4, runs=1, max_threads=2, max_buffer_size=128)
        self.assertFalse(self.sdr.device.is_streaming)
        self.output.seek(0)
        formatter = writer.SoapyPowerBinFormat()
        for _ in range(2):
            header, power = formatter.read(self.output)
            self.assertEqual(header.samples, 256)
            self.assertEqual(header.start, 10000)
            self.assertEqual(header.stop, 11024)
            self.assertLessEqual(header.time_start, header.time_stop)
            self.assertEqual(len(power), 64)
        self.assertIsNone(formatter.read(self.output))

    def test_interrupted_acquisition_reports_actual_samples(self):
        original_read = self.sdr.device.read_stream
        def interrupt():
            result = original_read()
            self.power._shutdown = True
            return result
        self.sdr.device.read_stream = interrupt
        self.sdr.sweep(10000, 10000, 64, 4, runs=1, max_threads=1, max_buffer_size=128)
        self.output.seek(0)
        header, _ = writer.SoapyPowerBinFormat().read(self.output)
        self.assertEqual(header.samples, 64)

    def test_startup_and_acquisition_errors_cleanup(self):
        with self.assertRaises(ValueError):
            self.sdr.sweep(10000, 10000, 64, 4, runs=1, max_buffer_size=8)
        self.assertFalse(self.sdr.device.is_streaming)
        self.sdr.device.fail_read = True
        with self.assertRaisesRegex(RuntimeError, 'device disconnected'):
            self.sdr.sweep(10000, 10000, 64, 4, runs=1, max_threads=1)
        self.assertFalse(self.sdr.device.is_streaming)
        self.assertIsNone(self.sdr._psd)

    def test_shutdown_before_first_run(self):
        self.power._shutdown = True
        self.sdr.sweep(10000, 10000, 64, 4, runs=1, max_threads=1)
        self.assertFalse(self.sdr.device.is_streaming)
        self.assertEqual(self.sdr.device.reads, 0)

    def test_hop_alignment_for_odd_and_even_fft(self):
        for bins in (63, 64):
            overlap = self.sdr.nearest_overlap(0.25, bins)
            centers = self.sdr.freq_plan(10000, 13000, bins, overlap, quiet=True)
            psd = PSD(bins, 1024, crop_factor=overlap, log_scale=False, max_threads=1)
            try:
                all_freqs = []
                for center in centers:
                    state = psd.set_center_freq(center)
                    psd.update(state, np.ones(bins, dtype=np.complex64))
                    all_freqs.extend(psd.result(state)[0])
                self.assertAlmostEqual(all_freqs[0], 10000)
                np.testing.assert_allclose(np.diff(all_freqs), 1024 / bins)
                self.assertGreaterEqual(all_freqs[-1] + 1024 / bins, 13000)
            finally:
                psd.close()

    def test_buffer_cap_and_exact_integration_length(self):
        repeats, buffer = self.sdr.create_buffer(64, 17, 64, 1024)
        self.assertEqual(len(buffer), 1024)
        self.assertEqual(repeats, 2)
        self.sdr.sweep(10000, 10000, 64, 17, runs=1, max_buffer_size=1024)
        self.output.seek(0)
        header, _ = writer.SoapyPowerBinFormat().read(self.output)
        self.assertEqual(header.samples, 64 * 17)

    def test_import_does_not_replace_signal_handlers(self):
        import importlib
        previous = signal.getsignal(signal.SIGINT)
        importlib.reload(self.power)
        self.assertEqual(signal.getsignal(signal.SIGINT), previous)


if __name__ == '__main__':
    unittest.main()
