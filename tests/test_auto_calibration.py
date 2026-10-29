"""Real DSP and simulated SDR/source boundaries for guided automation."""
import copy
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock
import numpy as np

from soapypower.auto_calibration import configuration, plan, run_session, SourceController, family
from soapypower.calibration import PowerCalibration
from soapypower.calibration_cli import main
import test_audit as audit
import test_architecture as architecture


def config(method='tone'):
    c={'version':1,'receiver':{'device':'driver=test','sample_rate':1024,'gain':20.7},
       'rf_hz':[10000,20000],'baseband_hz':[-128,128],'reference_baseband_hz':128,
       'reference':'SYNTHETIC source only','reference_plane':'simulated connector',
       'holdouts':[{'rf_hz':15000,'baseband_hz':-128},{'rf_hz':15000,'baseband_hz':128}],
       'physical_setup_confirmed':True,
       'acquisition':{'fft_bins':64,'segments':32,'integration_width_hz':128,'tune_delay':0,'settle_samples':0}}
    if method=='tone':c['source']={'type':'tone','levels_dbm':[-50,-40,-30],'path_loss_db':0,'max_receiver_input_dbm':-20}
    elif method=='hot-cold':c['source']={'type':'hot-cold','cold_temperature_k':300,'hot_temperature_k':3200,
        'source_ohms':50,'receiver_ohms':50,'linearity_confirmed':True}
    else:c.update(source=None,termination={'temperature_k':290,'source_ohms':75,'receiver_ohms':50})
    return c


class ConfigurationTests(unittest.TestCase):
    def test_family_rules_distinguish_special_cases(self):
        for selector,expected in [('driver=airspyhf','airspyhf'),('driver=uhd,type=b200','uhd'),
                ('driver=remote,remote:driver=airspyhf','remote'),('driver=rtlsdr','rtlsdr'),
                ('driver=lime','lime'),('driver=other','unknown')]:
            self.assertEqual(family(selector),expected)
    def test_invalid_geometry_and_missing_evidence_reject_without_hardware(self):
        for mutate in (lambda c:c['receiver'].update(gain=None),lambda c:c.update(holdouts=[]),
                       lambda c:c.update(baseband_hz=[0,128]),lambda c:c['source'].update(levels_dbm=[-30,-30,-20]),
                       lambda c:c['receiver'].update(settings={'digital_agc':True}),
                       lambda c:c['source'].update(path_loss_db=[[10000,0],[12000,1]]),
                       lambda c:c['source'].update(max_receiver_input_dbm=-35),
                       lambda c:c.update(source={'type':'baseline'})):
            c=config();mutate(c)
            with self.assertRaises(ValueError):plan(c,offline=True)
    def test_offline_plan_is_never_claimed_ready(self):
        c=config();c['receiver']['device']='driver=airspyhf'
        p=plan(c,offline=True)
        self.assertEqual(p['status'],'unverified_offline')
        self.assertTrue(any('HF+' in b for b in p['blockers']))
    def test_hot_cold_requires_confirmed_linearity_and_matching_impedance(self):
        c=config('hot-cold');c['source']['linearity_confirmed']=False
        self.assertTrue(any('linear' in b for b in plan(c,offline=True)['blockers']))
        c['source']['source_ohms']=75
        with self.assertRaises(ValueError):configuration(c)
    def test_assist_offline_cli_protects_input_and_publishes_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'config.json';source.write_text(json.dumps(config()))
            self.assertEqual(main(['assist',str(source),'--offline','-O',str(root/'plan.json')]),0)
            with mock.patch('sys.stderr'),self.assertRaises(SystemExit):
                main(['assist',str(source),'--offline','-O',str(source),'--force'])
            self.assertEqual(json.loads(source.read_text())['version'],1)


    def test_cli_interrupt_has_explicit_status_without_a_traceback(self):
        with mock.patch('soapypower.auto_calibration.install_signal_handlers'), \
                mock.patch('soapypower.auto_calibration.run_session',side_effect=KeyboardInterrupt), \
                mock.patch('sys.stderr'),self.assertRaises(SystemExit) as stopped:
            main(['auto','unused.json','--directory','unused-session'])
        self.assertEqual(stopped.exception.code,130)


class SourceProtocolTests(unittest.TestCase):
    def test_noninteractive_session_requires_explicit_hook(self):
        with self.assertRaisesRegex(ValueError,'terminal'):SourceController(input_stream=io.StringIO())
    def test_operator_must_confirm_each_state(self):
        stream=io.StringIO('READY\nno\n');stream.isatty=lambda:True
        source=SourceController(input_stream=stream,output_stream=io.StringIO())
        self.assertTrue(source('off',1.)['confirmed'])
        with self.assertRaisesRegex(ValueError,'confirmed'):source('on',1.,-40.)
    def test_hook_acknowledgment_binding_timeout_and_changed_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            executable=Path(temp)/'source';executable.write_text('synthetic')
            controller=SourceController(executable)
            def execute(argv,input,stdout,stderr,timeout,check):
                request=json.loads(input);request['confirmed']=True;stdout.write(json.dumps(request).encode())
                return types.SimpleNamespace(returncode=0)
            with mock.patch('soapypower.auto_calibration.subprocess.run',side_effect=execute):
                self.assertTrue(controller('on',1.,-30.)['confirmed'])
            def stale(argv,input,stdout,stderr,timeout,check):
                request=json.loads(input);request.update(confirmed=True,request_id='old');stdout.write(json.dumps(request).encode())
                return types.SimpleNamespace(returncode=0)
            with mock.patch('soapypower.auto_calibration.subprocess.run',side_effect=stale),self.assertRaisesRegex(ValueError,'mismatch'):
                controller('on',1.,-30.)
            from subprocess import TimeoutExpired
            with mock.patch('soapypower.auto_calibration.subprocess.run',side_effect=TimeoutExpired('source',1)),self.assertRaisesRegex(ValueError,'timed out'):
                controller('off',1.)
            executable.write_text('changed')
            with self.assertRaisesRegex(ValueError,'changed'):controller('off',1.)


class SessionTests(unittest.TestCase):
    setUp=audit.AcquisitionTests.setUp
    capable_receiver=architecture.HardwareIntegrationTests.capable_receiver

    def simulation(self, method='tone', compress=False, fail=False, drifting=False, holdout_bias=False):
        current={'state':'off','rf':10000,'level':-40}
        class Controller:
            automatic=True
            def __call__(controller,state,rf,level=None):
                current.update(state=state,rf=rf,level=level)
                return {'state':state,'rf_hz':rf,'input_dbm':level,'confirmed':True}
        def factory(**options):
            receiver,_=self.capable_receiver()
            raw=receiver.device.device
            raw.getSettingInfo=lambda:[]
            position=[0]
            def read():
                if fail and current['state']=='on':raise RuntimeError('simulated disconnect')
                if drifting:receiver.device.gain+=1
                n=len(receiver.device.buffer);indices=np.arange(n)+position[0];position[0]+=n
                if method=='hot-cold':
                    # Flat periodic comb: source-temperature ratios are exact.
                    rng=np.random.default_rng(37)
                    comb=np.fft.ifft(np.exp(2j*np.pi*rng.random(64)))
                    temperature=3200 if current['state']=='hot' else 300
                    receiver.device.buffer[:]=comb[indices%64]*np.sqrt(temperature+580)*1e-5
                elif current['state']=='on':
                    power=10**(current['level']/10)
                    if compress:power=power**0.5
                    if holdout_bias and current['rf']==15000:power*=2
                    sign=-1 if receiver._lnb_high_side else 1
                    bb=sign*(current['rf']-receiver._lnb_lo)-receiver.device.freq
                    if receiver._iq_conjugate:bb=-bb
                    receiver.device.buffer[:]=np.sqrt(power)*np.exp(2j*np.pi*bb*indices/1024)
                else:receiver.device.buffer[:]=0
                receiver.device.reads+=1
                return types.SimpleNamespace(ret=n,flags=0,timeNs=0)
            receiver.device.read_stream=read
            return receiver
        return Controller(),factory,current

    def run_config(self,c,controller,factory):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);path=root/'config.json';path.write_text(json.dumps(c))
        return run_session(path,root/'session',controller,factory),root/'session'

    def test_tone_captures_linearity_grid_and_independent_holdouts(self):
        controller,factory,current=self.simulation()
        report,directory=self.run_config(config(),controller,factory)
        self.assertEqual(report['status'],'completed');self.assertTrue(report['linearity']['accepted'])
        self.assertTrue(report['validation']['passed']);self.assertEqual(current['state'],'off')
        profile=PowerCalibration.load(directory/'profile.json')
        np.testing.assert_allclose(profile.offsets_db([10000,20000],[-128,128]),0,atol=2e-6)
        self.assertEqual(len(list(directory.glob('capture-*.jsonl'))),36)
        self.assertTrue((directory/'measurements.json').exists());self.assertTrue((directory/'holdouts.json').exists())

    def test_terminated_baseline_does_not_publish_absolute_profile(self):
        controller,factory,_=self.simulation()
        report,directory=self.run_config(config('baseline'),controller,factory)
        self.assertFalse(report['absolute_profile_published']);self.assertFalse((directory/'profile.json').exists())
        self.assertEqual(len(report['baseline']['cells']),4)

    def test_hot_cold_end_to_end(self):
        controller,factory,_=self.simulation('hot-cold')
        c=config('hot-cold');c['source']['hot_temperature_k']=[[10000,3200],[20000,3200]]
        report,directory=self.run_config(c,controller,factory)
        self.assertTrue(report['validation']['passed']);self.assertTrue((directory/'profile.json').exists())
        self.assertEqual(report['last_source_state'],'cold')

    def test_compression_or_fixed_context_drift_never_publishes_profile(self):
        for options in ({'compress':True},{'drifting':True},{'fail':True},{'holdout_bias':True}):
            controller,factory,current=self.simulation(**options)
            with tempfile.TemporaryDirectory() as temp:
                root=Path(temp);path=root/'config.json';path.write_text(json.dumps(config()))
                with self.assertRaises((ValueError,RuntimeError)):run_session(path,root/'session',controller,factory)
                self.assertFalse((root/'session/profile.json').exists())
                self.assertEqual(json.loads((root/'session/session.json').read_text())['status'],'failed')
                if options.get('fail'):self.assertEqual(current['state'],'off')

    def test_prepare_disables_only_advertised_secondary_controls_and_verifies(self):
        controller,factory,_=self.simulation()
        writes=[]
        def described(**options):
            sdr=factory(**options);settings={'digital_agc':'true','biastee':'true'}
            sdr._frontend.raw.getSettingInfo=lambda:[types.SimpleNamespace(key=k,options=['false','true']) for k in settings]
            sdr._frontend.raw.readSetting=lambda key:settings[key]
            def write(key,value):settings[key]=value;writes.append((key,value))
            sdr._frontend.raw.writeSetting=write
            return sdr
        p=plan(config(),factory=described)
        self.assertEqual(p['status'],'ready');self.assertEqual(writes,[('digital_agc','false'),('biastee','false')])
        self.assertTrue(p['context']['dc_correction']);self.assertTrue(p['context']['iq_correction'])

    def test_existing_session_directory_is_not_overwritten(self):
        controller,factory,_=self.simulation()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'config.json';path.write_text(json.dumps(config()))
            target=root/'session';target.mkdir();(target/'evidence').write_text('keep')
            with self.assertRaises(FileExistsError):run_session(path,target,controller,factory)
            self.assertEqual((target/'evidence').read_text(),'keep')

    def test_unadvertised_lock_sensor_blocks_before_any_source_switch(self):
        controller,factory,current=self.simulation()
        c=config();c['acquisition']['lock_sensor']='missing'
        with self.assertRaisesRegex(ValueError,'not advertised'):plan(c,factory=factory)
        self.assertEqual(current['state'],'off')

    def test_unknown_secondary_agc_readback_blocks_plan(self):
        controller,factory,_=self.simulation()
        def broken(**options):
            sdr=factory(**options)
            sdr._frontend.raw.getSettingInfo=lambda:[types.SimpleNamespace(key='digital_agc',options=[])]
            sdr._frontend.raw.writeSetting=lambda *args:None
            sdr._frontend.raw.readSetting=lambda *args:None
            return sdr
        with self.assertRaisesRegex(ValueError,'cannot verify'):plan(config(),factory=broken)

    def test_profile_publication_failure_leaves_failed_report_and_no_profile(self):
        controller,factory,_=self.simulation()
        from soapypower import auto_calibration as module
        original=module.write_json
        def fail_profile(path,*args,**kwargs):
            if Path(path).name=='profile.json':raise OSError('publication failed')
            return original(path,*args,**kwargs)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'config.json';path.write_text(json.dumps(config()))
            with mock.patch.object(module,'write_json',side_effect=fail_profile),self.assertRaisesRegex(OSError,'publication failed'):
                run_session(path,root/'session',controller,factory)
            self.assertFalse((root/'session/profile.json').exists())
            self.assertEqual(json.loads((root/'session/session.json').read_text())['status'],'failed')

    def test_configuration_mutation_during_capture_prevents_profile(self):
        controller,factory,_=self.simulation()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'config.json';path.write_text(json.dumps(config()))
            def changing(state,rf,level=None):
                result=controller(state,rf,level)
                with path.open('a') as stream:stream.write(' ')
                return result
            with self.assertRaisesRegex(ValueError,'changed'):run_session(path,root/'session',changing,factory)
            self.assertFalse((root/'session/profile.json').exists())

    def test_mutated_linearity_capture_is_detected_before_profile_publication(self):
        controller,factory,_=self.simulation()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'config.json';path.write_text(json.dumps(config()))
            modified=[False]
            def changing(state,rf,level=None):
                result=controller(state,rf,level)
                capture=root/'session/capture-0001-on.jsonl'
                if capture.exists() and not modified[0]:
                    with capture.open('a') as stream:stream.write(' ')
                    modified[0]=True
                return result
            with self.assertRaisesRegex(ValueError,'changed'):run_session(path,root/'session',changing,factory)
            self.assertFalse((root/'session/profile.json').exists())

    def test_external_converter_and_iq_orientation_use_one_rf_mapping(self):
        for high_side,conjugate,lo in ((False,False,1000),(True,False,30000),(True,True,30000),(False,True,1000)):
            controller,factory,_=self.simulation()
            c=config();c['receiver'].update(lnb_lo=lo,lnb_high_side=high_side,iq_conjugate=conjugate)
            report,directory=self.run_config(c,controller,factory)
            self.assertTrue(report['validation']['passed'])
            profile=PowerCalibration.load(directory/'profile.json')
            np.testing.assert_allclose(profile.offsets_db([10000,20000],[-128,128]),0,atol=2e-6)

    def test_passive_termination_runs_unattended_without_a_source_controller(self):
        _,factory,_=self.simulation()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'config.json';path.write_text(json.dumps(config('baseline')))
            with mock.patch('soapypower.auto_calibration.SourceController',side_effect=AssertionError('no generator needed')):
                report=run_session(path,root/'session',factory=factory)
            self.assertEqual(report['status'],'completed')
            self.assertTrue(all(e['mode']=='operator_configured_passive_termination' for e in report['events']))
            self.assertFalse((root/'session/profile.json').exists())

    def test_cli_signal_handlers_interrupt_waiting_prompts(self):
        import signal
        from soapypower.auto_calibration import install_signal_handlers
        with mock.patch.object(signal,'signal') as register:
            install_signal_handlers()
        handlers={call.args[0]:call.args[1] for call in register.call_args_list}
        for name in (signal.SIGINT,signal.SIGTERM):
            with self.assertRaises(KeyboardInterrupt):handlers[name](name,None)

    def test_interrupt_during_source_change_records_failure_and_attempts_off(self):
        controller,factory,current=self.simulation()
        class Interrupting:
            automatic=True
            def __call__(self,state,rf,level=None):
                if state=='on':raise KeyboardInterrupt('simulated Ctrl-C')
                return controller(state,rf,level)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'config.json';path.write_text(json.dumps(config()))
            with self.assertRaises(KeyboardInterrupt):run_session(path,root/'session',Interrupting(),factory)
            self.assertFalse((root/'session/profile.json').exists())
            report=json.loads((root/'session/session.json').read_text())
            self.assertEqual(report['status'],'failed');self.assertEqual(report['last_source_state'],'off')
