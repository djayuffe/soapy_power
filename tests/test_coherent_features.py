import unittest
import importlib.util
import numpy as np
from soapypower.coherent_features import coherent_features

@unittest.skipUnless(importlib.util.find_spec('scipy'), 'coherent features require optional SciPy')
class CoherentFeatureTests(unittest.TestCase):
    fs=1e6;nfft=2048
    def analyze(self,z,**kw):
        return coherent_features(z,self.fs,fft_size=self.nfft,origin_exclusion_hz=5000,usable_halfwidth_hz=400000,**kw)
    def tone(self,frequency=123456.7,seed=1):
        rng=np.random.default_rng(seed);n=np.arange(262144)
        return .1*np.exp(2j*np.pi*frequency*n/self.fs)+.08*(rng.normal(size=len(n))+1j*rng.normal(size=len(n)))
    def test_off_bin_tone_validates_with_frequency_fitted_only_on_training(self):
        r=self.analyze(self.tone());accepted=[c for c in r['candidates'] if c['accepted']]
        self.assertTrue(accepted);best=min(accepted,key=lambda c:abs(c['baseband_hz']-123456.7))
        self.assertLess(abs(best['baseband_hz']-123456.7),1)
        self.assertGreater(best['validation_phase_lock'],.95)
        self.assertEqual(r['training_sample_stop_exclusive'],r['validation_sample_start'])
        self.assertAlmostEqual(r['short_enbw_hz']/r['fine_enbw_hz'],64)
    def test_white_noise_does_not_qualify_as_stable_line(self):
        rng=np.random.default_rng(2);z=rng.normal(size=262144)+1j*rng.normal(size=262144)
        r=self.analyze(z);self.assertFalse(any(c['accepted'] for c in r['candidates']))
    def test_changed_validation_frequency_is_rejected(self):
        z=self.tone();h=len(z)//2;n=np.arange(h,len(z));z[h:]*=np.exp(2j*np.pi*5000*n/self.fs)
        r=self.analyze(z);near=[c for c in r['candidates'] if abs(c['baseband_hz']-123456.7)<100]
        self.assertTrue(near);self.assertFalse(any(c['accepted'] for c in near))
    def test_validation_phase_scrambling_is_rejected(self):
        z=self.tone();h=len(z)//2;rng=np.random.default_rng(4)
        for start in range(h,len(z),self.nfft):z[start:start+self.nfft]*=np.exp(1j*rng.uniform(-np.pi,np.pi))
        r=self.analyze(z);near=[c for c in r['candidates'] if abs(c['baseband_hz']-123456.7)<100]
        self.assertTrue(near);self.assertFalse(any(c['accepted'] for c in near))
    def test_input_is_preserved_and_dc_trial_is_not_applied(self):
        z=self.tone()+.4+.2j;original=z.copy();r=self.analyze(z)
        np.testing.assert_array_equal(z,original);self.assertFalse(r['correction_applied_to_source'])
        self.assertLess(r['dc_trial_mean_power_after'],r['dc_trial_mean_power_before']/100)
    def test_negative_tone_and_global_phase_have_correct_frequency(self):
        r=self.analyze(self.tone(-123456.7)*np.exp(1.2j));a=[c for c in r['candidates'] if c['accepted']]
        self.assertTrue(a);self.assertLess(min(abs(c['baseband_hz']+123456.7) for c in a),1)
    def test_invalid_input_and_settings_rejected(self):
        for z in [np.ones(100,complex),np.ones(262144),np.full(262144,complex(np.nan,0))]:
            with self.assertRaises(ValueError):self.analyze(z)
        with self.assertRaises(ValueError):self.analyze(self.tone(),phase_lock_gate=0)

    def test_missing_optional_scipy_has_actionable_error(self):
        import sys
        from unittest.mock import patch
        with patch.dict(sys.modules, {'scipy.ndimage': None}):
            with self.assertRaisesRegex(ValueError, 'optional SciPy'):
                self.analyze(self.tone())
