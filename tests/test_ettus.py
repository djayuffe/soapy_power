"""USRP/UHD-style clocking, sensor domains, mapping and manual controls."""
import io
import json
from types import SimpleNamespace
import unittest
from soapypower.frontend import Frontend
from soapypower.calibration import PowerCalibration
import test_audit as audit
import test_architecture as architecture


class ClockControls:
    def __init__(self):
        self.clock='internal';self.time='internal';self.rate=32e6
    def listClockSources(self): return ['internal','external']
    def listTimeSources(self): return ['internal','external']
    def setClockSource(self,value): self.clock=value
    def getClockSource(self): return self.clock
    def setTimeSource(self,value): self.time=value
    def getTimeSource(self): return self.time
    def setMasterClockRate(self,value): self.rate=value
    def getMasterClockRate(self): return self.rate
    def getHardwareInfo(self): return {'mboard_serial':'USRP-TEST','rx0_serial':'DB-TEST'}
    def getFrontendMapping(self,direction): return 'A:0'
    def getDriverKey(self): return 'b200'  # SoapyUHD driver key can be a device type.


class EttusControlTests(unittest.TestCase):
    def frontend(self, **kwargs):
        raw=ClockControls()
        return raw,Frontend(SimpleNamespace(device=raw),1,0,**kwargs)

    def test_clock_time_master_rate_readback_and_usrp_identity(self):
        raw,frontend=self.frontend()
        frontend.configure_clocking('external','external',30.72e6)
        context=frontend.context({}, {},0,False,False)
        self.assertEqual(context['clock_source'],'external')
        self.assertEqual(context['time_source'],'external')
        self.assertEqual(context['master_clock_rate'],30.72e6)
        self.assertEqual(context['serial'],'USRP-TEST')
        self.assertEqual(context['hardware_info']['soapypower_rx_frontend_mapping'],'A:0')

    def test_invalid_unavailable_and_ignored_clock_configuration(self):
        raw,frontend=self.frontend()
        with self.assertRaises(ValueError): frontend.configure_clocking('unsupported')
        with self.assertRaises(ValueError): frontend.configure_clocking(master_clock_rate=-1)
        raw.setClockSource=lambda value: None
        with self.assertRaisesRegex(RuntimeError,'ClockSource'): frontend.configure_clocking('external')
        with self.assertRaises(ValueError): Frontend(SimpleNamespace(device=None),1,0).configure_clocking('external')

    def test_explicit_uhd_manual_tolerance_and_auto_conflict(self):
        raw,frontend=self.frontend(manual_dc=(0.1,0),manual_tolerance=0.001,dc_correction='off')
        raw.hasDCOffset=lambda *args: True
        raw.hasDCOffsetMode=lambda *args: True
        raw.setDCOffsetMode=lambda *args: None
        raw.getDCOffsetMode=lambda *args: False
        raw.setDCOffset=lambda *args: None
        raw.getDCOffset=lambda *args: 0.1005+0j
        frontend.apply_corrections()
        info=frontend.context({}, {},0,False,False)['hardware_info']['soapypower_manual_rx_corrections']
        self.assertEqual(info['DCOffset']['readback_tolerance'],0.001)
        raw.getDCOffset=lambda *args: 0.2+0j
        with self.assertRaises(RuntimeError): frontend.apply_corrections()


class EttusPipelineTests(unittest.TestCase):
    setUp=audit.AcquisitionTests.setUp
    capable_receiver=architecture.HardwareIntegrationTests.capable_receiver

    def test_reference_and_lo_lock_domains_and_context_matched_calibration(self):
        sdr,output=self.capable_receiver()
        raw=sdr.device.device;clock=ClockControls()
        for method in ('listClockSources','setClockSource','getClockSource','listTimeSources','setTimeSource','getTimeSource','setMasterClockRate','getMasterClockRate','getFrontendMapping','getHardwareInfo','getDriverKey'):
            setattr(raw,method,getattr(clock,method))
        calls=[]
        def sensor(*args):
            calls.append(args)
            return 'true'
        raw.readSensor=sensor
        sdr._frontend.configure_clocking('external','external',32e6)
        sdr.sweep(10000,10000,64,4,runs=1,lock_sensor='lo_locked',reference_lock_sensor='ref_locked')
        record=json.loads(output.getvalue())
        self.assertIn(('ref_locked',),calls)
        self.assertIn((1,0,'lo_locked'),calls)
        self.assertEqual(record['metadata']['context']['serial'],'USRP-TEST')
        profile=PowerCalibration(architecture.profile(ctx=record['metadata']['context']))
        output.seek(0);output.truncate()
        sdr.sweep(10000,10000,64,4,runs=1,calibration=profile)
        self.assertEqual(json.loads(output.getvalue())['unit'],'dBm/Hz')
        clock.clock='internal'
        with self.assertRaisesRegex(ValueError,'clock_source'):
            sdr.sweep(10000,10000,64,4,runs=1,calibration=profile)

    def test_reference_lock_timeout_and_missing_sensor_fail(self):
        for value in (None,'false'):
            sdr,output=self.capable_receiver()
            sdr.device.device.readSensor=lambda *args: value
            with self.assertRaises((ValueError,RuntimeError)):
                sdr.sweep(10000,10000,64,1,runs=1,reference_lock_sensor='ref_locked',lock_timeout=0.005)
            self.assertEqual(output.getvalue(),b'')
