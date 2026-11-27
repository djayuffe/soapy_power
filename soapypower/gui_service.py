"""Manage the local GUI process without trusting an occupied port or a bare PID."""

import argparse
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid


def atomic_json(path, value):
    temp = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        with temp.open('x', encoding='utf-8') as stream:
            os.chmod(temp, 0o600)
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextlib.contextmanager
def file_lock(path, blocking=True):
    """A kernel lock survives stale files and is shared across GUI processes."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'r+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield stream
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def read_state(path):
    try:
        with path.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            return {}
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def process_stamp(pid):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return None
    try:
        result = subprocess.run(
            ['ps', '-p', str(pid), '-o', 'lstart=', '-o', 'command='],
            capture_output=True,
            text=True,
            timeout=2,
            env={**os.environ, 'LC_ALL': 'C'},
        )
        line = result.stdout.strip()
        return line if result.returncode == 0 and 'soapypower.gui ' in line else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def health(port):
    try:
        # Never inherit HTTP proxy settings for a local service health check.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:{}/api/health'.format(port), timeout=0.5) as response:
            value = json.loads(response.read(8193))
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError, urllib.error.URLError):
        return {}


def matches(state):
    stamp = process_stamp(state.get('pid'))
    return bool(stamp and stamp == state.get('process_stamp') and state.get('instance_id') in stamp)


def stop_process(state, grace=8):
    if not matches(state):
        raise RuntimeError('Stored GUI process identity is no longer valid; refusing to signal that PID')
    os.kill(state['pid'], signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not matches(state):
            return
        time.sleep(0.05)
    if matches(state):
        os.kill(state['pid'], signal.SIGKILL)
    deadline = time.monotonic() + 2
    while matches(state) and time.monotonic() < deadline:
        time.sleep(0.05)
    if matches(state):
        raise RuntimeError('GUI did not stop; process metadata retained')


def manage(action, repo, sessions, port, startup_timeout=10):
    repo, sessions = Path(repo).resolve(), Path(sessions).resolve()
    sessions.mkdir(parents=True, exist_ok=True)
    statefile, pidfile = sessions / 'gui-service.json', sessions / 'gui.pid'
    with file_lock(sessions / '.gui-service.lock'):
        state = read_state(statefile)
        if action == 'status':
            if not state and pidfile.exists():
                try:
                    legacy = int(pidfile.read_text().strip())
                except (OSError, ValueError):
                    legacy = None
                if process_stamp(legacy):
                    return dict(state='gui_unmanaged', pid=legacy, port=None)
            owned = matches(state)
            observed = health(state.get('port')) if owned else {}
            running = owned and observed.get('instance_id') == state.get('instance_id')
            return dict(
                state='gui_running' if running else 'gui_unresponsive' if owned else 'gui_stopped',
                pid=state.get('pid') if owned else None,
                port=state.get('port'),
            )
        if state and (state.get('repo') not in (None, str(repo)) or state.get('sessions') not in (None, str(sessions))):
            raise RuntimeError('GUI metadata belongs to a different checkout or session directory')
        if not state and pidfile.exists():
            try:
                legacy = int(pidfile.read_text().strip())
            except (OSError, ValueError):
                legacy = None
            if process_stamp(legacy):
                raise RuntimeError(
                    'Legacy GUI is running without instance metadata; stop it explicitly before migration'
                )
            pidfile.unlink(missing_ok=True)
        if action == 'stop':
            if state:
                if matches(state):
                    stop_process(state)
                elif process_stamp(state.get('pid')):
                    raise RuntimeError('GUI PID belongs to another instance; metadata retained')
            elif pidfile.exists():
                raise RuntimeError('Legacy GUI PID has no verified instance metadata; stop that GUI explicitly')
            statefile.unlink(missing_ok=True)
            pidfile.unlink(missing_ok=True)
            return dict(state='gui_stopped')
        if state and matches(state):
            observed = health(state.get('port'))
            if state.get('port') != port:
                raise RuntimeError('GUI is already running on port {}'.format(state.get('port')))
            if observed.get('instance_id') == state.get('instance_id'):
                return dict(state='gui_running', url='http://127.0.0.1:{}'.format(port), pid=state['pid'])
            raise RuntimeError('Existing GUI process is unresponsive; stop it before restarting')
        if state and process_stamp(state.get('pid')):
            raise RuntimeError('GUI identity changed; refusing to overwrite process metadata')
        instance = uuid.uuid4().hex
        logfile = sessions / 'gui-startup.log'
        if logfile.exists() and logfile.stat().st_size > 512 * 1024:
            os.replace(logfile, sessions / 'gui-startup.previous.log')
        env = {k: v for k, v in os.environ.items() if not k.startswith(('DYLD_', 'SOAPY_', 'PYTHONPATH'))}
        with logfile.open('ab') as output:
            child = subprocess.Popen(
                [
                    sys.executable,
                    '-m',
                    'soapypower.gui',
                    '--repo',
                    str(repo),
                    '--sessions',
                    str(sessions),
                    '--port',
                    str(port),
                    '--no-browser',
                    '--instance-id',
                    instance,
                    '--log-file',
                    str(sessions / 'gui.log'),
                ],
                cwd=repo,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )
        started = False
        try:
            deadline = time.monotonic() + startup_timeout
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError('GUI exited during startup (exit {}); see gui.log'.format(child.returncode))
                observed = health(port)
                stamp = process_stamp(child.pid)
                if observed.get('instance_id') == instance and observed.get('pid') == child.pid and stamp:
                    state = dict(
                        pid=child.pid,
                        process_stamp=stamp,
                        instance_id=instance,
                        port=port,
                        repo=str(repo),
                        sessions=str(sessions),
                    )
                    atomic_json(statefile, state)
                    temp = pidfile.with_name('.gui.pid-' + instance)
                    temp.write_text(str(child.pid), encoding='ascii')
                    os.replace(temp, pidfile)
                    started = True
                    return dict(state='gui_started', url='http://127.0.0.1:{}'.format(port), pid=child.pid)
                time.sleep(0.1)
            raise RuntimeError('GUI startup timed out; see gui.log')
        finally:
            if not started and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop', 'status'))
    parser.add_argument('--repo', required=True)
    parser.add_argument('--sessions', required=True)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be 1024–65535')
    try:
        result = manage(args.action, args.repo, args.sessions, args.port)
        print(json.dumps(result), flush=True)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
