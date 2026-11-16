"""Physical sign conventions and undefined-noise-phase gates are explicit."""
import unittest
import numpy as np
import importlib.util
from soapypower.phase_diagnostics import analyze_phase

class PhaseDiagnosticTests(unittest.TestCase):
    def test_positive_complex_tone_has_negative_90_iq_cross_phase(self):
        n=np.arange(65536);z=np.exp(2j*np.pi*73*n/1024)
        r=analyze_phase(z,1024,fft_size=1024,max_segments=64)
        self.assertAlmostEqual(r['iq_cross_phase_deg'][73],-90,places=8)
        self.assertAlmostEqual(r['iq_coherence_squared'][73],1,places=10)

    @unittest.skipUnless(importlib.util.find_spec('scipy'), 'SciPy comparison is optional')
    def test_cross_spectrum_matches_scipy_convention(self):
        from scipy import signal
        rng=np.random.default_rng(10);z=rng.normal(size=32768)+1j*rng.normal(size=32768)
        r=analyze_phase(z,2048,fft_size=512,max_segments=127)
        z=z-z.mean();win=.5-.5*np.cos(2*np.pi*np.arange(512)/512)
        f,c=signal.csd(z.real,z.imag,fs=2048,window=win,nperseg=512,noverlap=256,detrend=False)
        # scipy one-sided spectra double interior bins; phase/coherence unchanged.
        expected=r['iq_cross_spectrum'].copy()/(2048*np.sum(win**2));expected[1:-1]*=2
        np.testing.assert_allclose(c,expected,rtol=1e-10,atol=1e-15)

    def test_circular_noise_masks_phase_without_inventing_axis(self):
        rng=np.random.default_rng(1);z=rng.normal(size=262144)+1j*rng.normal(size=262144)
        r=analyze_phase(z,1e6,fft_size=1024,max_segments=255)
        self.assertLess(r['noncircularity_abs'],.02);self.assertIsNone(r['ellipse_axis_deg'])
        self.assertGreater(np.isnan(r['iq_cross_phase_deg']).mean(),.99)
        self.assertLess(r['circular_resultant_r2'],.02)

    def test_rotated_ellipse_has_correct_axis_and_rotation_invariant_strength(self):
        rng=np.random.default_rng(3);z=3*rng.normal(size=65536)+1j*rng.normal(size=65536)
        a=analyze_phase(z,1e6,fft_size=512)
        b=analyze_phase(z*np.exp(1j*np.deg2rad(32)),1e6,fft_size=512)
        self.assertAlmostEqual(a['noncircularity_abs'],b['noncircularity_abs'],places=10)
        self.assertAlmostEqual(b['ellipse_axis_deg']-a['ellipse_axis_deg'],32,places=8)
        self.assertGreater(a['noncircularity_abs'],.75)

    def test_real_improper_noise_has_mirror_pair_coherence(self):
        z=np.random.default_rng(4).normal(size=65536).astype(complex)
        r=analyze_phase(z,1e6,fft_size=512)
        np.testing.assert_allclose(r['complementary_coherence_squared'],1,atol=1e-12)
        np.testing.assert_allclose(r['complementary_phase_deg'],0,atol=1e-9)

    def test_offset_and_amplitude_scaling_do_not_change_centered_phase_statistics(self):
        rng=np.random.default_rng(6);z=rng.normal(size=65536)+1j*rng.normal(size=65536)
        a=analyze_phase(z,1e6,fft_size=512);b=analyze_phase(z*7+12-9j,1e6,fft_size=512)
        np.testing.assert_allclose(a['iq_coherence_squared'],b['iq_coherence_squared'],atol=1e-12)
        self.assertAlmostEqual(a['noncircularity_abs'],b['noncircularity_abs'],places=12)

    def test_invalid_input_and_undefined_zero_variance_are_rejected(self):
        for z in [np.zeros(100,complex),np.ones(65536,complex),np.ones(65536),np.full(65536,complex(np.nan,0))]:
            with self.assertRaises(ValueError):analyze_phase(z,1e6)
        for kw in [dict(coherence_gate=0),dict(overlap=1),dict(sample_rate=-1),dict(fft_size=9)]:
            args=dict(sample_rate=1e6,fft_size=512);args.update(kw)
            with self.assertRaises(ValueError):analyze_phase(np.arange(65536).astype(complex),**args)

    def test_negative_complex_tone_reverses_cross_phase_sign(self):
        n=np.arange(65536);z=np.exp(-2j*np.pi*73*n/1024)
        r=analyze_phase(z,1024,fft_size=1024,max_segments=64)
        self.assertAlmostEqual(r['iq_cross_phase_deg'][73],90,places=8)

    def test_mirror_pair_phase_rotates_twice_the_iq_coordinate_angle(self):
        z=np.random.default_rng(8).normal(size=65536).astype(complex)*np.exp(1j*np.deg2rad(32))
        r=analyze_phase(z,1e6,fft_size=512)
        np.testing.assert_allclose(r['complementary_phase_deg'],64,atol=1e-9)

    def test_unexamined_tail_cannot_change_analyzed_prefix_phase(self):
        rng=np.random.default_rng(9);z=rng.normal(size=2816)+1j*rng.normal(size=2816)
        a=analyze_phase(z,1e6,fft_size=512,max_segments=10)
        b=analyze_phase(np.r_[z,np.full(5000,1e6+3e6j)],1e6,fft_size=512,max_segments=10)
        self.assertEqual(b['unexamined_samples'],5000)
        self.assertAlmostEqual(a['circular_resultant_r2'],b['circular_resultant_r2'],places=15)
        np.testing.assert_allclose(a['iq_coherence_squared'],b['iq_coherence_squared'],atol=0)
