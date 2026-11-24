"""Partial transport evidence is not a failed full-acquisition headroom check."""
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
import pytest

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='POSIX-only runner tooling: fcntl locks, symlink publication, process groups')


def needs(*modules):
    """Skip a test whose optional analysis/report dependencies are not installed."""
    for name in modules:
        pytest.importorskip(name)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from analyze_scan_set import rejected_iq,evidence_findings


def evidence(tmp_path,z,*,reason='RuntimeError: SDR read failed or timed out: -1',context=None,integrity=None):
    session=tmp_path/'session';d=session/'rejected-main-1';d.mkdir(parents=True)
    p=d/'rejected-000000.npz';np.savez_compressed(p,iq=z)
    record=dict(iq_file=p.name,iq_sha256=hashlib.sha256(p.read_bytes()).hexdigest(),samples=len(z),context=context,integrity=integrity,reason=reason,acquisition_stage='measurement')
    (d/'rejected-000000.json').write_text(json.dumps(record))
    out=tmp_path/'out';out.mkdir()
    return session,out,p


def test_short_transport_prefix_with_null_integrity_and_missing_metrics(tmp_path):
    needs('matplotlib')
    s,out,p=evidence(tmp_path,np.array([.01+.02j,.02+.03j,.03+.04j],np.complex64),context=dict(sample_rate=2e6))
    digest=hashlib.sha256(p.read_bytes()).hexdigest();r=rejected_iq([s],out)[0]
    assert r['evidence_class']=='transport_failure' and r['recorded_metrics'] is None
    assert r['measurement_status'] is None and r['continuity'] is None
    assert r['metrics_origin']=='recomputed from retained samples only'
    assert r['metrics']['max_component']==pytest.approx(.04) and r['samples']==3
    assert 'prefix' in r['sample_scope'] and (out/r['plot']).stat().st_size>10000
    assert hashlib.sha256(p.read_bytes()).hexdigest()==digest


@pytest.mark.parametrize('z',[np.array([],np.complex64),np.array([complex(float('nan'),0)],np.complex64)])
def test_empty_or_nonfinite_prefix_is_reported_unavailable(tmp_path,z):
    needs('matplotlib')
    s,out,_=evidence(tmp_path,z)
    r=rejected_iq([s],out)[0]
    assert not r['usable_iq'] and r['metrics'] is None and r['sample_rate_hz'] is None
    assert r['above_05_samples'] is None and 'unavailable' in r['phase_scope']
    assert (out/r['plot']).exists()


def test_headroom_diagnostics_include_the_final_partial_block(tmp_path):
    needs('matplotlib')
    from matplotlib.axes import Axes
    from unittest.mock import patch
    z=np.full(8193,.01+.02j,np.complex64);z[-1]=.8
    s,out,_=evidence(tmp_path,z,reason='headroom_rejected',context=dict(sample_rate=2e6),integrity=dict(measurement_status='complete',continuity='verified'))
    seen=[];original=Axes.plot
    def spy(self,*args,**kw):
        if kw.get('label')=='Block maximum':seen.append(np.asarray(args[1]))
        return original(self,*args,**kw)
    with patch.object(Axes,'plot',spy):r=rejected_iq([s],out)[0]
    assert r['evidence_class']=='headroom_rejection' and r['above_05_sample_indices']==[8192]
    assert seen[0][-1]==pytest.approx(.8) and len(seen[0])==3


def test_valid_prefix_without_rate_uses_sample_index(tmp_path):
    needs('matplotlib')
    s,out,_=evidence(tmp_path,np.array([.01+.02j,.02+.03j],np.complex64))
    r=rejected_iq([s],out)[0]
    assert r['usable_iq'] and r['sample_rate_hz'] is None and r['metrics'] is not None


def test_hash_mismatch_still_refuses_evidence(tmp_path):
    needs('matplotlib')
    s,out,p=evidence(tmp_path,np.ones(4,np.complex64))
    p.write_bytes(p.read_bytes()+b'changed')
    with pytest.raises(ValueError,match='hash mismatch'):rejected_iq([s],out)


def test_run_findings_do_not_invent_headroom_or_sleep_causes():
    r=evidence_findings([dict(attempts=[dict(failure_class='transport'),dict(failure_class='finished')])],[dict(evidence_class='transport_failure')])
    assert '1 transport-failure attempts' in r[0] and '0 headroom' in r[0]
    assert 'does not establish host sleep' in r[0]


def test_complete_pdf_report_accepts_transport_evidence_without_metrics(tmp_path):
    needs('matplotlib', 'scipy', 'reportlab', 'pypdf')
    from test_analyze_superscan import synth,BANDS
    from analyze_scan_set import generate
    from report_bundle import verify
    s,out,_=evidence(tmp_path,np.array([],np.complex64))
    synth(s/'superscan.jsonl',12,[]);(s/'bands.json').write_text(json.dumps(BANDS))
    r=generate(tmp_path/'report',[s])
    assert r['records']==72 and r['rejected_iq'][0]['metrics_origin']=='unavailable'
    assert (tmp_path/'report/full-analysis.pdf').stat().st_size>10000
    verify(tmp_path/'report')
