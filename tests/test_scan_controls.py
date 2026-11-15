"""Regressions for USB stream lifecycle and complete-hop elapsed deadlines."""
import json
import unittest
from unittest import mock
import test_audit as audit
import test_architecture as architecture
from soapypower import writer

class StreamLifecycleTests(unittest.TestCase):
    setUp = audit.AcquisitionTests.setUp
    capable_receiver = architecture.HardwareIntegrationTests.capable_receiver

    def test_reopen_repeated_frequency_tunes_while_stopped_and_keeps_tune_args(self):
        sdr, output = self.capable_receiver(tune_args={'OFFSET': '32'})
        sdr.setup(64, 4, reopen_stream=True, reset_stream=True, max_threads=1)
        raw = sdr.device.device
        original_tune = raw.setFrequency
        def tune(*args):
            self.assertFalse(sdr.device.is_streaming)
            return original_tune(*args)
        with mock.patch.object(raw, 'setFrequency', side_effect=tune), \
             mock.patch.object(sdr.device, 'start_stream', wraps=sdr.device.start_stream) as start, \
             mock.patch.object(sdr.device, 'stop_stream', wraps=sdr.device.stop_stream) as stop:
            for _ in range(2):
                future, _, _ = sdr.psd(10000)
                future.result(timeout=2)
            self.assertEqual(start.call_count, 2)
            self.assertEqual(stop.call_count, 2)
            self.assertEqual(raw.frequency_args, [{'OFFSET': '32'}]*2)
            self.assertTrue(sdr._last_integrity['stream_reopened_per_hop'])

    def test_failed_tune_never_reactivates_stream(self):
        sdr, output = self.capable_receiver(tune_args={'OFFSET': '32'})
        sdr.setup(64, 4, reopen_stream=True, max_threads=1)
        with mock.patch.object(sdr.device.device, 'setFrequency', side_effect=RuntimeError('bad tune')), \
             mock.patch.object(sdr.device, 'start_stream', wraps=sdr.device.start_stream) as start:
            with self.assertRaisesRegex(RuntimeError, 'bad tune'):
                sdr.psd(10000)
            start.assert_not_called()
            self.assertFalse(sdr.device.is_streaming)

class DeadlinePolicyTests(unittest.TestCase):
    setUp = audit.AcquisitionTests.setUp

    def test_finish_hop_retains_strict_timestamps_then_stops(self):
        now = [0.0]
        original = self.sdr.device.read_stream
        index = [0]
        def read():
            packet = original()
            packet.flags = 4
            packet.timeNs = index[0]*62500000
            index[0] += 1
            now[0] += .1
            return packet
        self.sdr.device.read_stream = read
        with mock.patch.object(self.power.time, 'monotonic', side_effect=lambda: now[0]):
            self.sdr.sweep(10000, 20000, 64, 4, runs=1, time_limit=.15,
                           deadline_policy='finish-hop', partial_policy='reject',
                           require_timestamps=True, max_threads=1)
        self.output.seek(0)
        fmt = writer.SoapyPowerBinFormat()
        header, _ = fmt.read(self.output)
        self.assertEqual(header.samples, 256)
        self.assertEqual(index[0], 4)
        self.assertIsNone(fmt.read(self.output))
        self.assertIsNone(self.sdr._deadline)
        self.assertFalse(self.sdr.device.is_streaming)

    def test_finish_hop_does_not_suppress_signal_cancellation(self):
        original = self.sdr.device.read_stream
        def read():
            packet = original()
            self.power._shutdown = True
            return packet
        self.sdr.device.read_stream = read
        self.sdr.sweep(10000, 20000, 64, 4, runs=1, deadline_policy='finish-hop',
                       partial_policy='legacy', max_threads=1)
        self.output.seek(0)
        fmt = writer.SoapyPowerBinFormat()
        header, _ = fmt.read(self.output)
        self.assertEqual(header.samples, 64)
        self.assertIsNone(fmt.read(self.output))

    def test_unknown_policy_rejects_before_setup(self):
        with mock.patch.object(self.sdr, 'setup') as setup:
            with self.assertRaisesRegex(ValueError, 'deadline policy'):
                self.sdr.sweep(10000, 10000, 64, 4, deadline_policy='invalid')
            setup.assert_not_called()
