import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools' / 'bladerf'))
import bladerf_profile as bp  # noqa: E402

G_LOW = {'lna': 6, 'rxvga1': 30, 'rxvga2': 0}
G_HIGH = {'lna': 6, 'rxvga1': 30, 'rxvga2': 24}


def profile():
    anchor = lambda hz, gains, dc, ok=True: dict(center_hz=hz, gains=gains, normalized_dc=dc, validated=ok)
    return dict(lms_state_at_fit='25,31', dc_max_anchor_distance_hz=150e6, bands=[
        dict(start_hz=42e6, stop_hz=237.5e6, gains=G_LOW, dc_anchors=[anchor(70e6, G_LOW, [-.125, .375])]),
        dict(start_hz=237.5e6, stop_hz=3800e6, gains=G_HIGH,
             dc_anchors=[anchor(240e6, G_LOW, [.25, .25]), anchor(3500e6, G_HIGH, [.0625, -.5]),
                         anchor(3000e6, G_HIGH, [.5, .5], ok=False)])])


class ProfileSelectionTests(unittest.TestCase):
    def test_anchor_requires_same_gain_domain_and_distance(self):
        gains, dc, hz, ok = bp.select(profile(), None, 3450e6)
        self.assertEqual((dc, hz, ok), ([.0625, -.5], 3500e6, True))
        # 240 MHz anchor has other gains; the unvalidated 3000 MHz anchor is never used
        self.assertEqual(bp.select(profile(), None, 3100e6)[1:], ([0.0, 0.0], None, False))
        # an XB-200 anchor (70 MHz) never serves a native-path center and vice versa
        self.assertEqual(bp.select(profile(), None, 200e6)[2], 70e6)
        # 230 MHz (XB-200, low gains): the 240 MHz anchor has those gains and is 10 MHz away,
        # but on the native path; the 70 MHz anchor is beyond 150 MHz
        self.assertEqual(bp.select(profile(), None, 230e6)[1:], ([0.0, 0.0], None, False))
        self.assertIsNone(bp.matching_dc_anchor([dict(center_hz=230e6, gains=G_LOW, validated=True)], G_LOW, 240e6))

    def test_dense_gain_point_overrides_band_gain(self):
        dense = dict(points=[dict(center_hz=3450e6, gains={'lna': 6, 'rxvga1': 30, 'rxvga2': 18}, total_gain_db=48)])
        gains, dc, hz, ok = bp.select(profile(), dense, 3450e6)
        self.assertEqual(gains['rxvga2'], 18.0)
        self.assertEqual((dc, ok), ([0.0, 0.0], False))      # no anchor at the reduced gain

    def test_dense_dc_overrides_only_matching_gains(self):
        table = dict(kind='bladerf1-dense-dc-table', lms_state='25,31', points=[
            dict(center_hz=3450e6, gains=G_HIGH, normalized_dc=[.109375, -.40625], accepted=True),
            dict(center_hz=3453.5e6, gains=G_HIGH, normalized_dc=[.5, .5], accepted=False),
            dict(center_hz=100e6, gains=G_HIGH, normalized_dc=[.5, .5], accepted=True)])
        dense_dc = bp.DenseDC(table)
        self.assertEqual(bp.settings(profile(), None, 3450e6, dense_dc)[1:], ([.109375, -.40625], 'dense', True))
        self.assertEqual(bp.settings(profile(), None, 3453.5e6, dense_dc)[2], 3500e6)   # rejected row ignored
        self.assertEqual(bp.settings(profile(), None, 100e6, dense_dc)[2], 70e6)        # gains differ
        self.assertEqual(bp.settings(profile(), None, 3450e6, dense_dc, iq_guard=True)[2], 3500e6)
        with self.assertRaises(ValueError):
            bp.DenseDC(dict(kind='other', points=[]))


class DcSolveTests(unittest.TestCase):
    def simulate(self, jac, m0_at_zero, base, step_i, step_q):
        mean = lambda dc: complex(*(np.asarray(m0_at_zero) + jac @ np.asarray(dc, float)))
        q = bp.DC_QUANTUM
        return (mean(base), mean([base[0] + step_i * q, base[1]]), mean([base[0], base[1] + step_q * q]))

    def test_recovers_lattice_optimum_with_cross_coupling_and_swap(self):
        for jac in (np.array([[-.23, .035], [-.26, -1.03]]),      # RXVGA2 24 dB, cross-coupled
                    np.array([[.006, -.073], [-.027, -.005]])):   # XB-200 mix path: I/Q swapped
            truth = np.array([.2031, -.3125])                       # control values that null the mean
            m0 = -jac @ truth
            base = [0.0, 0.0]
            m_base, m_i, m_q = self.simulate(jac, m0, base, 8, 8)
            result = bp.solve_dc(base, m_base, m_i, m_q, 8, 8)
            self.assertLess(result['predicted_abs_mean'], np.abs(jac).max() * bp.DC_QUANTUM)
            self.assertTrue(all(abs(v * 64 - round(v * 64)) < 1e-12 for v in result['dc']))
            self.assertLess(np.abs(np.array(result['dc']) - truth).max(), 2 * bp.DC_QUANTUM)

    def test_ill_conditioned_and_range(self):
        jac = np.array([[.1, .1], [.1, .1000001]])
        m_base, m_i, m_q = self.simulate(jac, [.01, .01], [0, 0], 8, 8)
        self.assertIsNone(bp.solve_dc([0, 0], m_base, m_i, m_q, 8, 8))
        self.assertEqual(list(bp.clip_codes([70, -70, 3.4])), [63, -63, 3])
        self.assertEqual(bp.perturbation_codes([10, -60], 8), [-8, 8])
        self.assertEqual(bp.perturbation_codes([-60, 0], 8), [8, 8])
        self.assertEqual(bp.perturbation_codes([-58, 0], 8), [8, 8])
        self.assertEqual(bp.perturbation_codes([0, 58], 8), [8, -8])
        with self.assertRaises(ValueError):
            bp.solve_dc([0, 0], 0j, 0j, 0j, 0, 8)

    def test_verification_rule(self):
        self.assertTrue(bp.accept_verified(.05, .003))
        self.assertFalse(bp.accept_verified(.05, .04))
        self.assertTrue(bp.accept_verified(.0025, .0019))     # below the noise floor
        self.assertFalse(bp.accept_verified(.001, .0015))     # never accept a worse value


if __name__ == '__main__':
    unittest.main()
