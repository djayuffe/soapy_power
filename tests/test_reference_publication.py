"""Known-truth timing, immutable-reference and failure-publication regressions."""
import datetime
import json
import sys
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pytest

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='POSIX-only runner tooling: fcntl locks, symlink publication, process groups')


def needs(*modules):
    """Skip a test whose optional analysis/report dependencies are not installed."""
    for name in modules:
        pytest.importorskip(name)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import satdb as sd
import superscan_id as sid
from report_bundle import bundle, prune, verify


def test_bundle_failed_build_preserves_published_generation(tmp_path):
    out=tmp_path/'report'
    with bundle(out) as stage: (stage/'plot.png').write_bytes(b'old')
    old=out.resolve()
    with pytest.raises(RuntimeError):
        with bundle(out) as stage:
            (stage/'plot.png').write_bytes(b'partial')
            raise RuntimeError('injected renderer failure')
    assert out.resolve()==old and (out/'plot.png').read_bytes()==b'old'
    verify(out)
    with bundle(out) as stage: (stage/'new.csv').write_text('new')
    assert not (out/'plot.png').exists() and (old/'plot.png').read_bytes()==b'old'
    verify(out)


def test_bundle_keeps_every_generation_until_pruned(tmp_path):
    out=tmp_path/'report';gens=tmp_path/'.report-generations'
    published=[]
    for n in range(4):
        with bundle(out) as stage:(stage/'n.txt').write_text(str(n))
        published.append(out.resolve())
    assert len(list(gens.glob('generation-*')))==4          # audit trail: nothing removed by publication
    (gens/'generation-orphan').mkdir()                     # left behind by a killed build
    removed=prune(out,keep=2)
    left=sorted(p.name for p in gens.iterdir())
    assert left==sorted([published[-1].name,published[-2].name]) and len(removed)==3
    assert (published[-2]/'n.txt').read_text()=='2' and (out/'n.txt').read_text()=='3'
    verify(out)
    with pytest.raises(ValueError):prune(out,keep=0)


def test_manifest_records_relative_inputs_and_withholds_observer_digest(tmp_path):
    session=tmp_path/'session';session.mkdir();raw=session/'scan.jsonl';raw.write_text('{}');qth=session/'qth.json';qth.write_text('{"lat_deg":1}')
    with bundle(session/'analysis',[raw,qth]) as stage:(stage/'x').write_text('1')
    inputs=json.loads((session/'analysis'/'bundle-manifest.json').read_text())['inputs']
    assert set(inputs)=={'scan.jsonl','qth.json'} and inputs['qth.json']=='withheld (private input)' and len(inputs['scan.jsonl'])==64
    assert str(tmp_path) not in (session/'analysis'/'bundle-manifest.json').read_text()
    verify(session/'analysis')


def test_prune_never_removes_archived_legacy_directory(tmp_path):
    out=tmp_path/'report';out.mkdir();(out/'legacy').write_text('kept')
    for n in range(3):
        with bundle(out) as stage:(stage/'n').write_text(str(n))
    prune(out,keep=1)
    assert [p.read_text() for p in (tmp_path/'.report-generations').glob('legacy-*/legacy')]==['kept']
    assert len(list((tmp_path/'.report-generations').glob('generation-*')))==1 and (out/'n').read_text()=='2'


def test_source_changed_during_render_refuses_publication(tmp_path):
    raw=tmp_path/'raw';raw.write_text('a');out=tmp_path/'report'
    with pytest.raises(RuntimeError,match='changed'):
        with bundle(out,[raw]) as stage:
            (stage/'plot').write_text('partial');raw.write_text('b')
    assert not out.exists()


def test_legacy_report_migration_retains_files_and_integrity_detects_changes(tmp_path):
    out=tmp_path/'report';out.mkdir();(out/'legacy').write_text('preserved')
    with bundle(out) as stage:(stage/'new').write_text('complete')
    assert out.is_symlink() and list((tmp_path/'.report-generations').glob('legacy-*/legacy'))
    (out/'new').write_text('corrupt')
    with pytest.raises(ValueError,match='integrity'):verify(out)


def test_reference_refresh_failure_cannot_mix_old_and_new_files(tmp_path):
    out=tmp_path/'satdb';out.mkdir();(out/'transmitters.json').write_text('["old"]')
    def get(url):
        if 'transmitters/' in url:return b'[{"new":true}]'
        raise OSError('offline')
    with patch.object(sd,'_get',get),pytest.raises(RuntimeError,match='incomplete'):
        sd.refresh(out)
    assert (out/'transmitters.json').read_text()=='["old"]'
    assert not (out/'meta.json').exists()


def test_reference_snapshot_copies_qth_and_pins_hashes(tmp_path):
    cache=tmp_path/'cache';cache.mkdir()
    for n in ('tle','satellites','transmitters'):(cache/(n+'.json')).write_text('[{"test":1}]')
    (cache/'meta.json').write_text('{}');q=tmp_path/'qth.json';q.write_text('{"lat_deg":63,"lon_deg":10}')
    out=tmp_path/'reference';sd.snapshot(cache,out,q)
    q.write_text('{"lat_deg":0,"lon_deg":0}')
    assert json.loads((out/'qth.json').read_text())['lat_deg']==63
    assert verify(out)['files']['qth.json']
    with pytest.raises(ValueError):sd.validate_qth({'lat_deg':91,'lon_deg':0})


def test_tle_age_gate_covers_entire_capture_and_alpha5():
    needs('sgp4')
    from test_analyze_superscan import _tle
    epoch=datetime.datetime(2026,11,22,tzinfo=datetime.timezone.utc).timestamp()
    a,b=_tle(99001,epoch,97.5,40.,15.2)
    assert sd.tle_eligibility(a,b,epoch,epoch+60)['eligible']
    for t in (epoch+15*86400,epoch-15*86400):assert not sd.tle_eligibility(a,b,t,t+60)['eligible']
    assert not sd.tle_eligibility(a,b,epoch,epoch+15*86400)['eligible']
    assert not sd.tle_eligibility('','',epoch,epoch+60)['eligible']
    assert sd.catalog_number('A0001')==100001 and sd.catalog_number('J0001')==180001


def make_multihop(other_delay=1000):
    rng=np.random.default_rng(41);f=100e6+np.arange(-20000,20250,250.)
    nr=45;rows=np.arange(nr);t=rows*10.+100.;T=np.tile(t[:,None],(1,len(f)))
    hop=(f>100e6).astype(int);T[:,hop==1]+=other_delay
    D=rng.normal(0,.07,T.shape)
    # A negative-frequency half-band beacon moves 7500 Hz during its pass.
    fp=100e6-12000+rows*175.
    idx=np.searchsorted(f,fp+500)
    D[rows,idx]+=1.5
    S=dict(f=f,fsrc=f,hop=hop,T=T,D=D,B=D)
    A=dict(t_ref=0.,bands=[dict(band=dict(lo_hz=f[0],hi_hz=f[-1]),S=S,lines=[],tracks=[])])
    pt=np.arange(0,2001.);pred=100e6-12000+(pt-100)/10*175.
    p=dict(t=pt,el=np.where(pt<=700,30.,-30.),rr=(1-pred/100e6)*sd.C)
    return A,p,dict(f_hz=100e6),t


def test_guided_uses_actual_source_hop_time_not_other_hops_delay():
    results=[]
    for delay in (600,1200):
        A,p,tx,t=make_multihop(delay);hits=sid.guided(A,p,tx)
        assert len(hits)==len(t)
        np.testing.assert_allclose([h[1] for h in hits],t)
        assert max(abs(h[3]-500) for h in hits)<=250
        results.append(hits)
    np.testing.assert_allclose(results[0],results[1])


def test_stacked_uses_actual_source_hop_times_and_excludes_missing_rows():
    A,p,tx,t=make_multihop();S=A['bands'][0]['S'];S['D'][3:5,:]=np.nan;S['T'][3:5,:]=np.nan
    r=sid.stacked(A,p,tx)
    assert r and r['timing_basis']=='source-hop midpoint'
    assert abs(r['offset_hz']-500)<=250
    assert r['rows']==len(t)-2 and r['t_first_s']==t[0] and r['t_last_s']==t[-1]
    assert r['doppler_span_hz']>7000 and sid.stack_hit(r,250)


def test_failed_auto_report_keeps_old_bundle_and_returns_failure(tmp_path):
    import report_auto as ra
    out=tmp_path/'report'
    with bundle(out) as stage:(stage/'old.png').write_text('old')
    with patch.object(sys,'argv',['report_auto.py',str(tmp_path)]),patch.object(ra,'_generate',return_value=1):
        assert ra.main()==1
    assert (out/'old.png').read_text()=='old'
    assert (tmp_path/'report-failure.txt').exists()


def test_nearest_native_rf_bins_do_not_clamp_or_round_up():
    c=dict(fsrc=np.array([100.,200.,400.]),step=100.,D=np.array([[10.,20.,30.]]))
    values=sid._sample_group(c,np.array([0,1,2]),np.array([0]),np.array([[110.,180.,300.,470.]]))
    np.testing.assert_allclose(values[0,:2],[10.,100.])
    assert np.isnan(values[0,2:]).all()


def test_checksum_mismatch_and_dead_orbit_are_ineligible():
    from test_analyze_superscan import _tle
    epoch=datetime.datetime(2026,11,22,tzinfo=datetime.timezone.utc).timestamp()
    a,b=_tle(99001,epoch,97.5,40.,15.2)
    bad=a[:68]+str((int(a[68])+1)%10)
    assert not sd.tle_eligibility(bad,b,epoch,epoch+60)['eligible']
    with patch.object(sd,'topo') as topo:
        assert sd.match_orbit(np.arange(epoch,epoch+60,10),np.full(6,137e6),[dict(l1=a,l2=b,sat_status='dead')],{},1)==[]
        topo.assert_not_called()


def test_report_pass_timeline_uses_capture_window_only(tmp_path):
    needs('matplotlib', 'scipy')
    import analyze_superscan as an
    from matplotlib.figure import Figure
    from unittest.mock import Mock
    p=dict(curve_t_s=[-60,0,5,10,70],curve_el=[80,20,30,40,85],heard=False,name='synthetic',max_el=40,transmitters=[])
    A=dict(id=dict(available=True,passes=[p]),bands=[],timing=dict(span_s=10))
    saved=[]
    with patch.object(Figure,'savefig',lambda fig,*a,**k:saved.append(fig)):
        an.id_pages(A,Mock(),tmp_path,'test')
    timeline=saved[-1].axes[0]
    assert timeline.get_xlim()==(0.,10.)
    np.testing.assert_array_equal(timeline.lines[0].get_xdata(),[0,5,10])


def test_long_report_heading_fits_inside_page():
    needs('matplotlib', 'scipy')
    import analyze_superscan as an
    import matplotlib.pyplot as plt
    fig=plt.figure(figsize=(16,9))
    text=an.fit_heading(fig,.04,.8,'A long band title with candidate counts '*12,fontsize=12.5)
    assert text.get_window_extent(fig.canvas.get_renderer()).x1 <= fig.bbox.x1*.981
    plt.close(fig)
