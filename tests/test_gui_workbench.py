"""Expert workbench validation and worker tests; never opens RF hardware."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest import mock

import numpy as np
import pytest
from soapypower import gui_jobs as jobs
from soapypower.scan_spurs import analyze_files

ROOT = Path(__file__).resolve().parents[1]


def test_catalog_matches_actual_parsers():
    spec = importlib.util.spec_from_file_location('build_gui_catalog', ROOT / 'tools/gui/build_catalog.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert jobs.catalog() == module.generate()
    assert len(jobs.catalog()['commands']) == 44


def test_all_leaf_commands_plan_and_parse_without_execution(tmp_path):
    spec = importlib.util.spec_from_file_location('build_gui_catalog', ROOT / 'tools/gui/build_catalog.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dummy = tmp_path / 'input.json'
    dummy.write_text('{}')
    for command in jobs.catalog()['commands']:
        values = {}
        for o in command['options']:
            if o['required'] and o['role'] not in ('managed', 'output'):
                value = (
                    'input.json'
                    if o['role'] == 'input'
                    else (
                        o['choices'][0]
                        if o['choices']
                        else 1
                        if o['kind'] in ('integer', 'number')
                        else 'driver=uhd,type=b200'
                        if o['dest'] == 'device'
                        else 'reference'
                    )
                )
                values[o['dest']] = [value] if o['nargs'] == '+' else value
        if command['id'] == 'acquisition':
            values.update(device='driver=uhd,type=b200')
        planned = jobs.plan(dict(command=command['id'], values=values), tmp_path)
        parser = module.parser_for(command['module'])
        parser.parse_args(planned['argv'])


@pytest.mark.parametrize(
    'values',
    [
        {'device': '--output'},
        {'device': 'driver=uhd,type=b200', 'gain': True},
        {'device': 'driver=uhd,type=b200', 'gain': float('nan')},
        {'output': 'overwrite'},
        {'device': 'driver=uhd,type=b200', 'gain': 30, 'specific_gains': 'LNA=12'},
        {'device': 'driver=uhd,type=b200', 'format': '--injected'},
        {'unknown': 1},
        {'device': 'driver=rtlsdr'},
        {'source_hook': '/tmp/execute'},
    ],
)
def test_rejects_malformed_or_conflicting_options(tmp_path, values):
    with pytest.raises(ValueError):
        jobs.plan(dict(command='acquisition', values=values), tmp_path)


def test_artifacts_confined_and_never_overwritten(tmp_path):
    result = jobs.create_artifact(tmp_path, {'name': 'policy.json', 'data': {'x': 1}})
    assert jobs.artifact(tmp_path, result['path']).read_text().strip() == '{\n  "x": 1\n}'
    with pytest.raises(ValueError, match="already exists"):
        jobs.create_artifact(tmp_path, {'name': 'policy.json', 'data': {}})
    for path in ('../outside', '/tmp', 'missing'):
        with pytest.raises(ValueError):
            jobs.artifact(tmp_path, path)
    (tmp_path / 'link.json').symlink_to('/etc/hosts')
    with pytest.raises(ValueError):
        jobs.artifact(tmp_path, 'link.json')
    assert not any(i['path'] == 'link.json' for i in jobs.artifacts(tmp_path)['items'])


def test_experiment_roles_do_not_confuse_operator_declaration_with_file(tmp_path):
    spec = jobs.plan(
        {'command': 'experiments:plan', 'values': {'device': 'driver=uhd', 'input': 'terminated'}}, tmp_path
    )
    assert spec['argv'][-1] == 'terminated'
    assert not spec['hardware']
    suite = tmp_path / 'suite'
    suite.mkdir()
    spec = jobs.plan({'command': 'experiments:analyze', 'values': {'directory': 'suite'}}, tmp_path)
    assert spec['argv'][1] == str(suite)


def test_offline_job_through_real_queue_and_runner(tmp_path):
    payload = {'command': 'spurs:plan', 'values': {'count': 3}, 'runtime': 'generic'}
    ident = jobs.queue(payload, tmp_path)['job_id']
    env = dict(os.environ, SPR_REPO=str(ROOT), SPR_SESSIONS=str(tmp_path), PYTHONPATH=str(ROOT))
    # Sessions-local Python is what the production runner prefers. No downloads.
    venv = tmp_path / '.venv/bin'
    venv.mkdir(parents=True)
    import shlex

    launcher = venv / 'python'
    launcher.write_text('#!/bin/bash\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
    launcher.chmod(0o700)
    process = subprocess.run(
        [sys.executable, str(ROOT / 'tools/macos_runner/runner.py')],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    assert process.returncode == 0, process.stdout.decode()
    detail = jobs.job_detail(tmp_path, ident)
    assert detail['state'] == 'finished', (detail, process.stdout.decode())
    assert (tmp_path / ident / 'result.json').exists()
    assert json.loads((tmp_path / 'status' / (ident + '.json')).read_text())['exit'] == 0
    assert not (tmp_path / '.receiver.lock').exists()


def _wait(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.03)
    raise AssertionError('Worker did not reach expected state')


def test_pause_resume_retry_abort_keep_attempts(tmp_path):
    # Run the real worker/controller with a fake executable module in a child
    # process. No production receiver or native bindings are imported.
    repo = tmp_path / 'repo'
    repo.mkdir()
    fake = repo / 'fake_rx.py'
    fake.write_text('import time\nwhile True: time.sleep(.05)\n')
    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    payload = dict(command='acquisition', values={'device': 'driver=uhd,type=b200'}, seconds=30)
    ident = jobs.queue(payload, sessions)['job_id']
    request = sessions / 'queue' / (ident + '.json')
    original = jobs.plan

    def fake_plan(*args, **kwargs):
        spec = original(*args, **kwargs)
        spec['module'] = 'fake_rx'
        return spec

    result = []
    # worker installs signals, so must run in main thread; controller is the thread.
    errors = []

    def controller():
        try:
            _wait(lambda: (sessions / ident / 'job.json').exists())
            jobs.control(sessions, ident, 'pause')
            _wait(lambda: jobs.job_detail(sessions, ident)['state'] == 'paused')
            jobs.control(sessions, ident, 'resume')
            _wait(lambda: jobs.job_detail(sessions, ident).get('state') == 'running')
            _wait(lambda: len(jobs.job_detail(sessions, ident).get('attempts', [])) >= 1)
            jobs.control(sessions, ident, 'retry')
            _wait(lambda: len(jobs.job_detail(sessions, ident).get('attempts', [])) >= 2)
            jobs.control(sessions, ident, 'abort')
        except Exception as e:
            errors.append(e)
            if (sessions / ident).exists():
                jobs.publish(sessions / ident / 'abort.json', {'action': 'abort'})

    with mock.patch.object(jobs, 'plan', fake_plan):
        t = threading.Thread(target=controller)
        t.start()
        # Acquisition parser validates the underlying arguments before fake_rx.
        result.append(jobs.worker(repo, sessions, request))
        t.join(timeout=12)
    assert not errors, errors
    assert result == [130]
    detail = jobs.job_detail(sessions, ident)
    assert detail['state'] == 'aborted'
    assert len(detail['attempts']) >= 2
    assert (sessions / ident / 'abort.json').exists()


def record(center, gain=30, n=256):
    rate = 2e6
    step = rate / n
    rng = np.random.default_rng(int(center))
    power = rng.gamma(16, 1 / 16, n)
    power[30] = 100
    power = power.tolist()
    power[n // 2] = None
    return dict(
        power=power,
        unit='input power/Hz',
        time_start='2026-11-26T00:00:00',
        metadata=dict(
            measurement_status='complete',
            continuity='verified',
            context={'gains': {'LNA': gain}},
            frequency_grid={'rf_origin_hz': center, 'step_hz': step, 'first_bin_index': -n // 2, 'bin_count': n},
            tuned_center_hz=center,
            sample_rate_hz=rate,
            window='blackman',
            segment_count=16,
        ),
    )


def test_measured_spurs_keep_masks_and_separate_gain_contexts(tmp_path):
    source = tmp_path / 'capture.jsonl'
    rows = [record(center) for center in (100.1e6, 100.3e6, 100.5e6, 100.7e6)]
    rows += [record(100.2e6, gain=40)]
    source.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    before = source.read_bytes()
    result = analyze_files([source], tmp_path / 'analysis')
    assert result['groups']
    assert result['examined_records'] == len(rows)
    assert source.read_bytes() == before
    assert any('fewer than three' in key for key in result['skipped'])
    for g in result['groups']:
        assert not g['if_start_hz'] <= 0 <= g['if_stop_hz']
        assert g['views'] == 4
    assert (tmp_path / 'analysis/index.html').exists()


def test_control_uses_sticky_abort_and_reconciles_crashed_runner(tmp_path):
    ident = 'expert-20261126-230000-12345678'
    folder = tmp_path / ident
    folder.mkdir()
    jobs.publish(folder / 'job.json', {'state': 'running', 'command': 'acquisition'})
    jobs.control(tmp_path, ident, 'abort')
    with pytest.raises(ValueError):
        jobs.control(tmp_path, ident, 'resume')
    assert (folder / 'abort.json').exists()
    status = tmp_path / 'status'
    status.mkdir()
    jobs.publish(status / (ident + '.json'), {'state': 'interrupted'})
    assert jobs.job_detail(tmp_path, ident)['state'] == 'interrupted'
    with pytest.raises(ValueError):
        jobs.control(tmp_path, ident, 'retry')


def test_high_resolution_report_preserves_data_and_reports_guard_limits(tmp_path):
    from soapypower.scan_report import generate

    source = tmp_path / 'attempt-001.jsonl'
    rows = [record(100.1e6 + i * 1e5) for i in range(5)]
    for row in rows:
        row.update(first_bin_center_hz=row['metadata']['tuned_center_hz'] - 1e6, step_hz=2e6 / 256)
    source.write_text(''.join(json.dumps(r) + '\n' for r in rows) + '{"incomplete":')
    before = source.read_bytes()
    result = generate([source], tmp_path / 'analysis')
    assert source.read_bytes() == before
    assert result['attempts'][0]['records'] == 5
    assert result['attempts'][0]['all_guards_passed'] == 0
    assert result['attempts'][0]['trailing_fragment']
    from PIL import Image

    with Image.open(tmp_path / 'analysis/attempt-001-waterfall.png') as image:
        assert image.size[0] >= 3900


def test_experiment_trial_inherits_receiver_lease(tmp_path, monkeypatch):
    from soapypower.experiments import subprocess_runner

    lease = tmp_path / '.receiver.lock'
    fd = os.open(lease, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        monkeypatch.setenv('SPR_RADIO_LEASE_FD', str(fd))
        monkeypatch.setenv('SPR_SESSIONS', str(tmp_path))
        target = tmp_path / 'inherited'
        script = 'import os,sys;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.fstat(int(os.environ["SPR_RADIO_LEASE_FD"])).st_ino))'
        result = subprocess_runner({}, [sys.executable, '-c', script, str(target)], None, 5)
        assert result['returncode'] == 0
        assert int(target.read_text()) == lease.stat().st_ino
    finally:
        os.close(fd)


def test_non_json_formats_are_partitioned_without_false_live_reports(tmp_path):
    spec = jobs.plan(
        {'command': 'acquisition', 'values': {'device': 'driver=uhd,type=b200', 'format': 'soapy_power_bin_v3'}},
        tmp_path,
    )
    assert spec['capture_extension'] == '.bin'
    assert spec['argv'][spec['argv'].index('--output') + 1].endswith('capture.bin')
    with pytest.raises(ValueError, match='requires JSON'):
        jobs.plan(
            {
                'command': 'acquisition',
                'anti_spur': True,
                'values': {'device': 'driver=uhd,type=b200', 'format': 'soapy_power_bin_v3'},
            },
            tmp_path,
        )


def test_direct_worker_cannot_bypass_receiver_lock(tmp_path):
    import fcntl

    payload = dict(command='acquisition', values={'device': 'driver=uhd,type=b200'})
    ident = jobs.queue(payload, tmp_path)['job_id']
    request = tmp_path / 'queue' / (ident + '.json')
    with (tmp_path / '.receiver.lock').open('w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match='already leased'):
            jobs.worker(ROOT, tmp_path, request)
    assert not (tmp_path / ident).exists()
