from pathlib import Path
import sys
from unittest import mock

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))


def test_no_reference_never_uses_current_cache(tmp_path):
    import report_orbits as orbit
    out=tmp_path/'out';out.mkdir()
    with mock.patch.object(orbit,'plot'):
        result=orbit.generate(tmp_path,{},out)
    assert result['status']=='unavailable'
    assert 'current cache is never substituted' in result['reason']
    assert (out/'orbital-az-el.csv').exists()


def test_orbital_export_uses_recording_time_and_verifies_reference(tmp_path):
    import report_orbits as orbit
    import satdb
    from soapypower import satellites
    reference=tmp_path/'reference';reference.mkdir();out=tmp_path/'out';out.mkdir()
    track=dict(norad=42,name='Test',t=np.array([1000,1005,1010]),az=np.array([359.,0.,1.]),
        el=np.array([10.,30.,10.]),rr=np.array([-1000.,0.,1000.]),max_el=30.,tle_epoch_utc='test',transmitters=[dict(f_hz=145e6,mode='CW')])
    with mock.patch('report_bundle.verify') as verify,mock.patch.object(satellites,'observer',return_value={'min_elevation_deg':0}),mock.patch.object(satellites,'transmitters',return_value={42:{}}),mock.patch.object(satdb,'predict',return_value=[track]) as predict,mock.patch.object(orbit,'plot'):
        result=orbit.generate(tmp_path,dict(start_mhz=70,stop_mhz=6000,**{'features-on':dict(time_origin_unix=1000,duration_s=10)}),out)
    verify.assert_called_once_with(reference)
    assert predict.call_args.args[2:4]==(1000,1010)
    assert result['variants']['features-on']['tracks'][0]['times_s']==[0,5,10]
    assert result['variants']['features-on']['tracks'][0]['az_deg']==[359,0,1]
    assert 'not measured antenna pointing' in result['scope']


def test_bad_frozen_reference_does_not_fall_back(tmp_path):
    import report_orbits as orbit
    (tmp_path/'reference').mkdir()
    with mock.patch('report_bundle.verify',side_effect=ValueError('bad hash')):
        with pytest.raises(ValueError,match='bad hash'):
            orbit.generate(tmp_path,{},tmp_path,reference=tmp_path/'other')


def test_cli_unknown_options_are_rejected():
    import report_auto
    with pytest.raises(SystemExit):report_auto.parser().parse_args(['session','--typo'])


def test_explicit_unavailable_reference_never_substitutes_another_bundle(tmp_path):
    import report_orbits as orbit
    ref=tmp_path/'reference';ref.mkdir();(ref/'unavailable.json').write_text('{"status":"unavailable"}')
    with mock.patch.object(orbit,'plot'):
        result=orbit.generate(tmp_path,{},tmp_path,reference=tmp_path/'other')
    assert result['status']=='unavailable' and 'no substitute' in result['reason']


def test_spur_tracker_preserves_off_center_common_if_support():
    from soapypower.spur_pipeline import make_capture,_select_tracks
    axis=np.linspace(500000,1000000,501)
    captures=[make_capture(100e6+i*50000,np.full(501,1e-9),if_hz=axis,rate_hz=2e6) for i in range(3)]
    peaks=[[dict(if_hz=700000-i*50000,index=200-i*50,power_db=-60)] for i in range(3)]
    tracks=_select_tracks(captures,peaks,(1,),1,3)
    assert tracks and tracks[0]['hits']==3
    assert tracks[0]['law']==(1,1,'signal')
