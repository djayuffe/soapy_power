import math
import unittest
import numpy as np
from soapypower import floor_estimation as fe
from soapypower import spur_twin as st
from soapypower.model_validation import HopStore, synthetic_scan, telemetry_summary
import tempfile, pathlib


class FloorTests(unittest.TestCase):
    def test_median_bias_matches_theory(self):
        self.assertAlmostEqual(fe.median_to_mean_db(2), 10 * math.log10(math.log(2)), places=6)
        self.assertGreater(fe.median_to_mean_db(64), -0.06)
        self.assertAlmostEqual(fe.min_hold_to_mean_db(2, 2), 10 * math.log10(0.5), places=3)

    def test_floor_unbiased_and_beats_median(self):
        for dof in (2, 10, 64):
            rng = np.random.default_rng(dof)
            x = rng.gamma(dof / 2, 2 / dof, 100000)
            est = fe.floor_estimate(x, dof=dof)
            self.assertAlmostEqual(est['mean_db'], 0.0, delta=0.05)
            lo, hi = est['ci_db']
            self.assertLess(lo, 0.0 + 0.02); self.assertGreater(hi, -0.02)
            self.assertLess(abs(est['mean_db']), abs(est['median_db_uncorrected']) + 1e-9 if dof < 64 else 1)

    def test_dof_estimate_and_spur_exclusion(self):
        rng = np.random.default_rng(1)
        x = rng.gamma(5, 1 / 5, 50000)
        self.assertAlmostEqual(fe.estimate_dof(x), 10, delta=1.0)
        y = x.copy(); y[:200] *= 1000
        self.assertAlmostEqual(fe.floor_estimate(y, dof=10)['mean_db'], 0.0, delta=0.05)

    def test_confidence_interval_coverage(self):
        rng = np.random.default_rng(2)
        hit = 0
        for _ in range(100):
            e = fe.floor_estimate(rng.gamma(4, 1 / 4, 2000), dof=8)
            hit += e['ci_db'][0] <= 0 <= e['ci_db'][1]
        self.assertGreaterEqual(hit, 88)

    def test_fdr_controls_false_alarms(self):
        rng = np.random.default_rng(3)
        x = rng.gamma(4, 1 / 4, 200000)
        self.assertLessEqual(len(fe.detect_peaks(x, 1.0, 8, method='bh', alpha=0.01)['index']), 3)
        self.assertGreater(len(fe.detect_peaks(x, 1.0, 8, method='fixed', fixed_db=3.0)['index']), 50)
        x[1234] *= 100
        self.assertIn(1234, fe.detect_peaks(x, 1.0, 8, method='bonferroni')['index'])

    def test_lo_slope_classification(self):
        lo = np.array([260e6, 262e6, 258e6])
        for n, s, kind in ((1, 1, 'signal'), (1, -1, 'image'), (3, 1, 'lo_harmonic'), (5, 1, 'lo_harmonic')):
            f = s * (800e6 - n * lo)
            r = fe.classify_lo_slope(lo, f, resolution_hz=14e3)
            self.assertEqual((r['status'], r['order'], r['sign'], r['kind']), ('classified', n, s, kind))
        self.assertEqual(fe.classify_lo_slope(lo[:2], [1, 2])['status'], 'unidentifiable')
        self.assertEqual(fe.classify_lo_slope(lo, [5e6, 6.3e6, 3.1e6], resolution_hz=14e3)['status'], 'no_law_fits')

    def test_drive_order(self):
        for n in (1, 2, 3):
            r = fe.drive_order([-20, -15, -10, -5], [n * d + 7 for d in (-20, -15, -10, -5)])
            self.assertEqual(r['order'], n)
        self.assertEqual(fe.drive_order([0, 0.2], [0, 1])['status'], 'unidentifiable')

    def test_kalman_recovers_slope_and_flags_monotone(self):
        rng = np.random.default_rng(4)
        t, temp, y, g = [], [], [], []
        offs = rng.normal(0, 3, 30)
        clock = 0.0
        for _ in range(8):
            for c in range(30):
                clock += 0.5
                tt = 46.5 + 0.5 * math.sin(2 * math.pi * clock / 55)
                t.append(clock); temp.append(tt); g.append(c)
                y.append(offs[c] + 0.05 * (tt - 46.5) + rng.normal(0, 0.03))
        k = fe.floor_temperature_kalman(t, temp, y, g)
        self.assertEqual(k['status'], 'fit')
        self.assertAlmostEqual(k['slope_db_per_c'], 0.05, delta=0.03)
        self.assertFalse(k['confounded'])
        mono = fe.floor_temperature_kalman(t, np.linspace(46, 47, len(t)), y, g)
        self.assertTrue(mono['confounded'])
        self.assertEqual(fe.floor_temperature_kalman(t, [46.0] * len(t), y, g)['status'], 'insufficient_temperature_variation')

    def test_telemetry_summary_reports_kalman(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / 's.jsonl'
            temp = lambda t: 46.5 + 0.5 * math.sin(t / 6.0)
            synthetic_scan(path, 100e6, 300e6, 40e6, passes=12, temperature=temp, thermal_db_per_c=0.05, seed=2,
                           bins=1024, rate_hz=56e6)
            store = HopStore.from_files([path], 100e6, 300e6, 1e6)
            report = telemetry_summary([store], controlled_input=True, min_groups=3)
        self.assertIn('floor_vs_temperature_kalman', report)
        self.assertEqual(report['floor_vs_temperature_kalman']['status'], 'fit')


class TwinTests(unittest.TestCase):
    def test_matrix_classifies_every_scenario(self):
        for name, sc in st.scenarios().items():
            for seed in range(3):
                twin = st.Twin(seed=seed)
                res = st.analyze(twin, sc['los'], tones=sc['tones'])
                s = st.score(res['peaks'], sc['expected'], twin.step)
                self.assertEqual((s['fp'], s['missed'], s['kind_errors']), (0, 0, 0), (name, seed, res['peaks']))

    def test_legacy_misreads_the_spur(self):
        sc = st.scenarios()['T3 800 MHz out-of-band tone']
        twin = st.Twin(seed=0)
        old = st.legacy_analyze(twin, sc['los'], tones=sc['tones'])
        self.assertEqual(old['peaks'][0]['kind'], 'signal')   # no way to tell it is a 3rd-order response
        new = st.analyze(st.Twin(seed=0), sc['los'], tones=sc['tones'])
        self.assertEqual(new['peaks'][0]['kind'], 'spur-3')
        self.assertAlmostEqual(new['peaks'][0]['source_hz'], 800e6, delta=1e5)

    def test_imd_order_from_drive(self):
        twin = st.Twin(seed=5, dof=200)
        drive = [-10.0, -5.0, 0.0, 5.0]
        levels = []
        for d in drive:
            cap = twin.capture(260e6, imd=((265e6, 30.0, 3),), drive_db=d)
            i = int(round(5e6 / twin.step)) + twin.bins // 2
            levels.append(10 * math.log10(cap[i - 1:i + 2].max()))
        self.assertEqual(fe.drive_order(drive, levels)['order'], 3)


if __name__ == '__main__':
    unittest.main()
