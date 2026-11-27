"""Runner crash/lock regressions with shell stubs; no RF hardware or launchd."""

import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import time
import pytest

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='POSIX runner locks and process groups')
ROOT = Path(__file__).resolve().parents[1]


def wait(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.025)
    raise AssertionError('runner state did not arrive')


def status(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


@pytest.fixture
def setup_runner(tmp_path):
    repo = tmp_path / 'repo'
    sessions = tmp_path / 'sessions'
    queue = sessions / 'queue'
    queue.mkdir(parents=True)
    scripts = repo / 'tools/macos_runner'
    scripts.mkdir(parents=True)
    stub = scripts / 'run_session.sh'
    stub.write_text('''#!/bin/bash
printf '%s\\n' "$$" > "$SPR_SESSIONS/child.pid"
printf 'run\\n' >> "$SPR_SESSIONS/executed"
while [ -f "$SPR_SESSIONS/hold" ];do sleep .05;done
printf 'RESULT=ok\\n' > "$SPR_SESSIONS/last-run.env"
''')
    env = dict(os.environ, SPR_REPO=str(repo), SPR_SESSIONS=str(sessions))

    def queue_job(name):
        (queue / (name + '.json')).write_bytes(plistlib.dumps(dict(mode='scan')))

    def start():
        return subprocess.Popen(
            [sys.executable, str(ROOT / 'tools/macos_runner/runner.py')],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    yield sessions, queue_job, start
    (sessions / 'hold').unlink(missing_ok=True)
    try:
        pid = int((sessions / 'child.pid').read_text())
        os.killpg(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass


def test_sigterm_publishes_interrupted_and_preserves_taken_request(setup_runner):
    sessions, queue, start = setup_runner
    (sessions / 'hold').write_text('')
    queue('one')
    process = start()
    wait(lambda: status(sessions / 'status/one.json').get('state') == 'running')
    process.terminate()
    assert process.wait(timeout=12) == 130
    result = status(sessions / 'status/one.json')
    assert result['state'] == 'interrupted' and result['exit'] == 130
    assert len(list((sessions / 'queue/taken').glob('one-*.json'))) == 1


def test_sigkill_does_not_release_hardware_lock_while_child_is_alive(setup_runner):
    sessions, queue, start = setup_runner
    (sessions / 'hold').write_text('')
    queue('one')
    first = start()
    wait(lambda: status(sessions / 'status/one.json').get('state') == 'running')
    queue('two')
    first.kill()
    assert first.wait(timeout=5) == -signal.SIGKILL
    second = start()
    assert second.wait(timeout=5) == 0
    assert (sessions / 'queue/two.json').exists()
    assert (sessions / 'executed').read_text().splitlines() == ['run']
    (sessions / 'hold').unlink()
    import fcntl

    def released():
        with (sessions / '.runner.lock').open() as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False

    wait(released)
    third = start()
    assert third.wait(timeout=5) == 0
    assert status(sessions / 'status/one.json')['state'] == 'interrupted'
    assert status(sessions / 'status/two.json')['state'] == 'finished'
    assert (sessions / 'executed').read_text().splitlines() == ['run', 'run']


def test_duplicate_runners_do_not_execute_a_request_twice(setup_runner):
    sessions, queue, start = setup_runner
    (sessions / 'hold').write_text('')
    queue('one')
    first = start()
    wait(lambda: status(sessions / 'status/one.json').get('state') == 'running')
    second = start()
    assert second.wait(timeout=5) == 0
    (sessions / 'hold').unlink()
    assert first.wait(timeout=5) == 0
    assert (sessions / 'executed').read_text().splitlines() == ['run']


def test_killed_supervisor_cannot_leave_an_unlocked_receiver(setup_runner, tmp_path):
    sessions, queue, start = setup_runner
    fake = tmp_path / 'fake_rx.py'
    hold = tmp_path / 'hold-rx'
    hold.write_text('')
    pidfile = tmp_path / 'rx.pid'
    fake.write_text('''import os,sys,time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
Path(sys.argv[sys.argv.index('-O')+1]).write_text('')
while Path(sys.argv[2]).exists():time.sleep(.03)
''')
    script = "import sys;sys.path.insert(0,sys.argv[1]);import superscan_run as r; r.supervise(sys.argv[2],[sys.executable,sys.argv[3],sys.argv[4],sys.argv[5]],10,60)"
    supervisor = subprocess.Popen(
        [
            sys.executable,
            '-c',
            script,
            str(ROOT / 'tools'),
            str(sessions / 'superscan-test'),
            str(fake),
            str(pidfile),
            str(hold),
        ]
    )
    try:
        wait(lambda: pidfile.exists())
        supervisor.kill()
        supervisor.wait(timeout=5)
        queue('pending')
        runner = start()
        assert runner.wait(timeout=5) == 0
        assert (sessions / 'queue/pending.json').exists() and not (sessions / 'executed').exists()
        hold.unlink()
        import fcntl

        def released():
            with (sessions / '.receiver.lock').open() as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except BlockingIOError:
                    return False

        wait(released)
        runner = start()
        assert runner.wait(timeout=5) == 0
        assert status(sessions / 'status/pending.json')['state'] == 'finished'
    finally:
        hold.unlink(missing_ok=True)
        if supervisor.poll() is None:
            supervisor.terminate()
            supervisor.wait(timeout=8)
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGTERM)
            except OSError:
                pass


def test_large_failure_log_is_classified_without_loading_the_whole_file(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location('audit_supervisor', ROOT / 'tools/superscan_run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / 'log'
    path.write_bytes(b'x' * 65530 + b'SDR read failed or timed out' + b'x' * 1000000)
    assert module.classify_log(2, path) == 'transport'
    with path.open('ab') as stream:
        stream.write(b'headroom rejected; original IQ retained')
    assert module.classify_log(2, path) == 'headroom'
