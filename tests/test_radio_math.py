"""Independent DSP invariants and radio acquisition failure cases."""
import concurrent.futures
import io
import math
import os
import struct
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np

from soapypower.psd import PSD, periodic_window
from soapypower import writer
from soapypower.__main__ import setup_argument_parser, specific_gains, device_settings
from soapypower.threadpool import ThreadPoolExecutor
import test_audit as audit


def explicit_welch(x, bins, rate, overlap):
    win = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(bins) / bins)
    step = bins - math.floor(bins * overlap)
    result = np.zeros(bins)
    count = 0
    for offset in range(0, len(x) - bins + 1, step):
        transformed = np.fft.fft(x[offset:offset + bins] * win)
        result += transformed.real**2 + transformed.imag**2
        count += 1
    return np.fft.fftshift(result / (count * rate * np.dot(win, win))), count


class RadioMathTests(unittest.TestCase):
    def psd(self, bins=64, rate=4096, **options):
        settings = dict(max_threads=2, max_queue_size=1, log_scale=False)
        settings.update(options)
        result = PSD(bins, rate, **settings)
        self.addCleanup(result.close)
        return result

    def test_contiguous_chunks_equal_one_record_for_arbitrary_boundaries(self):
        rng = np.random.default_rng(732)
        x = rng.normal(size=521) + 1j * rng.normal(size=521)
        for bins in (2, 3, 7, 63, 64, 127):
            for overlap in (0, 0.1, 0.5, 0.9, 0.99):
                with self.subTest(bins=bins, overlap=overlap):
                    psd = self.psd(bins=bins, fft_overlap=overlap)
                    state = psd.set_center_freq(0)
                    offset = 0
                    for length in (0, 1, 2, 5, 0, 64, 7, 129, 1, 0, 300, 12):
                        chunk = x[offset:offset + length]
                        psd.update_contiguous_async(state, chunk)
                        offset += len(chunk)
                        self.assertLess(len(state['tail']), bins)
                    expected, segments = explicit_welch(x, bins, 4096, overlap)
                    freqs, power = psd.wait_for_result(state)
                    np.testing.assert_allclose(power, expected, rtol=4e-14, atol=1e-18)
                    self.assertEqual(state['repeats'], segments)
                    self.assertEqual(len(state['futures']), 0)

    def test_complex_and_real_signal_energy_and_frequency_sign(self):
        n = np.arange(256)
        for signal, expected in ((np.exp(2j * np.pi * 11 * n / 64), 1),
                                 (np.exp(-2j * np.pi * 11 * n / 64), 1),
                                 (np.cos(2 * np.pi * 11 * n / 64), 0.5)):
            psd = self.psd(fft_window='boxcar')
            state = psd.set_center_freq(0)
            psd.update(state, signal)
            freqs, power = psd.result(state)
            self.assertAlmostEqual(np.sum(power) * psd.bin_width, expected, places=13)
            if np.iscomplexobj(signal):
                expected_sign = np.sign(np.angle(signal[1]))
                self.assertEqual(np.sign(freqs[np.argmax(power)]), expected_sign)

    def test_iq_image_ratio_and_optional_conjugation(self):
        n = np.arange(512)
        desired = np.exp(2j * np.pi * 9 * n / 64)
        x = 0.8 * desired + 0.08 * desired.conj()
        for conjugate in (False, True):
            psd = self.psd(fft_window='boxcar', iq_conjugate=conjugate)
            state = psd.set_center_freq(0)
            psd.update(state, x)
            freqs, power = psd.result(state)
            sign = -1 if conjugate else 1
            main = power[freqs == sign * 576][0]
            image = power[freqs == -sign * 576][0]
            self.assertAlmostEqual(10 * np.log10(main / image), 20, places=12)
            self.assertAlmostEqual(np.sum(power) * psd.bin_width, 0.8**2 + 0.08**2, places=13)

    def test_high_side_lo_frequency_mapping_and_nyquist_orientation(self):
        for bins in (63, 64):
            psd = self.psd(bins=bins, frequency_sign=-1, lnb_lo=10e9, fft_window='boxcar')
            x = np.exp(2j * np.pi * 7 * np.arange(bins * 4) / bins)
            state = psd.set_center_freq(1e9)
            psd.update(state, x)
            freqs, power = psd.result(state)
            self.assertAlmostEqual(freqs[np.argmax(power)], 9e9 - 7 * 4096 / bins, places=5)
            self.assertTrue(np.all(np.diff(freqs) > 0))
            expected_first = 9e9 - ((bins - 1) // 2) * 4096 / bins
            self.assertAlmostEqual(freqs[0], expected_first, places=5)
            self.assertAlmostEqual(np.sum(power) * psd.bin_width, 1, places=13)

    def test_parseval_for_arbitrary_iq_and_each_window(self):
        rng = np.random.default_rng(17)
        x = rng.normal(size=127) + 1j * rng.normal(size=127)
        for spec in ('boxcar', 'hann', 'hamming', 'blackman', 'bartlett', ('kaiser', 9), ('tukey', 0.7)):
            psd = self.psd(bins=127, fft_window=spec)
            state = psd.set_center_freq(0)
            psd.update(state, x)
            window = periodic_window(spec, 127)
            expected = np.sum(np.abs(x * window)**2) / np.sum(window**2)
            actual = np.sum(psd.result(state)[1]) * psd.bin_width
            self.assertAlmostEqual(actual, expected, places=13)

    def test_noncoherent_tone_preserves_energy(self):
        psd = self.psd(bins=127)
        x = np.exp(2j * np.pi * 10.37 * np.arange(4096) / 127)
        state = psd.set_center_freq(0)
        psd.update(state, x)
        self.assertAlmostEqual(np.sum(psd.result(state)[1]) * psd.bin_width, 1, places=13)

    def test_circular_white_noise_density(self):
        rng = np.random.default_rng(734)
        x = (rng.normal(size=65536) + 1j * rng.normal(size=65536)) / np.sqrt(2)
        psd = self.psd(bins=256)
        state = psd.set_center_freq(0)
        psd.update(state, x)
        power = psd.result(state)[1]
        self.assertAlmostEqual(np.sum(power) * psd.bin_width, 1, delta=0.015)
        self.assertLess(np.max(np.abs(power * 4096 - 1)), 0.3)

    def test_window_enbw_is_not_bin_spacing(self):
        boxcar = self.psd(fft_window='boxcar')
        hann = self.psd()
        self.assertAlmostEqual(boxcar.enbw / boxcar.bin_width, 1)
        self.assertAlmostEqual(hann.enbw / hann.bin_width, 1.5)
        self.assertAlmostEqual(hann.coherent_gain, 0.5)

    def test_single_fft_power_does_not_underflow_or_overflow_float32(self):
        for amplitude in (1e-30, 1e30):
            psd = self.psd(precision='single', fft_window='boxcar')
            state = psd.set_center_freq(0)
            x = np.full(128, amplitude, dtype=np.complex64)
            psd.update(state, x)
            actual = np.sum(psd.result(state)[1]) * psd.bin_width
            self.assertTrue(np.isfinite(actual))
            self.assertAlmostEqual(actual / amplitude**2, 1, places=6)

    def test_invalid_values_and_state_lifecycle(self):
        for options in ({'bins': True}, {'bins': 2.5}, {'rate': np.nan},
                        {'lnb_lo': np.inf}, {'precision': 'half'}, {'fft_overlap': -0.1},
                        {'max_threads': 1.5}, {'fft_window': ('kaiser', 1e300)}):
            with self.subTest(options=options), np.errstate(all='ignore'):
                with self.assertRaises(ValueError):
                    self.psd(**options)
        psd = self.psd()
        for bad in ([np.nan] * 64, [np.inf] * 64, ['1'] * 64, np.ones((2, 64))):
            with self.assertRaises(ValueError):
                psd.update(psd.set_center_freq(0), bad)
        state = psd.set_center_freq(0)
        psd.update_contiguous_async(state, np.ones(64))
        with self.assertRaises(ValueError):
            psd.update(state, np.ones(64))
        psd.wait_for_result(state)
        with self.assertRaises(RuntimeError):
            psd.update_contiguous_async(state, np.ones(64))
        with self.assertRaises(ValueError):
            psd.set_center_freq(1e30)

    def test_finished_updates_do_not_accumulate_futures(self):
        psd = self.psd()
        state = psd.set_center_freq(0)
        for _ in range(100):
            psd.update_contiguous_async(state, np.ones(64)).result(timeout=5)
        psd.wait_for_result(state)
        self.assertEqual(len(state['futures']), 0)

    def test_numpy_integer_configuration(self):
        psd = self.psd(bins=np.int64(63))
        self.assertEqual(psd._bins, 63)


class RadioAcquisitionTests(unittest.TestCase):
    setUp = audit.AcquisitionTests.setUp

    def test_short_packet_remainders_are_preserved(self):
        self.sdr.setup(8, 3, base_buffer_size=8, max_threads=1)
        position = [0]
        def read_packet():
            self.sdr.device.buffer[:4] = np.arange(position[0], position[0] + 4)
            position[0] += 4
            return type('Packet', (), dict(ret=4, flags=0, timeNs=0))()
        self.sdr.device.read_stream = read_packet
        chunks = []
        for _ in range(2):
            output = np.empty(6, dtype=np.complex64)
            self.assertEqual(self.sdr._read_chunk(output), 6)
            chunks.extend(output)
        np.testing.assert_array_equal(chunks, np.arange(12))
        self.assertEqual(position[0], 12)

    def test_overflow_and_abrupt_end_abort_measurement(self):
        for ret, flags, message in ((-4, 0, 'overflow'), (64, 8, 'abruptly'), (-1, 0, 'timed out'), (0, 0, 'timed out')):
            self.sdr.device.read_stream = lambda: type('Packet', (), dict(ret=ret, flags=flags, timeNs=0))()
            with self.subTest(ret=ret, flags=flags):
                with self.assertRaisesRegex(RuntimeError, message):
                    self.sdr.sweep(10000, 10000, 64, 4, runs=1, max_threads=1)
                self.assertFalse(self.sdr.device.is_streaming)
        self.assertEqual(self.output.getvalue(), b'')

    def test_integer_epoch_timestamp_precision_and_gap_detection(self):
        self.sdr.device.sample_rate = 10e6
        self.sdr.setup(64, 4, max_threads=1)
        base = 1_700_000_000_000_000_003
        index = [0]
        def packet():
            # 64 samples at 10 MHz occupy 6400 ns. The epoch's low bits must
            # survive; adding durations directly to float(epoch) loses them.
            timestamp = base + index[0] * 6400
            index[0] += 1
            return type('Packet', (), dict(ret=64, flags=4, timeNs=timestamp))()
        self.sdr.device.read_stream = packet
        self.sdr._read_chunk(np.empty(128, dtype=np.complex64))
        self.sdr.device.read_stream = lambda: type('Packet', (), dict(ret=64, flags=4, timeNs=base + 12900))()
        with self.assertRaisesRegex(RuntimeError, 'timestamp gap'):
            self.sdr._read_chunk(np.empty(64, dtype=np.complex64))

    def test_deadline_stops_within_a_hop_and_counts_partial_data(self):
        now = [0.0]
        original = self.sdr.device.read_stream
        def packet():
            result = original()
            now[0] += 0.1
            return result
        self.sdr.device.read_stream = packet
        with mock.patch.object(self.power.time, 'monotonic', side_effect=lambda: now[0]):
            self.sdr.sweep(10000, 20000, 64, 100, runs=1, time_limit=0.15, max_threads=1)
        self.output.seek(0)
        header, _ = writer.SoapyPowerBinFormat().read(self.output)
        self.assertEqual(header.samples, 128)
        self.assertIsNone(writer.SoapyPowerBinFormat().read(self.output))
        self.assertFalse(self.sdr.device.is_streaming)

    def test_narrow_ranges_are_covered_for_odd_and_even_bins(self):
        for bins in (63, 64):
            rate = self.sdr.device.sample_rate
            overlap = self.sdr.nearest_overlap(0.25, bins)
            width = (bins - 2 * round(overlap * bins / 2)) * rate / bins
            end = 10000 + width - rate / bins / 4
            center, = self.sdr.freq_plan(10000, end, bins, overlap, quiet=True)
            psd = PSD(bins, rate, crop_factor=overlap, max_threads=1, log_scale=False)
            try:
                state = psd.set_center_freq(center)
                psd.update(state, np.ones(bins))
                freqs, _ = psd.result(state)
                self.assertLessEqual(freqs[0], 10000)
                self.assertGreaterEqual(freqs[-1] + rate / bins, end)
            finally:
                psd.close()

    def test_tuning_quantization_uses_actual_center_in_output(self):
        cls = type(self.sdr.device)
        cls.freq = property(lambda obj: getattr(obj, '_tuned', 0),
                            lambda obj, value: setattr(obj, '_tuned', round(value / 10) * 10))
        with self.assertLogs(self.power.logger, level='WARNING'):
            self.sdr.sweep(10003, 10003, 64, 4, runs=1, max_threads=1)
        self.output.seek(0)
        header, _ = writer.SoapyPowerBinFormat().read(self.output)
        self.assertEqual(header.start, 10000 - 512)

    def test_high_side_sweep_has_ascending_contiguous_rf_coverage(self):
        for bins in (63, 64):
            self.output.seek(0)
            self.output.truncate()
            self.sdr.sweep(9e9, 9e9 + 3000, bins, 4, runs=1, overlap=0.25, crop=True,
                           lnb_lo=10e9, lnb_high_side=True, max_threads=1)
            self.output.seek(0)
            formatter = writer.SoapyPowerBinFormat()
            previous_stop = 9e9
            records = 0
            while True:
                record = formatter.read(self.output)
                if record is None:
                    break
                header, _ = record
                self.assertAlmostEqual(header.start, previous_stop, places=5)
                self.assertGreater(header.stop, header.start)
                previous_stop = header.stop
                records += 1
            self.assertGreater(records, 1)
            self.assertGreaterEqual(previous_stop, 9e9 + 3000)

    def test_buffer_size_does_not_change_acquired_samples(self):
        for limit in (64, 128, 512, 1024):
            self.output.seek(0)
            self.output.truncate()
            self.sdr.sweep(10000, 10000, 63, 17, runs=1, max_buffer_size=limit, max_threads=1)
            self.output.seek(0)
            header, _ = writer.SoapyPowerBinFormat().read(self.output)
            self.assertEqual(header.samples, 1088)


class OutputContractTests(unittest.TestCase):
    def test_binary_geometry_rejected_before_writing(self):
        formatter = writer.SoapyPowerBinFormat()
        for stop, step in ((3, 1), (2, 0), (2, -1), (np.inf, 1)):
            stream = io.BytesIO()
            with self.assertRaises(ValueError):
                formatter.write(stream, 0, 1, 0, stop, step, 64, np.ones(2))
            self.assertEqual(stream.getvalue(), b'')

    def test_header_payload_limit_and_consistency(self):
        formatter = writer.SoapyPowerBinFormat(max_payload_bytes=8)
        for size, stop in ((12, 3), (8, 3), (0, 0), (10, 2)):
            record = formatter.magic + formatter.header_struct.pack(2, 0, 1, 0, stop, 1, 64, size)
            with self.assertRaises(ValueError):
                formatter.read(io.BytesIO(record))

    def test_writer_rejects_mismatches_and_invalid_power(self):
        for freqs, power, log in (([0, 1], [1], True), ([1, 0], [1, 2], True),
                                  ([0, 1, 3], [1, 2, 3], True), ([0, 1], [1, np.nan], True),
                                  ([0, 1], [1, -1], False)):
            with self.assertRaises(ValueError):
                writer.spectrum_data((freqs, power), log)

    def test_linear_text_units_and_output_path_ownership(self):
        import datetime
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'spectrum.txt')
            output = writer.RtlPowerFftwWriter(path, log_scale=False)
            now = datetime.datetime.now(datetime.timezone.utc)
            output.write_async((np.array([0, 1]), np.array([1, 2])), now, now, 64)
            output.close()
            with open(path) as stream:
                text = stream.read()
            self.assertIn('[input power/Hz]', text)
            self.assertNotIn('dB/Hz', text)

    def test_large_rf_offset_does_not_amplify_bin_step_rounding(self):
        import datetime
        bins = 8193
        freqs = 1e12 + np.arange(bins) * (2e6 / bins)
        output_bytes = io.BytesIO()
        output = writer.SoapyPowerBinWriter(output_bytes)
        now = datetime.datetime.now(datetime.timezone.utc)
        output.write((freqs, np.ones(bins)), now, now, bins)
        output.close()
        output_bytes.seek(0)
        header, power = writer.SoapyPowerBinFormat().read(output_bytes)
        self.assertAlmostEqual(header.step, 2e6 / bins, places=7)
        self.assertAlmostEqual(header.stop, freqs[-1] + header.step, places=3)
        self.assertEqual(len(power), bins)

    def test_file_descriptor_is_borrowed(self):
        with tempfile.TemporaryFile() as stream:
            output = writer.SoapyPowerBinWriter(stream.fileno())
            output.close()
            stream.write(b'caller still owns it')

    def test_invalid_cli_does_not_truncate_output(self):
        parser = setup_argument_parser()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'keep.txt')
            with open(path, 'wb') as stream:
                stream.write(b'original')
            parser.parse_args(['-O', path, '-b', '1'])
            with open(path, 'rb') as stream:
                self.assertEqual(stream.read(), b'original')

    def test_nonfinite_and_duplicate_device_options(self):
        for value in ('LNA=nan', 'LNA=inf', '=1', 'LNA=1,LNA=2'):
            with self.assertRaises(ValueError):
                specific_gains(value)
        for value in ('=true', 'biastee=0,biastee=1'):
            with self.assertRaises(ValueError):
                device_settings(value)

    def test_submit_after_nonblocking_shutdown_does_not_wait_for_a_slot(self):
        pool = ThreadPoolExecutor(max_workers=1, max_queue_size=1)
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.addCleanup(pool.shutdown)
        pool.submit(gate.wait, 2)
        pool.submit(lambda: None)
        pool.shutdown(wait=False)
        with self.assertRaises(RuntimeError):
            pool.submit(lambda: None)
        gate.set()
