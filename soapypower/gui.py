"""Local receive-control GUI. Run: python -m soapypower.gui --repo CHECKOUT --sessions SESSIONS.

The browser queues typed requests through the existing macOS runner. It never
executes browser-supplied commands and never opens an SDR merely to view results.
"""
import argparse
import datetime
import json
import math
import mimetypes
import os
import plistlib
import uuid
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit
import webbrowser

NAME = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
ASSETS = Path(__file__).with_name('web')


def json_file(path, default=None):
    try:
        if path.stat().st_size > 12*1024*1024: return default
        value=json.loads(path.read_text())
        return value if isinstance(value,dict) else default
    except (OSError, ValueError): return default


def tail(path, size=16000):
    try:
        with path.open('rb') as f:
            f.seek(max(0,path.stat().st_size-size)); return f.read().decode('utf-8','replace')
    except OSError: return ''


def record_at_end(path):
    try:
        with path.open('rb') as f:
            size=path.stat().st_size;f.seek(max(0,size-2*1024*1024));lines=f.read().splitlines()
        for line in reversed(lines):
            try:
                r=json.loads(line)
                if isinstance(r,dict) and 'power' in r:return r
            except (ValueError,UnicodeDecodeError):pass
    except OSError:pass
    return None


def number(value, low, high, label, integer=False):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high or (integer and int(value)!=value):
        raise ValueError('{} must be {}–{}'.format(label,low,high))
    return int(value) if integer else value


def scan_arguments(payload):
    allowed={'duration_s','device','workflow','antenna','input','rate_msps','gain','plan','dither_khz','bladerf_job'}
    if not isinstance(payload,dict) or set(payload)-allowed:raise ValueError('Unknown scan fields')
    device=payload.get('device','b200');workflow=payload.get('workflow','satellite')
    antenna=payload.get('antenna','RX2');load=payload.get('input','antenna')
    if device not in ('b200','bladerf') or workflow not in ('satellite','hopscan','fullrange','baseline','suite'):raise ValueError('Unsupported device or workflow')
    if antenna not in ('RX2','TX/RX') or load not in ('antenna','terminated'):raise ValueError('Invalid RF input')
    duration=number(payload.get('duration_s',1800),5,3600,'Duration',True)
    gain=payload.get('gain',60)
    if gain!='auto':gain=number(gain,0,76 if workflow=='satellite' else 70,'Gain')
    if device=='bladerf' and workflow!='fullrange':raise ValueError('bladeRF supports its survey workflow')
    if gain=='auto' and workflow!='satellite':raise ValueError('Automatic gain selection requires satellite scanning')
    args=['scan','--seconds',str(duration),'--device',device,'--antenna',antenna,'--input',load,'--gain',str(min(gain,70) if gain!='auto' else 20)]
    if device=='bladerf':
        job=payload.get('bladerf_job','dense-dc+survey')
        if job not in ('dense-dc','survey','survey-anchor','dense-dc+survey'):raise ValueError('Invalid bladeRF job')
        return args+['--bladerf-job',job,'--procedure','fullrange']
    if workflow=='satellite':
        rate=number(payload.get('rate_msps',2),2,8,'Sample rate',True)
        if rate not in (2,4,8):raise ValueError('Sample rate must be 2, 4 or 8 MS/s')
        plan=payload.get('plan','sat-full')
        if plan not in ('sat','sat-full','sat-s'):raise ValueError('Invalid satellite plan')
        dither=number(payload.get('dither_khz',300),0,9999,'LO dither',True)
        args+=['--superscan','1','--superscan-plan',plan,'--superscan-rate',str(rate),'--superscan-dither',str(dither),'--superscan-gain',str(gain)]
    elif workflow=='hopscan':args+=['--hopscan','1']
    else:args+=['--procedure',workflow]
    return args


class Control:
    def __init__(self, repo, sessions, queue=None):
        self.repo=Path(repo).resolve();self.sessions=Path(sessions).resolve();self.queue_override=queue
        self.token=secrets.token_urlsafe(32);self.lock=threading.Lock();self.last_queue=0.;self.history={};self.health=(0,False);self.report_cache={}

    def ready(self):
        if self.queue_override:return True
        if time.monotonic()-self.health[0]<15:return self.health[1]
        ok=False
        if sys.platform=='darwin' and (self.repo/'tools/macos_runner/queue.sh').is_file():
            try:ok=subprocess.run(['launchctl','print','gui/{}/no.djayuffe.soapy-power-runner'.format(os.getuid())],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=2).returncode==0
            except (OSError,subprocess.TimeoutExpired):pass
        self.health=(time.monotonic(),ok);return ok

    def session(self, name):
        if not NAME.fullmatch(name):raise ValueError('Invalid session name')
        p=self.sessions/name
        if p.resolve().parent!=self.sessions or not p.is_dir():raise ValueError('Session not found')
        return p

    def child(self, folder, name):
        p=folder/str(name)
        return p if p.resolve().parent==folder.resolve() else None

    def report_link(self, folder):
        for rel in ('analysis/index.html','report/index.html','analysis/superscan_analysis.pdf','analysis/hopscan_analysis.pdf','report/report.pdf'):
            p=folder/rel
            if p.is_file() and p.resolve().is_relative_to(folder.resolve()):return '/files/{}/{}'.format(quote(folder.name),rel)
        return None

    def building(self, folder):
        try:
            import fcntl
            with (folder/'.analysis.lock').open('r') as f:
                try:fcntl.flock(f,fcntl.LOCK_SH|fcntl.LOCK_NB)
                except BlockingIOError:return True
                fcntl.flock(f,fcntl.LOCK_UN)
        except (OSError,ImportError):pass
        return False

    def item(self, folder):
        capture=json_file(folder/'captures.json',{}) or {}
        report={}
        for filename in ('analysis/summary.json','analysis/superscan_summary.json'):
            path=folder/filename
            try:
                signature=(path.stat().st_mtime_ns,path.stat().st_size)
                with self.lock:
                    cached=self.report_cache.get(str(path))
                    if cached is None or cached[0]!=signature:
                        cached=(signature,json_file(path,{}) or {});self.report_cache[str(path)]=cached
                report=cached[1]
                if report:break
            except OSError:pass
        cfg=json_file(folder/'bands.json',{}) or {};link=self.report_link(folder)
        status=capture.get('status','saved')
        if self.building(folder):status='reporting'
        elif (folder/'analysis-failure.txt').is_file():status='report_failed'
        return dict(id=folder.name,status=status,requested_s=capture.get('requested_seconds'),credited_s=capture.get('credited_seconds',report.get('credited_scan_s')),records=report.get('records',sum(x.get('records',0) for x in capture.get('captures',[]))) or None,attempts=len(capture.get('attempts',[])),recoveries=sum(x.get('failure_class')=='transport' for x in capture.get('attempts',[])),report_url=link,rate_hz=cfg.get('sample_rate_hz',report.get('sample_rate_hz')),centers=len(cfg.get('centers_hz',[])) or None,modified=folder.stat().st_mtime)

    def catalog(self):
        try:folders=[p for p in self.sessions.iterdir() if p.is_dir() and NAME.fullmatch(p.name) and p.resolve().parent==self.sessions and p.name.startswith(('superscan-','hopscan-','fullrange-','baseline-','bladerf-','suite-','okt-'))]
        except OSError:return []
        return [self.item(p) for p in sorted(folders,key=lambda x:x.stat().st_mtime,reverse=True)[:60]]

    def jobs(self):
        try:files=sorted((self.sessions/'status').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:30]
        except OSError:return []
        return [x for x in (json_file(p) for p in files) if isinstance(x,dict)]

    def busy(self):
        if any((self.sessions/'queue').glob('*.json')):return True
        for job in self.jobs():
            if job.get('state')!='running' or not isinstance(job.get('pid'),int):continue
            try:
                r=subprocess.run(['ps','-p',str(job['pid']),'-o','command='],capture_output=True,text=True,timeout=2)
                if re.search(r'/(runner|run_session)\.sh(?:\s|$)',r.stdout):return True
            except (OSError,subprocess.TimeoutExpired):pass
        return False

    def enqueue(self, args):
        with self.lock:
            if not self.ready():raise ValueError('The macOS scan runner is not loaded. Install tools/macos_runner/install.sh first.')
            if self.busy() or time.monotonic()-self.last_queue<3:raise RuntimeError('A job is already queued or running')
            if self.queue_override:ident=self.queue_override(args)
            else:
                r=subprocess.run(['bash',str(self.repo/'tools/macos_runner/queue.sh')]+args,env={**os.environ,'SPR_SESSIONS':str(self.sessions)},capture_output=True,text=True,timeout=10)
                if r.returncode:raise ValueError(r.stderr.strip() or 'Request validation failed')
                ident=r.stdout.strip().splitlines()[0]
            self.last_queue=time.monotonic();return {'job_id':ident,'state':'queued'}

    def retry_settings(self, folder):
        """Reconstruct only typed, supported settings; never replay shell arguments."""
        paths=[folder/'request.plist']
        for job in self.jobs():
            if job.get('mode')!='scan' or not isinstance(job.get('result'),str) or 'SESSION='+str(folder)+';' not in job['result']:continue
            ident=job.get('id','')
            if not isinstance(ident,str) or not NAME.fullmatch(ident):continue
            paths.extend(sorted((self.sessions/'queue/taken').glob(ident+'-*.json')))
        for path in paths:
            try:
                root=folder if path.name=='request.plist' else self.sessions/'queue/taken'
                if not path.resolve().is_relative_to(root.resolve()) or path.stat().st_size>16384:continue
                r=plistlib.loads(path.read_bytes())
                allowed={'mode','scan_seconds','gain_db','antenna','input','procedure','device',
                         'bladerf_job','superscan','hopscan','superscan_gain','superscan_rate',
                         'superscan_dither','superscan_plan','reanalyze','plan_args','overflow_probe','satdb_refresh'}
                if not isinstance(r,dict) or set(r)-allowed or r.get('mode')!='scan':continue
                if any(r.get(k) for k in ('reanalyze','plan_args','overflow_probe','satdb_refresh')):continue
                workflow='satellite' if r.get('superscan')==1 else 'hopscan' if r.get('hopscan')==1 else r.get('procedure','suite')
                payload=dict(duration_s=r.get('scan_seconds',30), device=r.get('device','b200'),
                             workflow=workflow,antenna=r.get('antenna','RX2'),input=r.get('input','antenna'),
                             gain=r.get('superscan_gain','auto') if workflow=='satellite' else r.get('gain_db',20),
                             rate_msps=int(r.get('superscan_rate',8)),plan=r.get('superscan_plan','sat-full'),
                             dither_khz=int(r.get('superscan_dither',0)),bladerf_job=r.get('bladerf_job','dense-dc+survey'))
                if isinstance(payload['gain'],bool):continue
                if payload['gain']!='auto':payload['gain']=float(payload['gain'])
                scan_arguments(payload)
                return payload
            except (OSError,ValueError,TypeError,OverflowError,plistlib.InvalidFileException):continue
        return None

    def operate(self, name, action):
        folder=self.session(name)
        with self.lock:
            m=json_file(folder/'captures.json',{}) or {};status=m.get('status')
            if status not in ('running','paused'):raise ValueError('No active supervised capture in this session')
            if action=='abort':
                if m.get('supports_stop_request') is not True:raise ValueError('Supervisor does not support abort')
                path=folder/'stop-request.json'
                try:
                    with path.open('x') as f:json.dump({'version':1,'action':'stop'},f)
                except FileExistsError:pass
                return {'state':'stop_requested'}
            if m.get('control_version')!=1:raise ValueError('This supervisor does not support pause/resume/retry')
            if (folder/'stop-request.json').exists():raise RuntimeError('Abort is already pending')
            request=json_file(folder/'control-request.json',{}) or {}
            if request.get('id') and request['id']!=m.get('control_ack'):raise RuntimeError('Waiting for supervisor acknowledgement')
            if action=='pause' and status!='running':raise ValueError('Capture is already paused')
            if action=='resume' and status!='paused':raise ValueError('Capture is not paused')
            if action not in ('pause','resume','retry'):raise ValueError('Unknown capture action')
            value=dict(version=1,id=uuid.uuid4().hex,action=action)
            path=folder/'control-request.json';temp=path.with_name('.control-'+value['id']+'.tmp')
            with temp.open('x') as f:
                json.dump(value,f);f.flush();os.fsync(f.fileno())
            os.replace(temp,path)
            return {'state':action+'_requested','request_id':value['id']}

    def stop(self, name):
        return self.operate(name,'abort')

    def retry(self, name):
        folder=self.session(name);m=json_file(folder/'captures.json',{}) or {}
        if m.get('status') in ('running','paused'):return self.operate(name,'retry')
        if self.building(folder):raise RuntimeError('Wait for report generation to finish')
        payload=self.retry_settings(folder)
        if payload is None:raise ValueError('Original supported scan settings are unavailable; configure a new scan')
        return self.enqueue(scan_arguments(payload))

    def detail(self, name):
        folder=self.session(name);item=self.item(folder);m=json_file(folder/'captures.json',{}) or {};active=m.get('current_attempt')
        filename=(active or {}).get('raw_file')
        if not filename and m.get('captures'):filename=m['captures'][-1]['file']
        path=self.child(folder,filename) if filename else folder/'superscan.jsonl'
        row=record_at_end(path) if path else None;live=None
        if row:
            meta=row.get('metadata',{});rf=(meta.get('rf_state') or {}).get('after') or {};hr=meta.get('headroom') or {};ctx=meta.get('context') or {};power=row.get('power',[]);stride=max(1,math.ceil(len(power)/600));values=[]
            for i in range(0,len(power),stride):
                block=power[i:i+stride];valid=[x for x in block if isinstance(x,(int,float)) and math.isfinite(x)];values.append(max(valid) if len(valid)==len(block) and valid else None)
            gains=ctx.get('gains',{});live=dict(start=row.get('time_start'),stop=row.get('time_stop'),center_hz=meta.get('tuned_center_hz'),rate_hz=meta.get('sample_rate_hz'),bins=len(power),first_hz=row.get('first_bin_center_hz'),step_hz=row.get('step_hz',0)*stride,power=values,gain=gains,headroom=hr,temperature_c=(rf.get('validity') or {}).get('temperature_c'),rf_valid=(rf.get('validity') or {}).get('passed'),lo_locked=(((rf.get('telemetry') or {}).get('rx:lo_locked') or {}).get('value') if ((rf.get('telemetry') or {}).get('rx:lo_locked') or {}).get('fresh') is True else None),continuity=meta.get('continuity'),hardware=ctx.get('hardware'),unit=row.get('unit'))
            try:
                first=None
                if active and path:
                    with path.open() as f:first=json.loads(f.readline())
                span=(datetime.datetime.fromisoformat(row['time_stop'])-datetime.datetime.fromisoformat(first['time_start'])).total_seconds() if first else 0
                item['live_estimated_s']=item.get('credited_s',0)+max(0,span)
            except (ValueError,KeyError,OSError,TypeError):pass
            signature=(live['rate_hz'],live['bins'],json.dumps(gains,sort_keys=True));now=live['stop']
            with self.lock:
                state=self.history.setdefault(name,dict(signature=signature,rows=[]))
                if state['signature']!=signature:state.update(signature=signature,rows=[])
                if not state['rows'] or state['rows'][-1]['stop']!=now:
                    state['rows'].append({k:live[k] for k in ('start','stop','center_hz','power')});state['rows']=state['rows'][-60:]
                history=list(state['rows'])
            live['history']=history
        logfile=(active or {}).get('log_file')
        if not logfile and m.get('attempts'):logfile=m['attempts'][-1].get('log_file')
        logpath=self.child(folder,logfile) if logfile else folder/'scan.log'
        running=m.get('status') in ('running','paused')
        request=json_file(folder/'control-request.json',{}) or {}
        pending=request.get('action') if running and request.get('id')!=m.get('control_ack') else None
        modern=running and m.get('control_version')==1
        retry_config=None if running else self.retry_settings(folder)
        return dict(session=item,live=live,can_stop=running and m.get('supports_stop_request') is True,
                    can_pause=modern and m.get('status')=='running',can_resume=modern and m.get('status')=='paused',
                    can_retry=modern or (retry_config is not None and not self.building(folder)),
                    retry_config=retry_config,pending_control=pending,controls=m.get('controls',[]),
                    paused_seconds=m.get('paused_seconds'),attempts=m.get('attempts',[]),
                    log=tail(logpath) if logpath else '',report_error=tail(folder/'analysis-failure.txt',3000),
                    stop_requested=(folder/'stop-request.json').exists())


class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,control):self.control=control;super().__init__(address,Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def send(self, code, body, kind='application/json; charset=utf-8', extra=None):
        if isinstance(body,(dict,list)):body=json.dumps(body,allow_nan=False).encode()
        if isinstance(body,str):body=body.encode()
        self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
        for k,v in (extra or {}).items():self.send_header(k,v)
        self.end_headers();self.wfile.write(body)

    def trusted(self):
        port=self.server.server_port
        hosts={'127.0.0.1:'+str(port),'localhost:'+str(port)}
        return self.headers.get('Host') in hosts

    def do_GET(self):
        if not self.trusted():self.send(403,{'error':'Invalid local Host'});return
        url=urlsplit(self.path);control=self.server.control
        try:
            if url.path=='/':
                body=(ASSETS/'index.html').read_text().replace('__TOKEN__',control.token)
                self.send(200,body,'text/html; charset=utf-8',{'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"})
            elif url.path in ('/app.js','/style.css'):
                p=ASSETS/url.path[1:];self.send(200,p.read_bytes(),'text/javascript' if p.suffix=='.js' else 'text/css')
            elif url.path=='/api/state':self.send(200,dict(sessions=control.catalog(),jobs=control.jobs(),runner_ready=control.ready(),busy=control.busy(),platform=sys.platform))
            elif url.path=='/api/session':self.send(200,control.detail(parse_qs(url.query).get('id',[''])[0]))
            elif url.path.startswith('/files/'):
                parts=unquote(url.path).split('/');folder=control.session(parts[2]);rel=Path(*parts[3:]);p=(folder/rel).resolve()
                if not parts[3:] or parts[3] not in ('analysis','report') or not (folder/parts[3]).resolve().is_relative_to(folder.resolve()) or not p.is_relative_to((folder/parts[3]).resolve()) or p.suffix.lower() not in ('.html','.pdf','.png','.json','.csv','.txt') or not p.is_file():raise ValueError('Report file not found')
                kind=mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
                extra={'Content-Security-Policy':"sandbox allow-scripts allow-downloads; default-src 'self' data: blob:; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'"} if p.suffix=='.html' else {}
                self.send(200,p.read_bytes(),kind,extra)
            else:self.send(404,{'error':'Not found'})
        except (ValueError,IndexError,FileNotFoundError) as e:self.send(404,{'error':str(e)})
        except (OSError,TypeError) as e:self.send(500,{'error':str(e)})

    def do_POST(self):
        c=self.server.control;origin=self.headers.get('Origin');expected='http://'+self.headers.get('Host','')
        if not self.trusted() or (origin and origin!=expected) or self.headers.get('X-CSRF-Token')!=c.token:
            self.send(403,{'error':'Invalid local request token or origin'});return
        try:
            n=int(self.headers.get('Content-Length','0'))
            if not 0<n<=16384 or self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('Expected a small JSON request')
            data=json.loads(self.rfile.read(n))
            if not isinstance(data,dict):raise ValueError('Expected an object')
            if self.path=='/api/scan':result=c.enqueue(scan_arguments(data))
            elif self.path=='/api/probe':
                if set(data)-{'device'} or data.get('device','b200') not in ('b200','bladerf'):raise ValueError('Invalid probe')
                result=c.enqueue(['probe','--device',data.get('device','b200')])
            elif self.path in ('/api/report','/api/stop','/api/abort','/api/pause','/api/resume','/api/retry'):
                if set(data)!={'session'}:raise ValueError('Expected session only')
                folder=c.session(data['session'])
                if self.path=='/api/report':
                    if not re.fullmatch(r'(superscan|hopscan)-\d{8}-\d{6}',folder.name):raise ValueError('This session does not support queued reanalysis')
                    if (json_file(folder/'captures.json',{}) or {}).get('status') in ('running','paused'):raise RuntimeError('Finish or abort acquisition before generating a report')
                    result=c.enqueue(['scan','--reanalyze',folder.name])
                elif self.path=='/api/retry':result=c.retry(folder.name)
                else:result=c.operate(folder.name,'abort' if self.path in ('/api/stop','/api/abort') else self.path.rsplit('/',1)[-1])
            else:self.send(404,{'error':'Unknown action'});return
            self.send(200,result)
        except (ValueError,KeyError,TypeError) as e:self.send(400,{'error':str(e)})
        except RuntimeError as e:self.send(409,{'error':str(e)})
        except (OSError,subprocess.SubprocessError) as e:self.send(500,{'error':str(e)})


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--repo',default=os.environ.get('SPR_REPO',str(Path(__file__).resolve().parents[1])));p.add_argument('--sessions',default=os.environ.get('SPR_SESSIONS'));p.add_argument('--port',type=int,default=8765);p.add_argument('--no-browser',action='store_true');a=p.parse_args()
    if not 0<=a.port<=65535:p.error('port must be 0–65535')
    control=Control(a.repo,a.sessions or str(Path(a.repo)/'sessions'));server=Server(('127.0.0.1',a.port),control);url='http://127.0.0.1:'+str(server.server_port)
    print('soapy_power GUI: '+url,flush=True)
    if not a.no_browser:webbrowser.open(url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
    return 0

if __name__=='__main__':sys.exit(main())
