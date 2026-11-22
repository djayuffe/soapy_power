import json, sys, tempfile, unittest
from pathlib import Path
import numpy as np

try:
    import matplotlib, scipy  # noqa
    HAVE = True
except Exception:
    HAVE = False
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))


@unittest.skipUnless(HAVE, 'needs matplotlib and scipy')
class AnalyzeHopscanTests(unittest.TestCase):
    def synthetic(self, path, sweeps=8, line_offset_bins=410):
        rng = np.random.default_rng(1); n = 8192; step = 244.140625; k = np.arange(n) - n // 2
        skirt = 8.5 * np.exp(-np.log(np.maximum(abs(k), 12) / 12) / 3.5)          # same artifact in every hop
        with open(path, 'w') as fh:
            t = 1_700_000_000.0
            for s in range(sweeps):
                for h, c in enumerate((140.75e6, 142.25e6, 143.75e6)):
                    p = -134.9 + skirt + rng.normal(0, 0.28, n)
                    if h == 1: p[n // 2 + line_offset_bins] += 2.0                   # a real line in one hop only
                    p[n // 2 - 10:n // 2 + 11] = np.nan
                    rec = dict(version=2, bin_width_hz=step, first_bin_center_hz=c - n // 2 * step, step_hz=step,
                               start_hz=c - n // 2 * step, stop_hz=c + n // 2 * step, samples=1007616, unit='dB(input power/Hz)',
                               time_start=f'2026-01-01T00:00:{(t % 60):06.3f}+00:00'.replace('00:00:', '00:00:'), time_stop=f'2026-01-01T00:00:{((t + .5) % 60):06.3f}+00:00',
                               power=[None if np.isnan(v) else float(v) for v in p],
                               metadata=dict(tuned_center_hz=c, measurement_status='complete', continuity='verified', sample_rate_hz=2e6,
                                             window='blackman', window_enbw_hz=421.57, overlap_samples=4096, segment_count=245,
                                             dc_mask_width_hz=5000.0, host_io=dict(read_latency_max_ms=5.0, rx_scheduling_gap_max_ms=3.0, read_latency_p95_ms=1.0)))
                    fh.write(json.dumps(rec) + '\n'); t += 0.6
                t += 3.0

    def test_report_files_and_injected_line_found_artifact_removed(self):
        import analyze_hopscan as ah
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / 'scan.jsonl'; out = Path(d) / 'out'; self.synthetic(src)
            sys_argv = sys.argv; sys.argv = ['x', str(src), str(out), '--tag', 't']
            try:
                self.assertEqual(ah.main(), 0)
            finally:
                sys.argv = sys_argv
            for name in ('t_analysis.pdf', 't_summary.json', 't_candidates.csv', 't_p1_summary.png', 't_waterfall.png',
                         't_waterfall_raw.png', 't_waterfall_zoom.png', 't_waterfall.html'):
                self.assertTrue((out / name).exists() and (out / name).stat().st_size > 0, name)
            summary = json.loads((out / 't_summary.json').read_text())
            self.assertEqual((summary['sweeps'], summary['hops']), (8, 3))
            line_mhz = 142.25 + 410 * 244.140625 / 1e6
            hits = [c for c in summary['candidates'] if abs(c['f_mhz'] - line_mhz) < 0.003]
            self.assertTrue(hits, summary['candidates'])
            self.assertGreater(hits[0]['peak_excess_db'], 1.0)
            # the skirt next to every hop centre must not be reported as a line
            self.assertFalse([c for c in summary['candidates'] if abs(c['offset_khz']) < 100 and c['hop'] != 1 or (abs(c['offset_khz']) < 12)])
            self.assertAlmostEqual(summary['sweep_to_sweep_sd_db'], 0.28, delta=0.05)

    def test_waterfall_levels_records_blanks_centres_and_quantises_losslessly_enough(self):
        import analyze_hopscan as ah, hopscan_waterfall as hw
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / 'scan.jsonl'; self.synthetic(src)
            lines = src.read_text().splitlines()                       # +3 dB gain step on one record (sweep 2, hop 0)
            r = json.loads(lines[6]); r['power'] = [None if v is None else v + 3.0 for v in r['power']]; lines[6] = json.dumps(r)
            src.write_text('\n'.join(lines) + '\n')
            A = ah.analyse(str(src), Path(d), 't'); W = hw.prepare(A)
            self.assertAlmostEqual(W['blk'][2, 0] - np.median(W['blk'][:, 0]), 3.0, delta=0.1)
            keep = W['keep']; self.assertLess(abs(np.nanmedian(W['R'][2, :keep])), 0.02)      # step removed from the picture
            self.assertTrue(np.isnan(W['R'][:, abs(W['off_khz']) < hw.CENTRE_KHZ]).all())   # hop centres blank, not filled
            self.assertLess(W['sig_e'], W['sig'])
            q, top = hw.quantize(W['R']); back = hw.dequantize(q, top); fin = np.isfinite(W['R'])
            self.assertTrue(np.array_equal(np.isfinite(back), fin))
            inside = fin & (W['R'] > hw.Q_LO) & (W['R'] < hw.Q_LO + hw.Q_LIN * hw.Q_STEP)
            self.assertLessEqual(np.max(abs(back[inside] - W['R'][inside])), hw.Q_STEP / 2 + 1e-9)


if __name__ == '__main__':
    unittest.main()
