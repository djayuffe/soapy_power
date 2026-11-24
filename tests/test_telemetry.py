"""Telemetry preserves source semantics, polling budgets and reference boundaries."""
import importlib.util
import copy,hashlib,io,json,tempfile,types,unittest
from pathlib import Path
from unittest import mock
import numpy as np
from soapypower.telemetry import TelemetryMonitor,TelemetryPolicy,digest
from soapypower.filter_model import validate_chain,digital_response
from soapypower.telemetry_models import fit_thermal,ThermalModel,fit_settling,evaluate_settling,ModelContextMismatch
from soapypower.telemetry_cli import apply_thermal,measurement_signature,main
from soapypower.frontend import Frontend
from soapypower.rf_state import lo_mapping
import test_architecture as architecture
import test_audit as audit


def chain():
 return dict(schema='soapypower_native_uhd_v1',module='fixture',sample_rate_hz=1024,bandwidth_hz=1024,master_clock_hz=2048,native_format='CS16',native_full_scale=32768,iq_tracking=True,dc_tracking=False,has_power_reference=False,complete_receiver_response=False,filters=[dict(name='fir',bypassed=False,input_rate_hz=4096,decimation=2,interpolation=1,full_scale=4,taps=[1,2,1])])

class Clock:
 def __init__(self):self.now=0.
 def __call__(self):return self.now

class Raw:
 def __init__(self,clock):self.clock=clock;self.values={'temp':'45','rssi':'-60.25','lo_locked':'true','ref_locked':'false'};self.calls=[];self.chain=chain();self.external=False;self.failure=None
 def listSensors(self,*args):return ['temp','rssi','lo_locked'] if args else ['ref_locked']
 def getSensorInfo(self,*args):
  key=args[-1];return types.SimpleNamespace(type=0 if key.endswith('locked') else 2,units='locked' if key.endswith('locked') else 'C' if key=='temp' else 'dB',description='fixture')
 def readSensor(self,*args):
  self.calls.append(args);self.clock.now+=.003 if args[-1]=='temp' else .0001
  if self.failure==args[-1]:raise RuntimeError('sensor failed')
  return self.values[args[-1]]
 def listFrequencies(self,*args):return ['RF','BB','CORR']
 def getFrequency(self,*args):return 10000 if args[-1]=='RF' else 100
 def getClockSource(self):return 'external' if self.external else 'internal'
 def getTimeSource(self):return 'internal'
 def getMasterClockRate(self):return 2048
 def hasDCOffsetMode(self,*a):return True
 def hasIQBalanceMode(self,*a):return True
 def getDCOffsetMode(self,*a):return self.chain['dc_tracking']
 def getIQBalanceMode(self,*a):return self.chain['iq_tracking']
 def getSettingInfo(self,*a):return [types.SimpleNamespace(key='SOAPY_POWER_RX_CHAIN_V1' if a else 'SOAPY_POWER_DEVICE_INFO_V1')]
 def readSetting(self,*args):return json.dumps(self.chain if len(args)>1 else dict(schema='soapypower_native_uhd_v1',usb_version=3))
 def readGPIO(self,bank):return 0

class MonitorTests(unittest.TestCase):
 def receiver(self,policy=None):
  c=Clock();raw=Raw(c);wrapper=types.SimpleNamespace(device=raw,is_streaming=False,sample_rate=1024,bandwidth=1024,antenna='RX');frontend=Frontend(wrapper,1,0);m=TelemetryMonitor(frontend,policy,clock=c);return m,raw,c,wrapper
 def test_sensor_schema_typed_units_and_boolean_labels(self):
  m,r,c,w=self.receiver();m.closed_slot();s=m.snapshot();json.dumps(s,allow_nan=False)
  self.assertEqual(s['telemetry']['rx:temp']['value'],45);self.assertIs(s['telemetry']['rx:lo_locked']['value'],True)
  self.assertIsNone(s['sensor_schema']['rx:lo_locked']['unit']);self.assertEqual(s['sensor_schema']['rx:temp']['unit'],'C')
 def test_temperature_never_polled_while_streaming(self):
  m,r,c,w=self.receiver();w.is_streaming=True;m.closed_slot();s=m.snapshot()
  self.assertEqual(s['telemetry']['rx:temp']['status'],'deferred');self.assertFalse(any(a[-1]=='temp' for a in r.calls))
 def test_temperature_cached_until_interval_and_age_preserved(self):
  m,r,c,w=self.receiver();m.closed_slot();first=m.snapshot()['telemetry']['rx:temp'];c.now+=.2;m.closed_slot();second=m.snapshot()['telemetry']['rx:temp']
  self.assertEqual(first['read_stop_monotonic_s'],second['read_stop_monotonic_s']);self.assertGreater(second['age_s'],first['age_s']);self.assertEqual(m.read_counts['rx:temp'],1)
  c.now+=1;m.closed_slot();self.assertEqual(m.read_counts['rx:temp'],2)
 def test_stale_and_failed_values_do_not_become_valid_zero(self):
  m,r,c,w=self.receiver({'require_temperature':True});m.closed_slot();m.snapshot();c.now+=4;s=m.snapshot();self.assertEqual(s['telemetry']['rx:temp']['status'],'stale');self.assertFalse(s['validity']['passed'])
  r.failure='temp';m.closed_slot();s=m.snapshot();self.assertEqual(s['telemetry']['rx:temp']['status'],'error');self.assertEqual(s['telemetry']['rx:temp']['last_good']['value'],45);self.assertIsNone(s['validity']['temperature_c'])
 def test_zero_temperature_is_a_measurement(self):
  m,r,c,w=self.receiver({'require_temperature':True});r.values['temp']='0';m.closed_slot();self.assertEqual(m.snapshot()['validity']['temperature_c'],0)
 def test_invalid_numeric_and_boolean_values_remain_errors(self):
  m,r,c,w=self.receiver();r.values['temp']='nan';r.values['lo_locked']='possibly';m.closed_slot();s=m.snapshot();self.assertEqual(s['telemetry']['rx:temp']['status'],'error');self.assertIsNone(s['telemetry']['rx:lo_locked']['value'])
 def test_internal_reference_false_is_not_external_failure(self):
  m,r,c,w=self.receiver();m.closed_slot();self.assertTrue(m.snapshot()['validity']['passed']);r.external=True;self.assertIn('selected_reference_not_locked_or_unknown',m.snapshot()['validity']['failures']);r.values['ref_locked']='true';self.assertTrue(m.snapshot()['validity']['passed'])
 def test_explicit_required_reference_is_strict(self):
  m,r,c,w=self.receiver({'reference_lock':'required'});m.closed_slot();self.assertFalse(m.snapshot()['validity']['passed'])
 def test_temperature_and_latency_domains_are_explicit(self):
  m,r,c,w=self.receiver({'temperature_range_c':[30,40],'max_latency_ms':2});m.closed_slot();s=m.snapshot();self.assertIn('temperature_outside_validated_domain',s['validity']['failures']);self.assertIn('telemetry_latency_budget_exceeded',s['validity']['failures'])
 def test_native_chain_fingerprint_and_configuration_refresh(self):
  m,r,c,w=self.receiver();first=m.chain['sha256'];self.assertEqual(m.chain['status'],'ok');m.closed_slot();self.assertEqual(m.native_read_count,1)
  r.chain['iq_tracking']=False;m.closed_slot();self.assertNotEqual(m.chain['sha256'],first)
 def test_expected_fingerprint_mismatch_is_rejected(self):
  m,r,c,w=self.receiver({'expected_chain_sha256':'0'*64});m.closed_slot();self.assertIn('processing_chain_fingerprint_mismatch',m.snapshot()['validity']['failures'])
 def test_fixture_feedback_has_no_implicit_mapping(self):
  m,r,c,w=self.receiver({'gpio':[dict(bank='FP0',mask=1,expected=1,reference='independently verified load-switch feedback')]});m.closed_slot();self.assertIn('fixture_feedback_mismatch_or_unknown',m.snapshot()['validity']['failures'])
 def test_thermal_trend_uses_time_window(self):
  m,r,c,w=self.receiver({'warmup_window_s':10,'warmup_max_slope_c_per_min':1});
  for t in [0,3,6,9,12]:c.now=t;r.values['temp']='45';m.closed_slot()
  self.assertTrue(m.snapshot()['thermal']['stable'])
 def test_rssi_iq_disagreement_is_advisory_and_state_bound(self):
  m,r,c,w=self.receiver();m.closed_slot();s=m.snapshot();ctx=architecture.context();h={'ac_rms':.1};self.assertEqual(m.qualify_iq(ctx,s,h)['status'],'baseline');h['ac_rms']=.001;result=m.qualify_iq(ctx,s,h);self.assertEqual(result['status'],'disagreement');self.assertFalse(result['automatic_gain_change']);ctx['gains']={'overall':10};self.assertEqual(m.qualify_iq(ctx,s,h)['status'],'baseline')
 def test_bad_policy_types_ranges_and_budgets_fail(self):
  for value in [{'temperature_interval_s':0},{'temperature_max_age_s':.1},{'require_temperature':1},{'temperature_range_c':[1,1]},{'history_samples':100001},{'reference_lock':'guess'},{'gpio':[dict(bank='FP0',mask=1,expected=2,reference='x')]}]:
   with self.subTest(value=value),self.assertRaises(ValueError):TelemetryPolicy(value)
 def test_lo_mapping_handles_shift_and_preserves_hypothesis(self):
  s={'tuning_components_hz':{'RF':50e6,'BB':14e6}};r=lo_mapping(s,36e6,28e6);self.assertEqual(r['rx_rf_minus_bb_residual_hz'],0);self.assertEqual(r['analog_lo_baseband_hz'],-14e6);self.assertFalse(r['automatic_exclusion'])

@unittest.skipUnless(importlib.util.find_spec('scipy'), 'needs SciPy (optional [fft] extra)')
class FilterTests(unittest.TestCase):
 def test_chain_validation_is_idempotent_and_preserves_raw_source(self):
  original=chain();before=copy.deepcopy(original);a=validate_chain(original);b=validate_chain(original)
  self.assertEqual(original,before);self.assertIs(type(original['filters'][0]['taps'][0]),int)
  self.assertEqual(digest(a),digest(b));self.assertEqual(digest(a),digest(validate_chain(a)))
 def test_native_chain_rejects_unknown_scales_or_missing_taps(self):
  for change in [{'native_full_scale':0},{'filters':[dict(name='x',bypassed=False,taps=[])]}]:
   with self.assertRaises(ValueError):validate_chain(dict(chain(),**change))
 def test_partial_filter_phase_delay_and_dc_shape(self):
  r=digital_response(chain(),[0,128,256]);self.assertAlmostEqual(abs(r['response'][0]),1);self.assertAlmostEqual(r['exposed_linear_phase_delay_s'],1/4096);self.assertFalse(r['inverse_correction_authorized'])
 def test_asymmetric_filter_unknown_delay_stays_unknown(self):
  c=chain();c['filters']=[dict(c['filters'][0],taps=[1,2,0]),dict(c['filters'][0],name='symmetric')];r=digital_response(c,[0,100]);self.assertIsNone(r['exposed_linear_phase_delay_s'])
 def test_filter_model_does_not_extrapolate_stage_nyquist(self):
  with self.assertRaises(ValueError):digital_response(chain(),[3000])


def source(context='a'*64):return dict(reference='Synthetic known-reference fixture',reference_sha256='b'*64,controlled_input=True,sensor_key='rx:temp',context_sha256=context,response_scope='whole_grid_scalar',max_sensor_age_s=3)
def thermal(context='a'*64):
 rows=[]
 for partition,temps in [('train',[20,25,30,35]),('validation',[22,28,34])]:
  for t in temps:rows.append(dict(partition=partition,temperature_c=t,correction_db=1+.02*(t-27.5),capture_sha256=hashlib.sha256((partition+str(t)).encode()).hexdigest()))
 return dict(version=1,type='thermal_reference_measurements',source=source(context),measurements=rows,max_validation_error_db=.1,max_correction_db=2,reference_uncertainty_db=.02,temperature_uncertainty_c=.1)

class ReferenceModelTests(unittest.TestCase):
 def test_thermal_model_freezes_and_validates_holdouts(self):
  value=fit_thermal(thermal());m=ThermalModel(value);r=m.evaluate(27.5,'a'*64,'rx:temp');self.assertAlmostEqual(r['correction_db'],1);self.assertGreater(r['conditional_uncertainty_db'],0);self.assertFalse(r['absolute_power_calibration'])
 def test_antenna_cannot_establish_thermal_correction(self):
  v=thermal();v['source']['controlled_input']=False
  with self.assertRaisesRegex(ValueError,'uncontrolled'):fit_thermal(v)
 def test_model_refuses_context_domain_and_digest_mismatch(self):
  m=ThermalModel(fit_thermal(thermal()))
  for t,ctx in [(40,'a'*64),(25,'c'*64)]:
   with self.assertRaises(ValueError):m.evaluate(t,ctx,'rx:temp')
  v=fit_thermal(thermal());v['coefficients_db'][0]=99
  with self.assertRaisesRegex(ValueError,'digest'):ThermalModel(v)
 def test_overfit_or_duplicate_or_untrained_temperature_fails(self):
  for kind in ('duplicate','holdout','outside'):
   v=thermal()
   if kind=='duplicate':v['measurements'][-1]['capture_sha256']=v['measurements'][0]['capture_sha256']
   if kind=='holdout':
    for r in v['measurements']:
     if r['partition']=='validation':r['correction_db']=-10
   if kind=='outside':v['measurements'][-1]['temperature_c']=50
   with self.subTest(kind=kind),self.assertRaises(ValueError):fit_thermal(v)
 def test_thermal_apply_preserves_raw_and_units(self):
  with tempfile.TemporaryDirectory() as directory:
   p=Path(directory)/'raw.npz';ctx=architecture.context();grid=[0,1,2];proc={'window':'hann'};sig=measurement_signature(ctx,grid,proc);model=ThermalModel(fit_thermal(thermal(sig)))
   np.savez(p,density=[0,1],frequency_edges_hz=grid,metadata=np.array(json.dumps(dict(context=ctx,processing=proc,units='input-power/Hz'))))
   obs=dict(fresh=True,age_s=.5,max_age_s=3,temperature_c=27.5,context_sha256=sig,sensor_key='rx:temp')
   with np.load(p) as z:r=apply_thermal(model,z,obs)
   np.testing.assert_array_equal(r['raw_density'],[0,1]);self.assertAlmostEqual(r['density'][1],10**.1);self.assertEqual(json.loads(str(r['metadata']))['units'],'input-power/Hz')
   obs['age_s']=4;obs['max_age_s']=100
   with np.load(p) as z,self.assertRaisesRegex(ValueError,'stale'):apply_thermal(model,z,obs)
 def settling(self):
  rows=[]
  for partition in ('train','validation'):
   for delay in [.005,.02]:
    for i in range(3):rows.append(dict(partition=partition,delay_s=delay,amplitude_error_db=.2 if delay==.005 else .01,frequency_error_hz=1,capture_sha256=hashlib.sha256((partition+str(delay)+str(i)).encode()).hexdigest()))
  return dict(version=1,type='settling_reference_measurements',source=source(),measurements=rows,max_amplitude_error_db=.1,max_frequency_error_hz=5,minimum_delay_s=0)
 def test_settling_preservation_and_context_binding(self):
  model=fit_settling(self.settling());self.assertEqual(evaluate_settling(model,'a'*64),.02)
  with self.assertRaises(ModelContextMismatch):evaluate_settling(model,'c'*64)
 def test_fast_delay_requires_actual_independent_holdout_at_that_delay(self):
  value=self.settling();value['measurements']=[r for r in value['measurements'] if not (r['partition']=='validation' and r['delay_s']==.005)]
  for r in value['measurements']:r['amplitude_error_db']=0
  self.assertEqual(fit_settling(value)['delay_s'],.02)
 def test_cli_fits_and_publishes_model_atomically(self):
  with tempfile.TemporaryDirectory() as directory:
   p=Path(directory)/'input.json';out=Path(directory)/'model.json';p.write_text(json.dumps(thermal()));main(['fit-thermal',str(p),str(out)]);self.assertEqual(ThermalModel(str(out)).sha256,json.loads(out.read_text())['model_sha256'])

class LivePathTests(unittest.TestCase):
 setUp=audit.AcquisitionTests.setUp
 capable_receiver=architecture.HardwareIntegrationTests.capable_receiver
 def test_slow_sensor_is_not_read_inside_rx_window(self):
  sdr,out=self.capable_receiver();raw=sdr.device.device
  raw.listSensors=lambda *a:['temp','lo_locked'] if a else []
  raw.getSensorInfo=lambda *a:types.SimpleNamespace(type=2 if a[-1]=='temp' else 0,units='C',description='fixture')
  reads=[]
  def sensor(*args):
   if args[-1]=='temp':self.assertFalse(sdr.device.is_streaming);reads.append(1);return '45'
   return 'true'
  raw.readSensor=sensor
  sdr.sweep(10000,10000,64,4,runs=3,reopen_stream=True,observe_rf_state=True,max_threads=1)
  self.assertEqual(len(reads),1)
  rows=[json.loads(l) for l in out.getvalue().splitlines() if l.strip()];self.assertEqual(len(rows),3);self.assertTrue(rows[-1]['metadata']['rf_state']['after']['telemetry']['rx:temp']['fresh'])
 def test_required_temperature_failure_keeps_rejected_iq_evidence(self):
  sdr,out=self.capable_receiver()
  with tempfile.TemporaryDirectory() as directory:
   with self.assertRaisesRegex(RuntimeError,'fresh_temperature_required'):
    sdr.sweep(10000,10000,64,4,runs=1,reopen_stream=True,telemetry_policy={'require_temperature':True},quality_policy={'evidence_dir':str(Path(directory)/'evidence')},max_threads=1)
   self.assertEqual(out.getvalue(),b'');self.assertTrue((Path(directory)/'evidence/rejected-000000.npz').exists())


class StartupPolicyTests(unittest.TestCase):
 receiver=MonitorTests.receiver
 setUp=audit.AcquisitionTests.setUp
 def test_warmup_waits_with_stream_closed_and_bounded_time(self):
  m,r,c,w=self.receiver({'warmup_window_s':2,'warmup_timeout_s':4,'warmup_max_slope_c_per_min':1,'temperature_interval_s':.25})
  m.await_warmup(sleeper=lambda t:setattr(c,'now',c.now+t))
  self.assertTrue(m.warmup_complete);self.assertGreaterEqual(c.now,1.6);self.assertLess(c.now,4)
  self.assertTrue(m.snapshot()['validity']['passed'])
 def test_warmup_timeout_does_not_hide_drift(self):
  m,r,c,w=self.receiver({'warmup_window_s':1,'warmup_timeout_s':2,'warmup_max_slope_c_per_min':.1,'temperature_interval_s':.1})
  def sleep(t):c.now+=t;r.values['temp']=str(45+c.now)
  with self.assertRaisesRegex(RuntimeError,'timed out'):m.await_warmup(sleeper=sleep)
  self.assertFalse(m.warmup_complete)
 def test_initial_read_retains_streaming_provenance(self):
  m,r,c,w=self.receiver();m.closed_slot();s=m.snapshot();self.assertFalse(s['telemetry']['rx:temp']['polled_while_streaming'])
 def test_periodic_chain_refresh_is_once_per_hop(self):
  m,r,c,w=self.receiver({'refresh_hops':2});m.hops=2;m.closed_slot();n=m.native_read_count;m.closed_slot();self.assertEqual(m.native_read_count,n)
 def test_required_thermal_policy_needs_closed_slots(self):
  sdr,out=architecture.HardwareIntegrationTests.capable_receiver(self)
  with self.assertRaisesRegex(ValueError,'reopen-stream'):sdr.setup(64,4,telemetry_policy={'require_temperature':True})
 def test_iq_conjugation_reflects_lo_hypothesis_about_origin(self):
  s={'tuning_components_hz':{'RF':1010,'BB':10}}
  r=lo_mapping(s,1000,100,frequency_sign=-1,lnb_lo=10000,iq_conjugate=True)
  self.assertEqual(r['analog_lo_display_hz'],9010);self.assertEqual(r['analog_lo_baseband_hz'],-10)
 def test_bad_lo_domains_are_rejected(self):
  for rate,sign in [(0,1),(1,0),(float('nan'),1)]:
   with self.assertRaises(ValueError):lo_mapping({'tuning_components_hz':{'RF':10,'BB':0}},10,rate,sign)


class ProfileAndPreservationTests(unittest.TestCase):
 setUp=audit.AcquisitionTests.setUp
 capable_receiver=architecture.HardwareIntegrationTests.capable_receiver
 def test_profile_binds_policy_and_rejects_weaker_override(self):
  from test_program_integration import profile
  value=profile();value['telemetry_policy']={'reference_lock':'off'}
  sdr,out=self.capable_receiver()
  with tempfile.TemporaryDirectory() as directory:
   sdr.setup(64,4,reopen_stream=True,receiver_profile=value,quality_policy={'evidence_dir':str(Path(directory)/'reject')})
   self.assertEqual(sdr._telemetry.policy.sha256,digest(value['telemetry_policy']))
   sdr.stop()
   with self.assertRaisesRegex(ValueError,'cannot be overridden'):
    sdr.setup(64,4,reopen_stream=True,receiver_profile=value,telemetry_policy={},quality_policy={'evidence_dir':str(Path(directory)/'reject2')})
 def test_telemetry_does_not_change_iq_psd_or_axis(self):
  sdr,out=self.capable_receiver();sdr.setup(64,4,reopen_stream=True,max_threads=1);f,a=sdr.psd(10000)[0].result();sdr.stop()
  sdr.setup(64,4,reopen_stream=True,observe_rf_state=True,max_threads=1);g,b=sdr.psd(10000)[0].result()
  np.testing.assert_array_equal(a,b);np.testing.assert_array_equal(f,g)
 def test_corrupt_settling_policy_fails_before_start(self):
  model=fit_settling(ReferenceModelTests().settling());model['delay_s']=0
  sdr,out=self.capable_receiver()
  with self.assertRaisesRegex(ValueError,'digest'):sdr.setup(64,4,settling_model=model)
  self.assertFalse(sdr.device.is_streaming)
 def test_cli_rejects_bad_settling_model_before_hardware(self):
  from soapypower.__main__ import setup_argument_parser,validate_arguments
  with tempfile.TemporaryDirectory() as directory:
   p=Path(directory)/'bad.json';p.write_text('{}');parser=setup_argument_parser()
   with mock.patch('sys.stderr',io.StringIO()),self.assertRaises(SystemExit):validate_arguments(parser,parser.parse_args(['--settling-model',str(p)]))

class AdditionalBoundaryTests(unittest.TestCase):
 receiver=MonitorTests.receiver
 def test_stability_window_keeps_its_boundary_bracket_between_polls(self):
  m,r,c,w=self.receiver({'warmup_window_s':2,'warmup_max_slope_c_per_min':1,'temperature_interval_s':.25})
  for t in np.arange(8)*.26:c.now=float(t);m.closed_slot()
  c.now=2.01
  self.assertTrue(m.snapshot()['thermal']['stable'])
 def test_warmup_obeys_cancellation(self):
  m,r,c,w=self.receiver({'warmup_window_s':1,'warmup_max_slope_c_per_min':1})
  with self.assertRaisesRegex(RuntimeError,'cancelled'):m.await_warmup(cancelled=lambda:True)
 def test_missing_explicit_temperature_key_is_unknown(self):
  m,r,c,w=self.receiver({'require_temperature':True,'temperature_sensor_key':'rx:missing'});m.closed_slot()
  self.assertIn('fresh_temperature_required',m.snapshot()['validity']['failures'])
 def test_rssi_baseline_binds_master_clock_and_full_hardware_context(self):
  m,r,c,w=self.receiver();m.closed_slot();s=m.snapshot();ctx=architecture.context()
  m.qualify_iq(ctx,s,{'ac_rms':.1});ctx['master_clock_rate']=999
  self.assertEqual(m.qualify_iq(ctx,s,{'ac_rms':.001})['status'],'baseline')

if __name__=='__main__':unittest.main()
