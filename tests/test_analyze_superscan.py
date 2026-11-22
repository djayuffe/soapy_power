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


def synth(path, sweeps, signals, t_start=1.7e9):
    """signals: list of (f(t) callable, amplitude dB, active(t) callable); t is seconds since t_start"""
    rng = np.random.default_rng(7); k = np.arange(N) - N // 2
    skirt = 8.5 * np.exp(-np.log(np.maximum(abs(k), 12) / 12) / 3.5); t = t_start
    with open(path, 'w') as fh:
        for s in range(sweeps):
            for c in CENTERS:
                tm = t - t_start; lin = 10 ** ((-134.9 + skirt + rng.normal(0, 0.28, N)) / 10)
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
    def run_tool(self, signals, sweeps=90, t_start=1.7e9, extra=()):
        import analyze_superscan as an
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        src = Path(d.name) / 'superscan.jsonl'; synth(src, sweeps, signals, t_start)
        (Path(d.name) / 'bands.json').write_text(json.dumps(BANDS))
        out = Path(d.name) / 'out'; argv = sys.argv
        sys.argv = ['x', str(src), str(out), '--tag', 't'] + [x.replace('{d}', d.name) for x in extra]
        self._dir = Path(d.name)
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


def _cks(l):
    return str(sum(int(c) if c.isdigit() else (1 if c == '-' else 0) for c in l[:68]) % 10)


def _tle(norad, epoch_unix, inc, raan, mm, ma=0.0):
    e = datetime.datetime.fromtimestamp(epoch_unix, datetime.timezone.utc); doy = (e - datetime.datetime(e.year, 1, 1, tzinfo=datetime.timezone.utc)).total_seconds() / 86400 + 1
    l1 = f"1 {norad:05d}U 26001A   {e.year % 100:02d}{doy:012.8f}  .00000000  00000-0  00000-0 0  999"
    l2 = f"2 {norad:05d} {inc:8.4f} {raan:8.4f} 0001000   0.0000 {ma:8.4f} {mm:11.8f}    1"
    return l1 + _cks(l1), l2 + _cks(l2)


try:
    import sgp4  # noqa
    HAVE_SGP4 = True
except Exception:
    HAVE_SGP4 = False


@unittest.skipUnless(HAVE and HAVE_SGP4, 'needs matplotlib, scipy and sgp4')
class IdentificationTests(unittest.TestCase):
    run_tool = SuperscanTests.run_tool
    QTH = dict(lat_deg=63.3, lon_deg=10.3, alt_m=50)

    def test_pass_is_identified_by_its_predicted_doppler_and_silent_pass_is_listed(self):
        import satdb as sd
        epoch = datetime.datetime(2026, 11, 22, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
        l1, l2 = _tle(99001, epoch, 97.5, 40.0, 15.2)
        t = np.arange(epoch, epoch + 86400, 10.0); el = sd.topo(l1, l2, t, self.QTH)[0]
        tmax = float(t[int(np.nanargmax(np.where(el < 75, el, -90)))]); t_start = tmax - 240
        m1, m2 = _tle(99002, epoch, 97.5, 40.0, 15.2, ma=7.0)            # same plane, ~2 min behind: also visible, silent
        tt = np.arange(t_start - 120, t_start + 600, 2.0); rr = sd.topo(l1, l2, tt, self.QTH)[3]
        f_rx = lambda x: 437.2e6 * (1 - np.interp(x + t_start, tt, rr) / sd.C) + 1500.0      # 1.5 kHz transmitter offset
        d = tempfile.mkdtemp(); self.addCleanup(lambda: __import__('shutil').rmtree(d))
        db = Path(d) / 'satdb'; db.mkdir()
        (db / 'tle.json').write_text(json.dumps([dict(name='TESTSAT-1', norad=99001, l1=l1, l2=l2), dict(name='TESTSAT-2', norad=99002, l1=m1, l2=m2)]))
        (db / 'transmitters.json').write_text(json.dumps([
            dict(norad_cat_id=99001, downlink_low=437.2e6, mode='CW', baud=None, alive=True, status='active', description='beacon'),
            dict(norad_cat_id=99002, downlink_low=436.5e6, mode='GMSK', baud=9600, alive=True, status='active', description='telemetry')]))
        (db / 'satellites.json').write_text(json.dumps([dict(norad_cat_id=99001, name='TESTSAT-1', status='alive'), dict(norad_cat_id=99002, name='TESTSAT-2', status='alive')]))
        (db / 'meta.json').write_text(json.dumps(dict(fetched_utc='2026-11-22T00:00:00+00:00', tle_count=2)))
        (Path(d) / 'qth.json').write_text(json.dumps(self.QTH))
        out, s = self.run_tool([(f_rx, 14, lambda x: True)], sweeps=120, t_start=t_start,
                               extra=['--qth', str(Path(d) / 'qth.json'), '--satdb', str(db)])
        ids = s['identification']['ids']
        self.assertTrue(s['identification']['available'], s['identification'])
        hit = [i for i in ids if i['norad'] == 99001]
        self.assertTrue(hit, ids)
        self.assertEqual(hit[0]['confidence'], 'strong', hit[0])
        self.assertAlmostEqual(hit[0]['offset_hz'], 1500, delta=250)
        self.assertEqual(hit[0]['format_check'], 'consistent', hit[0])
        self.assertIn('carrier', hit[0]['observed_format'])
        self.assertIn(99002, [p['norad'] for p in s['identification']['not_heard']])
        self.assertFalse([i for i in ids if i['norad'] == 99002])
        for name in ('t_passes.csv', 't_p7_identification.png', 't_p8_passes.png'):
            self.assertTrue((out / name).exists(), name)

    def test_weak_steady_carrier_is_found_by_the_doppler_corrected_stack_only(self):
        import satdb as sd
        epoch = datetime.datetime(2026, 11, 22, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
        l1, l2 = _tle(99011, epoch, 97.5, 40.0, 15.2)
        t = np.arange(epoch, epoch + 86400, 10.0); el = sd.topo(l1, l2, t, self.QTH)[0]
        tmax = float(t[int(np.nanargmax(np.where(el < 75, el, -90)))]); t_start = tmax - 240
        tt = np.arange(t_start - 120, t_start + 600, 2.0); rr = sd.topo(l1, l2, tt, self.QTH)[3]
        f_rx = lambda x: 435.6e6 * (1 - np.interp(x + t_start, tt, rr) / sd.C) - 2200.0
        d = tempfile.mkdtemp(); self.addCleanup(lambda: __import__('shutil').rmtree(d))
        db = Path(d) / 'satdb'; db.mkdir()
        (db / 'tle.json').write_text(json.dumps([dict(name='FAINTSAT', norad=99011, l1=l1, l2=l2)]))
        (db / 'transmitters.json').write_text(json.dumps([dict(norad_cat_id=99011, downlink_low=435.6e6, mode='CW', alive=True, status='active', description='beacon')]))
        (db / 'satellites.json').write_text(json.dumps([dict(norad_cat_id=99011, name='FAINTSAT', status='alive')]))
        (db / 'meta.json').write_text(json.dumps(dict(fetched_utc='2026-11-22T00:00:00+00:00', tle_count=1)))
        (Path(d) / 'qth.json').write_text(json.dumps(self.QTH))
        out, s = self.run_tool([(f_rx, -9.5, lambda x: True)], sweeps=120, t_start=t_start, extra=['--qth', str(Path(d) / 'qth.json'), '--satdb', str(db)])
        I = s['identification']
        self.assertEqual(s['doppler_tracks'], 0)
        hit = [i for i in I['ids'] if i['norad'] == 99011]
        self.assertTrue(hit and hit[0]['kind'] == 'stacked', I['ids'])
        self.assertAlmostEqual(hit[0]['offset_hz'], -2200, delta=400)
        self.assertEqual(I['stack_control']['false_detections'], 0)

    def test_without_database_report_says_why(self):
        out, s = self.run_tool([(lambda t: 436.2e6, 6, lambda t: True)], sweeps=30)
        self.assertFalse(s['identification']['available'])
        self.assertIn('database', s['identification']['reason'])


if __name__ == '__main__':
    unittest.main()
