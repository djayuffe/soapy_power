"""Local receive-control GUI. Run: python -m soapypower.gui --repo CHECKOUT --sessions SESSIONS.

The browser queues typed requests through the existing macOS runner. It never
executes browser-supplied commands and never opens an SDR merely to view results.
"""

import argparse
import hashlib
import datetime
import json
import math
import logging
from logging.handlers import RotatingFileHandler
import signal
import socket
from collections import OrderedDict
from contextlib import nullcontext
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
LOG = logging.getLogger('soapypower.gui')


def mapping(value):
    return value if isinstance(value, dict) else {}


def objects(value):
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def clean_json(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: clean_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_json(v) for v in value]
    return value


def json_file(path, default=None):
    try:
        if path.stat().st_size > 12 * 1024 * 1024:
            return default
        with path.open('rb') as stream:
            raw = stream.read(12 * 1024 * 1024 + 1)
        if len(raw) > 12 * 1024 * 1024:
            return default
        value = json.loads(raw)
        return value if isinstance(value, dict) else default
    except (OSError, ValueError, RecursionError):
        return default


def tail(path, size=16000):
    try:
        with path.open('rb') as f:
            f.seek(max(0, path.stat().st_size - size))
            return f.read(size).decode('utf-8', 'replace')
    except OSError:
        return ''


def record_at_end(path):
    try:
        with path.open('rb') as f:
            size = path.stat().st_size
            f.seek(max(0, size - 2 * 1024 * 1024))
            raw = f.read(2 * 1024 * 1024)
            lines = raw.splitlines()
            if raw and not raw.endswith(b'\n'):
                lines = lines[:-1]
        for line in reversed(lines):
            try:
                r = json.loads(line)
                if isinstance(r, dict) and isinstance(r.get('power'), list) and isinstance(r.get('metadata', {}), dict):
                    return r
            except (ValueError, UnicodeDecodeError, RecursionError):
                pass
    except OSError:
        pass
    return None


def number(value, low, high, label, integer=False):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low <= value <= high
        or (integer and int(value) != value)
    ):
        raise ValueError('{} must be {}–{}'.format(label, low, high))
    return int(value) if integer else value


def scan_arguments(payload):
    allowed = {
        'duration_s',
        'device',
        'workflow',
        'antenna',
        'input',
        'rate_msps',
        'gain',
        'plan',
        'dither_khz',
        'bladerf_job',
    }
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError('Unknown scan fields')
    device = payload.get('device', 'b200')
    workflow = payload.get('workflow', 'satellite')
    antenna = payload.get('antenna', 'RX2')
    load = payload.get('input', 'antenna')
    if device not in ('b200', 'bladerf') or workflow not in ('satellite', 'hopscan', 'fullrange', 'baseline', 'suite'):
        raise ValueError('Unsupported device or workflow')
    if antenna not in ('RX2', 'TX/RX') or load not in ('antenna', 'terminated'):
        raise ValueError('Invalid RF input')
    duration = number(payload.get('duration_s', 1800), 5, 3600, 'Duration', True)
    gain = payload.get('gain', 60)
    if gain != 'auto':
        gain = number(gain, 0, 76 if workflow == 'satellite' else 70, 'Gain')
    if device == 'bladerf' and workflow != 'fullrange':
        raise ValueError('bladeRF supports its survey workflow')
    if gain == 'auto' and workflow != 'satellite':
        raise ValueError('Automatic gain selection requires satellite scanning')
    args = [
        'scan',
        '--seconds',
        str(duration),
        '--device',
        device,
        '--antenna',
        antenna,
        '--input',
        load,
        '--gain',
        str(min(gain, 70) if gain != 'auto' else 20),
    ]
    if device == 'bladerf':
        job = payload.get('bladerf_job', 'dense-dc+survey')
        if job not in ('dense-dc', 'survey', 'survey-anchor', 'dense-dc+survey'):
            raise ValueError('Invalid bladeRF job')
        return args + ['--bladerf-job', job, '--procedure', 'fullrange']
    if workflow == 'satellite':
        rate = number(payload.get('rate_msps', 2), 2, 8, 'Sample rate', True)
        if rate not in (2, 4, 8):
            raise ValueError('Sample rate must be 2, 4 or 8 MS/s')
        plan = payload.get('plan', 'sat-full')
        if plan not in ('sat', 'sat-full', 'sat-s'):
            raise ValueError('Invalid satellite plan')
        dither = number(payload.get('dither_khz', 300), 0, 9999, 'LO dither', True)
        args += [
            '--superscan',
            '1',
            '--superscan-plan',
            plan,
            '--superscan-rate',
            str(rate),
            '--superscan-dither',
            str(dither),
            '--superscan-gain',
            str(gain),
        ]
    elif workflow == 'hopscan':
        args += ['--hopscan', '1']
    else:
        args += ['--procedure', workflow]
    return args


class Control:
    def __init__(self, repo, sessions, queue=None):
        self.repo = Path(repo).resolve()
        self.sessions = Path(sessions).resolve()
        self.queue_override = queue
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.action_lock = threading.Lock()
        self.last_queue = 0.0
        self.history = OrderedDict()
        self.health = (0, False)
        self.ready_reason = 'Runner not checked'
        self.report_cache = OrderedDict()
        self.responses = OrderedDict()
        self.snapshot_lock = threading.Lock()
        self.snapshot_cache = (0, None)
        self.instance_id = uuid.uuid4().hex
        from soapypower.satellites import SatelliteService

        self.satellites = SatelliteService(self.repo, self.sessions)

    def satellite_view(self, query):
        from soapypower.satellites import instant

        low = number(float(query.get('lo_mhz', ['100'])[0]), 1, 10000, 'Satellite frequency minimum') * 1e6
        high = number(float(query.get('hi_mhz', ['6000'])[0]), 1, 10000, 'Satellite frequency maximum') * 1e6
        if high <= low:
            raise ValueError('Satellite frequency range must be ascending')
        seconds = number(float(query.get('seconds', ['7200'])[0]), 60, 7200, 'Prediction horizon', True)
        name = query.get('session', [None])[0] or None
        frame_time = None
        if name:
            detail = self.detail(name)
            stop = mapping(detail.get('live')).get('stop')
            if stop:
                frame_time = instant(stop)
                start = mapping(detail.get('live')).get('start')
                if start:
                    first = instant(start)
                    if first <= frame_time:
                        frame_time = (first + frame_time) / 2
        value = self.satellites.snapshot(
            dict(session=name, lo_hz=low, hi_hz=high, seconds=seconds, frame_time=frame_time)
        )
        value['refresh'] = self.satellites.refresh_status()
        return value

    def satellite_observer(self, data):
        from soapypower.gui_jobs import publish
        from soapypower.gui_service import file_lock

        if set(data) != {'lat_deg', 'lon_deg', 'alt_m', 'min_elevation_deg'}:
            raise ValueError('Expected WGS84 latitude, longitude, altitude and elevation mask')
        for key, low, high in [
            ('lat_deg', -90, 90),
            ('lon_deg', -180, 180),
            ('alt_m', -500, 10000),
            ('min_elevation_deg', 0, 90),
        ]:
            number(data[key], low, high, key)
        with file_lock(self.sessions / '.observer.lock'):
            path = self.sessions / 'qth.json'
            if path.is_symlink():
                raise ValueError('Observer file must be local, not a symlink')
            previous = json_file(path, {}) or {}
            if path.exists():
                history = self.sessions / '.observer-history'
                if history.is_symlink():
                    raise ValueError('Observer history cannot be a symlink')
                history.mkdir(mode=0o700, exist_ok=True)
                history.chmod(0o700)
                publish(history / (uuid.uuid4().hex + '.json'), previous)
            mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
            temporary = path.with_name('.observer-' + uuid.uuid4().hex)
            try:
                with temporary.open('x', encoding='utf-8') as stream:
                    temporary.chmod(mode)
                    json.dump(dict(previous, **data), stream, allow_nan=False)
                    stream.write('\n')
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return dict(state='saved', scope='Local observer; coordinates are never sent in satellite API requests')

    def ready(self):
        if self.queue_override:
            return True
        if time.monotonic() - self.health[0] < 15:
            return self.health[1]
        ok = False
        self.ready_reason = 'A macOS launch agent is required for acquisition'
        if sys.platform == 'darwin' and (self.repo / 'tools/macos_runner/queue.sh').is_file():
            try:
                result = subprocess.run(
                    ['launchctl', 'print', 'gui/{}/no.djayuffe.soapy-power-runner'.format(os.getuid())],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if result.returncode == 0:
                    paths = {
                        key: re.search(r'(?m)^\s*' + key + r' => (.+)$', result.stdout)
                        for key in ('SPR_REPO', 'SPR_SESSIONS')
                    }
                    ok = (
                        all(paths.values())
                        and Path(paths['SPR_REPO'].group(1).strip().strip('"')).resolve() == self.repo
                        and Path(paths['SPR_SESSIONS'].group(1).strip().strip('"')).resolve() == self.sessions
                    )
                    self.ready_reason = (
                        'Runner available' if ok else 'Loaded runner watches a different checkout or session directory'
                    )
                else:
                    self.ready_reason = 'The scan launch agent is not loaded'
            except (OSError, subprocess.TimeoutExpired):
                self.ready_reason = 'Could not query the scan launch agent'
        self.health = (time.monotonic(), ok)
        return ok

    def session(self, name):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError('Invalid session name')
        p = self.sessions / name
        if p.resolve().parent != self.sessions or not p.is_dir():
            raise ValueError('Session not found')
        return p

    def child(self, folder, name):
        p = folder / str(name)
        return p if p.resolve().parent == folder.resolve() else None

    def report_link(self, folder):
        for rel in (
            'analysis/index.html',
            'report/index.html',
            'analysis/superscan_analysis.pdf',
            'analysis/hopscan_analysis.pdf',
            'report/report.pdf',
        ):
            p = folder / rel
            if p.is_file() and p.resolve().is_relative_to(folder.resolve()):
                return '/files/{}/{}'.format(quote(folder.name), rel)
        return None

    def building(self, folder):
        try:
            import fcntl

            with (folder / '.analysis.lock').open('r') as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(f, fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        return False

    def item(self, folder):
        if folder.name.startswith('expert-'):
            from soapypower.gui_jobs import job_detail

            job = job_detail(self.sessions, folder.name)
            inventory = job.get('capture_report', {}).get('attempts', [])
            records = sum(x.get('all_guards_passed', 0) for x in inventory) if inventory else None
            return dict(
                id=folder.name,
                status=job['state'],
                requested_s=job.get('requested_seconds'),
                credited_s=None,
                records=records,
                attempts=len(job.get('attempts', [])),
                recoveries=0,
                report_url=self.report_link(folder),
                rate_hz=None,
                centers=None,
                modified=folder.stat().st_mtime,
                expert=True,
            )
        capture = json_file(folder / 'captures.json', {}) or {}
        report = {}
        for filename in ('analysis/summary.json', 'analysis/superscan_summary.json'):
            path = folder / filename
            try:
                signature = (path.stat().st_mtime_ns, path.stat().st_size)
                with self.lock:
                    cached = self.report_cache.get(str(path))
                    if cached is None or cached[0] != signature:
                        summary = json_file(path, {}) or {}
                        summary = {
                            key: summary[key]
                            for key in ('records', 'credited_scan_s', 'sample_rate_hz')
                            if key in summary
                        }
                        cached = (signature, summary)
                        self.report_cache[str(path)] = cached
                        while len(self.report_cache) > 32:
                            self.report_cache.popitem(last=False)
                report = cached[1]
                if report:
                    break
            except OSError:
                pass
        cfg = json_file(folder / 'bands.json', {}) or {}
        link = self.report_link(folder)
        recorded_status = capture.get('status', 'saved')
        status = recorded_status if isinstance(recorded_status, str) else 'invalid'
        alive = self.supervisor_alive(folder, capture) if status in ('running', 'paused') else None
        if alive is False:
            status = 'interrupted'
        if status not in ('running', 'paused') and self.building(folder):
            status = 'reporting'
        elif status not in ('running', 'paused') and (folder / 'analysis-failure.txt').is_file():
            status = 'report_failed'
        return dict(
            id=folder.name,
            status=status,
            requested_s=capture.get('requested_seconds'),
            credited_s=capture.get('credited_seconds', report.get('credited_scan_s')),
            records=(
                report.get('records')
                if finite(report.get('records'))
                else sum(x.get('records', 0) for x in objects(capture.get('captures')) if finite(x.get('records')))
                if 'captures' in capture
                else None
            ),
            recorded_status=recorded_status,
            supervisor_alive=alive,
            attempts=len(objects(capture.get('attempts'))),
            recoveries=sum(x.get('failure_class') == 'transport' for x in objects(capture.get('attempts'))),
            report_url=link,
            rate_hz=cfg.get('sample_rate_hz', report.get('sample_rate_hz')),
            centers=len(cfg.get('centers_hz') or []) if isinstance(cfg.get('centers_hz'), list) else 0 or None,
            modified=folder.stat().st_mtime,
        )

    def catalog(self):
        folders = []
        try:
            for path in self.sessions.iterdir():
                try:
                    if (
                        path.is_dir()
                        and path.name not in ('bladerf-stack', 'bladerf-profile')
                        and NAME.fullmatch(path.name)
                        and path.resolve().parent == self.sessions
                        and path.name.startswith(
                            (
                                'superscan-',
                                'hopscan-',
                                'fullrange-',
                                'baseline-',
                                'bladerf-',
                                'suite-',
                                'okt-',
                                'expert-',
                            )
                        )
                    ):
                        if (
                            path.name.startswith('expert-')
                            and (json_file(path / 'job.json', {}) or {}).get('command') != 'acquisition'
                        ):
                            continue
                        folders.append((path.stat().st_mtime, path))
                except OSError:
                    continue
        except OSError:
            return []
        result = []
        for _, path in sorted(folders, reverse=True)[:60]:
            try:
                result.append(self.item(path))
            except (OSError, TypeError, ValueError, AttributeError) as error:
                LOG.warning('Cannot inspect session %s: %s', path.name, error)
        return result

    def jobs(self):
        files = []
        for path in (self.sessions / 'status').glob('*.json'):
            try:
                if path.stat().st_size <= 65536:
                    files.append((path.stat().st_mtime, path))
            except OSError:
                continue
        return [x for _, p in sorted(files, reverse=True)[:30] if isinstance((x := json_file(p)), dict)]

    def supervisor_alive(self, folder, manifest):
        if self.queue_override:
            return True  # injected test executor; no native process
        pid = manifest.get('supervisor_pid')
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
            return False
        try:
            result = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, timeout=2)
            return result.returncode == 0 and 'superscan_run.py' in result.stdout and str(folder) in result.stdout
        except (OSError, subprocess.TimeoutExpired):
            return False

    def busy(self):
        if any((self.sessions / 'queue').glob('*.json')):
            return True
        if os.name == 'nt':
            return False
        for name in ('.runner.lock', '.receiver.lock'):
            path = self.sessions / name
            if path.exists():
                try:
                    from soapypower.gui_service import file_lock

                    with file_lock(path, blocking=False):
                        pass
                except BlockingIOError:
                    return True
                except OSError:
                    return True  # uncertainty must not admit another hardware job
        for job in self.jobs():
            if job.get('state') != 'running' or not isinstance(job.get('pid'), int):
                continue
            try:
                r = subprocess.run(
                    ['ps', '-p', str(job['pid']), '-o', 'command='], capture_output=True, text=True, timeout=2
                )
                if re.search(r'/(runner\.(?:sh|py)|run_session\.sh)(?:\s|$)', r.stdout):
                    return True
            except (OSError, subprocess.TimeoutExpired):
                return True
        return False

    def snapshot(self):
        with self.snapshot_lock:
            stamp, value = self.snapshot_cache
            if value is None or time.monotonic() - stamp > 1:
                value = dict(
                    sessions=self.catalog(),
                    jobs=self.jobs(),
                    runner_ready=self.ready(),
                    runner_message=self.ready_reason,
                    busy=self.busy(),
                    platform=sys.platform,
                )
                self.snapshot_cache = (time.monotonic(), value)
            return value

    def enqueue(self, args):
        from soapypower.gui_service import file_lock

        admission = nullcontext() if self.queue_override else file_lock(self.sessions / '.gui-admission.lock')
        with self.lock, admission:
            if not self.ready():
                raise ValueError('The macOS scan runner is not loaded. Install tools/macos_runner/install.sh first.')
            if self.busy() or time.monotonic() - self.last_queue < 3:
                raise RuntimeError('A job is already queued or running')
            if self.queue_override:
                ident = self.queue_override(args)
            else:
                r = subprocess.run(
                    ['bash', str(self.repo / 'tools/macos_runner/queue.sh')] + args,
                    env={**os.environ, 'SPR_SESSIONS': str(self.sessions)},
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if r.returncode:
                    raise ValueError(r.stderr.strip() or 'Request validation failed')
                lines = r.stdout.strip().splitlines()
                if not lines or not NAME.fullmatch(lines[0]):
                    raise RuntimeError('Runner did not return a valid job identifier; inspect queue before retrying')
                ident = lines[0]
            self.last_queue = time.monotonic()
            return {'job_id': ident, 'state': 'queued'}

    def expert(self, payload):
        from soapypower.gui_jobs import queue, preflight
        from soapypower.gui_service import file_lock

        preflight(payload, self.repo, self.sessions)
        with self.lock, file_lock(self.sessions / '.gui-admission.lock'):
            if not self.ready():
                raise ValueError(self.ready_reason or 'Runner is unavailable')
            if self.busy():
                raise RuntimeError('A job is already queued or running')
            answer = queue(payload, self.sessions)
            self.snapshot_cache = (0, None)
            return answer

    def retry_settings(self, folder):
        """Reconstruct only typed, supported settings; never replay shell arguments."""
        paths = [folder / 'request.plist']
        for job in self.jobs():
            if (
                job.get('mode') != 'scan'
                or not isinstance(job.get('result'), str)
                or 'SESSION=' + str(folder) + ';' not in job['result']
            ):
                continue
            ident = job.get('id', '')
            if not isinstance(ident, str) or not NAME.fullmatch(ident):
                continue
            paths.extend(sorted((self.sessions / 'queue/taken').glob(ident + '-*.json')))
        for path in paths:
            try:
                root = folder if path.name == 'request.plist' else self.sessions / 'queue/taken'
                if not path.resolve().is_relative_to(root.resolve()) or path.stat().st_size > 16384:
                    continue
                r = plistlib.loads(path.read_bytes())
                allowed = {
                    'mode',
                    'scan_seconds',
                    'gain_db',
                    'antenna',
                    'input',
                    'procedure',
                    'device',
                    'bladerf_job',
                    'superscan',
                    'hopscan',
                    'superscan_gain',
                    'superscan_rate',
                    'superscan_dither',
                    'superscan_plan',
                    'reanalyze',
                    'plan_args',
                    'overflow_probe',
                    'satdb_refresh',
                }
                if not isinstance(r, dict) or set(r) - allowed or r.get('mode') != 'scan':
                    continue
                if any(r.get(k) for k in ('reanalyze', 'plan_args', 'overflow_probe', 'satdb_refresh')):
                    continue
                workflow = (
                    'satellite'
                    if r.get('superscan') == 1
                    else 'hopscan'
                    if r.get('hopscan') == 1
                    else r.get('procedure', 'suite')
                )
                payload = dict(
                    duration_s=r.get('scan_seconds', 30),
                    device=r.get('device', 'b200'),
                    workflow=workflow,
                    antenna=r.get('antenna', 'RX2'),
                    input=r.get('input', 'antenna'),
                    gain=r.get('superscan_gain', 'auto') if workflow == 'satellite' else r.get('gain_db', 20),
                    rate_msps=int(r.get('superscan_rate', 8)),
                    plan=r.get('superscan_plan', 'sat-full'),
                    dither_khz=int(r.get('superscan_dither', 0)),
                    bladerf_job=r.get('bladerf_job', 'dense-dc+survey'),
                )
                if isinstance(payload['gain'], bool):
                    continue
                if payload['gain'] != 'auto':
                    payload['gain'] = float(payload['gain'])
                scan_arguments(payload)
                return payload
            except (OSError, ValueError, TypeError, OverflowError, plistlib.InvalidFileException):
                continue
        return None

    def operate(self, name, action):
        folder = self.session(name)
        from soapypower.gui_service import file_lock

        guard = nullcontext() if self.queue_override else file_lock(folder / '.control.lock')
        with self.lock, guard:
            m = json_file(folder / 'captures.json', {}) or {}
            if not self.supervisor_alive(folder, m):
                raise RuntimeError('Supervisor is no longer running; request was not written')
            status = m.get('status')
            if status not in ('running', 'paused'):
                raise ValueError('No active supervised capture in this session')
            if action == 'abort':
                if m.get('supports_stop_request') is not True:
                    raise ValueError('Supervisor does not support abort')
                path = folder / 'stop-request.json'
                try:
                    with path.open('x') as f:
                        json.dump({'version': 1, 'action': 'stop'}, f)
                except FileExistsError:
                    pass
                return {'state': 'stop_requested'}
            if m.get('control_version') != 1:
                raise ValueError('This supervisor does not support pause/resume/retry')
            if (folder / 'stop-request.json').exists():
                raise RuntimeError('Abort is already pending')
            request = json_file(folder / 'control-request.json', {}) or {}
            if request.get('id') and request['id'] != m.get('control_ack'):
                raise RuntimeError('Waiting for supervisor acknowledgement')
            if action == 'pause' and status != 'running':
                raise ValueError('Capture is already paused')
            if action == 'resume' and status != 'paused':
                raise ValueError('Capture is not paused')
            if action not in ('pause', 'resume', 'retry'):
                raise ValueError('Unknown capture action')
            value = dict(
                version=1,
                id=uuid.uuid4().hex,
                action=action,
                requested_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
            path = folder / 'control-request.json'
            temp = path.with_name('.control-' + value['id'] + '.tmp')
            with temp.open('x') as f:
                json.dump(value, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)
            return {'state': action + '_requested', 'request_id': value['id']}

    def stop(self, name):
        return self.operate(name, 'abort')

    def retry(self, name, mode=None):
        folder = self.session(name)
        m = json_file(folder / 'captures.json', {}) or {}
        is_active = m.get('status') in ('running', 'paused') and self.supervisor_alive(folder, m)
        if mode not in (None, 'attempt', 'repeat'):
            raise ValueError('Invalid retry mode')
        if mode == 'attempt' and not is_active:
            raise RuntimeError('Acquisition ended; confirm a new repeat scan explicitly')
        if mode == 'repeat' and is_active:
            raise RuntimeError('Acquisition is still active; use Retry attempt')
        if is_active:
            return self.operate(name, 'retry')
        if self.building(folder):
            raise RuntimeError('Wait for report generation to finish')
        payload = self.retry_settings(folder)
        if payload is None:
            raise ValueError('Original supported scan settings are unavailable; configure a new scan')
        return self.enqueue(scan_arguments(payload))

    def detail(self, name):
        folder = self.session(name)
        item = self.item(folder)
        m = json_file(folder / 'captures.json', {}) or {}
        active = mapping(m.get('current_attempt'))
        filename = (active or {}).get('raw_file')
        if not filename and objects(m.get('captures')):
            filename = objects(m.get('captures'))[-1].get('file')
        path = self.child(folder, filename) if filename else folder / 'superscan.jsonl'
        if folder.name.startswith('expert-'):
            candidates = sorted(folder.glob('attempt-*.jsonl'))
            path = candidates[-1] if candidates else None
        row = record_at_end(path) if path else None
        live = None
        if row:
            meta = mapping(row.get('metadata'))
            rf = mapping(mapping(meta.get('rf_state')).get('after'))
            hr = mapping(meta.get('headroom'))
            ctx = mapping(meta.get('context'))
            power = list(row['power'])
            for index in row.get('masked_bins') or []:
                if type(index) is int and 0 <= index < len(power):
                    power[index] = None
            frequencies = []
            stride = max(1, math.ceil(len(power) / 600))
            values = []
            for i in range(0, len(power), stride):
                block = power[i : i + stride]
                valid = [x for x in block if finite(x)]
                first = row.get('first_bin_center_hz', row.get('start_hz'))
                step = row.get('step_hz')
                frequencies.append(
                    first + (i + (len(block) - 1) / 2) * step if finite(first) and finite(step) else None
                )
                values.append(max(valid) if len(valid) == len(block) and valid else None)
            gains = mapping(ctx.get('gains'))
            lock = mapping(mapping(rf.get('telemetry')).get('rx:lo_locked'))  # a stale reading is shown as unknown
            live = dict(
                start=row.get('time_start'),
                stop=row.get('time_stop'),
                center_hz=meta.get('tuned_center_hz'),
                rate_hz=meta.get('sample_rate_hz'),
                bins=len(power),
                first_hz=row.get('first_bin_center_hz'),
                step_hz=row.get('step_hz', 0) * stride if finite(row.get('step_hz')) else None,
                frequencies_hz=frequencies,
                qualified=meta.get('measurement_status') == 'complete'
                and meta.get('continuity') == 'verified'
                and hr.get('accepted') is True
                and mapping(rf.get('validity')).get('passed') is True,
                power=values,
                gain=gains,
                headroom=hr,
                temperature_c=mapping(rf.get('validity')).get('temperature_c'),
                rf_valid=mapping(rf.get('validity')).get('passed'),
                lo_locked=lock.get('value') if lock.get('fresh') is True else None,
                continuity=meta.get('continuity'),
                hardware=ctx.get('hardware'),
                unit=row.get('unit'),
            )
            try:
                first = None
                if active and path:
                    with path.open('rb') as f:
                        line = f.readline(2 * 1024 * 1024)
                        if line.endswith(b'\n'):
                            first = json.loads(line)
                span = (
                    (
                        datetime.datetime.fromisoformat(row['time_stop'])
                        - datetime.datetime.fromisoformat(first['time_start'])
                    ).total_seconds()
                    if first
                    else 0
                )
                item['live_estimated_s'] = (item.get('credited_s') or 0) + max(0, span)
            except (ValueError, KeyError, OSError, TypeError):
                pass
            stable_context = {
                k: v
                for k, v in ctx.items()
                if k not in ('frequency', 'freq', 'center_hz', 'frequency_hz', 'tuned_center_hz')
            }
            signature = (
                live['rate_hz'],
                live['bins'],
                json.dumps(stable_context, sort_keys=True),
                row.get('step_hz'),
                (row.get('first_bin_center_hz') - live['center_hz'])
                if finite(row.get('first_bin_center_hz')) and finite(live['center_hz'])
                else None,
            )
            now = live['stop']
            with self.lock:
                state = self.history.setdefault(name, dict(signature=signature, rows=[]))
                self.history.move_to_end(name)
                while len(self.history) > 12:
                    self.history.popitem(last=False)
                if state['signature'] != signature:
                    state.update(signature=signature, rows=[])
                if not state['rows'] or state['rows'][-1]['stop'] != now:
                    state['rows'].append({k: live[k] for k in ('start', 'stop', 'center_hz', 'power')})
                    state['rows'] = state['rows'][-60:]
                history = list(state['rows'])
            live['history'] = history
        logfile = (active or {}).get('log_file')
        if not logfile and objects(m.get('attempts')):
            logfile = objects(m.get('attempts'))[-1].get('log_file')
        logpath = self.child(folder, logfile) if logfile else folder / 'scan.log'
        if folder.name.startswith('expert-'):
            logpath = folder / 'job.log'
        running = m.get('status') in ('running', 'paused') and item.get('supervisor_alive') is not False
        request = json_file(folder / 'control-request.json', {}) or {}
        pending = request.get('action') if running and request.get('id') != m.get('control_ack') else None
        modern = running and m.get('control_version') == 1
        retry_config = None if running else self.retry_settings(folder)
        return dict(
            session=item,
            live=live,
            can_stop=running and m.get('supports_stop_request') is True,
            can_pause=modern and m.get('status') == 'running',
            can_resume=modern and m.get('status') == 'paused',
            can_retry=modern or (retry_config is not None and not self.building(folder)),
            retry_config=retry_config,
            pending_control=pending,
            controls=objects(m.get('controls'))[-100:],
            paused_seconds=m.get('paused_seconds'),
            attempts=objects(m.get('attempts'))[-100:],
            log=tail(logpath) if logpath else '',
            report_error=tail(folder / 'analysis-failure.txt', 3000),
            stop_requested=(folder / 'stop-request.json').exists(),
        )


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, control):
        self.control = control
        self.slots = threading.BoundedSemaphore(12)
        super().__init__(address, Handler)

    def server_close(self):
        self.control.satellites.close()
        super().server_close()

    def handle_error(self, request, client_address):
        LOG.exception('HTTP request handler failed')

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(0.2)
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(8)

    def do_HEAD(self):
        self.do_GET()

    def log_message(self, *args):
        pass

    def send(self, code, body, kind='application/json; charset=utf-8', extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(clean_json(body), allow_nan=False).encode()
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass

    def send_file(self, path, kind, extra):
        with path.open('rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            start, end = 0, size - 1
            status = 200
            header = self.headers.get('Range')
            if header:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
                if not match or not any(match.groups()):
                    self.send(416, b'', extra={'Content-Range': 'bytes */' + str(size)})
                    return
                a, b = match.groups()
                if a:
                    start = int(a)
                    end = min(int(b), end) if b else end
                else:
                    start = max(0, size - int(b))
                if start >= size or end < start:
                    self.send(416, b'', extra={'Content-Range': 'bytes */' + str(size)})
                    return
                status = 206
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(max(0, end - start + 1)))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            if status == 206:
                self.send_header('Content-Range', 'bytes {}-{}/{}'.format(start, end, size))
            for key, value in extra.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command == 'HEAD':
                return
            stream.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = stream.read(min(left, 65536))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass

    def trusted(self):
        port = self.server.server_port
        hosts = {'127.0.0.1:' + str(port), 'localhost:' + str(port)}
        return self.headers.get('Host') in hosts

    def do_GET(self):
        if not self.trusted():
            self.send(403, {'error': 'Invalid local Host'})
            return
        url = urlsplit(self.path)
        control = self.server.control
        try:
            if url.path == '/':
                body = (ASSETS / 'index.html').read_text(encoding='utf-8').replace('__TOKEN__', control.token)
                self.send(
                    200,
                    body,
                    'text/html; charset=utf-8',
                    {
                        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"
                    },
                )
            elif url.path in ('/app.js', '/workbench.js', '/satellites.js', '/skyview.js', '/style.css'):
                p = ASSETS / url.path[1:]
                self.send(200, p.read_bytes(), 'text/javascript' if p.suffix == '.js' else 'text/css')
            elif url.path == '/api/satellites':
                self.send(200, control.satellite_view(parse_qs(url.query)))
            elif url.path == '/api/options':
                from soapypower.gui_jobs import catalog

                self.send(200, catalog())
            elif url.path == '/api/artifacts':
                from soapypower.gui_jobs import artifacts

                self.send(200, artifacts(control.sessions))
            elif url.path == '/api/expert':
                from soapypower.gui_jobs import job_detail

                self.send(200, job_detail(control.sessions, parse_qs(url.query).get('id', [''])[0]))
            elif url.path == '/api/health':
                self.send(200, dict(service='soapy_power', instance_id=control.instance_id, pid=os.getpid()))
            elif url.path == '/api/state':
                self.send(200, control.snapshot())
            elif url.path == '/api/session':
                self.send(200, control.detail(parse_qs(url.query).get('id', [''])[0]))
            elif url.path.startswith('/artifacts/'):
                from soapypower.gui_jobs import artifact

                p = artifact(control.sessions, unquote(url.path[len('/artifacts/') :]))
                if (
                    p.suffix.lower()
                    not in ('.html', '.pdf', '.png', '.json', '.jsonl', '.npz', '.npy', '.csv', '.txt', '.log', '.bin')
                    or not p.is_file()
                ):
                    raise ValueError('Unsupported artifact')
                self.send_file(
                    p,
                    mimetypes.guess_type(p.name)[0] or 'application/octet-stream',
                    {
                        'Content-Security-Policy': "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:"
                    },
                )
            elif url.path.startswith('/files/'):
                parts = unquote(url.path).split('/')
                folder = control.session(parts[2])
                rel = Path(*parts[3:])
                p = (folder / rel).resolve()
                if (
                    not parts[3:]
                    or parts[3] not in ('analysis', 'report')
                    or not (folder / parts[3]).resolve().is_relative_to(folder.resolve())
                    or not p.is_relative_to((folder / parts[3]).resolve())
                    or p.suffix.lower() not in ('.html', '.pdf', '.png', '.json', '.csv', '.txt')
                    or not p.is_file()
                ):
                    raise ValueError('Report file not found')
                kind = mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
                extra = (
                    {
                        'Content-Security-Policy': "sandbox allow-scripts allow-downloads; default-src 'self' data: blob:; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'"
                    }
                    if p.suffix == '.html'
                    else {}
                )
                self.send_file(p, kind, extra)
            else:
                self.send(404, {'error': 'Not found'})
        except (ValueError, IndexError, FileNotFoundError) as e:
            self.send(404, {'error': str(e)})
        except (OSError, TypeError, AttributeError, KeyError):
            LOG.exception('GUI read failed')
            self.send(500, {'error': 'Could not read session data; inspect gui.log'})

    def do_POST(self):
        c = self.server.control
        origin = self.headers.get('Origin')
        expected = 'http://' + self.headers.get('Host', '')
        if not self.trusted() or (origin and origin != expected) or self.headers.get('X-CSRF-Token') != c.token:
            self.send(403, {'error': 'Invalid local request token or origin'})
            return
        locked = False
        try:
            n = int(self.headers.get('Content-Length', '0'))
            if not 0 < n <= 16384 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('Expected a small JSON request')
            if self.headers.get('Transfer-Encoding'):
                raise ValueError('Transfer encoding is unsupported')
            raw = self.rfile.read(n)
            if len(raw) != n:
                raise ValueError('Incomplete request body')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('Expected an object')
            request_id = self.headers.get('X-Request-ID')
            if request_id and not re.fullmatch(r'[a-f0-9]{32}', request_id):
                raise ValueError('Invalid request ID')
            fingerprint = hashlib.sha256((self.path + json.dumps(data, sort_keys=True)).encode()).hexdigest()
            c.action_lock.acquire()
            locked = True
            if request_id in c.responses:
                previous, answer, created = c.responses[request_id]
                if fingerprint != previous:
                    raise ValueError('Request ID was already used for different parameters')
                if time.monotonic() - created < 300:
                    self.send(200, answer)
                    return
                del c.responses[request_id]
            if self.path == '/api/satellites/refresh':
                if data:
                    raise ValueError('Refresh does not accept external URLs or credentials')
                result = c.satellites.refresh()
            elif self.path == '/api/satellites/observer':
                result = c.satellite_observer(data)
            elif self.path == '/api/satellites/prepare':
                result = c.satellites.prepare(data)
            elif self.path == '/api/plan':
                from soapypower.gui_jobs import preflight

                result = preflight(data, c.repo, c.sessions)
            elif self.path == '/api/artifact':
                from soapypower.gui_jobs import create_artifact

                result = create_artifact(c.sessions, data)
            elif self.path == '/api/expert':
                result = c.expert(data)
            elif self.path == '/api/expert-control':
                from soapypower.gui_jobs import control

                if set(data) != {'id', 'action'}:
                    raise ValueError('Expected expert job id and action')
                result = control(c.sessions, data['id'], data['action'])
            elif self.path == '/api/scan':
                result = c.enqueue(scan_arguments(data))
            elif self.path == '/api/probe':
                if set(data) - {'device'} or data.get('device', 'b200') not in ('b200', 'bladerf'):
                    raise ValueError('Invalid probe')
                result = c.enqueue(['probe', '--device', data.get('device', 'b200')])
            elif self.path in ('/api/report', '/api/stop', '/api/abort', '/api/pause', '/api/resume', '/api/retry'):
                if set(data) != {'session'} and not (self.path == '/api/retry' and set(data) == {'session', 'mode'}):
                    raise ValueError('Expected session only, with optional retry mode')
                folder = c.session(data['session'])
                if self.path == '/api/report':
                    if not re.fullmatch(r'(superscan|hopscan)-\d{8}-\d{6}', folder.name):
                        raise ValueError('This session does not support queued reanalysis')
                    manifest = json_file(folder / 'captures.json', {}) or {}
                    if c.building(folder):
                        raise RuntimeError('Report generation is already in progress')
                    if manifest.get('status') in ('running', 'paused') and c.supervisor_alive(folder, manifest):
                        raise RuntimeError('Finish or abort acquisition before generating a report')
                    result = c.enqueue(['scan', '--reanalyze', folder.name])
                elif self.path == '/api/retry':
                    result = c.retry(folder.name, data.get('mode'))
                else:
                    result = c.operate(
                        folder.name,
                        'abort' if self.path in ('/api/stop', '/api/abort') else self.path.rsplit('/', 1)[-1],
                    )
            else:
                self.send(404, {'error': 'Unknown action'})
                return
            if request_id:
                c.responses[request_id] = (fingerprint, result, time.monotonic())
                while len(c.responses) > 128:
                    c.responses.popitem(last=False)
            self.send(200, result)
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError) as e:
            self.send(400, {'error': str(e)})
        except RuntimeError as e:
            self.send(409, {'error': str(e)})
        except (OSError, subprocess.SubprocessError) as e:
            LOG.warning('GUI action failed: %s', e)
            self.send(503, {'error': 'Action could not be confirmed. Refresh job status before retrying.'})
        finally:
            if locked:
                c.action_lock.release()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', default=os.environ.get('SPR_REPO', str(Path(__file__).resolve().parents[1])))
    p.add_argument('--sessions', default=os.environ.get('SPR_SESSIONS'))
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--no-browser', action='store_true')
    p.add_argument('--instance-id', default=None, help=argparse.SUPPRESS)
    p.add_argument('--log-file', default=None, help='Rotate service diagnostics at 2 MiB, retaining two backups')
    a = p.parse_args()
    if a.log_file:
        handler = RotatingFileHandler(a.log_file, maxBytes=2 * 1024 * 1024, backupCount=2, encoding='utf-8')
        logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(levelname)s %(message)s')
    if not 0 <= a.port <= 65535:
        p.error('port must be 0–65535')
    control = Control(a.repo, a.sessions or str(Path(a.repo) / 'sessions'))
    if a.instance_id:
        control.instance_id = a.instance_id
    server = Server(('127.0.0.1', a.port), control)
    url = 'http://127.0.0.1:' + str(server.server_port)
    print('soapy_power GUI: ' + url, flush=True)
    if not a.no_browser:
        webbrowser.open(url)

    def terminate(signum, frame):
        raise KeyboardInterrupt()

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == '__main__':
    sys.exit(main())
