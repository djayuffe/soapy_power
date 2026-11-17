import json
import math
import unittest

import numpy as np

from soapypower import spur_pipeline as sp
from soapypower import spur_twin as st


def _score(report, expected, step_hz):
    found = []
    for track in report['tracks']:
        kind = track.get('kind')
        if kind == 'lo_harmonic':
            kind = 'spur-{}'.format(track.get('order'))
        found.append({'if_hz': track['points'][0]['if_hz'], 'kind': kind})
    return st.score(found, expected, step_hz)


class SpurPipelineTests(unittest.TestCase):
    def setUp(self):
        self.twin = st.Twin(bins=2048, rate_hz=56e6, dof=20.0, seed=2)

    def test_t1_to_t5_pipeline_scores(self):
        for name, scenario in st.scenarios().items():
            captures = sp.captures_from_twin(self.twin, scenario['los'],
                                             tones=scenario['tones'])
            report = sp.analyze_sweeps(captures, profile='b200', min_hits=3)
            score = _score(report, scenario['expected'], self.twin.step)
            self.assertEqual(score['missed'], 0, (name, score, report['diagnostics']))
            self.assertEqual(score['kind_errors'], 0, (name, score))
            if name.startswith('T1'):
                self.assertEqual(score['fp'], 0, (name, score))
                self.assertLess(abs(report['floor']['change_db']), 0.2)
                self.assertLess(report['floor']['median_bias_db'], 0.0)
                self.assertLess(report['floor']['min_hold_bias_3_db'], 0.0)

    def test_t3_is_a_third_order_lo_harmonic(self):
        scenario = st.scenarios()['T3 800 MHz out-of-band tone']
        report = sp.analyze_sweeps(sp.captures_from_twin(self.twin, scenario['los'],
                                                         tones=scenario['tones']),
                                   profile='b200')
        self.assertEqual(len(report['spurs']), 1)
        spur = report['spurs'][0]
        self.assertEqual(spur['kind'], 'lo_harmonic')
        self.assertEqual(spur['order'], 3)
        self.assertAlmostEqual(spur['source_hz'], 800e6, delta=0.2e6)
        self.assertEqual(spur['band'], 'LTE 800 downlink')
        self.assertEqual(report['automatic_exclusions'], [])

    def test_t2_signal_and_optional_image_are_distinguished(self):
        scenario = st.scenarios()['T2 200 MHz-band in-band tone']
        report = sp.analyze_sweeps(sp.captures_from_twin(self.twin, scenario['los'],
                                                         tones=scenario['tones']),
                                   profile='b200')
        signals = [t for t in report['tracks'] if t['kind'] == 'signal']
        self.assertEqual(len(signals), 1)
        self.assertGreater(signals[0]['signal_probability'], 0.9)
        self.assertAlmostEqual(signals[0]['source_hz'], 200e6, delta=0.1e6)

    def test_floor_after_has_explicit_spur_fraction(self):
        scenario = st.scenarios()['T3 800 MHz out-of-band tone']
        report = sp.analyze_sweeps(sp.captures_from_twin(self.twin, scenario['los'],
                                                         tones=scenario['tones']),
                                   profile='b200')
        self.assertIn('spur_fraction', report['floor']['after'])
        self.assertGreaterEqual(report['floor']['after']['spur_fraction'], 0.0)
        self.assertLessEqual(report['floor']['after']['spur_fraction'], 1.0)
        self.assertLess(report['floor']['after']['spur_bins'], self.twin.bins)

    def test_drive_order_finds_cubic_intermod(self):
        captures = []
        for drive in (0.0, -5.0, -10.0):
            for lo in (260e6, 262e6, 258e6):
                captures.append(sp.make_capture(
                    lo, self.twin.capture(lo, imd=((268e6, 35.0, 3),), drive_db=drive),
                    if_hz=self.twin.if_hz, rate_hz=self.twin.rate, dof=self.twin.dof,
                    drive_db=drive, capture_id='d{}-{}'.format(drive, lo)))
        report = sp.analyze_sweeps(captures, profile='b200')
        tracks = [t for t in report['tracks'] if t['drive'].get('slope_db_per_db') is not None]
        self.assertTrue(tracks)
        self.assertTrue(any(abs(t['drive']['slope_db_per_db'] - 3.0) < 0.5 for t in tracks))
        self.assertEqual(len(report['intermod']), 1)
        self.assertEqual(report['intermod'][0]['attribution'], 'intermod')
        self.assertEqual(report['intermod'][0]['drive_kind'], 'nonlinear-3')
        self.assertEqual(report['signals'], [])

    def test_json_output_is_strict(self):
        scenario = st.scenarios()['T2 200 MHz-band in-band tone']
        report = sp.analyze_sweeps(sp.captures_from_twin(self.twin, scenario['los'],
                                                         tones=scenario['tones']),
                                   profile='b200')
        text = json.dumps(report, allow_nan=False)
        self.assertIn('automatic_exclusions', text)
        self.assertNotIn('NaN', text)

    def test_json_roundtrip_captures_are_normalized(self):
        scenario = st.scenarios()['T2 200 MHz-band in-band tone']
        captures = sp.captures_from_twin(self.twin, scenario['los'],
                                         tones=scenario['tones'])
        payload = [{'lo_hz': float(c['lo_hz']),
                    'if_hz': [float(x) for x in c['if_hz']],
                    'power': [float(x) for x in c['power']],
                    'dof': float(c['dof']),
                    'drive_db': float(c['drive_db']),
                    'capture_id': c['capture_id']}
                   for c in captures]
        report = sp.analyze_sweeps(payload, profile='b200')
        self.assertEqual(len(report['signals']), 1)
        self.assertAlmostEqual(report['signals'][0]['source_hz'], 200e6, delta=0.2e6)
        self.assertIsInstance(report['tracks'][0]['points'][0]['if_hz'], float)

    def test_single_capture_is_unidentifiable_but_not_fabricated(self):
        tone = ((269e6, 40.0),)
        capture = sp.captures_from_twin(self.twin, [260e6], tones=tone)[0]
        report = sp.analyze_sweeps([capture], profile='b200')
        self.assertFalse(report['identifiability']['identifiable_for_3rd_order'])
        self.assertEqual(report['signals'], [])
        self.assertEqual(report['spurs'], [])


if __name__ == '__main__':
    unittest.main()
