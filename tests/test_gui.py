"""Local GUI contracts; no SDR is opened by these tests."""
import importlib.util
import json
from pathlib import Path
import sys
import threading
import urllib.request
import urllib.error
import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('local_gui',ROOT/'soapypower/gui.py')
gui=importlib.util.module_from_spec(spec);spec.loader.exec_module(gui)

@pytest.mark.parametrize('payload',[{'duration_s':True},{'gain':float('nan')},{'gain':99},{'rate_msps':3},{'device':'rtl'},{'workflow':'shell'},{'antenna':'RX2; touch x'},{'duration_s':4},{'unexpected':'x'},{'workflow':'baseline','gain':'auto'},{'device':'bladerf'}])
def test_validation(payload):
    with pytest.raises(ValueError):gui.scan_arguments(payload)

def test_typed_arguments():
    args=gui.scan_arguments({'duration_s':600,'gain':'auto','rate_msps':2})
    assert args[args.index('--superscan-gain')+1]=='auto'
    assert args[args.index('--seconds')+1]=='600'
    assert '--superscan' in args
    assert '--bladerf-job' in gui.scan_arguments({'device':'bladerf','workflow':'fullrange'})

@pytest.fixture
def control(tmp_path):
    queued=[];c=gui.Control(tmp_path,tmp_path/'sessions',queue=lambda args:queued.append(args) or 'test-job')
    c.sessions.mkdir();c.queued=queued
    folder=c.sessions/'superscan-20261123-132204';folder.mkdir();(folder/'analysis').mkdir();(folder/'analysis/index.html').write_text('<h1>Report</h1>')
    (folder/'captures.json').write_text(json.dumps({'status':'completed','credited_seconds':2,'captures':[{'file':'capture-001.jsonl','records':2}]}))
    row={'power':[-100,None,-80],'first_bin_center_hz':100e6,'step_hz':100,'time_start':'2026-11-23T12:00:00+00:00','time_stop':'2026-11-23T12:00:01+00:00','metadata':{'sample_rate_hz':300,'context':{'gains':{'PGA':60}},'continuity':'verified'}}
    (folder/'capture-001.jsonl').write_text(json.dumps(row)+'\n{"partial":')
    return c,folder

def test_truthful_preview_and_stop(control):
    c,folder=control;d=c.detail(folder.name)
    assert d['session']['records']==2 and d['session']['credited_s']==2
    assert d['live']['power']==[-100,None,-80] and d['live']['temperature_c'] is None
    assert not d['can_stop'] and len(d['live']['history'])==1
    assert len(c.detail(folder.name)['live']['history'])==1
    with pytest.raises(ValueError):c.stop(folder.name)
    m={'status':'running','current_attempt':{'raw_file':'capture-001.jsonl'},'supports_stop_request':True}
    (folder/'captures.json').write_text(json.dumps(m));assert c.stop(folder.name)['state']=='stop_requested'
    assert c.stop(folder.name)['state']=='stop_requested'
    assert (folder/'stop-request.json').is_file()

@pytest.fixture
def http(control):
    c,folder=control;s=gui.Server(('127.0.0.1',0),c);t=threading.Thread(target=s.serve_forever,daemon=True);t.start()
    yield c,folder,'http://127.0.0.1:'+str(s.server_port)
    s.shutdown();s.server_close();t.join()

def request(url,data=None,headers=None):
    return urllib.request.urlopen(urllib.request.Request(url,data=json.dumps(data).encode() if data is not None else None,headers=headers or {}),timeout=5)

def test_http_assets_actions_and_auth(http):
    c,folder,url=http
    assert c.token in request(url).read().decode()
    assert request(url+'/app.js').status==200
    assert json.load(request(url+'/api/state'))['sessions'][0]['id']==folder.name
    with pytest.raises(urllib.error.HTTPError) as e:request(url+'/api/scan',{})
    assert e.value.code==403
    headers={'Content-Type':'application/json','X-CSRF-Token':c.token}
    with pytest.raises(urllib.error.HTTPError) as e:request(url+'/api/scan',{},dict(headers,Origin='https://external.invalid'))
    assert e.value.code==403 and not c.queued
    assert json.load(request(url+'/api/scan',{'duration_s':30},headers))['state']=='queued'
    assert c.queued[0][0]=='scan'
    with pytest.raises(urllib.error.HTTPError) as e:request(url+'/api/probe',{},headers)
    assert e.value.code==409 and len(c.queued)==1

def test_report_containment(http,tmp_path):
    c,folder,url=http
    prefix=url+'/files/'+folder.name+'/'
    assert request(prefix+'analysis/index.html').status==200
    (folder/'reference').mkdir();(folder/'reference/secret.json').write_text('{}')
    outside=tmp_path/'outside.txt';outside.write_text('private')
    (folder/'analysis/leak.txt').symlink_to(outside)
    for path in ['analysis/../reference/secret.json','analysis/leak.txt','analysis/%2e%2e/reference/secret.json','../superscan-20261123-132204/captures.json']:
        with pytest.raises(urllib.error.HTTPError) as e:request(prefix+path)
        assert e.value.code==404
    with pytest.raises(urllib.error.HTTPError) as e:request(url,headers={'Host':'evil.test'})
    assert e.value.code==403

def test_stop_preserves_accepted_prefix(tmp_path):
    spec=importlib.util.spec_from_file_location('gui_supervisor',ROOT/'tools/superscan_run.py');run=importlib.util.module_from_spec(spec);spec.loader.exec_module(run)
    def execute(args,log,raw,stall):
        row=dict(power=[-100]*16,step_hz=250,unit='dB(input power/Hz)',time_start='2026-11-23T12:00:00+00:00',time_stop='2026-11-23T12:00:00.5+00:00',metadata=dict(measurement_status='complete',continuity='verified',sample_rate_hz=4000,context=dict(gains=dict(PGA=60)),headroom=dict(accepted=True),rf_state=dict(after=dict(validity=dict(passed=True)))))
        raw.write_text(json.dumps(row)+'\n');log.write_text('');raise KeyboardInterrupt()
    assert run.supervise(tmp_path,['fake'],10,60,execute=execute)==130
    m=json.loads((tmp_path/'captures.json').read_text())
    assert m['status']=='cancelled' and m['credited_seconds']==.5 and m['captures'][0]['records']==1
    assert m['current_attempt'] is None

def test_stop_file_terminates_child(tmp_path):
    spec=importlib.util.spec_from_file_location('gui_supervisor',ROOT/'tools/superscan_run.py');run=importlib.util.module_from_spec(spec);spec.loader.exec_module(run)
    stop=tmp_path/'stop';stop.write_text('{}')
    with pytest.raises(KeyboardInterrupt):run.run_child([sys.executable,'-c','import time;time.sleep(30)'],tmp_path/'log',tmp_path/'data',60,cancel_file=stop)


def test_stale_lock_and_history_context(control):
    c,folder=control
    path=folder/'capture-001.jsonl';row=json.loads(path.read_text().splitlines()[0])
    row['metadata']['rf_state']={'after':{'telemetry':{'rx:lo_locked':{'value':True,'fresh':False}}}}
    path.write_text(json.dumps(row)+'\n')
    assert c.detail(folder.name)['live']['lo_locked'] is None
    row['metadata']['context']['gains']['PGA']=50
    row['time_stop']='2026-11-23T12:00:02+00:00'
    path.write_text(json.dumps(row)+'\n')
    assert len(c.detail(folder.name)['live']['history'])==1


def test_invalid_json_mapping_is_unknown(tmp_path):
    path=tmp_path/'status.json';path.write_text('[]')
    assert gui.json_file(path,{})=={}
