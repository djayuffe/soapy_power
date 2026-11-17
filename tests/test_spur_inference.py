import math
import unittest

import numpy as np

from soapypower import floor_estimation as fe
from soapypower import spur_inference as si


class SpurInferenceTests(unittest.TestCase):
    def test_gamma_floor_posterior_recovers_exponential_mean(self):
        rng = np.random.default_rng(11)
        p = rng.exponential(1.0, 20000)
        post = si.gamma_floor_posterior(p, dof=2.0)
        self.assertAlmostEqual(post['mean_linear'], 1.0, delta=0.03)
        self.assertLess(post['median_linear'], post['mean_linear'])
        self.assertLess(post['ci_db'][0], post['ci_db'][1])
        self.assertLess(abs(post['mean_db']), 0.15)

    def test_mixture_floor_separates_upper_tail_spurs(self):
        rng = np.random.default_rng(12)
        n = 8000
        clean = rng.exponential(1.0, n)
        p = clean.copy()
        injected = rng.choice(n, 800, replace=False)
        p[injected] *= 40.0
        post = si.mixture_floor_estimate(p, dof=2.0)
        self.assertAlmostEqual(post['mean_linear'], 1.0, delta=0.06)
        self.assertGreater(post['spur_bins'], 400)
        self.assertLess(post['spur_bins'], 1200)
        self.assertGreater(float(np.mean(post['spur_probability'][injected])), 0.8)
        self.assertLess(float(np.mean(post['spur_probability'][np.setdiff1d(np.arange(n), injected)])), 0.2)

    def test_median_and_min_hold_bias_are_negative(self):
        self.assertAlmostEqual(fe.median_to_mean_db(2.0), 10.0 * math.log10(math.log(2.0)), places=9)
        b3, b8 = fe.min_hold_to_mean_db(3, 2.0), fe.min_hold_to_mean_db(8, 2.0)
        self.assertLess(b8, b3)
        self.assertLess(b3, 0.0)

    def test_bayes_track_classifies_signal_image_and_harmonic(self):
        lo = np.array([258e6, 260e6, 262e6])
        # A real source at 269 MHz: IF = +9, +7, +5 MHz.
        signal = si.bayes_classify_track(lo, np.array([9e6, 7e6, 5e6]),
                                         resolution_hz=10e3)
        self.assertEqual(signal['status'], 'classified')
        self.assertEqual(signal['kind'], 'signal')
        self.assertGreater(signal['signal_probability'], 0.8)
        # Its IQ image: IF = -9, -7, -5 MHz.
        image = si.bayes_classify_track(lo, np.array([-9e6, -7e6, -5e6]),
                                        resolution_hz=10e3)
        self.assertEqual(image['kind'], 'image')
        # An 800 MHz source through the 3rd harmonic: IF = +26, +20, +14 MHz.
        spur = si.bayes_classify_track(lo, np.array([26e6, 20e6, 14e6]),
                                       resolution_hz=10e3)
        self.assertEqual(spur['kind'], 'lo_harmonic')
        self.assertEqual(spur['order'], 3)
        self.assertGreater(spur['spur_probability'], 0.8)
        self.assertAlmostEqual(spur['source_hz'], 800e6, delta=1e3)

    def test_bayes_track_refuses_unidentifiable_span(self):
        result = si.bayes_classify_track(np.array([260e6, 260.1e6]),
                                         np.array([9e6, 8.9e6]), resolution_hz=10e3)
        self.assertEqual(result['status'], 'unidentifiable')
        self.assertEqual(result['posterior'], {})

    def test_robust_line_fit_ignores_one_outlier(self):
        x = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
        y = np.array([1.0, 2.0, 3.0, 4.0, 30.0])
        robust = si.robust_line_fit(x, y, sigma=np.ones_like(x))
        self.assertAlmostEqual(robust['slope_hz_per_hz'], 1.0, delta=0.05)
        self.assertLess(robust['robust_scale_hz'], 2.0)

    def test_crlb_shows_wider_lo_span_is_better(self):
        narrow = si.slope_separation([259e6, 260e6, 261e6], 10e3, 1, 3)
        wide = si.slope_separation([250e6, 260e6, 270e6], 10e3, 1, 3)
        self.assertLess(wide['sd_slope'], narrow['sd_slope'])
        self.assertTrue(wide['separable_3sigma'])

    def test_ista_and_omp_recover_a_sparse_signal(self):
        rng = np.random.default_rng(3)
        A = rng.normal(size=(80, 40))
        A /= np.linalg.norm(A, axis=0)
        truth = np.zeros(40)
        truth[[5, 21]] = [2.5, -1.7]
        y = A @ truth
        omp = si.orthogonal_matching_pursuit(y, A, components=2)
        self.assertEqual(set(omp['active'].tolist()), {5, 21})
        self.assertLess(omp['residual_l2'], 1e-9)
        ista = si.ista(y, A, lam=0.001, iterations=1000)
        self.assertGreater(abs(ista['x'][5]), 1.5)
        self.assertGreater(abs(ista['x'][21]), 1.0)

    def test_dictionary_labels_include_harmonic_atoms(self):
        if_hz = np.linspace(-28e6, 28e6, 2048)
        dictionary = si.build_spur_dictionary(if_hz, 260e6, [269e6, 800e6],
                                              orders=(1, 3, 5), sigma_hz=20e3)
        labels = dictionary['labels']
        self.assertTrue(any(label['kind'] == 'lo_harmonic' and label['order'] == 3
                            for label in labels))
        self.assertTrue(any(label['kind'] == 'signal' for label in labels))
        self.assertEqual(dictionary['matrix'].shape[0], if_hz.size)

    def test_sparse_decompose_spectrum_returns_labelled_atoms(self):
        if_hz = np.linspace(-28e6, 28e6, 2048)
        power = np.full(if_hz.size, 1.0)
        # A signal at IF +9 MHz and a 3rd harmonic at +20 MHz.
        power += 20.0 * np.exp(-0.5 * ((if_hz - 9e6) / 20e3) ** 2)
        power += 8.0 * np.exp(-0.5 * ((if_hz - 20e6) / 20e3) ** 2)
        result = si.sparse_decompose_spectrum(power, if_hz, 260e6,
                                              [269e6, 800e6], orders=(1, 3, 5),
                                              sigma_hz=20e3, max_components=4)
        self.assertTrue(result['atoms'])
        self.assertGreater(result['active'], 0)

    def test_coherent_stack_aligns_phase_and_reduces_noise(self):
        rng = np.random.default_rng(21)
        reference = np.exp(1j * 0.3) * (1.0 + 0.0j) + rng.normal(0, 0.01, 256)
        rows = []
        phases = [0.0, 0.7, -1.1]
        for phase in phases:
            rows.append(reference * np.exp(1j * phase))
        result = si.coherent_stack(np.asarray(rows), method='mean')
        self.assertEqual(len(result['phases_rad']), 3)
        self.assertLess(float(np.mean(np.abs(result['combined'] - reference))), 0.02)
        self.assertAlmostEqual(float(result['phases_rad'][1]), 0.7, delta=0.02)


if __name__ == '__main__':
    unittest.main()
