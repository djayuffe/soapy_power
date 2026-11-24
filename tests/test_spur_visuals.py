import tempfile
import unittest
from pathlib import Path

import numpy as np

from soapypower import spur_twin as st
from soapypower import spur_pipeline as sp

try:
    import matplotlib  # noqa: F401
    HAVE_MPL = True
except Exception:
    HAVE_MPL = False


@unittest.skipUnless(HAVE_MPL, 'matplotlib is not installed')
class SpurVisualTests(unittest.TestCase):
    def test_demo_figures_are_png_and_pdf(self):
        from soapypower import spur_visuals as sv
        twin = st.Twin(bins=1024, rate_hz=56e6, dof=20.0, seed=4)
        scenario = st.scenarios()['T3 800 MHz out-of-band tone']
        report = sp.analyze_sweeps(sp.captures_from_twin(twin, scenario['los'],
                                                         tones=scenario['tones']),
                                   profile='b200')
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            paths = sv.save_report_figures(report, out,
                                           mixing={'lo_hz': 260e6,
                                                   'rf_sources': [269e6, 800e6],
                                                   'orders': (1, 3, 5),
                                                   'rate_hz': 56e6})
            self.assertTrue(paths)
            for name in ('spur_slope', 'before_after', 'decision_flow',
                         'mixing_diagram'):
                self.assertIn(name, paths)
                data = Path(paths[name]).read_bytes()
                self.assertTrue(data.startswith(b'\x89PNG\r\n\x1a\n'), name)

    def test_temperature_figure(self):
        from soapypower import spur_visuals as sv
        rng = np.random.default_rng(9)
        t = np.arange(40, dtype=float) * 10.0
        temp = 40.0 + 2.0 * np.sin(t / 50.0) + rng.normal(0, 0.05, t.size)
        floor = -0.02 * (temp - 40.0) + rng.normal(0, 0.02, t.size)
        fit = {'status': 'fit', 'slope_db_per_c': -0.02, 'slope_sd_db': 0.002,
               'reference_temperature_c': float(temp.mean())}
        with tempfile.TemporaryDirectory() as directory:
            path = sv.plot_floor_vs_temperature(t, temp, floor, fit,
                                                Path(directory) / 'temp.png')
            self.assertTrue(Path(path).read_bytes().startswith(b'\x89PNG'))

    def test_drive_order_figure_uses_level_versus_drive(self):
        from soapypower import spur_visuals as sv
        twin = st.Twin(bins=1024, rate_hz=56e6, dof=20.0, seed=5)
        captures = []
        for drive in (0.0, -5.0, -10.0):
            for lo in (260e6, 262e6, 258e6):
                captures.append(sp.make_capture(
                    lo, twin.capture(lo, imd=((268e6, 35.0, 3),), drive_db=drive),
                    if_hz=twin.if_hz, rate_hz=twin.rate, dof=twin.dof,
                    drive_db=drive, capture_id='d{}-{}'.format(drive, lo)))
        report = sp.analyze_sweeps(captures, profile='b200')
        with tempfile.TemporaryDirectory() as directory:
            path = sv.plot_drive_order(report, Path(directory) / 'drive.png')
            self.assertTrue(Path(path).read_bytes().startswith(b'\x89PNG'))
            self.assertTrue(any(t['drive'].get('status') == 'measured'
                                for t in report['tracks']))


if __name__ == '__main__':
    unittest.main()
