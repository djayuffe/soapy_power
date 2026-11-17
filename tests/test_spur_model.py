import math
import unittest

import numpy as np

from soapypower import spur_model as sm


class SpurModelTests(unittest.TestCase):
    def test_square_wave_harmonics_are_odd_and_decay(self):
        amps = sm.square_wave_amplitudes((1, 3, 5, 7))
        self.assertAlmostEqual(amps[1], 4.0 / math.pi)
        self.assertAlmostEqual(amps[3], 4.0 / (3.0 * math.pi))
        self.assertGreater(amps[1], amps[3])
        self.assertGreater(amps[3], amps[5])
        with self.assertRaises(ValueError):
            sm.square_wave_amplitudes((2,))

    def test_mixing_products_include_signal_image_and_harmonic(self):
        products = sm.mixing_products(800e6, 260e6, orders=(1, 3, 5))
        by = {(p['order'], p['sign']): p for p in products}
        self.assertAlmostEqual(by[(1, 1)]['if_hz'], 540e6)
        self.assertAlmostEqual(by[(1, -1)]['if_hz'], -540e6)
        self.assertAlmostEqual(by[(3, 1)]['if_hz'], 20e6)
        self.assertEqual(by[(1, 1)]['kind'], 'signal')
        self.assertEqual(by[(1, -1)]['kind'], 'image')
        self.assertEqual(by[(3, 1)]['kind'], 'lo_harmonic')
        self.assertEqual(by[(3, 1)]['slope_hz_per_hz'], -3)

    def test_spur_atlas_marks_in_band(self):
        atlas = sm.spur_atlas(260e6, [(800e6, 1.0)], rate_hz=56e6)
        self.assertTrue(any(p['in_band'] and p['order'] == 3 for p in atlas))
        self.assertFalse(any(p['in_band'] and p['order'] == 1 and p['kind'] == 'signal'
                             for p in atlas))

    def test_iq_model_matches_requested_image_rejection(self):
        for target in (20.0, 35.0, 50.0):
            gain = sm.iq_from_image_rejection(target)
            self.assertAlmostEqual(sm.image_rejection_db(gain), target, places=4)
        self.assertEqual(sm.image_rejection_db(0.0, 0.0), float('inf'))
        c, d = sm.iq_coefficients(0.0, 0.0)
        self.assertEqual(c, 1.0)
        self.assertEqual(d, 0.0)

    def test_time_domain_image_ratio_matches_coefficients(self):
        rate, fft_size = 56e6, 1024
        iq, truth = sm.simulate_iq(260e6, rate, fft_size * 8,
                                   tones=[(269e6, 1.0)], noise_amplitude=1e-4,
                                   iq_gain_db=1.0, iq_phase_deg=5.0, seed=3)
        freqs, psd, _ = sm.welch_psd(iq, rate, fft_size=fft_size, segments=6)
        signal = psd[np.argmin(np.abs(freqs - 9e6))]
        image = psd[np.argmin(np.abs(freqs + 9e6))]
        measured = 10.0 * math.log10(signal / image)
        self.assertAlmostEqual(measured, sm.image_rejection_db(1.0, 5.0), delta=1.0)

    def test_simulated_psd_puts_third_harmonic_in_band_without_alias(self):
        result = sm.simulate_psd(260e6, 56e6, tones=[(800e6, 1.0)],
                                 fft_size=1024, segments=8, noise_amplitude=0.01,
                                 bandwidth_hz=56e6, seed=1)
        if_hz, power = result['if_hz'], result['power']
        peak = if_hz[int(np.argmax(power))]
        self.assertAlmostEqual(peak, 20.0e6, delta=0.1e6)
        self.assertLess(power[np.argmin(np.abs(if_hz + 20e6))],
                        0.01 * power[np.argmin(np.abs(if_hz - 20e6))])

    def test_welch_psd_white_noise_normalization(self):
        rng = np.random.default_rng(5)
        iq = (rng.normal(size=200000) + 1j * rng.normal(size=200000)) / math.sqrt(2.0)
        freqs, psd, dof = sm.welch_psd(iq, 2e6, fft_size=1024, segments=100)
        self.assertEqual(dof, 200)
        self.assertAlmostEqual(float(np.mean(psd) * 2e6), 1.0, delta=0.04)
        self.assertAlmostEqual(float(np.mean(psd[np.abs(freqs) > 10e3]) * 2e6),
                               1.0, delta=0.04)

    def test_two_tone_intermod_products(self):
        products = sm.two_tone_intermod(200e6, 300e6, order=3)
        frequencies = {round(p['rf_hz'] / 1e6, 6) for p in products}
        self.assertIn(100.0, frequencies)
        self.assertIn(400.0, frequencies)
        self.assertIn(700.0, frequencies)
        self.assertIn(800.0, frequencies)
        self.assertTrue(all(p['order'] == 3 for p in products))

    def test_receiver_profiles_cover_documented_ranges(self):
        b200 = sm.receiver_profile('b200')
        self.assertEqual((b200.rf_min_hz, b200.rf_max_hz), (70e6, 6e9))
        blade = sm.receiver_profile('bladerf')
        self.assertEqual((blade.rf_min_hz, blade.rf_max_hz), (300e6, 3.8e9))
        self.assertEqual(b200.harmonic_orders, (1, 3, 5, 7))
        self.assertIn('no preselector', b200.notes)

    def test_adc_quantization_is_bounded_and_reports_clipping(self):
        iq, _ = sm.simulate_iq(260e6, 56e6, 4096, tones=[(269e6, 1.0)],
                               noise_amplitude=0.1, seed=4)
        quantized, info = sm.quantize_iq(iq, bits=8, full_scale=0.5)
        self.assertEqual(quantized.shape, iq.shape)
        self.assertLessEqual(float(np.max(np.abs(quantized.real))), 0.5)
        self.assertGreater(info['clipped_components'], 0)
        self.assertGreater(info['step'], 0)

    def test_iq_temperature_drift_is_explicit(self):
        result = sm.iq_temperature_error(0.0, 0.0, 30.0, 25.0)
        self.assertAlmostEqual(result['gain_db'], 0.005)
        self.assertAlmostEqual(result['phase_deg'], 0.05)
        self.assertEqual(result['delta_c'], 5.0)


if __name__ == '__main__':
    unittest.main()
