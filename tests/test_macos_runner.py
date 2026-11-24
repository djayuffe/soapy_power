"""tools/macos_runner: request validation and hand-off (POSIX shell; no launchd, no hardware)."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'tools' / 'macos_runner'
FAKE_PLUTIL = '''#!{python}
import plistlib, sys
args = sys.argv[1:]
try:
    data = plistlib.load(open(args[-1], 'rb'))
except Exception:
    sys.exit(1)
if args[0] == '-lint':
    sys.exit(0)
if args[0] == '-extract' and isinstance(data, dict) and args[1] in data:
    print(data[args[1]])
    sys.exit(0)
sys.exit(1)
'''
STUB_SESSION = '''#!/bin/bash
env | grep -E '^(MODE|SCAN_SECONDS|GAIN_DB|ANTENNA|INPUT|PROCEDURE|REQUEST_FILE)=' | sort > "$SPR_SESSIONS/seen-$MODE"
printf 'STAMP=x\\nLOG=%s/stub.log\\nRESULT=%s_ok\\n' "$SPR_SESSIONS" "$MODE" > "$SPR_SESSIONS/last-run.env"
'''


@unittest.skipIf(os.name == 'nt' or shutil.which('bash') is None, 'POSIX bash scripts')
class MacosRunnerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.bin, self.app, self.repo = root / 'bin', root / 'app', root / 'repo'
        self.sessions = self.repo / 'sessions'
        for d in (self.bin, self.app, self.repo / 'tools' / 'macos_runner', self.sessions / 'queue'):
            d.mkdir(parents=True)
        plutil = self.bin / 'plutil'
        plutil.write_text(FAKE_PLUTIL.format(python=sys.executable))
        plutil.chmod(0o755)
        shutil.copy(RUNNER / 'runner.sh', self.app / 'runner.sh')
        shutil.copy(RUNNER / 'queue.sh', self.repo / 'tools' / 'macos_runner' / 'queue.sh')
        stub = self.repo / 'tools' / 'macos_runner' / 'run_session.sh'
        stub.write_text(STUB_SESSION)
        stub.chmod(0o755)
        self.env = dict(os.environ, PATH='{}:{}'.format(self.bin, os.environ.get('PATH', '/usr/bin:/bin')),
                        SPR_REPO=str(self.repo), SPR_SESSIONS=str(self.sessions), HOME=str(root))

    def run_script(self, *args, env=None):
        return subprocess.run(['bash'] + [str(a) for a in args], env=env or self.env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, timeout=60)

    def queue(self, *args):
        return self.run_script(self.repo / 'tools' / 'macos_runner' / 'queue.sh', *args)

    def test_scripts_parse(self):
        for script in sorted(RUNNER.glob('*.sh')):
            self.assertEqual(self.run_script('-n', script).returncode, 0, script.name)

    def test_queue_writes_a_typed_plist_and_refuses_bad_arguments(self):
        done = self.queue('scan', '--seconds', '45', '--gain', '12.5', '--antenna', 'TX/RX', '--input', 'terminated',
                          '--procedure', 'baseline')
        self.assertEqual(done.returncode, 0, done.stderr)
        request = self.sessions / 'queue' / (done.stdout.strip() + '.json')
        self.assertEqual(plistlib.loads(request.read_bytes()), {'mode': 'scan', 'scan_seconds': 45, 'gain_db': 12.5,
                                                                'antenna': 'TX/RX', 'input': 'terminated',
                                                                'procedure': 'baseline', 'device': 'b200',
                                                                'bladerf_job': 'dense-dc+survey'})
        blade = self.queue('scan', '--device', 'bladerf', '--bladerf-job', 'survey-anchor', '--seconds', '300')
        body = plistlib.loads((self.sessions / 'queue' / (blade.stdout.strip() + '.json')).read_bytes())
        self.assertEqual((body['device'], body['bladerf_job'], body['scan_seconds']), ('bladerf', 'survey-anchor', 300))
        full = self.queue('scan', '--procedure', 'fullrange', '--seconds', '300')
        body = plistlib.loads((self.sessions / 'queue' / (full.stdout.strip() + '.json')).read_bytes())
        self.assertEqual((body['procedure'], body['scan_seconds']), ('fullrange', 300))
        for bad in (('hot',), ('scan', '--seconds', '4'), ('scan', '--gain', '71'), ('scan', '--antenna', 'RX1'),
                    ('scan', '--input', 'dummy'), ('scan', '--procedure', 'nf'), ('scan', '--seconds'),
                    ('scan', '--wait', 'x'), ('scan', '--device', 'hackrf'), ('scan', '--bladerf-job', 'flash')):
            self.assertEqual(self.queue(*bad).returncode, 2, bad)
        self.assertEqual(len(list((self.sessions / 'queue').glob('*.json'))), 3)

    def test_runner_validates_requests_and_hands_over_only_checked_values(self):
        ok = self.queue('scan', '--gain', '12.5', '--input', 'terminated').stdout.strip()
        q = self.sessions / 'queue'

        def raw(name, body):
            (q / name).write_bytes(plistlib.dumps(body))
        raw('evil.json', {'mode': 'scan; rm -rf ~'})
        raw('badinput.json', {'mode': 'scan', 'input': 'antenna;reboot'})
        raw('badnumber.json', {'mode': 'scan', 'scan_seconds': '30 && x'})
        raw('badproc.json', {'mode': 'scan', 'procedure': 'suite;id'})
        (q / 'notplist.json').write_text('{"mode": "probe"}')
        raw('bad id.json', {'mode': 'probe'})
        result = self.run_script(self.app / 'runner.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        status = lambda name: (self.sessions / 'status' / (name + '.json')).read_text()
        self.assertIn('"state":"finished"', status(ok))
        self.assertIn('"exit":0', status(ok))
        for name, reason in (('evil', 'mode must be'), ('badinput', 'bad input'), ('badnumber', 'bad number'),
                             ('badproc', 'bad procedure'), ('notplist', 'invalid')):
            self.assertIn('rejected', status(name))
            self.assertIn(reason, status(name))
        self.assertEqual(len(list((self.sessions / 'status').glob('rejected-*.json'))), 1)
        seen = (self.sessions / 'seen-scan').read_text()
        for line in ('MODE=scan', 'INPUT=terminated', 'GAIN_DB=12.5', 'SCAN_SECONDS=30', 'ANTENNA=RX2', 'PROCEDURE=suite'):
            self.assertIn(line + '\n', seen)
        self.assertIn('REQUEST_FILE={}'.format(self.sessions / 'queue' / 'taken'), seen)
        self.assertEqual(list(q.glob('*.json')), [])

    def test_session_script_rejects_an_undeclared_input_before_touching_hardware(self):
        session = RUNNER / 'run_session.sh'
        env = dict(self.env, MODE='probe', INPUT='open-circuit')
        result = self.run_script(session, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn('INPUT', result.stdout)
        request = self.sessions / 'r.json'
        request.write_bytes(plistlib.dumps({'mode': 'probe', 'input': 'dummy'}))
        env = dict(self.env, MODE='probe', REQUEST_FILE=str(request))
        env.pop('INPUT', None)
        self.assertEqual(self.run_script(session, env=env).returncode, 2)
        bad = dict(self.env, MODE='probe', INPUT='terminated', PROCEDURE='noise-figure')
        result = self.run_script(session, env=bad)
        self.assertEqual(result.returncode, 2)
        self.assertIn('PROCEDURE', result.stdout)


if __name__ == '__main__':
    unittest.main()


@unittest.skipIf(os.name == 'nt' or shutil.which('bash') is None, 'POSIX bash scripts')
def test_auto_reanalysis_dispatches_before_sdr_discovery(tmp_path):
    import json
    root=tmp_path/'repo';sessions=root/'sessions';venv=sessions/'.venv/bin';binpath=tmp_path/'bin'
    for p in (venv,binpath,root/'tools'):p.mkdir(parents=True,exist_ok=True)
    plutil=binpath/'plutil';plutil.write_text(FAKE_PLUTIL.format(python=sys.executable));plutil.chmod(0o755)
    python=venv/'python';python.write_text('#!'+sys.executable+'\n'+'''import os,sys,json
from pathlib import Path
if sys.argv[1]=='-c':
 assert 'SoapySDR' not in sys.argv[2], 'reanalysis attempted SDR discovery'
 sys.exit(0)
assert sys.argv[1].endswith('analyze_scan_set.py'),sys.argv
Path(os.environ['REANALYSIS_SEEN']).write_text(json.dumps(sys.argv[1:]))
''');python.chmod(0o755)
    (root/'tools/analyze_scan_set.py').write_text('# simulated report entrypoint\n')
    name='superscan-20261123-093131';session=sessions/name;session.mkdir();capture=session/'captures.json';capture.write_text('{"captures":[]}')
    request=tmp_path/'request.plist';request.write_bytes(plistlib.dumps(dict(reanalyze=name)))
    seen=tmp_path/'seen.json';env=dict(os.environ,PATH=str(binpath)+':'+os.environ['PATH'],MODE='scan',INPUT='antenna',SPR_REPO=str(root),SPR_SESSIONS=str(sessions),REQUEST_FILE=str(request),REANALYSIS_SEEN=str(seen))
    done=subprocess.run(['bash',str(RUNNER/'run_session.sh')],env=env,capture_output=True,text=True,timeout=30)
    assert done.returncode==0,done.stdout+done.stderr
    assert json.loads(seen.read_text())[1:]==[str(session/'analysis'),str(session)]
    assert 'device:' not in done.stdout and 'SoapySDR' not in done.stdout
    assert 'RESULT=reanalyze_0' in (sessions/'last-run.env').read_text()
    assert capture.read_text()=='{"captures":[]}'


@unittest.skipIf(os.name == 'nt' or shutil.which('bash') is None or shutil.which('curl') is None, 'POSIX bash scripts + curl')
def test_gui_request_starts_detached_workspace_and_stops_it(tmp_path):
    import json, socket, time, urllib.request
    sessions=tmp_path/'sessions';venv=sessions/'.venv/bin';binpath=tmp_path/'bin'
    for p in (venv,binpath):p.mkdir(parents=True)
    plutil=binpath/'plutil';plutil.write_text(FAKE_PLUTIL.format(python=sys.executable));plutil.chmod(0o755)
    (venv/'python').write_text('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable);(venv/'python').chmod(0o755)
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    def run(action):
        request=tmp_path/(action+'.plist');request.write_bytes(plistlib.dumps(dict(mode='scan',gui=action,gui_port=str(port))))
        env=dict(os.environ,PATH=str(binpath)+':'+os.environ['PATH'],MODE='scan',SPR_REPO=str(ROOT),SPR_SESSIONS=str(sessions),REQUEST_FILE=str(request))
        done=subprocess.run(['bash',str(RUNNER/'run_session.sh')],env=env,capture_output=True,text=True,timeout=60)
        return done,(sessions/'last-run.env').read_text()
    try:
        done,result=run('start')
        assert done.returncode==0,done.stdout+done.stderr
        assert 'RESULT=gui_started' in result and 'URL=http://127.0.0.1:%d' % port in result
        assert 'SoapySDR' not in done.stdout and 'device:' not in done.stdout
        pid=int((sessions/'gui.pid').read_text())
        with urllib.request.urlopen('http://127.0.0.1:%d/api/state' % port,timeout=5) as r:
            assert 'sessions' in json.load(r)            # the server outlived the runner job
        done,result=run('start')
        assert done.returncode==0 and 'RESULT=gui_running' in result   # no second server
        done,result=run('stop')
        assert done.returncode==0 and 'RESULT=gui_stopped' in result and not (sessions/'gui.pid').exists()
        for _ in range(50):
            try:os.kill(pid,0)
            except OSError:break
            time.sleep(.1)
        else:raise AssertionError('gui process still running after stop')
    finally:
        if (sessions/'gui.pid').exists():
            try:os.kill(int((sessions/'gui.pid').read_text()),15)
            except OSError:pass
