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
                    metadata=dict(measurement_status=status, continuity='verified', tuned_center_hz=100e6+2))

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
