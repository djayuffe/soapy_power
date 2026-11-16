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
                                                                'procedure': 'baseline'})
        for bad in (('hot',), ('scan', '--seconds', '4'), ('scan', '--gain', '71'), ('scan', '--antenna', 'RX1'),
                    ('scan', '--input', 'dummy'), ('scan', '--procedure', 'nf'), ('scan', '--seconds'),
                    ('scan', '--wait', 'x')):
            self.assertEqual(self.queue(*bad).returncode, 2, bad)
        self.assertEqual(len(list((self.sessions / 'queue').glob('*.json'))), 1)

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
