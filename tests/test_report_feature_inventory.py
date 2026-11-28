import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))


def test_linear_pooling_honors_recorded_masks_and_zero_is_not_a_signal(tmp_path):
    from soapypower.report_spectrum import density
    from fullrange_waterfall import run
    row=dict(first_bin_center_hz=100e6,step_hz=1e5,coverage_start_hz=99e6,coverage_stop_hz=103e6,
             time_start='2026-11-28T00:00:00+00:00',metadata=dict(tuned_center_hz=101e6,sample_rate_hz=4e6),
             unit='dB(input power/Hz)',power=[-100,-80,-20],masked_bins=[2])
    f,p,k,c=density(row,1,0)
    assert k.tolist()==[True,True,False]
    source=tmp_path/'raw.jsonl';source.write_text(json.dumps(row)+'\n')
    grid,times=run(source,usable=1,dc_hz=0)
    expected=10*np.log10((10**-10+10**-8)/2)
    assert grid[0,15]==pytest.approx(expected)
    assert np.isnan(grid[0,14])


def test_inventory_processes_all_rows_and_categories_without_faking_phase(tmp_path):
    from soapypower.report_telemetry import generate
    source=tmp_path/'raw.jsonl'
    rows=[dict(time_start=f'2026-11-28T00:00:{i:02d}+00:00',metadata=dict(sensor=dict(temperature=20+i),
          flag=i%2==0,status='ok',unknown=None)) for i in range(10)]
    source.write_text(''.join(json.dumps(r)+'\n' for r in rows)+'{"partial":')
    result=generate([source],tmp_path/'report',max_points=4)
    assert result['records']==10 and result['trailing_fragments']==1
    assert result['fields']['metadata.sensor.temperature']['mean']==pytest.approx(24.5)
    assert result['fields']['metadata.unknown']['missing']==10
    assert result['fields']['metadata.status']['values']=={'ok':10}
    assert len((tmp_path/'report/telemetry-records.jsonl').read_text().splitlines())==10
    assert result['figures'] and 'cannot recover waveform phase' in result['phase']


def test_partial_narrow_waterfall_rows_are_not_discarded(tmp_path):
    from report_waterfalls3d import load
    path=tmp_path/'waterfall.json'
    path.write_text(json.dumps(dict(start_mhz=70,cell_mhz=2,
       **{'features-on':dict(t0_s=[0,10],grid=[[-130,None,None,None],[None,None,-120,None]])})))
    frequencies,data=load(path)
    assert data['features-on'][0].shape==(2,4)
