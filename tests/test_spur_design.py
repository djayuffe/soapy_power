import json
import unittest

from soapypower import spur_design as sd


class SpurDesignTests(unittest.TestCase):
    def test_default_lo_plan_is_separable(self):
        plan = sd.default_lo_plan(260e6, rate_hz=56e6)
        self.assertEqual(len(plan['offsets_hz']), 3)
        self.assertGreater(plan['span_hz'], 0)
        self.assertTrue(plan['separation_1_vs_3']['separable_3sigma'])
        self.assertGreater(plan['separation_1_vs_3']['separation_sigma'], 3)

    def test_d_optimal_offsets_respect_count_and_span(self):
        candidates = [250e6, 255e6, 258e6, 260e6, 262e6, 265e6, 270e6]
        result = sd.d_optimal_offsets(260e6, candidates, count=3, sigma_hz=10e3,
                                      min_span_hz=10e6)
        self.assertEqual(len(result['offsets_hz']), 3)
        self.assertGreaterEqual(result['span_hz'], 10e6)
        self.assertLessEqual(set(result['offsets_hz']), set(candidates))

    def test_filter_bank_covers_the_whole_range_without_gaps(self):
        bank = sd.suboctave_filter_bank(70e6, 6e9, max_ratio=2.0)
        self.assertEqual(bank['bands'][0]['low_hz'], 70e6)
        self.assertEqual(bank['bands'][-1]['high_hz'], 6e9)
        for first, second in zip(bank['bands'], bank['bands'][1:]):
            self.assertEqual(first['high_hz'], second['low_hz'])
        self.assertIsNotNone(sd.filter_for_rf(bank, 800e6))
        self.assertIsNone(sd.filter_for_rf(bank, 10e6))

    def test_sweep_plan_is_cartesian_and_JSON_safe(self):
        sweeps = sd.sweep_plan(260e6, [258e6, 260e6, 262e6],
                               gains_db=(0.0, -10.0),
                               filter_states=('off', 'on'))
        self.assertEqual(len(sweeps), 12)
        json.dumps(sweeps, allow_nan=False)
        self.assertTrue(all('lo_hz' in s and 'filter' in s for s in sweeps))

    def test_lab_matrix_has_t1_to_t5(self):
        matrix = sd.lab_matrix()
        self.assertEqual(set(matrix), {'T1', 'T2', 'T3', 'T4', 'T5'})
        self.assertIn('LO-harmonic', matrix['T3']['expected'])
        self.assertIn('drive order', matrix['T4']['tests'])

    def test_design_report_is_complete_and_JSON_safe(self):
        report = sd.design_report(center_hz=260e6, profile='b200')
        self.assertEqual(report['profile']['name'], 'Ettus B200mini (AD9364)')
        self.assertEqual(len(report['sweeps']), 18)
        self.assertTrue(report['identifiability']['separation_1_vs_3']['separable_3sigma'])
        text = json.dumps(report, allow_nan=False)
        self.assertIn('advisory hardware plan', text)


if __name__ == '__main__':
    unittest.main()
