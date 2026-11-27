"""Boundary and failure tests for the receive console and its process manager."""

import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from unittest import mock

import pytest
from soapypower import gui, gui_service

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def server(tmp_path):
    calls = []
    control = gui.Control(ROOT, tmp_path, queue=lambda args: calls.append(args) or 'test-job')
    control.calls = calls
    http = gui.Server(('127.0.0.1', 0), control)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield control, 'http://127.0.0.1:' + str(http.server_port)
    http.shutdown()
    http.server_close()
    thread.join()


def get(url, headers=None, method=None):
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}, method=method), timeout=5)


def post(control, url, path, data, request_id=None):
    headers = {'Content-Type': 'application/json', 'X-CSRF-Token': control.token}
    if request_id:
        headers['X-Request-ID'] = request_id
    return urllib.request.urlopen(
        urllib.request.Request(url + path, data=json.dumps(data).encode(), headers=headers), timeout=5
    )


def test_parseable_but_unterminated_record_is_not_published(tmp_path):
    path = tmp_path / 'raw.jsonl'
    old = dict(power=[1], metadata={}, time_stop='old')
    new = dict(power=[2], metadata={}, time_stop='new')
    path.write_text(json.dumps(old) + '\n' + json.dumps(new))
    assert gui.record_at_end(path)['time_stop'] == 'old'
    path.write_text(json.dumps(new))
    assert gui.record_at_end(path) is None


def test_schema_damage_does_not_break_session_catalog(server):
    control, url = server
    folder = control.sessions / 'superscan-20261126-120000'
    folder.mkdir()
    (folder / 'captures.json').write_text(
        json.dumps(dict(status='completed', captures=[None, 'bad', {'records': 0}], attempts=None))
    )
    state = json.load(get(url + '/api/state'))
    assert state['sessions'][0]['records'] == 0
    assert state['sessions'][0]['attempts'] == 0


def test_masked_bins_and_nonfinite_metadata(server):
    control, url = server
    folder = control.sessions / 'superscan-20261126-120000'
    folder.mkdir()
    row = dict(
        power=[-100, -10, -99],
        masked_bins=[1],
        time_start='2026-11-26T12:00:00+00:00',
        time_stop='2026-11-26T12:00:01+00:00',
        first_bin_center_hz=100,
        step_hz=10,
        metadata=dict(context={}, rf_state={'after': {'validity': {'temperature_c': float('nan')}}}),
    )
    (folder / 'superscan.jsonl').write_text(json.dumps(row) + '\n')
    result = json.load(get(url + '/api/session?id=' + folder.name))
    assert result['live']['power'] == [-100, None, -99]
    assert result['live']['frequencies_hz'] == [100, 110, 120]
    assert result['live']['temperature_c'] is None and not result['live']['qualified']


def test_http_streaming_ranges_and_head(server):
    c, url = server
    p = c.sessions / 'superscan-20261126-120000' / 'analysis'
    p.mkdir(parents=True)
    payload = b'%PDF-' + b'a' * 200000
    (p / 'test.pdf').write_bytes(payload)
    target = url + '/files/' + p.parent.name + '/analysis/test.pdf'
    response = get(target, {'Range': 'bytes=123-456'})
    assert response.status == 206 and response.read() == payload[123:457]
    assert response.headers['Content-Range'] == 'bytes 123-456/200005'
    response = get(target, method='HEAD')
    assert response.read() == b'' and int(response.headers['Content-Length']) == len(payload)
    with pytest.raises(urllib.error.HTTPError) as error:
        get(target, {'Range': 'bytes=999999-'})
    assert error.value.code == 416


def test_repeated_mutation_id_is_not_reexecuted(server):
    control, url = server
    ident = 'a' * 32
    assert json.load(post(control, url, '/api/scan', {'duration_s': 30}, ident))['state'] == 'queued'
    assert json.load(post(control, url, '/api/scan', {'duration_s': 30}, ident))['state'] == 'queued'
    assert len(control.calls) == 1
    with pytest.raises(urllib.error.HTTPError) as error:
        post(control, url, '/api/scan', {'duration_s': 60}, ident)
    assert error.value.code == 400 and len(control.calls) == 1


def test_retry_attempt_cannot_turn_into_a_new_scan(server):
    control, url = server
    p = control.sessions / 'superscan-20261126-120000'
    p.mkdir()
    (p / 'captures.json').write_text('{"status":"completed"}')
    with pytest.raises(urllib.error.HTTPError) as error:
        post(control, url, '/api/retry', {'session': p.name, 'mode': 'attempt'})
    assert error.value.code == 409 and not control.calls


def test_stale_supervisor_is_not_controllable(tmp_path):
    p = tmp_path / 'superscan-20261126-120000'
    p.mkdir()
    (p / 'captures.json').write_text(
        json.dumps(dict(status='running', supervisor_pid=1, control_version=1, supports_stop_request=True))
    )
    control = gui.Control(ROOT, tmp_path)
    assert control.item(p)['status'] == 'interrupted'
    result = control.detail(p.name)
    assert not result['can_stop'] and not result['can_pause']
    with pytest.raises(RuntimeError):
        control.operate(p.name, 'abort')
    assert not (p / 'stop-request.json').exists()


def test_history_and_summary_cache_are_bounded(tmp_path):
    control = gui.Control(ROOT, tmp_path, queue=lambda _: 'test')
    for i in range(40):
        p = tmp_path / ('superscan-test-' + str(i))
        (p / 'analysis').mkdir(parents=True)
        (p / 'analysis/summary.json').write_text('{"records":0,"large_unused_field":[1,2,3]}')
        (p / 'superscan.jsonl').write_text(
            json.dumps(dict(power=[-100], time_start='a', time_stop='b', metadata={})) + '\n'
        )
        control.detail(p.name)
    assert len(control.history) <= 12 and len(control.report_cache) <= 32
    assert all('large_unused_field' not in value[1] for value in control.report_cache.values())


def test_gui_service_refuses_pid_reuse(tmp_path):
    state = dict(pid=100, process_stamp='old process', instance_id='a' * 32, port=8765)
    gui_service.atomic_json(tmp_path / 'gui-service.json', state)
    with (
        mock.patch.object(gui_service, 'process_stamp', return_value='another soapypower.gui process'),
        mock.patch.object(os, 'kill') as kill,
    ):
        with pytest.raises(RuntimeError):
            gui_service.manage('stop', ROOT, tmp_path, 8765)
        kill.assert_not_called()
    assert (tmp_path / 'gui-service.json').exists()


def test_runner_validates_full_schema_before_execution():
    spec = importlib.util.spec_from_file_location('audited_runner', ROOT / 'tools/macos_runner/runner.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for payload in [
        dict(mode='scan', scan_seconds=4000),
        dict(mode='scan', gain_db=71),
        dict(mode='scan', gain_db=float('nan')),
        dict(mode='scan', gui='start', superscan=1),
        dict(mode='scan', device='rtlsdr'),
        dict(mode='scan', superscan_gain='77'),
        dict(mode='scan', gui_port='8765'),
        dict(mode='scan', superscan=True),
        dict(mode='scan', unknown='value'),
    ]:
        with pytest.raises(ValueError):
            module.validate(payload)


def test_service_does_not_adopt_an_unrelated_http_server(tmp_path):
    other = gui.Control(ROOT, tmp_path / 'other', queue=lambda _: 'unused')
    http = gui.Server(('127.0.0.1', 0), other)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(RuntimeError):
            gui_service.manage('start', ROOT, tmp_path / 'service', http.server_port, startup_timeout=2)
        assert gui_service.health(http.server_port)['instance_id'] == other.instance_id
        assert not (tmp_path / 'service/gui-service.json').exists()
        assert not (tmp_path / 'service/gui.pid').exists()
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def test_busy_server_rejects_excess_connections_and_recovers(tmp_path):
    control = gui.Control(ROOT, tmp_path, queue=lambda _: 'unused')
    http = gui.Server(('127.0.0.1', 0), control)
    http.slots = threading.BoundedSemaphore(2)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    clients = []
    try:
        for _ in range(2):
            sock = socket.create_connection(http.server_address, timeout=2)
            clients.append(sock)
            sock.sendall(
                (
                    'POST /api/scan HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nX-CSRF-Token: {}\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n{{'.format(
                        http.server_port, control.token
                    )
                ).encode()
            )
        time.sleep(0.1)
        with pytest.raises(urllib.error.HTTPError) as error:
            get('http://127.0.0.1:{}/api/health'.format(http.server_port))
        assert error.value.code == 503
        for client in clients:
            client.close()
        clients = []
        deadline = time.monotonic() + 3
        while True:
            try:
                assert (
                    json.load(get('http://127.0.0.1:{}/api/health'.format(http.server_port)))['service']
                    == 'soapy_power'
                )
                break
            except urllib.error.HTTPError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.03)
    finally:
        for client in clients:
            client.close()
        http.shutdown()
        http.server_close()
        thread.join()


def test_two_gui_instances_admit_only_one_queued_job(tmp_path):
    controls = [gui.Control(ROOT, tmp_path) for _ in range(2)]
    for control in controls:
        control.ready = lambda: True
    barrier = threading.Barrier(2)
    answers = []

    def submit(control):
        barrier.wait()
        try:
            answers.append(control.enqueue(gui.scan_arguments({'duration_s': 30})))
        except RuntimeError:
            answers.append('busy')

    threads = [threading.Thread(target=submit, args=(control,)) for control in controls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert sum(isinstance(answer, dict) for answer in answers) == 1 and answers.count('busy') == 1
    assert len(list((tmp_path / 'queue').glob('*.json'))) == 1


def test_control_mailbox_is_serialized_across_gui_instances(tmp_path):
    folder = tmp_path / 'superscan-20261126-120000'
    folder.mkdir()
    (folder / 'captures.json').write_text(
        json.dumps(dict(status='running', control_version=1, supports_stop_request=True))
    )
    controls = [gui.Control(ROOT, tmp_path) for _ in range(2)]
    for control in controls:
        control.supervisor_alive = lambda *_: True
    barrier = threading.Barrier(2)
    answers = []

    def pause(control):
        barrier.wait()
        try:
            answers.append(control.operate(folder.name, 'pause'))
        except RuntimeError:
            answers.append('pending')

    threads = [threading.Thread(target=pause, args=(c,)) for c in controls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert len(answers) == 2 and answers.count('pending') == 1


def test_loaded_runner_for_another_directory_is_not_ready(tmp_path):
    control = gui.Control(ROOT, tmp_path)
    result = subprocess.CompletedProcess([], 0, '\tSPR_REPO => /other/checkout\n\tSPR_SESSIONS => /other/data\n', '')
    with (
        mock.patch.object(gui.sys, 'platform', 'darwin'),
        mock.patch.object(gui.subprocess, 'run', return_value=result),
    ):
        assert not control.ready()
    assert 'different' in control.ready_reason


def test_loaded_runner_paths_are_verified(tmp_path):
    control = gui.Control(ROOT, tmp_path)
    result = subprocess.CompletedProcess(
        [], 0, '\tSPR_REPO => ' + str(ROOT) + '\n\tSPR_SESSIONS => ' + str(tmp_path) + '\n', ''
    )
    with (
        mock.patch.object(gui.sys, 'platform', 'darwin'),
        mock.patch.object(gui.subprocess, 'run', return_value=result),
    ):
        assert control.ready()


def test_frontend_bindings_have_matching_elements():
    from html.parser import HTMLParser
    import re

    class Elements(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = set()

        def handle_starttag(self, tag, attrs):
            self.ids.update(value for key, value in attrs if key == 'id')

    parser = Elements()
    parser.feed((ROOT / 'soapypower/web/index.html').read_text(encoding='utf-8'))
    script = (ROOT / 'soapypower/web/app.js').read_text(encoding='utf-8')
    names = set(re.findall(r"\$\('([^']+)'\)", script)) | set(re.findall(r"put\('([^']+)'", script))
    assert (
        not (names | {'peak-limit', 'p999-limit', 'peak-meter', 'p999-meter', 'peak-value', 'p999-value'}) - parser.ids
    )


def test_expert_http_plan_artifact_and_job_flow(server):
    c, url = server
    options = json.load(get(url + '/api/options'))
    assert len(options['commands']) == 44
    data = {'command': 'spurs:plan', 'values': {'count': 3}}
    plan = json.load(post(c, url, '/api/plan', data))
    assert not plan['hardware'] and '--out' in plan['argv']
    artifact = json.load(post(c, url, '/api/artifact', {'name': 'settings.json', 'data': {'x': 1}}))
    assert artifact['path'] == 'inputs/settings.json'
    assert any(x['path'] == artifact['path'] for x in json.load(get(url + '/api/artifacts'))['items'])
    response = get(url + '/artifacts/inputs/settings.json')
    assert json.load(response) == {'x': 1}
    assert 'sandbox' in response.headers['Content-Security-Policy']
    answer = json.load(post(c, url, '/api/expert', data, 'c' * 32))
    again = json.load(post(c, url, '/api/expert', data, 'c' * 32))
    assert answer == again
    assert len(list((c.sessions / 'queue').glob('*.json'))) == 1
    assert json.load(get(url + '/api/expert?id=' + answer['job_id']))['state'] == 'queued'
    with pytest.raises(urllib.error.HTTPError) as error:
        post(c, url, '/api/expert', data)
    assert error.value.code == 409


def test_expert_artifact_download_cannot_escape_sessions(server):
    c, url = server
    (c.sessions / 'outside.json').symlink_to('/etc/hosts')
    with pytest.raises(urllib.error.HTTPError) as error:
        get(url + '/artifacts/outside.json')
    assert error.value.code == 404


def test_expert_rejects_invalid_dsp_before_queue(server):
    c, url = server
    with pytest.raises(urllib.error.HTTPError) as error:
        post(c, url, '/api/plan', {'command': 'acquisition', 'values': {'device': 'driver=uhd,type=b200', 'bins': 0}})
    assert error.value.code == 400
    assert not list((c.sessions / 'queue').glob('*.json'))


def test_expert_raw_rows_are_not_claimed_as_accepted(server):
    c, url = server
    folder = c.sessions / 'expert-20261126-230000-12345678'
    folder.mkdir()
    (folder / 'job.json').write_text(
        json.dumps(
            {
                'state': 'finished',
                'command': 'acquisition',
                'capture_report': {'attempts': [{'records': 10, 'all_guards_passed': 0}]},
            }
        )
    )
    state = json.load(get(url + '/api/state'))
    assert state['sessions'][0]['records'] == 0
    assert state['sessions'][0]['credited_s'] is None
    (folder / 'job.log').write_text('retained log')
    assert get(url + '/artifacts/' + folder.name + '/job.log').read() == b'retained log'


def test_runtime_stack_folders_are_not_measurement_sessions(server):
    c, url = server
    for name in ('bladerf-stack', 'bladerf-profile'):
        (c.sessions / name).mkdir()
    assert json.load(get(url + '/api/state'))['sessions'] == []


def test_satellite_routes_validate_observer_and_never_queue_rx(server):
    c, url = server
    with mock.patch.object(c.satellites, 'snapshot', return_value={'status': 'loading', 'curves': []}) as calculate:
        answer = json.load(get(url + '/api/satellites?lo_mhz=140&hi_mhz=150&seconds=3600'))
        assert answer['status'] == 'loading'
        assert calculate.call_args.args[0]['lo_hz'] == 140e6
    qth = {'lat_deg': 0.0, 'lon_deg': 0.0, 'alt_m': 10.0, 'min_elevation_deg': 5.0}
    assert json.load(post(c, url, '/api/satellites/observer', qth))['state'] == 'saved'
    newer = dict(qth, alt_m=20.0)
    post(c, url, '/api/satellites/observer', newer).close()
    assert json.loads((c.sessions / 'qth.json').read_text()) == newer
    assert len(list((c.sessions / '.observer-history').glob('*.json'))) == 1
    assert (c.sessions / 'qth.json').stat().st_mode & 0o777 == 0o600
    assert (c.sessions / '.observer-history').stat().st_mode & 0o777 == 0o700
    with pytest.raises(urllib.error.HTTPError):
        post(c, url, '/api/satellites/observer', dict(qth, lat_deg=True))
    with mock.patch.object(c.satellites, 'refresh', return_value={'state': 'starting'}) as refresh:
        assert json.load(post(c, url, '/api/satellites/refresh', {}))['state'] == 'starting'
        assert refresh.call_count == 1
    with pytest.raises(urllib.error.HTTPError):
        post(c, url, '/api/satellites/refresh', {'url': 'https://example.com'})
    with mock.patch.object(c.satellites, 'prepare', return_value={'command': 'acquisition'}) as prepare:
        payload = {'norad': 99001, 'transmitter_id': 'test-tx', 'seconds': 300}
        assert json.load(post(c, url, '/api/satellites/prepare', payload))['command'] == 'acquisition'
        prepare.assert_called_once_with(payload)
    assert not c.calls
    assert not list((c.sessions / 'queue').glob('*.json'))
