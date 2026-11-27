"""Typed expert jobs. HTTP planning imports no numerical or SDR libraries.

The queue carries values, never shell commands. Every output has a fresh job
folder; the worker revalidates inputs and the real CLI parser before execution.
"""

import argparse
import datetime
import json
import math
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

CATALOG = Path(__file__).with_name('web') / 'options.json'
NAME = re.compile(r'^expert-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}$')
HARDWARE = {'acquisition', 'telemetry:inventory', 'calibration:auto', 'experiments:run'}


def catalog():
    return json.loads(CATALOG.read_text(encoding='utf-8'))


def publish(path, value):
    temp = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        temp.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def artifact(root, value, hook=False):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError('Choose an input artifact relative to the session directory')
    relative = Path(value)
    base = Path(root).resolve()
    path = (base / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(base) or path == base or not path.exists():
        raise ValueError('Input artifact must exist inside the sessions directory: ' + value)
    if hook and (not path.is_relative_to(base / 'hooks') or not path.is_file() or not os.access(path, os.X_OK)):
        raise ValueError('Source hook must be an executable installed in sessions/hooks')
    return path


def output_name(command, dest):
    if dest in ('directory', 'out') and command != 'spurs:plan':
        return 'result'
    if command.startswith('analysis:') and command not in ('analysis:spur-atlas', 'analysis:import-profile'):
        return 'result.npz'
    if command == 'acquisition':
        return 'capture.jsonl'
    return 'result.json'


def plan(payload, sessions, folder=None):
    if not isinstance(payload, dict) or set(payload) - {
        'command',
        'values',
        'runtime',
        'seconds',
        'anti_spur',
        'satellite',
    }:
        raise ValueError('Unknown expert job fields')
    target = payload.get('satellite')
    if target is not None:
        if (
            payload.get('command') != 'acquisition'
            or not isinstance(target, dict)
            or set(target) != {'norad', 'transmitter_id', 'cache_identity'}
        ):
            raise ValueError('Invalid satellite target')
        if (
            type(target['norad']) is not int
            or not 1 <= target['norad'] <= 1000000000
            or not isinstance(target['transmitter_id'], str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', target['transmitter_id'])
        ):
            raise ValueError('Invalid satellite transmitter identity')
        if not isinstance(target['cache_identity'], str) or not re.fullmatch(r'[a-f0-9]{64}', target['cache_identity']):
            raise ValueError('Invalid satellite reference identity')
    command = next((c for c in catalog()['commands'] if c['id'] == payload.get('command')), None)
    if command is None:
        raise ValueError('Unknown operation')
    values = payload.get('values', {})
    if not isinstance(values, dict) or len(values) > 100:
        raise ValueError('Expected typed option values')
    options = {o['dest']: o for o in command['options']}
    if set(values) - set(options):
        raise ValueError('Unknown options: ' + ', '.join(sorted(set(values) - set(options))))
    runtime = payload.get('runtime', 'b200')
    if runtime not in ('b200', 'bladerf', 'generic'):
        raise ValueError('Choose a supported runtime')
    hardware = command['id'] in HARDWARE or (command['id'] == 'calibration:assist' and not values.get('offline'))
    seconds = payload.get('seconds', 30)
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not math.isfinite(seconds)
        or not 5 <= seconds <= 3600
    ):
        raise ValueError('Scan duration must be 5–3600 seconds')
    anti = payload.get('anti_spur', False)
    if type(anti) is not bool or (anti and command['id'] != 'acquisition'):
        raise ValueError('Automatic measured spur analysis is available for acquisition jobs')
    target = Path(folder) if folder else Path(sessions) / 'expert-PREVIEW'
    extensions = {
        'json': '.jsonl',
        'rtl_power': '.csv',
        'rtl_power_fftw': '.txt',
        'soapy_power_bin': '.bin',
        'soapy_power_bin_v3': '.bin',
    }
    capture_format = values.get('format', 'json')
    extension = extensions.get(capture_format, '.jsonl')
    argv = list(command['command'])
    normalized = {}
    for o in command['options']:
        dest, role = o['dest'], o['role']
        if role in ('managed', 'output') and dest in values:
            raise ValueError(dest + ' is managed by the job runner')
        if role == 'managed':
            continue
        value = values.get(dest)
        if role == 'output':
            value = str(
                target
                / (('capture' + extension) if command['id'] == 'acquisition' else output_name(command['id'], dest))
            )
        elif value is None:
            if o['required']:
                raise ValueError('Missing required option: ' + dest)
            continue
        else:
            normalized[dest] = value
        if o['kind'] == 'boolean':
            if type(value) is not bool:
                raise ValueError(dest + ' must be boolean')
            if value == o['polarity']:
                argv.append(o['flags'][-1])
            continue
        many = o['nargs'] in ('+', '*') or isinstance(o['nargs'], int) and o['nargs'] > 1
        items = value if many else [value]
        if not isinstance(items, list) or not items or len(items) > 128:
            raise ValueError(dest + ' requires a nonempty, bounded list')
        if isinstance(o['nargs'], int) and len(items) != o['nargs']:
            raise ValueError('Wrong number of values for ' + dest)
        encoded = []
        for item in items:
            if o['kind'] in ('integer', 'number'):
                if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
                    raise ValueError(dest + ' must be finite numeric data')
                if o['kind'] == 'integer' and int(item) != item:
                    raise ValueError(dest + ' must be an integer')
            elif not isinstance(item, str) or len(item) > 8192 or '\x00' in item or '\n' in item:
                raise ValueError(dest + ' must be a bounded single-line string')
            if o['choices'] is not None and item not in o['choices']:
                raise ValueError('Unsupported value for ' + dest)
            if role in ('input', 'source'):
                item = str(artifact(sessions, item, hook=role == 'source'))
            elif role != 'output' and str(item).startswith('-') and not re.fullmatch(r'-[0-9].*', str(item)):
                # Positional/variable-length arguments must never inject CLI options.
                raise ValueError('Option-like value is not allowed for ' + dest)
            encoded.append(str(item))
        if o['flags']:
            argv.append(o['flags'][-1])
        argv.extend(encoded)
    for group in command['mutex']:
        if sum(k in normalized and normalized[k] is not False and normalized[k] is not None for k in group) > 1:
            raise ValueError('Mutually exclusive options: ' + ', '.join(group))
    if command['id'] == 'acquisition':
        device = values.get('device', '')
        if not device:
            raise ValueError('Expert acquisition requires an explicit SoapySDR device selector')
        if runtime == 'b200' and ('driver=uhd' not in device or 'type=b200' not in device):
            raise ValueError('B200 runtime requires driver=uhd,type=b200')
        if runtime == 'bladerf' and 'driver=bladerf' not in device:
            raise ValueError('bladeRF runtime requires driver=bladerf')
        if values.get('detect') or values.get('info'):
            hardware = True
        else:
            if values.get('format', 'json') != 'json':
                if anti:
                    raise ValueError('Measured spur analysis requires JSON density records')
            if 'format' not in values:
                argv += ['--format', 'json']
            argv += ['--elapsed', str(seconds)]
            if values.get('headroom') or values.get('receiver_profile'):
                argv += ['--rejection-evidence', str(target / 'rejected')]
    return dict(
        satellite=payload.get('satellite'),
        command=command['id'],
        module=command['module'],
        argv=argv,
        hardware=hardware,
        runtime=runtime,
        seconds=seconds,
        anti_spur=anti,
        values=normalized,
        output=str(target),
        capture_format=capture_format,
        capture_extension=extension,
        scope='Raw data retained; qualification follows recorded guards; no automatic notches',
    )


def validate_cli(payload, sessions):
    # This runs in an isolated CPU-only child, never in an HTTP worker.
    from tools.gui.build_catalog import parser_for

    spec = plan(payload, sessions)
    parser = parser_for(spec['module'])
    args = parser.parse_args(spec['argv'])
    if spec['command'] == 'acquisition':
        from soapypower.__main__ import validate_arguments

        validate_arguments(parser, args)
    return spec


def preflight(payload, repo, sessions):
    candidate = Path(sessions) / '.venv/bin/python'
    python = str(candidate) if candidate.is_file() else sys.executable
    result = subprocess.run(
        [python, '-m', 'soapypower.gui_jobs', '--validate', '--repo', str(repo), '--sessions', str(sessions)],
        input=json.dumps(payload, allow_nan=False),
        text=True,
        capture_output=True,
        timeout=12,
        env=dict(os.environ, PYTHONPATH=str(repo)),
    )
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or 'CLI preflight failed')[-12000:])
    return json.loads(result.stdout)


def queue(payload, sessions):
    plan(payload, sessions)
    ident = 'expert-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
    folder = Path(sessions) / 'queue'
    folder.mkdir(parents=True, exist_ok=True)
    # JSON string keeps plist representation free from unsupported null values.
    data = plistlib.dumps({'mode': 'scan', 'expert': json.dumps(payload, allow_nan=False)})
    temp = folder / ('.' + ident)
    try:
        with temp.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, folder / (ident + '.json'))
    finally:
        temp.unlink(missing_ok=True)
    return dict(job_id=ident, state='queued')


def artifacts(sessions, limit=1200):
    base = Path(sessions).resolve()
    found = []
    visited = 0
    # Walk only visible session folders; never inspect venvs/native stacks or symlinks.
    for root, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith('.')
            and d not in ('queue', 'status', 'bladerf-stack', 'bladerf-profile', '__pycache__', 'node_modules')
            and not (Path(root) / d).is_symlink()
        )
        for name in sorted(files):
            visited += 1
            if visited > 20000 or len(found) >= limit:
                return dict(items=found, truncated=True)
            p = Path(root) / name
            if p.is_symlink() or p.suffix.lower() not in ('.json', '.jsonl', '.npy', '.npz', '.csv', '.txt'):
                continue
            try:
                st = p.stat()
                found.append(dict(path=str(p.relative_to(base)), bytes=st.st_size, modified=st.st_mtime))
            except OSError:
                pass
    return dict(items=found, truncated=False)


def create_artifact(sessions, payload):
    if not isinstance(payload, dict) or set(payload) != {'name', 'data'}:
        raise ValueError('Expected an artifact name and JSON data')
    if not isinstance(payload['name'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}\.json', payload['name']):
        raise ValueError('Choose a simple .json filename')
    folder = Path(sessions) / 'inputs'
    folder.mkdir(exist_ok=True)
    if folder.is_symlink():
        raise ValueError('Input directory cannot be a symlink')
    path = folder / payload['name']
    text = json.dumps(payload['data'], indent=2, allow_nan=False) + '\n'
    try:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(text)
    except FileExistsError as error:
        raise ValueError('Artifact already exists; choose a new filename') from error
    return dict(path=str(path.relative_to(sessions)))


def job_detail(sessions, ident):
    if not isinstance(ident, str) or not NAME.fullmatch(ident):
        raise ValueError('Invalid expert job identifier')
    base = Path(sessions).resolve()
    folder = base / ident
    if folder.is_symlink():
        raise ValueError('Invalid expert job folder')
    result = {'id': ident, 'state': 'queued'}
    try:
        runner = json.loads((base / 'status' / (ident + '.json')).read_text())
    except FileNotFoundError:
        runner = {}
    try:
        result.update(json.loads((folder / 'job.json').read_text()))
        with (folder / 'job.log').open('rb') as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - 24000))
            result['log'] = stream.read(24000).decode('utf-8', 'replace')
    except FileNotFoundError:
        pass
    if runner.get('state') in ('rejected', 'interrupted') or (
        runner.get('state') == 'finished' and result['state'] in ('queued', 'running', 'paused')
    ):
        result.update(
            state='failed' if runner['state'] == 'finished' and runner.get('exit') else runner['state'], runner=runner
        )
    try:
        pending = json.loads((folder / 'control.json').read_text())
        result['pending_control'] = pending.get('action') if pending.get('id') != result.get('control_ack') else None
    except FileNotFoundError:
        pass
    result['abort_requested'] = (folder / 'abort.json').exists()
    result['files'] = []
    visits = 0
    allowed = {'.html', '.pdf', '.png', '.json', '.jsonl', '.npz', '.npy', '.csv', '.txt', '.log', '.bin'}
    for root, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(root) / d).is_symlink())
        for name in sorted(files):
            visits += 1
            path = Path(root) / name
            if not path.is_symlink() and path.suffix.lower() in allowed:
                result['files'].append(str(path.relative_to(folder)))
            if visits >= 2000 or len(result['files']) >= 200:
                result['files_truncated'] = True
                break
        if visits >= 2000 or len(result['files']) >= 200:
            break
    if result['state'] in ('failed', 'rejected', 'interrupted') and runner:
        result.setdefault('error', str(runner.get('reason') or runner.get('result') or 'Inspect runner status')[-3000:])
    return result


def control(sessions, ident, action):
    if action not in ('pause', 'resume', 'abort', 'retry'):
        raise ValueError('Unknown job operation')
    state = job_detail(sessions, ident)
    if state.get('state') not in ('running', 'paused'):
        raise ValueError('Job is not active; submit a fresh reviewed plan to repeat it')
    if action != 'abort' and state.get('command') != 'acquisition':
        raise ValueError('Only acquisition jobs support pause/resume/retry')
    from soapypower.gui_service import file_lock

    folder = Path(sessions) / ident
    with file_lock(folder / '.control.lock'):
        state = job_detail(sessions, ident)
        if state.get('state') not in ('running', 'paused'):
            raise ValueError('Job state changed; refresh before applying this control')
        if action != 'abort':
            if (folder / 'abort.json').exists():
                raise ValueError('Abort has already been requested')
            try:
                previous = json.loads((folder / 'control.json').read_text())
            except FileNotFoundError:
                previous = {}
            if previous.get('id') and previous['id'] != state.get('control_ack'):
                raise ValueError('Wait for acknowledgement of the previous control')
            if action == 'resume' and state['state'] != 'paused':
                raise ValueError('Only a paused scan can resume')
            if action == 'pause' and state['state'] != 'running':
                raise ValueError('Only a running scan can pause')
        # Abort has a separate sticky marker; resume can never erase it.
        publish(
            folder / ('abort.json' if action == 'abort' else 'control.json'), dict(action=action, id=uuid.uuid4().hex)
        )
    return dict(id=ident, requested=action)


def _stop(child):
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()


def _worker(repo, sessions, request):
    payload = json.loads(plistlib.loads(Path(request).read_bytes())['expert'])
    match = re.fullmatch(r'(expert-[0-9]{8}-[0-9]{6}-[a-f0-9]{8})(?:-[0-9]+)?\.json', Path(request).name)
    ident = match[1] if match else ''
    if not NAME.fullmatch(ident):
        raise ValueError('Invalid expert request id')
    folder = Path(sessions) / ident
    spec = plan(payload, sessions, folder)
    folder.mkdir(exist_ok=False)
    publish(folder / 'plan.json', spec)
    state = dict(
        id=ident,
        state='running',
        command=spec['command'],
        pid=os.getpid(),
        started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        attempts=[],
        requested_seconds=spec['seconds'],
        active_seconds=0,
        qualification='See per-record metadata',
    )
    child = None
    interrupted = False

    def terminate(signum, frame):
        nonlocal interrupted
        interrupted = True

    previous = {s: signal.signal(s, terminate) for s in (signal.SIGTERM, signal.SIGINT)}
    code = 1
    try:
        if spec.get('satellite') is not None:
            from soapypower.satellites import freeze

            freeze(repo, sessions, folder, spec['satellite'], seconds=spec['seconds'])
        # Reuse real parser validation before any native device initialization.
        if spec['command'] == 'acquisition':
            from soapypower.__main__ import setup_argument_parser, validate_arguments

            parser = setup_argument_parser()
            validate_arguments(parser, parser.parse_args(spec['argv']))
        argv = [sys.executable, '-m', spec['module']] + spec['argv']
        if spec['command'] == 'experiments:run':
            # Source-installed entry point may refer to another checkout. Generate
            # a managed wrapper pointing to this worker's interpreter and repo.
            import shlex

            wrapper = folder / 'acquire'
            wrapper.write_text(
                '#!/bin/bash\n'
                + '\n'.join(
                    'export ' + k + '=' + shlex.quote(v)
                    for k, v in os.environ.items()
                    if k
                    in (
                        'PYTHONPATH',
                        'DYLD_LIBRARY_PATH',
                        'DYLD_FALLBACK_LIBRARY_PATH',
                        'SOAPY_SDR_PLUGIN_PATH',
                        'UHD_IMAGES_DIR',
                    )
                )
                + '\nexec '
                + shlex.quote(sys.executable)
                + ' -m soapypower "$@"\n'
            )
            wrapper.chmod(0o700)
            argv += ['--soapy-power', str(wrapper)]
        if spec['command'] == 'experiments:analyze':
            source = artifact(sessions, spec['values']['directory'])
            if sum(p.stat().st_size for p in source.rglob('*') if p.is_file()) > 2 * 1024**3:
                raise ValueError('Suite snapshot exceeds 2 GiB; use CLI analysis for this dataset')
            destination = folder / 'suite-copy'
            if any(p.is_symlink() for p in source.rglob('*')):
                raise ValueError('Suite snapshots cannot contain symlinks')
            shutil.copytree(source, destination)
            argv[3 + len(spec['command'].split(':')) - 1] = str(destination)
        acquisition = spec['command'] == 'acquisition' and not any(spec['values'].get(k) for k in ('info', 'detect'))
        last_control = None
        while not interrupted:
            publish(folder / 'job.json', state)
            if (folder / 'abort.json').exists():
                interrupted = True
                break
            try:
                request_value = json.loads((folder / 'control.json').read_text())
            except FileNotFoundError:
                request_value = {}
            if request_value.get('id') != last_control and request_value.get('action'):
                last_control = request_value['id']
                state['control_ack'] = last_control
                state.setdefault('controls', []).append(
                    dict(request_value, utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
                )
                state['state'] = 'paused' if request_value['action'] == 'pause' else 'running'
            if state['state'] == 'paused':
                time.sleep(0.2)
                continue
            if acquisition and state['active_seconds'] >= spec['seconds']:
                code = 0
                break
            attempt = len(state['attempts']) + 1
            if attempt > 128:
                raise ValueError('Attempt budget exhausted; inspect capture logs before retrying')
            run_argv = list(argv)
            if acquisition:
                run_argv[run_argv.index('--output') + 1] = str(
                    folder / ('attempt-%03d' % attempt + spec['capture_extension'])
                )
                run_argv[run_argv.index('--elapsed') + 1] = str(max(0.01, spec['seconds'] - state['active_seconds']))
                if '--rejection-evidence' in run_argv:
                    run_argv[run_argv.index('--rejection-evidence') + 1] = str(folder / ('rejected-%03d' % attempt))
            started = time.monotonic()
            lease = int(os.environ.get('SPR_RADIO_LEASE_FD', '-1'))
            with (folder / 'job.log').open('ab', buffering=0) as log:
                child = subprocess.Popen(
                    run_argv,
                    cwd=repo,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    pass_fds=(lease,) if lease >= 3 else (),
                )
                reason = 'exit'
                next_publish = 0.0
                while child.poll() is None:
                    if interrupted or (folder / 'abort.json').exists():
                        interrupted = True
                        reason = 'abort'
                        break
                    try:
                        value = json.loads((folder / 'control.json').read_text())
                    except FileNotFoundError:
                        value = {}
                    if acquisition and value.get('id') != last_control and value.get('action') in ('pause', 'retry'):
                        reason = value['action']
                        break
                    if time.monotonic() >= next_publish:
                        publish(
                            folder / 'job.json',
                            dict(state, active_seconds=state['active_seconds'] + time.monotonic() - started),
                        )
                        next_publish = time.monotonic() + 1
                    # CPU tools have a bounded runtime too. Paused time is excluded.
                    if time.monotonic() - started > (
                        spec['seconds'] - state['active_seconds'] + 120 if acquisition else 7200
                    ):
                        reason = 'timeout'
                        break
                    time.sleep(0.2)
                _stop(child)
                code = child.returncode
            elapsed = time.monotonic() - started
            state['active_seconds'] += elapsed
            state['attempts'].append(dict(number=attempt, exit=code, elapsed_s=elapsed, reason=reason))
            if acquisition and reason in ('pause', 'retry'):
                continue
            if not acquisition or code != 0 or reason == 'timeout':
                break
        state.update(state='aborted' if interrupted else 'finished' if code == 0 else 'failed', exit=code)
        if acquisition and spec['capture_format'] == 'json':
            capture_state = state['state']
            state.update(state='reporting', capture_state=capture_state)
            publish(folder / 'job.json', state)
            report_errors = []
            if spec['anti_spur'] and not interrupted:
                try:
                    from soapypower.scan_spurs import analyze_files

                    state['spur_analysis'] = analyze_files(sorted(folder.glob('attempt-*.jsonl')), folder / 'analysis')
                except Exception as error:
                    report_errors.append('Spur analysis: ' + str(error))
            try:
                from soapypower.scan_report import generate

                state['capture_report'] = generate(sorted(folder.glob('attempt-*.jsonl')), folder / 'analysis')
            except Exception as error:
                report_errors.append('Capture report: ' + str(error))
            state['state'] = capture_state
            if report_errors:
                state.update(
                    report_errors=report_errors, state='report_failed' if capture_state == 'finished' else capture_state
                )
                if code == 0:
                    code = 1
                    state['exit'] = code
        return code if not interrupted else 130
    except BaseException as error:
        state.update(state='failed', error=str(error), exit=1)
        raise
    finally:
        if child is not None:
            _stop(child)
        state['finished_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        publish(folder / 'job.json', state)
        publish(Path(sessions) / 'expert-last.json', dict(id=ident, state=state['state']))
        (Path(sessions) / 'last-run.env').write_text(
            'SESSION=' + str(folder) + '\nRESULT=expert_' + state['state'] + '\n'
        )
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def worker(repo, sessions, request):
    """Honor the runner lease, and acquire one for a directly launched worker."""
    payload = json.loads(plistlib.loads(Path(request).read_bytes())['expert'])
    spec = plan(payload, sessions)
    if not spec['hardware'] or os.name == 'nt':
        return _worker(repo, sessions, request)
    import fcntl
    from soapypower.gui_service import file_lock

    lease = Path(sessions) / '.receiver.lock'
    inherited = os.environ.get('SPR_RADIO_LEASE_FD')
    old_sessions = os.environ.get('SPR_SESSIONS')
    try:
        os.environ['SPR_SESSIONS'] = str(sessions)
        if inherited is not None:
            try:
                fd = int(inherited)
                actual, expected = os.fstat(fd), lease.stat()
                if fd < 3 or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                    raise ValueError('Receiver lease identity differs')
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (ValueError, OSError) as error:
                raise ValueError('Invalid inherited receiver lease') from error
            return _worker(repo, sessions, request)
        try:
            with file_lock(lease, blocking=False) as stream:
                os.environ['SPR_RADIO_LEASE_FD'] = str(stream.fileno())
                return _worker(repo, sessions, request)
        except BlockingIOError as error:
            raise ValueError('Receiver is already leased by another capture') from error
    finally:
        if inherited is None:
            os.environ.pop('SPR_RADIO_LEASE_FD', None)
        if old_sessions is None:
            os.environ.pop('SPR_SESSIONS', None)
        else:
            os.environ['SPR_SESSIONS'] = old_sessions


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', type=Path, required=True)
    p.add_argument('--sessions', type=Path, required=True)
    p.add_argument('--request', type=Path)
    p.add_argument('--validate', action='store_true')
    a = p.parse_args()
    if a.validate:
        sys.path.insert(0, str(a.repo.resolve()))
        print(json.dumps(validate_cli(json.load(sys.stdin), a.sessions.resolve()), allow_nan=False))
    elif a.request is not None:
        sys.exit(worker(a.repo.resolve(), a.sessions.resolve(), a.request.resolve()))
    else:
        p.error('--request or --validate is required')
