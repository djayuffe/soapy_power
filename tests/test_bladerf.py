"""bladeRF-style manual controls and settings-matched power calibration."""
import argparse
import json
import math
from types import SimpleNamespace
import unittest
from soapypower.frontend import Frontend
from soapypower.calibration import PowerCalibration
from soapypower.__main__ import correction_pair
import test_architecture as architecture
import test_audit as audit


class Controls:
    def __init__(self):
        self.dc = self.iq = 0j
        self.probes = 0
    def getDriverKey(self): return 'bladerf'
    def hasDCOffset(self, *args): return True
    def hasIQBalance(self, *args): return True
    def hasGainMode(self, *args):
        self.probes += 1
        return True
    def getGainMode(self, *args): return False
    def setDCOffset(self, direction, channel, value):
        self.dc = complex(math.trunc(value.real*2048)/2048, math.trunc(value.imag*2048)/2048)
    def getDCOffset(self, *args): return self.dc
    def setIQBalance(self, direction, channel, value):
        self.iq = complex(math.trunc(value.real*4096)/4096, math.trunc(value.imag*4096)/4096)
    def getIQBalance(self, *args): return self.iq


class BladeRFControlTests(unittest.TestCase):
    def make(self, **kwargs):
        raw = Controls()
        return raw, Frontend(SimpleNamespace(device=raw), 1, 1, **kwargs)

    def test_normalized_quantization_and_context_readback(self):
        raw, frontend = self.make(manual_dc=(0.1234, -0.4567), manual_iq=(0.0234, -0.0345))
        frontend.apply_corrections()
        context = frontend.context({}, {}, 0, False, False)
        corrections = context['hardware_info']['soapypower_manual_rx_corrections']
        self.assertEqual(corrections['DCOffset']['actual'], [raw.dc.real, raw.dc.imag])
        self.assertEqual(corrections['IQBalance']['requested'], [0.0234, -0.0345])
        raw.dc += 0.01
        with self.assertRaisesRegex(RuntimeError, 'manual DCOffset'):
            frontend.context({}, {}, 0, False, False)

    def test_capability_probe_is_not_repeated_during_context_reads(self):
        raw, frontend = self.make()
        frontend.context({}, {}, 0, False, False)
        frontend.context({}, {}, 0, False, False)
        self.assertEqual(raw.probes, 1)

    def test_unsupported_and_automatic_modes_reject_manual_requests(self):
        raw, frontend = self.make(manual_dc=(0.1, 0))
        raw.hasDCOffset = lambda *args: False
        with self.assertRaisesRegex(ValueError, 'support'):
            frontend.apply_corrections()
        raw, frontend = self.make(manual_iq=(0.1, 0))
        raw.hasIQBalanceMode = lambda *args: True
        with self.assertRaisesRegex(ValueError, 'explicitly off'):
            frontend.apply_corrections()

    def test_invalid_normalized_values_and_cli_pairs(self):
        for value in ((2, 0), (float('nan'), 0), (0,), 'bad'):
            with self.assertRaises(ValueError):
                self.make(manual_dc=value)
        self.assertEqual(correction_pair('0.1,-0.2'), (0.1, -0.2))
        for value in ('nan,0', '1,2', '0', '0,0,0'):
            with self.assertRaises(argparse.ArgumentTypeError):
                correction_pair(value)


class BladeRFPipelineTests(unittest.TestCase):
    setUp = audit.AcquisitionTests.setUp
    capable_receiver = architecture.HardwareIntegrationTests.capable_receiver

    def test_manual_corrections_reapplied_and_absolute_profile_matched(self):
        sdr, output = self.capable_receiver()
        raw = sdr.device.device
        controls = Controls()
        for name in ('hasDCOffset', 'hasIQBalance', 'setDCOffset', 'getDCOffset', 'setIQBalance', 'getIQBalance'):
            setattr(raw, name, getattr(controls, name))
        raw.getDriverKey = controls.getDriverKey
        raw.hasDCOffsetMode = raw.hasIQBalanceMode = lambda *args: False
        sdr._frontend = Frontend(sdr.device, 1, 0, manual_dc=(0.1, -0.1), manual_iq=(0.02, -0.03))
        sdr._frontend.apply_corrections()
        sdr.sweep(10000, 10000, 64, 4, runs=1)
        record = json.loads(output.getvalue())
        calibration = PowerCalibration(architecture.profile(ctx=record['metadata']['context']))
        controls.dc = controls.iq = 0j
        output.seek(0); output.truncate()
        sdr.sweep(11000, 11000, 64, 4, runs=1, calibration=calibration)
        self.assertNotEqual(controls.dc, 0)
        self.assertEqual(json.loads(output.getvalue())['unit'], 'dBm/Hz')
        other = architecture.context()
        other.update(record['metadata']['context'])
        other['channel'] = 1
        with self.assertRaisesRegex(ValueError, 'channel'):
            calibration.validate_context(other)
