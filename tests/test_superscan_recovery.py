"""Fault injection for unattended RX supervision, without SDR hardware."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import superscan_run as run
from report_bundle import bundle,verify


def record(t,gain=60):
    dt=lambda v:datetime.datetime.fromtimestamp(v,datetime.timezone.utc).isoformat()
    return dict(power=[-100]*16,step_hz=250.,unit='dB(input power/Hz)',time_start=dt(t),time_stop=dt(t+.5),metadata=dict(measurement_status='complete',continuity='verified',sample_rate_hz=4000,context=dict(gains=dict(PGA=gain)),headroom=dict(accepted=True),rf_state=dict(after=dict(validity=dict(passed=True)))))


def write(path,rows,fragment=b''):
    path.write_bytes(b''.join(json.dumps(r).encode()+b'\n' for r in rows)+fragment)


def test_coverage_excludes_outages_and_keeps_crash_fragment(tmp_path):
    raw=tmp_path/'raw';write(raw,[record(0),record(.7),record(100)],b'{"partial":')
    r=run.accepted_prefix(raw,tmp_path/'accepted',60)
    assert r['records']==3 and r['trailing_fragment']
    assert r['credited_seconds']==pytest.approx(1.7) and r['excluded_gap_seconds']==pytest.approx(98.8)
    assert raw.read_bytes().endswith(b'{"partial":')


@pytest.mark.parametrize('fault',['gain','context','status','headroom','overlap','nan'])
def test_unqualified_record_cannot_consume_budget(tmp_path,fault):
    a,b=record(0),record(1)
    if fault=='gain':b['metadata']['context']['gains']['PGA']=70
    if fault=='context':b['metadata']['context']['clock_source']='external'
    if fault=='status':b['metadata']['measurement_status']='partial'
    if fault=='headroom':b['metadata']['headroom']['accepted']=False
    if fault=='overlap':b['time_start']=a['time_start']
    if fault=='nan':b['time_stop']='invalid'
    raw=tmp_path/'raw';write(raw,[a,b])
    with pytest.raises(ValueError):run.accepted_prefix(raw,tmp_path/'accepted',60)


def executor(outcomes):
    calls=[]
    def execute(args,log,raw,stall):
        gain=float(args[args.index('-g')+1]);code,text,t,n=outcomes[len(calls)];calls.append(gain)
        write(raw,[record(t+i*.7,gain) for i in range(n)]);log.write_text(text)
        return code,False
    return execute,calls


def test_transport_abort_reopens_without_counting_downtime(tmp_path):
    execute,calls=executor([(-6,'uhd::usb_error USBError -4 LIBUSB_ERROR_NO_DEVICE',0,1),(0,'',1000,2)])
    assert run.supervise(tmp_path,['fake'],1.5,60,execute=execute,sleep=lambda _:None)==0
    m=json.loads((tmp_path/'captures.json').read_text())
    assert len(m['captures'])==2 and m['credited_seconds']==pytest.approx(1.7)
    assert calls==[60,60] and m['attempts'][0]['failure_class']=='transport'


def test_unrelated_native_abort_is_not_retried_or_reported_success(tmp_path):
    execute,calls=executor([(-6,'unrelated native memory assertion',0,2)])
    assert run.supervise(tmp_path,['fake'],.5,60,execute=execute,sleep=lambda _:None)!=0
    assert len(calls)==1 and json.loads((tmp_path/'captures.json').read_text())['status']=='incomplete'


def test_repeated_device_absence_is_bounded(tmp_path):
    execute,calls=executor([(2,'No UHD Devices Found',0,0)]*8)
    assert run.supervise(tmp_path,['fake'],10,60,transport_retries=2,execute=execute,sleep=lambda _:None)!=0
    assert len(calls)==3


def test_headroom_backoff_preserves_raw_and_gain_partitions(tmp_path):
    calls=[]
    def execute(args,log,raw,stall):
        gain=float(args[args.index('-g')+1]);calls.append(gain);write(raw,[record(len(calls)*100,gain)])
        if len(calls)==1:
            p=Path(args[args.index('--rejection-evidence')+1]);p.mkdir();(p/'iq.npz').write_bytes(b'raw evidence')
            (p/'meta.json').write_text(json.dumps(dict(reason='headroom_rejected')))
            (p/'index.json').write_text(json.dumps(dict(records=[dict(iq_file='iq.npz',iq_sha256=hashlib.sha256(b'raw evidence').hexdigest(),metadata_file='meta.json')])))
            log.write_text('headroom rejected; original IQ retained');return 2,False
        log.write_text('');return 0,False
    assert run.supervise(tmp_path,['fake'],1.,60,execute=execute,sleep=lambda _:None)==0
    m=json.loads((tmp_path/'captures.json').read_text());assert calls==[60,54] and [c['gain_db'] for c in m['captures']]==[60,54]
    assert (tmp_path/'attempt-001.jsonl').is_file() and (tmp_path/'rejected-main-1/iq.npz').is_file()


def test_missing_rejected_iq_fails_closed(tmp_path):
    execute,_=executor([(2,'headroom rejected; original IQ retained',0,1)])
    with pytest.raises(FileNotFoundError):run.supervise(tmp_path,['fake'],10,60,execute=execute,sleep=lambda _:None)
    assert json.loads((tmp_path/'captures.json').read_text())['status']=='failed'


def test_watchdog_terminates_stalled_process(tmp_path):
    before=time.monotonic()
    code,stalled=run.run_child([sys.executable,'-c','import time;time.sleep(30)'],tmp_path/'log',tmp_path/'data',.2,poll_s=.05)
    assert stalled and code!=0 and time.monotonic()-before<8


def test_real_subprocess_native_abort_is_recovered(tmp_path):
    script=tmp_path/'device.py'
    script.write_text('''import sys,json,os,resource
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
a=sys.argv;gain=float(a[a.index('-g')+1]);path=a[a.index('-O')+1]
base=0 if 'attempt-001' in path else 1000
'''+"template="+repr(record(0))+'''\nimport datetime
with open(path,'w') as f:
 for i in range(2):
  r=dict(template);r['metadata']=dict(template['metadata']);r['metadata']['context']={'gains':{'PGA':gain}}
  r['time_start']=datetime.datetime.fromtimestamp(base+i*.7,datetime.timezone.utc).isoformat();r['time_stop']=datetime.datetime.fromtimestamp(base+i*.7+.5,datetime.timezone.utc).isoformat();f.write(json.dumps(r)+'\\n');f.flush()
if base==0:
 print('uhd::usb_error: LIBUSB_ERROR_NO_DEVICE',file=sys.stderr,flush=True);os.abort()
''')
    assert run.supervise(tmp_path/'run',[sys.executable,str(script)],2.,60,backoff_s=0)==0
    m=json.loads((tmp_path/'run/captures.json').read_text());assert len(m['attempts'])==2 and m['attempts'][0]['exit']==-signal.SIGABRT


def test_nested_report_bundles_verify(tmp_path):
    with bundle(tmp_path/'report') as outer:
        with bundle(outer/'part') as inner:(inner/'test').write_text('nested')
    verify(tmp_path/'report');verify(tmp_path/'report/part')


def test_tall_identification_rows_paginate_without_loss():
    from analyze_superscan import identification_chunks
    rows=[[str(i),'a wrapped candidate confidence statement '*6] for i in range(30)]
    chunks=list(identification_chunks(rows,[4,20]))
    assert len(chunks)>3 and sum(map(len,chunks))==30
    for chunk in chunks:assert .035+sum(.035+.024*(max(x.count('\n')+1 for x in row)-1) for row in chunk)<=.900001


def test_scan_set_rejects_duplicate_or_escaped_capture_paths(tmp_path):
    from analyze_scan_set import sources
    (tmp_path/'captures.json').write_text(json.dumps(dict(captures=[dict(file='../outside.jsonl')])))
    with pytest.raises(ValueError):sources(tmp_path)


def test_scan_set_full_report_on_known_truth_capture(tmp_path):
    from test_analyze_superscan import synth,BANDS
    from analyze_scan_set import generate
    session=tmp_path/'session';session.mkdir();synth(session/'superscan.jsonl',12,[])
    (session/'bands.json').write_text(json.dumps(BANDS))
    report=generate(tmp_path/'report',[session])
    assert report['records']==72 and len(report['parts'])==1
    assert (tmp_path/'report/full-analysis.pdf').stat().st_size>10000
    assert len((tmp_path/'report/telemetry.csv').read_text().splitlines())==73
    verify(tmp_path/'report')


def test_cli_cancellation_marks_manifest_and_terminates_child(tmp_path):
    import subprocess
    device=tmp_path/'device.py';device.write_text("import time,sys\nfrom pathlib import Path\np=Path(sys.argv[sys.argv.index('-O')+1]);p.write_text('')\ntime.sleep(30)\n")
    child=subprocess.Popen([sys.executable,str(Path(run.__file__)),str(tmp_path/'run'),'--seconds','10','--gain','60','--',sys.executable,str(device)])
    try:
        deadline=time.monotonic()+8
        while not (tmp_path/'run/attempt-001.jsonl').exists() and time.monotonic()<deadline:time.sleep(.05)
        assert (tmp_path/'run/attempt-001.jsonl').exists()
        child.terminate();assert child.wait(timeout=8)==130
        assert json.loads((tmp_path/'run/captures.json').read_text())['status']=='cancelled'
    finally:
        if child.poll() is None:child.kill();child.wait()


def test_partial_final_sweep_is_not_labelled_complete(tmp_path):
    from test_analyze_superscan import synth
    from analyze_superscan import load
    p=tmp_path/'scan.jsonl';synth(p,2,[]);lines=p.read_text().splitlines();p.write_text('\n'.join(lines[:-1])+'\n')
    info=load(p)[-1]
    assert info['sweeps']==2 and info['complete_sweeps']==1 and info['partial_sweeps']==1
