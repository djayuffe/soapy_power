"""Standalone explorer packaging must reject mislabeled measurement bundles."""
import base64
from copy import deepcopy
import importlib.util
import io
from pathlib import Path
import runpy
import unittest

HERE = Path(__file__).resolve().parents[1]
build = runpy.run_path(str(HERE/'examples/ettus/build_explorer.py'))['build']


@unittest.skipUnless(importlib.util.find_spec('PIL'), 'Pillow is optional')
class ExplorerBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PIL import Image
        b = io.BytesIO()
        Image.new('RGB', (16384, 60), (0, 0, 0)).save(b, format='PNG')
        encoded = base64.b64encode(b.getvalue()).decode()
        cls.textures = {name: encoded for name in ('mean', 'clean', 'raw', 'rawCount', 'cleanCount', 'first', 'last', 'rawFirst', 'rawLast', 'offset', 'rawOffset')}
        cls.info = dict(meta=dict(rows=60, columns=16384, time_cell_seconds=30,
                                  start_hz=42000000, stop_hz=6008000000,
                                  raw_fft_spacing_hz=6835.9375, source_records=2,
                                  tooltip_density_quantization_db=.01,
                                  tooltip_host_bound_quantization_seconds=.1,
                                  first_capture_local='2026-11-15T00:00:00+01:00',
                                  last_capture_local='2026-11-15T00:30:00+01:00'),
                        stats={mode: dict(min=-150, max=-50) for mode in ('mean','clean','raw','delta')},
                        luts={name: [[i,i,i] for i in range(256)] for name in ('magma','viridis','cividis')})

    def test_standalone_output_contains_all_modules_and_escapes_payload(self):
        info = deepcopy(self.info)
        info['meta']['label'] = '</script><script>alert(1)</script>'
        html = build(info, self.textures)
        self.assertIn('HeatmapAnalysis.analyze', html)
        self.assertIn('rf-time-trace-', html)
        self.assertNotIn('__PAYLOAD__', html)
        self.assertNotIn('__EXPLORER_JS__', html)
        self.assertNotIn('__ANALYSIS_JS__', html)
        self.assertNotIn(info['meta']['label'], html)
        self.assertIn('\\u003c/script>', html)

    def test_grid_and_encoding_changes_are_rejected(self):
        for key, value in [('rows',30),('columns',8192),('time_cell_seconds',60),('tooltip_density_quantization_db',.1)]:
            info = deepcopy(self.info)
            info['meta'][key] = value
            with self.assertRaisesRegex(ValueError, 'geometry or encoding'):
                build(info, self.textures)

    def test_missing_texture_is_rejected(self):
        textures = dict(self.textures)
        del textures['cleanCount']
        with self.assertRaisesRegex(ValueError, 'Missing'):
            build(self.info, textures)

    def test_wrong_image_size_is_rejected(self):
        from PIL import Image
        b = io.BytesIO()
        Image.new('RGB', (10,10)).save(b, format='PNG')
        textures = dict(self.textures, mean=base64.b64encode(b.getvalue()).decode())
        with self.assertRaisesRegex(ValueError, 'texture'):
            build(self.info, textures)

    def test_invalid_scale_palette_and_timezone_are_rejected(self):
        info = deepcopy(self.info)
        info['stats']['mean']['max'] = -200
        with self.assertRaisesRegex(ValueError, 'limits'):
            build(info, self.textures)
        info = deepcopy(self.info)
        info['luts']['magma'][0][0] = 256
        with self.assertRaisesRegex(ValueError, 'palette'):
            build(info, self.textures)
        info = deepcopy(self.info)
        info['meta']['first_capture_local'] = '2026-11-15T00:00:00'
        with self.assertRaisesRegex(ValueError, 'timezone'):
            build(info, self.textures)
