"""Synthetic scalar-noise metrology, conditioning, provenance and failure gates."""
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np

from soapypower.characterization import (fit_noise_temperature, separate_lna, validate_cw,
    propagate_joint_uncertainty, ReceiverCharacterization)
from soapypower.calibration import PowerCalibration
from soapypower.calibration_cli import main
from soapypower.frontend import Frontend
from types import SimpleNamespace
import test_calibration_workflows as workflows


class ModelTests(unittest.TestCase):
    def test_known_noise_fit_scale_invariance_and_friis(self):
        temperatures = np.array([290., 500., 1000., 3000.])
        for scale in (1e-100, 1e-15, 1e100):
            receiver = fit_noise_temperature(temperatures, scale*(temperatures+600))
            cascade = fit_noise_temperature(temperatures, scale*100*(temperatures+56))
            lna = separate_lna(receiver, cascade)
            self.assertAlmostEqual(receiver['receiver_noise_temp_k'], 600, places=9)
            self.assertAlmostEqual(cascade['receiver_noise_temp_k'], 56, places=9)
            self.assertAlmostEqual(lna['gain_db'], 20, places=10)
            self.assertAlmostEqual(lna['noise_temp_k'], 50, places=9)
            self.assertAlmostEqual(lna['noise_figure_db'], 10*math.log10(1+50/290), places=10)
            self.assertLess(receiver['max_abs_residual_db'], 1e-12)

    def test_two_level_and_degenerate_invalid_models_rejected(self):
        for t,d in (([290,1000],[1,2]), ([290]*3,[1,2,3]),
                    ([290,500,1000],[3,2,1]), ([290,500,1000],[1,1,1]), ([290,500,float('nan')],[1,2,3]),
                    ([290,500,1000],[0,2,3]), ([290,290+1e-9,290+2e-9],[1,2,3])):
            with self.assertRaises(ValueError): fit_noise_temperature(t,d)

    def test_weighted_fit_covariance_matches_independent_design(self):
        t=np.array([290.,600.,1200.,2500.]);d=2e-12*(t+400);sigma=d*0.01
        result=fit_noise_temperature(t,d,sigma)
        x=np.column_stack((t,np.ones(4)))
        expected=np.linalg.inv((x/sigma[:,None]).T@(x/sigma[:,None]))
        np.testing.assert_allclose(result['slope_intercept_covariance'],expected,rtol=1e-12,atol=0)
        jac=np.array([-400/2e-12,1/2e-12])
        self.assertAlmostEqual(result['conditional_noise_temp_std_k'],math.sqrt(jac@expected@jac),places=10)

    def test_negative_noise_not_clipped_and_curvature_visible(self):
        t=np.array([290.,600.,1200.,2500.])
        negative=fit_noise_temperature(t,1e-12*(t-20))
        self.assertFalse(negative['physical_noise_temperature'])
        self.assertLess(negative['receiver_noise_temp_k'],0)
        d=1e-12*(t+400);d[-1]*=0.7
        result=fit_noise_temperature(t,d)
        self.assertGreater(result['max_abs_residual_db'],0.1)
        lna=separate_lna({'slope_density_per_k':1,'receiver_noise_temp_k':100},
                         {'slope_density_per_k':2,'receiver_noise_temp_k':20})
        self.assertEqual(lna['noise_temp_k'],-30)
        self.assertFalse(lna['physical_noise_temperature'])

    def test_cw_linearity_checks_all_points(self):
        levels=np.array([-80.,-70.,-60.,-50.]);power=10**((levels+40)/10)
        self.assertTrue(validate_cw(levels,power)['accepted'])
        compressed=power.copy();compressed[-1]*=0.8
        self.assertFalse(validate_cw(levels,compressed)['accepted'])
        self.assertFalse(validate_cw(levels,10**((levels*0.95+40)/10))['accepted'])
        with self.assertRaises(ValueError):validate_cw([-80,-80,-80],[1,2,3])

    def test_joint_uncertainty_reproducibility_correlation_and_invalid_draws(self):
        t=np.array([290.,600.,1500.]);r=1e-12*(t+600);c=1e-10*(t+56)
        std=np.r_[np.ones(3),r*0.001,np.ones(3),c*0.001]
        model=dict(standard_uncertainties=std.tolist(),correlation=np.eye(12).tolist(),draws=128,seed=4)
        a=propagate_joint_uncertainty(t,r,t,c,model)
        self.assertEqual(a,propagate_joint_uncertainty(t,r,t,c,model))
        self.assertEqual(a['status'],'conditional_model_intervals')
        self.assertGreater(a['statistics']['noise_figure_db']['standard_uncertainty'],0)
        # Perfectly shared conversion-scale error cancels in the slope ratio.
        shared=np.zeros(12);shared[3:6]=r*0.03;shared[9:]=c*0.03
        model.update(standard_uncertainties=shared.tolist(),correlation=np.ones((12,12)).tolist())
        b=propagate_joint_uncertainty(t,r,t,c,model)
        self.assertLess(b['statistics']['gain_db']['standard_uncertainty'],1e-7)
        model['standard_uncertainties']=(std*2000).tolist();model['correlation']=np.eye(12).tolist()
        bad=propagate_joint_uncertainty(t,r,t,c,model)
        self.assertEqual(bad['status'],'invalid_realizations');self.assertIsNone(bad['statistics'])
        model['correlation'][0][1]=2
        with self.assertRaises(ValueError):propagate_joint_uncertainty(t,r,t,c,model)

    def test_monte_carlo_gain_variance_matches_linearized_reference(self):
        t=np.array([290.,600.,1500.,3000.]);r=1e-12*(t+600);c=1e-10*(t+56)
        std=np.r_[np.zeros(4),r*0.001,np.zeros(4),c*0.001]
        result=propagate_joint_uncertainty(t,r,t,c,dict(standard_uncertainties=std.tolist(),
            correlation=np.eye(16).tolist(),draws=1024,seed=19))
        x=np.column_stack((t-t.mean(),np.ones(4)))
        inverse=np.linalg.pinv(x)
        vr=float(np.sum((inverse[0]*r*0.001)**2))
        vc=float(np.sum((inverse[0]*c*0.001)**2))
        expected=10/math.log(10)*math.sqrt(vr/1e-24+vc/1e-20)
        observed=result['statistics']['gain_db']['standard_uncertainty']
        self.assertLess(abs(observed/expected-1),0.1)

    def test_native_format_scaling_is_context_and_not_cf32_voltage(self):
        raw=SimpleNamespace(getNativeStreamFormat=lambda *args: ('CS16',2048), hasHardwareTime=lambda:True)
        context=Frontend(SimpleNamespace(device=raw),1,0).context({}, {},0,False,False)
        self.assertEqual(context['hardware_info']['soapypower_native_stream']['full_scale'],2048)
        self.assertEqual(context['sample_format'],'CF32')
        raw.getNativeStreamFormat=lambda *args: ('CS16',4096)
        changed=Frontend(SimpleNamespace(device=raw),1,0).context({}, {},0,False,False)
        from soapypower.calibration import _compare
        with self.assertRaises(ValueError):_compare(context,changed)

        raw.getNativeStreamFormat=lambda *args: ('CS16',float('nan'))
        with self.assertRaises(ValueError):Frontend(SimpleNamespace(device=raw),1,0).context({}, {},0,False,False)


class LegacyNativeFormatTests(unittest.TestCase):
    def test_broken_legacy_output_parameter_is_explicitly_unknown(self):
        def legacy(direction, channel, full_scale):
            raise AssertionError('no unsafe pointer workaround should be attempted')
        raw=SimpleNamespace(getNativeStreamFormat=legacy)
        context=Frontend(SimpleNamespace(device=raw),1,0).context({}, {},0,False,False)
        native=context['hardware_info']['soapypower_native_stream']
        self.assertEqual(native['status'],'unavailable_python_binding')
        self.assertNotIn('full_scale',native)


class CaptureCharacterizationTests(unittest.TestCase):
    setUp=workflows.WorkflowTests.setUp
    capture=workflows.WorkflowTests.capture

    def make_manifest(self, with_cw=True):
        cell=dict(rf_hz=10000,baseband_hz=128,start_hz=9992,stop_hz=10008)
        for config,slope,te in (('receiver',1e-12,600),('cascade',1e-10,56)):
            cell[config]=[]
            for i,t in enumerate((290,600,1500,3000)):
                name=config+str(i)+'.jsonl';self.capture(name,density=slope*(t+te))
                cell[config].append(dict(temperature_k=t,capture=name))
            if with_cw:
                cell[config+'_cw']=[]
                for i,pin in enumerate((-80,-70,-60)):
                    on=config+'cw'+str(i)+'on.jsonl';off=config+'cw'+str(i)+'off.jsonl'
                    self.capture(on,density=1e-12,tone=10**(pin/10)*slope/(1000*1.380649e-23))
                    self.capture(off,density=1e-12)
                    cell[config+'_cw'].append(dict(input_dbm=pin,on=on,off=off))
        manifest=dict(version=1,interpretation='external-lna',reference='SYNTHETIC',reference_plane='TEST SMA',
                      downstream_chain='SYNTHETIC fixed pad+receiver',source_calibration='SYNTHETIC known temperatures',
                      matched_scalar_model_confirmed=True,unchanged_downstream_confirmed=True,cells=[cell])
        path=self.root/'manifest.json';path.write_text(json.dumps(manifest))
        return manifest,path

    def test_complete_capture_fit_cw_checks_cli_and_distinct_profile(self):
        manifest,path=self.make_manifest()
        result=ReceiverCharacterization.from_manifest(path).report
        cell=result['cells'][0]
        self.assertEqual(cell['failure_reasons'],[])
        self.assertEqual(cell['validation_status'],'passed_measured_checks')
        self.assertAlmostEqual(cell['lna']['gain_db'],20)
        self.assertAlmostEqual(cell['lna']['noise_temp_k'],50,places=8)
        self.assertAlmostEqual(cell['cw_noise_gain_delta_db'],0,places=9)
        self.assertEqual(cell['uncertainty']['status'],'not_evaluated')
        self.assertFalse(result['physical_certification'])
        self.assertGreater(len(result['capture_sha256']),10)
        with self.assertRaises(ValueError):PowerCalibration(result)
        output=self.root/'report.json'
        self.assertEqual(main(['characterize',str(path),'-O',str(output)]),0)
        self.assertEqual(json.loads(output.read_text())['report_sha256'],result['report_sha256'])
        with mock.patch('sys.stderr'):
            with self.assertRaises(SystemExit):main(['characterize',str(path),'-O',str(output)])

    def test_missing_cw_is_incomplete_and_internal_topology_not_lna(self):
        manifest,path=self.make_manifest(False)
        result=ReceiverCharacterization.from_manifest(path).report
        self.assertIn('receiver_missing_cw_linearity',result['cells'][0]['failure_reasons'])
        self.assertEqual(main(['characterize',str(path),'-O',str(self.root/'failed.json'),'--require-checks']),1)
        self.assertTrue((self.root/'failed.json').exists())
        manifest['interpretation']='configuration-comparison';path.write_text(json.dumps(manifest))
        self.assertIsNone(ReceiverCharacterization.from_manifest(path).report['cells'][0]['lna'])

    def test_downstream_gain_agc_incomplete_grid_and_assumptions_rejected(self):
        manifest,path=self.make_manifest(False)
        name=manifest['cells'][0]['cascade'][0]['capture'];original=json.loads((self.root/name).read_text())
        for field,value in (('agc',True),('gains',{'LNA':42})):
            changed=copy.deepcopy(original);changed['metadata']['context'][field]=value
            (self.root/name).write_text(json.dumps(changed)+'\n')
            with self.assertRaises(ValueError):ReceiverCharacterization.from_manifest(path)
        (self.root/name).write_text(json.dumps(original)+'\n')
        manifest['unchanged_downstream_confirmed']=False;path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):ReceiverCharacterization.from_manifest(path)

    def test_compression_and_cw_noise_disagreement_reported_without_point_deletion(self):
        manifest,path=self.make_manifest()
        name=manifest['cells'][0]['cascade'][-1]['capture'];capture=json.loads((self.root/name).read_text())
        capture['power']=[p*0.7 for p in capture['power']];(self.root/name).write_text(json.dumps(capture)+'\n')
        report=ReceiverCharacterization.from_manifest(path).report
        self.assertIn('cascade_noise_nonlinearity',report['cells'][0]['failure_reasons'])
        self.assertEqual(len(report['cells'][0]['observations']['cascade']['digital_density']),4)
        self.assertEqual(report['cells'][0]['validation_status'],'failed_checks')
