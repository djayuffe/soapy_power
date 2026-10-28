"""Passive references are relative, identifiable, stable and independently checked."""
import copy
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import numpy as np
from soapypower.passive_response import _solve,build_overlap,RelativeIFCalibration,load_response_profile
from soapypower.passive_iq import tone_audit,interval_audit
from soapypower.gain_comparison import compare_gain_states
from soapypower.calibration import PowerCalibration
from soapypower.calibration_cli import main
from soapypower.equalization import equalize_file
from soapypower.records import read_records
import test_calibration_workflows as workflows


class OverlapSolverTests(unittest.TestCase):
    def observations(self):
        ids=[];positions=[];values=[]
        for sid,level in (('a',10),('b',20),('c',30)):
            for bb,response in ((-256,-3),(128,0),(256,-2)):
                for delta in (-0.01,0.01):
                    ids.append(sid);positions.append(bb);values.append(level+response+delta)
        return ids,positions,values
    def test_known_response_anchor_and_conditional_covariance(self):
        ids,bb,y=self.observations();result=_solve(ids,bb,y,[0.05]*len(y),128)
        np.testing.assert_allclose(result['response_db'],[-3,0,-2],atol=1e-12)
        self.assertEqual(result['rank'],5)
        self.assertEqual(result['conditional_standard_db'][1],0)
        self.assertGreater(result['conditional_standard_db'][0],0)
        self.assertLess(max(abs(x) for x in result['residual_db']),0.011)
    def test_graph_identifiability_and_weight_validation(self):
        with self.assertRaises(ValueError):_solve(['a','b'],[-1,1],[1,2],[0.1,0.1],-1)
        # Two internally complete but disconnected components must not be joined by a prior.
        ids=[];bb=[];y=[]
        for names,positions in ((('a','b'),(-4,-3)),(('c','d'),(3,4))):
            for name in names:
                for value in positions:
                    ids.append(name);bb.append(value);y.append(10.)
        with self.assertRaisesRegex(ValueError,'disconnected'):_solve(ids,bb,y,[0.1]*len(y),-4)
        ids,bb,y=self.observations()
        with self.assertRaises(ValueError):_solve(ids,bb,y,[0]*len(y),128)
    def test_robust_fit_downweights_without_silently_deleting_observation(self):
        ids,bb,y=self.observations();y[0]+=4
        result=_solve(ids,bb,y,[0.05]*len(y),128)
        self.assertIn(0,result['downweighted_observations'])
        self.assertEqual(len(result['residual_db']),len(y))
        self.assertGreater(abs(result['residual_db'][0]),3)


class OverlapCaptureTests(unittest.TestCase):
    setUp=workflows.WorkflowTests.setUp
    capture=workflows.WorkflowTests.capture
    def evidence(self):
        cells=[]
        for split,jitters in (('train',(-0.01,0.01)),('validation',(-0.005,0.005))):
            for sid,rf,power in (('a',10000,1.),('b',20000,2.)):
                for bb,response in ((-256,-3),(128,0),(256,-2)):
                    for repeat,jitter in enumerate(jitters):
                        name='{}_{}_{}_{}.jsonl'.format(split,sid,bb,repeat)
                        self.capture(name,rf,bb,density=1e-9,tone=power*10**((response+jitter)/10))
                        cells.append(dict(capture=name,signal_id=sid,rf_hz=rf,baseband_hz=bb,
                            start_hz=rf-8,stop_hz=rf+8,background_start_hz=rf-80,background_stop_hz=rf-48,
                            standard_error_db=0.05,repeat_id=str(repeat),split=split))
        manifest=dict(version=1,reference='SYNTHETIC stable unknown-level RF references',stationarity_confirmed=True,
                      context_unchanged_confirmed=True,narrowband_response_confirmed=True,max_band_fraction=0.2,
                      reference_baseband_hz=128,cells=cells)
        path=self.root/'overlap.json';path.write_text(json.dumps(manifest))
        return manifest,path
    def test_capture_to_relative_profile_and_equalization_without_absolute_claim(self):
        manifest,path=self.evidence();cal=build_overlap(path)
        np.testing.assert_allclose(cal.profile['response_db'],[-3,0,-2],atol=1e-10)
        with self.assertRaises(ValueError):PowerCalibration(cal.profile)
        profile=self.root/'relative.json';profile.write_text(json.dumps(cal.profile))
        loaded=load_response_profile(profile);self.assertEqual(loaded.profile_id,cal.profile_id)
        with self.assertRaises(ValueError):loaded.offsets_db([99999],[128])
        source=self.root/'display.jsonl';self.capture(source.name,15000,128,density=1e-6)
        output=self.root/'equalized.jsonl'
        equalize_file(source,profile,output,15000,128,0,12,usable_policy={})
        record=list(read_records(output))[0]
        self.assertTrue(record['masked_bins'])
        self.assertFalse(record['metadata']['equalization']['profile']['absolute_power_calibration'])
        self.assertEqual(record['unit'],'input power/Hz')
        with mock.patch('sys.stdout',new=io.StringIO()):self.assertEqual(main(['validate',str(profile)]),0)
    def test_repeat_instability_and_validation_failure_rejected(self):
        manifest,path=self.evidence()
        name=manifest['cells'][0]['capture'];record=json.loads((self.root/name).read_text());record['power']=[p*2 for p in record['power']]
        (self.root/name).write_text(json.dumps(record)+'\n')
        with self.assertRaisesRegex(ValueError,'stable'):build_overlap(path)
        manifest,path=self.evidence()
        for cell in manifest['cells']:
            if cell['split']=='validation' and cell['baseband_hz']==256:
                name=cell['capture'];record=json.loads((self.root/name).read_text());record['power']=[p*1.5 for p in record['power']]
                (self.root/name).write_text(json.dumps(record)+'\n')
        with self.assertRaisesRegex(ValueError,'validation'):build_overlap(path)
    def test_context_and_reused_capture_fail_closed(self):
        manifest,path=self.evidence();a,b=manifest['cells'][:2]
        b['capture']=a['capture'];path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,'reuse'):build_overlap(path)
        manifest,path=self.evidence();name=manifest['cells'][0]['capture']
        record=json.loads((self.root/name).read_text());record['metadata']['context']['agc']=True
        (self.root/name).write_text(json.dumps(record)+'\n')
        with self.assertRaises(ValueError):build_overlap(path)
    def test_unknown_gain_readback_is_not_proof_of_fixed_configuration(self):
        manifest,path=self.evidence()
        for cell in manifest['cells']:
            name=cell['capture'];record=json.loads((self.root/name).read_text())
            record['metadata']['context']['gains']=None
            (self.root/name).write_text(json.dumps(record)+'\n')
        with self.assertRaisesRegex(ValueError,'known gains'):build_overlap(path)

    def test_wide_channel_is_not_mislabeled_as_pointwise_if_response(self):
        manifest,path=self.evidence();manifest['max_band_fraction']=0.01
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,'too broad'):build_overlap(path)

    def test_cli_does_not_overwrite_evidence(self):
        _,path=self.evidence();output=self.root/'relative.json'
        self.assertEqual(main(['overlap-fit',str(path),'-O',str(output)]),0)
        original=path.read_bytes()
        with mock.patch('sys.stderr'):
            with self.assertRaises(SystemExit):main(['overlap-fit',str(path),'-O',str(path),'--force'])
        self.assertEqual(path.read_bytes(),original)


class PassiveIQTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
    def test_selected_tone_frequency_and_drift(self):
        fs=8000;n=2048;frames=8;t=np.arange(n*frames)/fs
        samples=np.exp(2j*np.pi*(1007*t+0.5*2*t*t));path=self.root/'tone.npy';np.save(path,samples)
        report=tone_audit(path,fs,1000,100e6,n,frames,50,0.9)
        self.assertEqual(report['accepted_frames'],frames)
        self.assertAlmostEqual(report['times_from_start_s'][0],(n-1)/(2*fs))
        self.assertAlmostEqual(report['apparent_drift_hz_per_s'],2,places=7)
        expected=7+2*float(t.mean())
        self.assertAlmostEqual(report['mean_apparent_frequency_error_hz'],expected,places=8)
        self.assertFalse(report['automatic_frequency_correction_applied'])
        self.assertFalse(report['protocol_identified'])
        out=self.root/'tone.json'
        self.assertEqual(main(['tone-audit',str(path),'--sample-rate','8000','--expected-baseband-hz','1000',
            '--reference-rf-hz','100000000','--frame-size','2048','--frames','8','--search-hz','50','-O',str(out)]),0)
    def test_noise_modulation_and_dc_are_not_validated_reference_tones(self):
        rng=np.random.default_rng(19);path=self.root/'noise.npy';np.save(path,rng.normal(size=32768)+1j*rng.normal(size=32768))
        with self.assertRaises(ValueError):tone_audit(path,8000,1000,100e6)
        with self.assertRaises(ValueError):tone_audit(path,8000,0,100e6)
        np.save(path,np.ones(100))
        with self.assertRaises(ValueError):tone_audit(path,8000,1000,100e6)
    def test_quiet_intervals_are_diagnostics_not_noise_figure(self):
        path=self.root/'intervals.npy';np.save(path,np.r_[np.ones(100,dtype=complex),np.full(200,2+0j)])
        manifest=dict(version=1,reference='operator-provided synthetic quiet/active windows',intervals=[
            dict(start_sample=0,stop_sample=100,label='quiet'),dict(start_sample=100,stop_sample=300,label='active')])
        mp=self.root/'windows.json';mp.write_text(json.dumps(manifest))
        report=interval_audit(path,mp)
        self.assertAlmostEqual(report['active_over_quiet_db'],10*math.log10(4))
        self.assertFalse(report['noise_figure_identifiable'])
        manifest['intervals'][1]['start_sample']=99;mp.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):interval_audit(path,mp)


class GainStateTests(unittest.TestCase):
    setUp=workflows.WorkflowTests.setUp
    capture=workflows.WorkflowTests.capture
    def evidence(self):
        cells=[]
        for repeat,delta in enumerate((-0.01,0.01)):
            ref='ref'+str(repeat)+'.jsonl';obs='obs'+str(repeat)+'.jsonl'
            self.capture(ref,density=1e-9,tone=10**(delta/10))
            record=self.capture(obs,density=1e-8,tone=10*10**(delta/10))
            record['metadata']['context']['gains']={'overall':30.7};(self.root/obs).write_text(json.dumps(record)+'\n')
            cells.append(dict(rf_hz=10000,baseband_hz=128,start_hz=9992,stop_hz=10008,
                background_start_hz=9920,background_stop_hz=9952,reference=ref,observed=obs,repeat_id=str(repeat)))
        manifest=dict(version=1,reference='SYNTHETIC A/B gain state repeat',stationarity_confirmed=True,
                      nominal_gain_step_db=10,cells=cells)
        path=self.root/'gain.json';path.write_text(json.dumps(manifest));return manifest,path
    def test_measured_gain_step_and_failed_nominal_check(self):
        manifest,path=self.evidence();report=compare_gain_states(path)
        self.assertTrue(report['passed']);self.assertAlmostEqual(report['points'][0]['mean_gain_delta_db'],10)
        self.assertFalse(report['isolated_lna_noise_figure_identifiable'])
        manifest['nominal_gain_step_db']=20;path.write_text(json.dumps(manifest))
        output=self.root/'failed.json'
        self.assertEqual(main(['gain-compare',str(path),'-O',str(output),'--require-checks']),1)
        self.assertFalse(json.loads(output.read_text())['passed'])
    def test_only_gain_register_changes_are_allowed(self):
        manifest,path=self.evidence();name=manifest['cells'][0]['observed']
        record=json.loads((self.root/name).read_text());record['metadata']['context']['antenna']='OTHER'
        (self.root/name).write_text(json.dumps(record)+'\n')
        with self.assertRaises(ValueError):compare_gain_states(path)
