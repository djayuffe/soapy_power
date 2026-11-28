#!/usr/bin/env python3
"""Serial, crash-aware receive-job runner. No SDR modules are imported here."""

import fcntl
import json
import math
import os
from pathlib import Path
import plistlib
import re
import signal
import subprocess
import sys
import time
import uuid

NAME = re.compile(r'^[A-Za-z0-9_-]{1,64}$')


def now():
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def publish(path, value):
    temp = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        with temp.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, separators=(',', ':'), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def validate(value):
    if not isinstance(value, dict):
        raise ValueError('request must be a property-list dictionary')
    allowed = {
        'mode',
        'scan_seconds',
        'gain_db',
        'antenna',
        'input',
        'procedure',
        'device',
        'bladerf_job',
        'overflow_probe',
        'hopscan',
        'superscan',
        'superscan_plan',
        'superscan_gain',
        'superscan_rate',
        'superscan_dither',
        'satdb_refresh',
        'reanalyze',
        'gui',
        'gui_port',
        'plan_args',
        'expert',
    }
    if set(value) - allowed:
        raise ValueError('unknown request fields')
    if 'expert' in value:
        if set(value) != {'mode', 'expert'} or value['mode'] != 'scan' or not isinstance(value['expert'], str):
            raise ValueError('invalid expert envelope')
        from soapypower.gui_jobs import plan

        payload = json.loads(value['expert'])
        spec = plan(payload, os.environ.get('SPR_SESSIONS', '.'))
        return dict(
            mode='scan',
            expert=value['expert'],
            expert_hardware=spec['hardware'],
            scan_seconds=30,
            gain_db=20,
            antenna='RX2',
            input='antenna',
            procedure='suite',
        )
    r = dict(
        mode='scan',
        scan_seconds=30,
        gain_db=20,
        antenna='RX2',
        input='antenna',
        procedure='suite',
        device='b200',
        bladerf_job='dense-dc+survey',
    )
    r.update(value)
    for field, choices, message in [
        ('mode', ('probe', 'setup', 'scan'), 'mode must be probe, setup or scan'),
        ('antenna', ('RX2', 'TX/RX'), 'bad antenna'),
        ('input', ('antenna', 'terminated'), 'bad input'),
        ('procedure', ('suite', 'baseline', 'fullrange'), 'bad procedure'),
        ('device', ('b200', 'bladerf'), 'bad device'),
        ('bladerf_job', ('dense-dc', 'survey', 'survey-anchor', 'dense-dc+survey'), 'bad bladeRF procedure'),
    ]:
        if r[field] not in choices:
            raise ValueError(message)
    seconds, gain = r['scan_seconds'], r['gain_db']
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 5 <= seconds <= 3600:
        raise ValueError('bad number: seconds 5–3600')
    if isinstance(gain, bool) or not isinstance(gain, (int, float)) or not math.isfinite(gain) or not 0 <= gain <= 70:
        raise ValueError('bad number: gain 0–70')
    for field in ('overflow_probe', 'hopscan', 'superscan', 'satdb_refresh'):
        if field in r and (type(r[field]) is not int or r[field] != 1):
            raise ValueError('bad ' + field + ' flag')
    for field, choices in [
        ('superscan_plan', ('sat', 'sat-full', 'sat-s')),
        ('superscan_rate', ('2', '4', '8')),
        ('gui', ('start', 'stop')),
    ]:
        if field in r and r[field] not in choices:
            raise ValueError('bad ' + field)
    for field, pattern in [
        ('superscan_dither', r'[0-9]{1,4}'),
        ('reanalyze', r'(?:hopscan|superscan|fullrange)-[0-9]{8}-[0-9]{6}'),
        ('plan_args', r'[A-Za-z0-9 .=_-]*'),
    ]:
        if field in r and (not isinstance(r[field], str) or not re.fullmatch(pattern, r[field])):
            raise ValueError('bad ' + field)
    if 'superscan_gain' in r and r['superscan_gain'] != 'auto':
        g = r['superscan_gain']
        if not isinstance(g, str) or not re.fullmatch(r'[0-9]{1,2}(?:\.[0-9])?', g) or float(g) > 76:
            raise ValueError('bad superscan gain')
    if 'gui_port' in r:
        if (
            not isinstance(r['gui_port'], str)
            or not r['gui_port'].isdigit()
            or not 1024 <= int(r['gui_port']) <= 65535
            or 'gui' not in r
        ):
            raise ValueError('bad GUI port')
    operations = sum(bool(r.get(k)) for k in ('gui', 'reanalyze', 'superscan', 'hopscan', 'overflow_probe'))
    if operations > 1 or (operations and r['mode'] != 'scan'):
        raise ValueError('conflicting operations')
    if r['device'] == 'bladerf' and any(r.get(k) for k in ('superscan', 'hopscan', 'overflow_probe')):
        raise ValueError('unsupported device workflow')
    return r


def run(repo, sessions):
    sys.path.insert(0, str(repo))
    sessions.mkdir(parents=True, exist_ok=True)
    queue, statuses = sessions / 'queue', sessions / 'status'
    taken = queue / 'taken'
    taken.mkdir(parents=True, exist_ok=True)
    statuses.mkdir(exist_ok=True)
    fd = os.open(sessions / '.runner.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'r+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        # A previous runner's accepted request is never replayed automatically.
        for path in statuses.glob('*.json'):
            try:
                if path.is_symlink() or path.stat().st_size > 65536:
                    continue
                old = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(old, dict) and old.get('state') == 'running':
                    old.update(
                        state='interrupted',
                        exit=None,
                        finished_utc=now(),
                        reason='runner ended without a final status; inspect retained session evidence',
                    )
                    publish(path, old)
            except (OSError, ValueError):
                continue
        while True:
            requests = sorted(queue.glob('*.json'))
            if not requests:
                return 0
            request = requests[0]
            ident = request.stem
            if not NAME.fullmatch(ident):
                safe = 'rejected-' + uuid.uuid4().hex
                publish(statuses / (safe + '.json'), dict(state='rejected', reason='bad id', utc=now()))
                os.replace(request, taken / (safe + '.json'))
                continue
            status = statuses / (ident + '.json')
            claimed = taken / (ident + '-' + str(time.time_ns()) + '.json')
            try:
                if request.is_symlink() or request.stat().st_size > 16384:
                    raise ValueError('invalid request file')
                os.replace(request, claimed)
                try:
                    value = plistlib.loads(claimed.read_bytes())
                except Exception as error:
                    raise ValueError('invalid property list') from error
                config = validate(value)
                script = repo / 'tools/macos_runner/run_session.sh'
                if not script.is_file():
                    raise ValueError('run_session.sh missing in repo')
            except (OSError, ValueError) as error:
                # Quarantine bad files once instead of repeatedly waking launchd.
                if request.exists() or request.is_symlink():
                    os.replace(request, claimed)
                publish(status, dict(id=ident, state='rejected', reason=str(error), utc=now()))
                continue
            last = sessions / 'last-run.env'
            last.unlink(missing_ok=True)
            env = dict(
                os.environ,
                MODE=config['mode'],
                SCAN_SECONDS=str(config['scan_seconds']),
                GAIN_DB=str(config['gain_db']),
                ANTENNA=config['antenna'],
                INPUT=config['input'],
                PROCEDURE=config['procedure'],
                REQUEST_FILE=str(claimed),
            )
            radio = None
            if not config.get('gui') and not config.get('reanalyze') and config.get('expert_hardware', True):
                radio = os.fdopen(
                    os.open(sessions / '.receiver.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600),
                    'r+',
                )
                try:
                    fcntl.flock(radio, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    radio.close()
                    # Keep the request pending while an orphan RX child owns the lease.
                    os.replace(claimed, request)
                    return 0
                env['SPR_RADIO_LEASE_FD'] = str(radio.fileno())
            started = now()
            child = None
            interrupted = False

            def terminate(signum, frame):
                raise KeyboardInterrupt()

            previous = {s: signal.signal(s, terminate) for s in (signal.SIGTERM, signal.SIGINT)}
            try:
                child = subprocess.Popen(
                    ['/bin/bash', str(script)],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    pass_fds=(lock.fileno(),) + ((radio.fileno(),) if radio else ()),
                )
                publish(
                    status,
                    dict(
                        id=ident,
                        state='running',
                        mode=config['mode'],
                        started_utc=started,
                        pid=os.getpid(),
                        child_pid=child.pid,
                    ),
                )
                code = child.wait()
            except KeyboardInterrupt:
                interrupted = True
                code = 130
                if child is not None and child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
            except OSError as error:
                code = 1
                if child is not None and child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                publish(status, dict(id=ident, state='rejected', reason=str(error), utc=now()))
                continue
            finally:
                for s, handler in previous.items():
                    signal.signal(s, handler)
                if radio:
                    radio.close()
            try:
                with last.open('rb') as stream:
                    result = stream.read(32768).decode('utf-8', 'replace').replace('\n', ';')
            except OSError:
                result = ''
            publish(
                status,
                dict(
                    id=ident,
                    state='interrupted' if interrupted else 'finished',
                    mode=config['mode'],
                    started_utc=started,
                    finished_utc=now(),
                    exit=code,
                    result=result,
                ),
            )
            if interrupted:
                return 130


if __name__ == '__main__':
    sys.exit(run(Path(os.environ['SPR_REPO']).resolve(), Path(os.environ['SPR_SESSIONS']).resolve()))
