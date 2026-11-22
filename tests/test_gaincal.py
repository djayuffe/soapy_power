import json, sys, tempfile, unittest
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import gaincal as gc

CENTERS = [137.85e6, 400.75e6, 435.35e6]


def write_cal(d, gains, adc_db=-138.0, fe_db=-172.0, strong=0.002, rc_from=None):
    """digital floor = power sum of a fixed ADC floor and front-end noise + gain; raw IQ peak grows 20 dB/decade."""
    rng = np.random.default_rng(2)
    for g in gains:
        if rc_from is not None and g >= rc_from:
            (d / f'gain_{g}.jsonl').write_text(''); (d / f'gain_{g}.rc').write_text('1'); break
        lines = []
        for c in CENTERS:
            floor = 10 * np.log10(10 ** (adc_db / 10) + 10 ** ((fe_db + g) / 10))
            p = floor + rng.normal(0, 0.28, 8192); p[4086:4107] = np.nan
            peak = min(strong * 10 ** (g / 20), 0.99997)
            lines.append(json.dumps(dict(step_hz=244.140625, power=[None if np.isnan(v) else float(v) for v in p],
                                         metadata=dict(tuned_center_hz=c, measurement_status='complete',
                                                       headroom=dict(max_component=peak, p999_component=peak / 4, threshold_095_fraction=float(peak >= .95))))))
        (d / f'gain_{g}.jsonl').write_text('\n'.join(lines) + '\n'); (d / f'gain_{g}.rc').write_text('0')


class GainCalTests(unittest.TestCase):
    def test_picks_smallest_gain_near_the_noise_plateau_inside_headroom(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t); write_cal(d, [10, 20, 30, 40, 50, 60, 70], strong=0.0005)
            r = gc.choose(d, fallback=20)
            # input-referred noise: ADC-limited at low gain, front-end limited (-172) from ~45 dB; 0.0005*10^(70/20)=1.58 -> 70 unsafe
            self.assertEqual(r['chosen_gain_db'], 40.0, r['reason'])
            self.assertFalse([x for x in r['rows'] if x['gain_db'] == 70][0]['safe'])

    def test_strong_signal_limits_gain(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t); write_cal(d, [10, 20, 30, 40, 50, 60, 70], strong=0.01)       # 0.5 reached near 34 dB
            r = gc.choose(d, fallback=20)
            self.assertEqual(r['chosen_gain_db'], 30.0, r['reason'])

    def test_overload_stops_the_sweep_and_no_safe_point_falls_back(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t); write_cal(d, [10, 20], rc_from=10)
            r = gc.choose(d, fallback=20)
            self.assertEqual(r['chosen_gain_db'], 20)
            self.assertIn('fallback', r['reason'])


if __name__ == '__main__':
    unittest.main()
