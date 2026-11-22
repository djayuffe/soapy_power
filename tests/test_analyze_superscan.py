import json, sys, tempfile, unittest, datetime
from pathlib import Path
import numpy as np

try:
    import matplotlib, scipy  # noqa
    HAVE = True
except Exception:
    HAVE = False
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
N, STEP = 8192, 244.140625
CENTERS = [136.8e6, 138.2e6, 145.5e6, 435.85e6, 436.6e6, 437.35e6]
BANDS = {"dc_exclude_hz": 100000, "usable_halfwidth_hz": 850000,
         "bands": [{"name": "VHF 137", "lo_hz": 137.0e6, "hi_hz": 138.0e6}, {"name": "VHF 145.9", "lo_hz": 145.8e6, "hi_hz": 146.0e6},
                   {"name": "UHF 435-438", "lo_hz": 435.0e6, "hi_hz": 438.0e6}]}


def synth(path, sweeps, signals):
    """signals: list of (f(t) callable, amplitude dB, active(t) callable)"""
    rng = np.random.default_rng(7); k = np.arange(N) - N // 2
    skirt = 8.5 * np.exp(-np.log(np.maximum(abs(k), 12) / 12) / 3.5); t = 1.7e9
    with open(path, 'w') as fh:
        for s in range(sweeps):
            for c in CENTERS:
                tm = t - 1.7e9; lin = 10 ** ((-134.9 + skirt + rng.normal(0, 0.28, N)) / 10)
                for f_of_t, amp, active in signals:
                    x = (f_of_t(tm) - (c - N // 2 * STEP)) / STEP
                    if active(tm) and 4 <= x < N - 4:
                        idx = np.arange(int(x) - 4, int(x) + 5)
                        lin[idx] += 10 ** ((-134.9 + amp) / 10) * np.exp(-0.5 * ((idx - x) / 1.2) ** 2)
                p = 10 * np.log10(lin); p[N // 2 - 10:N // 2 + 11] = np.nan
                ts = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
                fh.write(json.dumps(dict(version=2, bin_width_hz=STEP, first_bin_center_hz=c - N // 2 * STEP, step_hz=STEP, samples=1007616, unit='dB(input power/Hz)',
                                         time_start=ts.isoformat(), time_stop=(ts + datetime.timedelta(seconds=.5)).isoformat(),
                                         power=[None if np.isnan(v) else round(float(v), 3) for v in p],
                                         metadata=dict(tuned_center_hz=c, measurement_status='complete', continuity='verified', sample_rate_hz=2e6,
                                                       window='blackman', segment_count=245, host_io={}))) + '\n')
                t += 0.6
            t += 0.02


@unittest.skipUnless(HAVE, 'needs matplotlib and scipy')
class SuperscanTests(unittest.TestCase):
    def run_tool(self, signals, sweeps=90):
        import analyze_superscan as an
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        src = Path(d.name) / 'superscan.jsonl'; synth(src, sweeps, signals)
        (Path(d.name) / 'bands.json').write_text(json.dumps(BANDS))
        out = Path(d.name) / 'out'; argv = sys.argv
        sys.argv = ['x', str(src), str(out), '--tag', 't']
        try:
            self.assertEqual(an.main(), 0)
        finally:
            sys.argv = argv
        return out, json.loads((out / 't_summary.json').read_text())

    def test_leo_pass_is_tracked_fitted_and_stable_and_rising_lines_are_not_doppler(self):
        always = lambda t: True
        out, s = self.run_tool([(lambda t: 437.1e6 - 9e3 * np.tanh((t - 160) / 70), 12, always),        # UHF pass, tca 160 s
                                (lambda t: 137.62e6 - 3e3 * np.tanh((t - 120) / 60), 15, always),       # VHF pass, tca 120 s
                                (lambda t: 436.2e6, 6, always),                                          # stable carrier
                                (lambda t: 435.4e6 + 6.0 * t, 8, always)])                                # rising drift (not LEO)
        for name in ('t_analysis.pdf', 't_doppler.png', 't_tracks.csv', 't_lines.csv', 't_events.csv', 't_waterfall_uhf_435_438.html', 't_waterfall_vhf_137.png'):
            self.assertTrue((out / name).exists(), name)
        tr = [t for b in s['bands'] for t in b['tracks']]
        dop = sorted([t for t in tr if t['kind'] == 'doppler'], key=lambda t: t['f0_hz'])
        self.assertEqual(len(dop), 2, tr)
        vhf, uhf = dop
        self.assertAlmostEqual(vhf['f0_hz'], 137.62e6, delta=150); self.assertAlmostEqual(vhf['tca_s'], 120, delta=6)
        self.assertAlmostEqual(vhf['rate_peak_hz_s'], -50, delta=6)
        self.assertAlmostEqual(uhf['f0_hz'], 437.1e6, delta=250); self.assertAlmostEqual(uhf['tca_s'], 160, delta=6)
        self.assertAlmostEqual(uhf['rate_peak_hz_s'], -9e3 / 70, delta=12)
        self.assertTrue(any(t['kind'] == 'drift' and t['shift_hz'] > 0 for t in tr), tr)
        self.assertTrue(any(abs(l['f_mhz'] - 436.2) < 0.001 for b in s['bands'] for l in b['lines']))

    def test_quiet_band_gives_no_tracks_and_a_burst_is_an_event(self):
        out, s = self.run_tool([(lambda t: 145.9e6, 10, lambda t: 60 < t < 90)], sweeps=60)
        self.assertEqual(s['doppler_tracks'], 0)
        ev = [e for b in s['bands'] for e in b['events']]
        self.assertTrue(any(abs(e['f_mhz'] - 145.9) < 0.002 and 55 < e['t_start_s'] < 95 for e in ev), ev)


if __name__ == '__main__':
    unittest.main()
