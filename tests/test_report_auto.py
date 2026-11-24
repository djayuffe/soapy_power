import json, subprocess, sys
from pathlib import Path
import pytest

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='POSIX-only runner tooling: fcntl locks, symlink publication, process groups')

pytest.importorskip('matplotlib')


def test_report_auto_on_synthetic_waterfall(tmp_path):
    import numpy as np
    rng = np.random.default_rng(1)
    n = 2965; grid = (-134 + rng.normal(0, .3, (6, n))).round(2).tolist()
    grid_on = [[v for v in r] for r in grid]
    for r in grid_on[::2]:
        r[400:410] = [-110.0] * 10
    wf = {'cell_mhz': 2.0, 'start_mhz': 70.0, 'stop_mhz': 6000.0,
          'features-on': {'passes': 6, 't0_s': [0, 12, 24, 36, 48, 60], 'grid': grid_on},
          'features-off': {'passes': 3, 't0_s': [0, 36, 72], 'grid': grid[:3]}}
    (tmp_path / 'waterfall.json').write_text(json.dumps(wf))
    (tmp_path / 'compare.json').write_text(json.dumps({'agreement': {}, 'variants': {}}))
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, str(root / 'tools' / 'report_auto.py'), str(tmp_path)], capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr
    out = tmp_path / 'report'
    assert (out / 'wf3d_on_fullrange.png').exists() and (out / 'wf3d_off_70-300MHz.png').exists()
    assert (out / 'intermittent.png').exists() and 'error' not in (out / 'status.txt').read_text()
