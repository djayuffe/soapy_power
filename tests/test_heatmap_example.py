"""Example plots preserve peak aggregation, missing cells and evidence gates."""
import json
from pathlib import Path
import runpy
import tempfile
import unittest

import numpy as np

aggregate = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                              'examples/ettus/heatmap.py'))['aggregate']

class HeatmapExampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'capture.jsonl'

    def record(self, seconds=0, values=None, status='complete'):
        return dict(version=1, start_hz=100e6, stop_hz=100e6+4, step_hz=1,
                    samples=8, unit='dB(input power/Hz)', power=values or [-10, -20, -30, -40],
                    masked_bins=[], zero_power_bins=[],
                    time_start='2026-11-15T00:00:{:02d}+00:00'.format(seconds),
                    time_stop='2026-11-15T00:00:{:02d}.010+00:00'.format(seconds),
                    metadata=dict(measurement_status=status, continuity='verified', tuned_center_hz=100e6+2,
                                  context=dict(agc=False, gains={'PGA': 20})))

    def write(self, records):
        self.path.write_text('\n'.join(json.dumps(r) for r in records)+'\n')

    def test_peak_reduction_is_not_db_average_and_missing_times_remain_blank(self):
        self.write([self.record(), self.record(values=[-5, -25, -35, -45]), self.record(seconds=2)])
        data, frequencies, times, summary = aggregate(self.path, 100e6, 100e6+4, bins=2, edge_fraction=0)
        np.testing.assert_array_equal(data[0], [-5, -30])
        self.assertTrue(np.isnan(data[1]).all())
        np.testing.assert_array_equal(data[2], [-10, -30])
        self.assertEqual(summary['records'], 3)
        self.assertEqual(len(times), data.shape[0]+1)
        self.assertTrue(np.all(np.diff(times)>0))

    def test_partial_capture_and_invalid_geometry_are_rejected(self):
        self.write([self.record(status='partial_deadline')])
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            aggregate(self.path, 100e6, 100e6+4)
        with self.assertRaisesRegex(ValueError, 'geometry'):
            aggregate(self.path, 100e6, 100e6, bins=2)

    def test_out_of_order_timestamps_are_rejected(self):
        self.write([self.record(seconds=2), self.record()])
        with self.assertRaisesRegex(ValueError, 'timestamps'):
            aggregate(self.path, 100e6, 100e6+4, bins=2)

    def test_weighted_mean_uses_linear_power_and_normalizes_taper(self):
        self.write([self.record(values=[-100]*4), self.record(values=[0]*4)])
        data, _, _, summary = aggregate(self.path, 100e6+1, 100e6+3, bins=2,
                                        edge_fraction=0, mode='weighted-mean')
        expected = 10*np.log10((1e-10+1)/2)
        np.testing.assert_allclose(data[0], [expected]*2, atol=1e-6)
        self.assertEqual(summary['mode'], 'weighted-mean')
        self.assertEqual(summary['observed_fraction'], 1)

    def test_dc_artifact_is_excluded_and_neighbor_hop_supplies_real_rf(self):
        first = self.record(values=[-30, -30, 100, -30])
        neighbor = self.record(values=[-30]*4)
        neighbor['metadata']['tuned_center_hz'] = 100e6+1
        self.write([first, neighbor])
        data, _, _, _ = aggregate(self.path, 100e6, 100e6+4, bins=4,
                                 edge_fraction=0, mode='weighted-mean', dc_mask_width_hz=1)
        self.assertAlmostEqual(float(data[0, 2]), -30, places=5)
        self.write([first])
        data, _, _, _ = aggregate(self.path, 100e6, 100e6+4, bins=4,
                                 edge_fraction=0, mode='peak', dc_mask_width_hz=1)
        self.assertTrue(np.isnan(data[0, 2]))

    def test_weighted_mean_does_not_overflow_at_extreme_db_values(self):
        for value in [4000, -4000]:
            self.write([self.record(values=[value]*4)])
            data, _, _, _ = aggregate(self.path, 100e6+1, 100e6+3, bins=2,
                                     edge_fraction=0, mode='weighted-mean')
            np.testing.assert_allclose(data[0], [value]*2, atol=1e-3)

    def test_weighted_mean_weights_actual_integration_duration(self):
        a, b = self.record(values=[0]*4), self.record(values=[10]*4)
        b['samples'] = 16
        self.write([a, b])
        data, _, _, _ = aggregate(self.path, 100e6+1, 100e6+3, bins=2,
                                 edge_fraction=0, mode='weighted-mean')
        np.testing.assert_allclose(data[0], [10*np.log10(7)]*2, atol=1e-6)

    def test_weighted_fusion_rejects_gain_changes_between_hops(self):
        a, b = self.record(), self.record()
        b['metadata']['context']['gains']['PGA'] = 10
        self.write([a, b])
        with self.assertRaisesRegex(ValueError, 'context changed'):
            aggregate(self.path, 100e6+1, 100e6+3, bins=2, mode='weighted-mean')

    def test_weighted_fusion_rejects_agc(self):
        r = self.record()
        r['metadata']['context']['agc'] = True
        self.write([r])
        with self.assertRaisesRegex(ValueError, 'fixed-gain'):
            aggregate(self.path, 100e6+1, 100e6+3, bins=2, mode='weighted-mean')

    def test_weighted_fusion_rejects_unverified_lo_dsp_offset_mapping(self):
        r = self.record()
        r['metadata']['context']['tune_args'] = {'OFFSET': '1000000'}
        self.write([r])
        with self.assertRaisesRegex(ValueError, 'LO/DSP mapping'):
            aggregate(self.path, 100e6+1, 100e6+3, bins=2, mode='weighted-mean')

    def test_center_mask_rejects_unverified_lo_dsp_offset_mapping(self):
        r = self.record()
        r['metadata']['context']['tune_args'] = {'OFFSET': '1000000'}
        self.write([r])
        with self.assertRaisesRegex(ValueError, 'LO/DSP mapping'):
            aggregate(self.path, 100e6, 100e6+4, bins=4, dc_mask_width_hz=1)
