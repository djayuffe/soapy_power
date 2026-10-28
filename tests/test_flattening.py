"""Flattening controls, constrained fits, independent evidence and failure gates."""
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np

from soapypower.calibration import PowerCalibration
from soapypower.calibration_cli import main
from soapypower.equalization import relative_inverse, equalize_record, equalize_file
from soapypower.response_smoothing import smooth_row, smooth_profile
from soapypower.response_validation import validate_holdout, response_sha256
from soapypower.records import read_records, integrate_record
import test_equalization as equalization
import test_calibration_workflows as workflows
import test_architecture as architecture


class FlatteningMathTests(unittest.TestCase):
    def test_attenuation_only_is_exact_and_never_boosts(self):
        result=relative_inverse([-30,-20,-10],-30,0,12,'attenuation-only')
        self.assertEqual(result['power_weight_db'],[-20,-10,0])
        self.assertEqual(result['target_relative_power_level_db'],-20)
        np.testing.assert_allclose(result['predicted_residual_power_response_db'],0)
        with self.assertRaises(ValueError):relative_inverse([-30,-10],-30,0.001,12,'attenuation-only')
        with self.assertRaises(ValueError):relative_inverse([-30,-10],-30,0,12,'attenuation-only',-20)

    def test_weighted_smoothing_reduces_noisy_knot_and_preserves_constraints(self):
        x=np.arange(7,dtype=float);y=np.array([0,0,0,1,0,0,0.])
        std=np.array([0.01,0.01,0.01,2,0.01,0.01,0.01])
        result=smooth_row(x,y,std,1000,1,protected_hz=[5])
        self.assertLess(abs(result[3]),0.01)
        np.testing.assert_allclose(result[[0,1,5,6]],y[[0,1,5,6]],atol=1e-12)
        # With tiny uncertainty at that knot, the fit must trust it more.
        trusted=smooth_row(x,y,np.full(7,0.001),1000,1,protected_hz=[5])
        self.assertGreater(trusted[3],result[3])

    def test_linear_nonuniform_response_and_fractional_reference_remain_exact(self):
        x=np.array([-5,-2,0,1,4,8.]);y=2*x+7
        np.testing.assert_allclose(smooth_row(x,y,np.ones(6),20,0.5),y,atol=1e-12)
        y=np.array([0,0,0.5,0,0,0.])
        result=smooth_row(x,y,np.ones(6),20,0.5)
        self.assertAlmostEqual(float(np.interp(0.5,x,result)),float(np.interp(0.5,x,y)),places=12)

    def test_explicit_breakpoint_prevents_smoothing_across_step(self):
        x=np.arange(8,dtype=float);y=np.array([0,0,0,0,20,20,20,20.])
        separated=smooth_row(x,y,np.ones(8),100,1,breakpoints_hz=[3.5])
        np.testing.assert_allclose(separated,y,atol=1e-11)
        blended=smooth_row(x,y,np.ones(8),100,1)
        self.assertGreater(abs(blended[3]-y[3]),1)

    def test_invalid_smoothing_models_fail(self):
        for kwargs in (dict(standard_db=[1,0,1]),dict(breakpoints_hz=[1]),dict(protected_hz=[0.2]),dict(strength=-1)):
            args=dict(baseband_hz=[0,1,2],correction_db=[0,1,0],standard_db=[1,1,1],strength=1,reference_baseband_hz=1)
            args.update(kwargs)
            with self.assertRaises(ValueError):smooth_row(**args)


class UsableBandTests(unittest.TestCase):
    setUp=workflows.WorkflowTests.setUp
    capture=workflows.WorkflowTests.capture
    data=equalization.EqualizationRecordTests.data
    calibration=equalization.EqualizationRecordTests.calibration

    def test_selection_marks_edges_dc_and_preserves_zero_classification(self):
        record=self.data();record['power'][32]=0;record['zero_power_bins']=[32]
        result=equalize_record(record,self.calibration(),10000,0,0,30,usable_policy=dict(edge_guard_hz=64,dc_exclusion_hz=32))
        meta=result['metadata']['equalization']
        self.assertTrue(result['masked_bins']);self.assertEqual(len(meta['usable_bands']),2)
        raw=np.asarray(meta['raw_baseband_hz'])
        for i in result['masked_bins']:self.assertIsNone(result['power'][i])
        self.assertTrue(any('dc_or_spur_guard' in reasons for reasons in meta['exclusion_reasons']))
        with self.assertRaises(ValueError):integrate_record(result)
        integrated=integrate_record(result,omit_masked=True)
        self.assertLess(integrated['measured_bandwidth_hz'],1024)

    def test_policy_rejects_all_bad_bins_and_masks_unmeasured_coverage(self):
        record=self.data();profile=self.calibration().profile
        profile['baseband_hz']=[-256,256];profile['reference_baseband_hz']=-256
        cal=PowerCalibration(profile)
        with self.assertRaises(ValueError):equalize_record(record,cal,10000,0)
        result=equalize_record(record,cal,10000,0,usable_policy={})
        self.assertTrue(result['masked_bins'])
        with self.assertRaises(ValueError):equalize_record(record,cal,10000,0,usable_policy=dict(max_correction_db=0))
        with self.assertRaises(ValueError):equalize_record(record,cal,10000,0,usable_policy=dict(typo=1))

    def test_regularization_residual_gate_is_explicit(self):
        record=self.data();cal=self.calibration()
        result=equalize_record(record,cal,10000,-512,0.01,3,usable_policy=dict(max_residual_db=0.5))
        meta=result['metadata']['equalization']
        self.assertTrue(any('excessive_residual_bias' in r for r in meta['exclusion_reasons']))
        self.assertTrue(all(x is None or abs(x)<=0.5 for x in meta['predicted_residual_power_response_db']))

    def test_attenuation_target_shared_across_all_file_hops(self):
        first=self.data();second=copy.deepcopy(first)
        second['start_hz']+=16;second['stop_hz']+=16
        for name in ('first_bin_center_hz','coverage_start_hz','coverage_stop_hz'):second[name]+=16
        second['metadata']['tuned_center_hz']+=16
        # RF-dependent profile means hops have different weakest responses.
        profile=self.calibration().profile;profile['points']=[[0,-40],[20000,-20]]
        path=self.root/'profile.json';path.write_text(json.dumps(profile))
        source=self.root/'source.jsonl';source.write_text(json.dumps(first)+'\n'+json.dumps(second)+'\n')
        output=self.root/'output.jsonl'
        args=['equalize',str(source),'--profile',str(path),'--reference-rf-hz','10000',
              '--reference-baseband-hz','0','--mode','attenuation-only','-O',str(output)]
        self.assertEqual(main(args),0)
        policy=self.root/'policy.json';policy.write_text('{}')
        guarded=args.copy();guarded[guarded.index('-O')+1]=str(policy)
        with mock.patch('sys.stderr'):
            with self.assertRaises(SystemExit):main(guarded+['--usable-policy',str(policy),'--force'])
        self.assertEqual(policy.read_text(),'{}')
        results=list(read_records(output));a,b=[r['metadata']['equalization'] for r in results]
        self.assertEqual(a['attenuation_target_correction_db'],b['attenuation_target_correction_db'])
        self.assertEqual(a['normalization_scope'],'whole_file')
        for m in (a,b):self.assertTrue(all(w is None or w<=0 for w in m['power_weight_db']))


class HoldoutAndProfileTests(unittest.TestCase):
    setUp=workflows.WorkflowTests.setUp
    capture=workflows.WorkflowTests.capture

    def evidence(self):
        profile=architecture.profile();profile['baseband_hz']=[-512,-256,0,128,256,512]
        profile['baseband_correction_db']=[[0,0,0,0.05,0,0],[0,0,0,0.05,0,0]]
        cells=[]
        for i,(rf,bb) in enumerate(((10000,64),(12000,-128),(15000,192))):
            on='hold'+str(i)+'on.jsonl';off='hold'+str(i)+'off.jsonl'
            # Actual digital conversion has flat -30 dB offset; mild noisy training knot.
            self.capture(on,rf,bb,tone=1);self.capture(off,rf,bb)
            cells.append(dict(rf_hz=rf,baseband_hz=bb,start_hz=rf-8,stop_hz=rf+8,input_dbm=-30,on=on,off=off))
        manifest=dict(version=1,reference='SYNTHETIC independent source check',independent_measurements_confirmed=True,
                      max_abs_error_db=0.1,max_ripple_db=0.1,cells=cells)
        path=self.root/'holdouts.json';path.write_text(json.dumps(manifest))
        model=dict(version=1,standard_uncertainty_db=[[0.01,0.01,0.01,1,0.01,0.01]]*2,
                   strength=100,max_change_db=0.1,breakpoints_hz=[],protected_baseband_hz=[],
                   uncertainty_assumption='independent_knot_standard_errors')
        mp=self.root/'model.json';mp.write_text(json.dumps(model))
        return profile,manifest,path,model,mp

    def test_independent_holdouts_report_bias_ripple_and_profile_fingerprint(self):
        profile,manifest,path,_,_=self.evidence();cal=PowerCalibration(profile)
        report=validate_holdout(cal,path)
        self.assertTrue(report['passed']);self.assertGreater(report['max_abs_error_db'],0)
        self.assertEqual(report['response_sha256'],response_sha256(cal))
        self.assertEqual(report['independence_status'],'operator_declared_no_training_capture_hashes')
        profile['points']=[[0,-25],[20000,-25]]
        bad=validate_holdout(PowerCalibration(profile),path)
        self.assertFalse(bad['passed']);self.assertGreater(bad['mean_error_db'],4)

    def test_single_holdout_cannot_approve_smoothed_full_response(self):
        profile,manifest,path,_,mp=self.evidence()
        manifest['cells']=manifest['cells'][:1];path.write_text(json.dumps(manifest))
        self.assertTrue(validate_holdout(PowerCalibration(profile),path)['passed'])
        with self.assertRaises(ValueError):smooth_profile(PowerCalibration(profile),mp,path)

    def test_training_capture_reuse_and_missing_confirmation_rejected(self):
        profile,manifest,path,_,_=self.evidence()
        from soapypower.calibration_workflows import _sha256
        profile['method_details']={'capture_sha256':{'trained':_sha256(self.root/manifest['cells'][0]['on'])}}
        with self.assertRaises(ValueError):validate_holdout(PowerCalibration(profile),path)
        profile.pop('method_details');manifest['independent_measurements_confirmed']=False;path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):validate_holdout(PowerCalibration(profile),path)

    def test_smoother_preserves_rf_rows_anchors_and_gates_publication(self):
        profile,_,path,model,mp=self.evidence();cal=PowerCalibration(profile)
        result=smooth_profile(cal,mp,path)
        np.testing.assert_allclose(result.profile['points'],profile['points'],atol=1e-12)
        self.assertNotIn('uncertainty_db',result.profile)
        details=result.profile['method_details']['response_smoothing']
        self.assertTrue(details['holdout_validation']['passed'])
        self.assertEqual(details['holdout_validation']['response_sha256'],response_sha256(result))
        self.assertLess(result.profile['baseband_correction_db'][0][3],profile['baseband_correction_db'][0][3])
        model['max_change_db']=0;mp.write_text(json.dumps(model))
        with self.assertRaises(ValueError):smooth_profile(cal,mp,path)

    def test_smoothed_v2_profile_invalidates_old_interpolation_uncertainty(self):
        from soapypower.uncertainty import COMPONENTS
        profile,_,path,_,mp=self.evidence()
        profile['version']=2
        profile['uncertainty']={'components':dict.fromkeys(COMPONENTS,0.1),'independent':True,'coverage_factor':2}
        result=smooth_profile(PowerCalibration(profile),mp,path)
        uncertainty=result.metadata()['uncertainty_evaluation']
        self.assertEqual(uncertainty['status'],'incomplete')
        self.assertIn('interpolation_db',uncertainty['missing_components'])
        self.assertEqual(profile['uncertainty']['components']['interpolation_db'],0.1)

    def test_cli_failed_checks_preserve_source_and_existing_destination(self):
        profile,manifest,path,model,mp=self.evidence();source=self.root/'profile.json';source.write_text(json.dumps(profile))
        output=self.root/'smoothed.json'
        self.assertEqual(main(['smooth-profile',str(source),'--model',str(mp),'--holdouts',str(path),'-O',str(output)]),0)
        saved=output.read_bytes();manifest['cells'][0]['input_dbm']=-20;path.write_text(json.dumps(manifest))
        with mock.patch('sys.stderr'):
            with self.assertRaises(SystemExit):main(['smooth-profile',str(source),'--model',str(mp),'--holdouts',str(path),'-O',str(output),'--force'])
            with self.assertRaises(SystemExit):main(['smooth-profile',str(source),'--model',str(mp),'--holdouts',str(path),'-O',str(source),'--force'])
        self.assertEqual(output.read_bytes(),saved)
        report=self.root/'failed.json'
        self.assertEqual(main(['validate-response',str(source),'--holdouts',str(path),'-O',str(report),'--require-checks']),1)
        self.assertFalse(json.loads(report.read_text())['passed'])
