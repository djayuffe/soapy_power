import math, sys, unittest
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
try:
    import sgp4  # noqa
    HAVE_SGP4 = True
except Exception:
    HAVE_SGP4 = False
import satdb as sd
import satplan as sp

Q = dict(lat_deg=63.44, lon_deg=10.66, alt_m=40)            # high-latitude observer


class LookAngleTests(unittest.TestCase):
    def test_geostationary_look_angles_match_the_closed_form(self):
        el, az, _, _ = sd.look(np.array([42164e3, 0, 0]), Q)   # GEO at 0 E, west of the observer's meridian
        dl, phi = math.radians(0 - Q['lon_deg']), math.radians(Q['lat_deg'])
        az_ref = (180 - math.degrees(math.atan(math.tan(dl) / math.sin(phi)))) % 360
        c = math.cos(dl) * math.cos(phi); el_ref = math.degrees(math.atan((c - 6378.137 / 42164) / math.sqrt(1 - c * c)))
        self.assertAlmostEqual(az[0], az_ref, delta=0.05)       # 191.9 deg: south-south-west
        self.assertGreater(az[0], 180)
        self.assertAlmostEqual(el[0], el_ref, delta=0.15)       # ellipsoid vs sphere

    def test_cardinal_directions_and_zenith(self):
        for dlat, dlon, want in ((1, 0, 0), (-1, 0, 180), (0, 2, 90), (0, -2, 270)):
            el, az, _, _ = sd.look(sd.observer_ecef(Q['lat_deg'] + dlat, Q['lon_deg'] + dlon, 100e3), Q)
            self.assertLess(min(abs(az[0] - want), 360 - abs(az[0] - want)), 1.2, (dlat, dlon, az))
        el, _, _, _ = sd.look(sd.observer_ecef(Q['lat_deg'], Q['lon_deg'], 500e3), Q)
        self.assertAlmostEqual(el[0], 90.0, delta=1e-3)

    def test_gmst_at_j2000(self):
        self.assertAlmostEqual(float(np.degrees(sd._gmst(np.array([2451545.0])))[0]), 280.46061837, delta=1e-6)

    @unittest.skipUnless(HAVE_SGP4, 'needs sgp4')
    def test_range_rate_is_the_derivative_of_range(self):
        def cks(l): return str(sum(int(c) if c.isdigit() else (1 if c == '-' else 0) for c in l[:68]) % 10)
        l1 = "1 99001U 26001A   26326.50000000  .00000000  00000-0  00000-0 0  999"; l2 = "2 99001  97.5000  40.0000 0001000   0.0000   0.0000 15.20000000    1"
        l1, l2 = l1 + cks(l1), l2 + cks(l2)
        t = 1795320000.0 + np.arange(0, 86400, 2.0); el, az, rng, rr = sd.topo(l1, l2, t, Q)
        self.assertLess(np.nanmax(np.abs(rr - np.gradient(rng, t))[1:-1]), 2.0)
        self.assertLess(np.nanmax(np.abs(rr)), 7.7e3)


class PlanTests(unittest.TestCase):
    def test_every_preset_covers_its_bands_outside_the_dc_exclusion(self):
        for name, bands in sp.PRESETS.items():
            p = sp.plan(bands)
            for b in p['bands']:
                for f in np.arange(b['lo_hz'], b['hi_hz'] + 1, 5e3):
                    self.assertTrue(sp.covered(f, p['centers_hz']), (name, f))
                    self.assertTrue(all(abs(f - c) >= sp.DC or abs(f - c) > sp.USABLE for c in p['centers_hz']) or sp.covered(f, p['centers_hz']))

    def test_dithered_grid_sees_band_frequencies_from_two_los(self):
        import subprocess, tempfile, json as js
        with tempfile.TemporaryDirectory() as d:
            out = subprocess.run([sys.executable, str(Path(sp.__file__)), 'sat-full', d + '/b.json', '--rate', '8', '--dither', '300'],
                                 capture_output=True, text=True, check=True).stdout.strip()
            p = js.loads(Path(d + '/b.json').read_text())
        c = p['centers_hz']; u = 0.425 * 8e6
        self.assertEqual(p['dither_hz'], 300e3); self.assertEqual(len(out.split(',')), len(c))
        twice = once = 0
        for b in p['bands']:
            for f in np.arange(b['lo_hz'], b['hi_hz'] + 1, 5e3):
                k = sum(sp.DC <= abs(f - x) <= u for x in c); self.assertGreaterEqual(k, 2, f)
                twice += k >= 2; once += 1
        self.assertGreater(twice / once, 0.75)

    def test_format_bandwidths(self):
        self.assertEqual(sd.format_consistency(dict(mode='CW'), 1200.0, 244.14), 'consistent')
        self.assertEqual(sd.format_consistency(dict(mode='GMSK', baud=9600), 11e3, 244.14), 'consistent')
        self.assertTrue(sd.format_consistency(dict(mode='APT'), 2e3, 244.14).startswith('inconsistent'))
        self.assertIn('FSK-like', sd.observed_format(8e3, 0.3, 2, 4.8e3, 244.14))


if __name__ == '__main__':
    unittest.main()
