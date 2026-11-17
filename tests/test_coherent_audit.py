import json
from pathlib import Path
import tempfile
import unittest
import importlib.util
import numpy as np
from soapypower.noise_audit import analyze_iq,main

@unittest.skipUnless(importlib.util.find_spec('scipy'), 'coherent features require optional SciPy')
class CoherentAuditTests(unittest.TestCase):
    def samples(self):
        n=np.arange(262144);rng=np.random.default_rng(8)
        return (.2*np.exp(2j*np.pi*.1234567*n)+.06*(rng.normal(size=len(n))+1j*rng.normal(size=len(n)))).astype(np.complex64)
    def test_phase_feature_report_is_json_serializable_and_preserves_uncalibrated_status(self):
        report=analyze_iq(self.samples(),1e6,fft_size=2048,segments=255,phase_features=True)
        json.dumps(report,allow_nan=False)
        features=report['coherent_features'];self.assertTrue(any(c['accepted'] for c in features['candidates']))
        self.assertFalse(features['automatic_notch_applied']);self.assertFalse(report['absolute_calibration'])
        self.assertEqual(features['training_sample_stop_exclusive'],features['validation_sample_start'])
    def test_cli_exposes_phase_features(self):
        with tempfile.TemporaryDirectory() as path:
            path=Path(path);np.save(path/'iq.npy',self.samples())
            self.assertEqual(main([str(path/'iq.npy'),'--sample-rate','1000000','--fft-size','2048','--segments','255','--coherent-features','-O',str(path/'out.json')]),0)
            features=json.loads((path/'out.json').read_text())['coherent_features']
            self.assertFalse(features['correction_applied_to_source'])
            self.assertTrue(features['candidates'])
    def test_default_report_does_not_run_extra_phase_analysis(self):
        report=analyze_iq(self.samples(),1e6,fft_size=2048,segments=255)
        self.assertNotIn('coherent_features',report)
