"""Operator control fault injection with a fake receiver; no hardware access."""
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
import uuid

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('control_gui',ROOT/'soapypower/gui.py')
gui=importlib.util.module_from_spec(spec);spec.loader.exec_module(gui)


def read(path):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return {}


def wait_for(predicate, timeout=8):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        value=predicate()
        if value:return value
        time.sleep(.03)
    raise AssertionError('Timed out waiting for supervisor state')


def publish(path, value):
    temp=path.with_name('.request.tmp');temp.write_text(json.dumps(value));os.replace(temp,path)


def command(folder,action):
    r=dict(version=1,id=uuid.uuid4().hex,action=action)
    publish(folder/'control-request.json',r)
    return r['id']


@pytest.fixture
def receiver(tmp_path):
    fake=tmp_path/'receiver.py'
    fake.write_text('''import datetime,json,sys,time
from pathlib import Path
p=Path(sys.argv[sys.argv.index('-O')+1]);gain=float(sys.argv[sys.argv.index('-g')+1])
t=time.time()-.2
dt=lambda x:datetime.datetime.fromtimestamp(x,datetime.timezone.utc).isoformat()
r=dict(power=[-100]*16,step_hz=250,unit='dB(input power/Hz)',time_start=dt(t),time_stop=dt(t+.1),metadata=dict(measurement_status='complete',continuity='verified',sample_rate_hz=4000,context=dict(gains=dict(PGA=gain)),headroom=dict(accepted=True),rf_state=dict(after=dict(validity=dict(passed=True)))))
p.write_text(json.dumps(r)+'\\n')
time.sleep(30)
''')
    folder=tmp_path/'superscan-20261123-120000'
    script="import sys;sys.path.insert(0,sys.argv[1]);import superscan_run as r;sys.exit(r.supervise(sys.argv[2],[sys.executable,sys.argv[3]],10,60,max_attempts=1,backoff_s=0))"
    proc=subprocess.Popen([sys.executable,'-c',script,str(ROOT/'tools'),str(folder),str(fake)])
    wait_for(lambda:(folder/'attempt-001.jsonl').is_file())
    yield folder,proc
    if proc.poll() is None:
        (folder/'stop-request.json').write_text('{}')
        try:proc.wait(timeout=8)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()


def test_pause_resume_retry_abort_preserves_partitions_and_budget(receiver):
    folder,proc=receiver;mfile=folder/'captures.json'
    pause=command(folder,'pause')
    m=wait_for(lambda: (lambda m:m if m.get('status')=='paused' and m.get('control_ack')==pause else None)(read(mfile)))
    assert m['current_attempt'] is None and m['credited_seconds']==pytest.approx(.1,abs=1e-5)
    accepted=(folder/'capture-001.jsonl').read_bytes();time.sleep(.3)
    assert read(mfile)['credited_seconds']==m['credited_seconds']
    assert not (folder/'attempt-002.jsonl').exists()
    resume=command(folder,'resume')
    wait_for(lambda:(folder/'attempt-002.jsonl').is_file())
    retry=command(folder,'retry')
    wait_for(lambda:(folder/'attempt-003.jsonl').is_file())
    (folder/'stop-request.json').write_text('{}')
    assert proc.wait(timeout=8)==130
    m=read(mfile)
    assert m['status']=='cancelled' and m['current_attempt'] is None
    assert m['credited_seconds']==pytest.approx(.3,abs=1e-5)
    assert m['remaining_seconds']==pytest.approx(9.7,abs=1e-5)
    assert m['paused_seconds']>=.3
    assert [x['action'] for x in m['controls']]==['pause','resume','retry','abort']
    assert [x['failure_class'] for x in m['attempts']]==['paused','manual_retry','cancelled']
    assert len(m['captures'])==3 and (folder/'capture-001.jsonl').read_bytes()==accepted
    assert m['control_ack']==retry


def test_abort_while_paused_does_not_open_another_stream(receiver):
    folder,proc=receiver
    command(folder,'pause');wait_for(lambda:read(folder/'captures.json').get('status')=='paused')
    (folder/'stop-request.json').write_text('{}');assert proc.wait(timeout=8)==130
    assert not (folder/'attempt-002.jsonl').exists()
    assert len(read(folder/'captures.json')['captures'])==1


def test_gui_controls_pending_and_abort_priority(tmp_path):
    folder=tmp_path/'superscan-20261123-120000';folder.mkdir()
    c=gui.Control(tmp_path,tmp_path,queue=lambda _: 'fake')
    m=dict(status='running',control_version=1,supports_stop_request=True,control_ack=None)
    publish(folder/'captures.json',m)
    r=c.operate(folder.name,'pause');assert r['state']=='pause_requested'
    assert c.detail(folder.name)['pending_control']=='pause'
    with pytest.raises(RuntimeError):c.operate(folder.name,'retry')
    assert c.operate(folder.name,'abort')['state']=='stop_requested'
    with pytest.raises(RuntimeError):c.operate(folder.name,'retry')
    assert read(folder/'control-request.json')['action']=='pause'
    (folder/'stop-request.json').unlink();(folder/'control-request.json').unlink()
    with pytest.raises(ValueError):c.operate(folder.name,'resume')


def test_retry_uses_typed_request_in_fresh_job(tmp_path):
    folder=tmp_path/'superscan-20261123-120000';folder.mkdir();queued=[]
    c=gui.Control(tmp_path,tmp_path,queue=lambda args:queued.append(args) or 'new-job')
    publish(folder/'captures.json',dict(status='cancelled',credited_seconds=3))
    original=dict(mode='scan',scan_seconds=600,gain_db=60,antenna='RX2',input='antenna',superscan=1,superscan_gain='60',superscan_rate='2',superscan_plan='sat-full',superscan_dither='300')
    (folder/'request.plist').write_bytes(plistlib.dumps(original))
    assert c.detail(folder.name)['can_retry']
    assert c.retry(folder.name)['job_id']=='new-job'
    args=queued[0];assert args[args.index('--seconds')+1]=='600'
    assert args[args.index('--superscan-rate')+1]=='2'
    assert read(folder/'captures.json')['credited_seconds']==3
    original['plan_args']='--unsafe-option';(folder/'request.plist').write_bytes(plistlib.dumps(original))
    assert c.retry_settings(folder) is None
    with pytest.raises(ValueError):c.retry(folder.name)


def test_legacy_request_recovery_requires_matching_status(tmp_path):
    folder=tmp_path/'superscan-20261123-120000';folder.mkdir();taken=tmp_path/'queue/taken';taken.mkdir(parents=True);status=tmp_path/'status';status.mkdir()
    (taken/'scan-123-456.json').write_bytes(plistlib.dumps(dict(mode='scan',superscan=1,scan_seconds=30)))
    publish(status/'scan-123.json',dict(id='scan-123',mode='scan',result='SESSION='+str(folder)+';RESULT=failed;'))
    c=gui.Control(tmp_path,tmp_path,queue=lambda _: 'new')
    assert c.retry_settings(folder)['rate_msps']==8
    publish(status/'scan-123.json',dict(id='scan-123',mode='scan',result='SESSION='+str(folder)+'-other;'))
    assert c.retry_settings(folder) is None
