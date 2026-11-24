#!/usr/bin/env python3
"""Supervise guarded receive captures without counting setup or outages as scan time.

Each attempt remains immutable. Only validated complete records enter a capture
partition; gain changes are never concatenated into a homogeneous dataset.
"""

import argparse
import contextlib
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


def publish(path, value):
    temp = path.with_name('.' + path.name + '.tmp')
    with temp.open('w') as f:
        json.dump(value, f, indent=2, allow_nan=False)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def classify_failure(code, text):
    if code == 0:
        return 'finished'
    if re.search(r'headroom rejected; original IQ retained', text, re.I):
        return 'headroom'
    if re.search(
        r'SDR overflow|SDR read failed or timed out|LIBUSB_ERROR_(NO_DEVICE|IO|PIPE|TIMEOUT)|USBError -4|uhd::usb_error|No UHD Devices Found|No devices found for.*(?:uhd|b200)',
        text,
        re.I,
    ):
        return 'transport'
    return 'fatal'


@contextlib.contextmanager
def awake():
    process = None
    if sys.platform == 'darwin':
        # Bind assertions to this supervisor, covering backoff, reopen and child captures.
        process = subprocess.Popen(
            ['/usr/bin/caffeinate', '-i', '-s', '-w', str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.05)
        if process.poll() is not None:
            raise RuntimeError('could not establish scan sleep assertion')
    try:
        yield
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


class CaptureControl(Exception):
    """An acknowledged operator action, distinct from a receiver failure."""

    def __init__(self, request):
        self.request = request
        super().__init__(request['action'])


def control_request(path, acknowledged=None):
    if path is None:
        return None
    try:
        if Path(path).stat().st_size > 4096:
            return None
        value = json.loads(Path(path).read_text())
        if (
            isinstance(value, dict)
            and value.get('version') == 1
            and value.get('action') in ('pause', 'resume', 'retry')
            and isinstance(value.get('id'), str)
            and re.fullmatch(r'[a-f0-9]{32}', value['id'])
            and value['id'] != acknowledged
        ):
            return value
    except (OSError, ValueError):
        pass
    return None


def run_child(
    command, log_path, data_path, stall_s, poll_s=0.25, cancel_file=None, control_file=None, acknowledged=None
):
    """Kill a stalled process group, retaining stdout evidence and accepted prefix."""

    def check_control():
        if cancel_file is not None and Path(cancel_file).exists():
            raise KeyboardInterrupt()
        request = control_request(control_file, acknowledged)
        if request and request['action'] in ('pause', 'retry'):
            raise CaptureControl(request)

    with log_path.open('wb') as log:
        check_control()  # Do not open a receiver for a request already pending.
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        last_progress = time.monotonic()
        size = 0
        stalled = False
        try:
            while child.poll() is None:
                check_control()
                current = data_path.stat().st_size if data_path.exists() else 0
                if current > size:
                    size = current
                    last_progress = time.monotonic()
                if time.monotonic() - last_progress > stall_s:
                    stalled = True
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                    break
                time.sleep(poll_s)
            code = child.wait()
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
    return code, stalled


RETUNED_KEYS = ('frequency', 'freq', 'center_hz', 'frequency_hz', 'tuned_center_hz')


def accepted_prefix(raw, output, gain, max_gap_s=5.0):
    """Validate records, exclude a crash-truncated final fragment and bound time credits.

    A complete malformed/unqualified record is a fatal evidence fault. The raw
    attempt is preserved even if validation fails; never silently skip such rows.
    """
    count = 0
    credit = 0.0
    dwell = 0.0
    excluded = 0.0
    previous = None
    context = None
    first = last = None
    digest = hashlib.sha256()
    fragment = False
    with output.open('wb') as dest:
        if raw.exists():
            with raw.open('rb') as source:
                for line in source:
                    digest.update(line)
                    if not line.endswith(b'\n'):
                        fragment = True
                        break
                    r = json.loads(line)
                    m = r['metadata']
                    if m.get('measurement_status') != 'complete' or m.get('continuity') != 'verified':
                        raise ValueError('unqualified acquisition record')
                    if (
                        m.get('headroom', {}).get('accepted') is not True
                        or m.get('rf_state', {}).get('after', {}).get('validity', {}).get('passed') is not True
                    ):
                        raise ValueError('missing or failed ongoing receive guards')
                    ctx = m.get('context', {})
                    g = ctx.get('gains', {})
                    if len(g) != 1 or not math.isclose(float(next(iter(g.values()))), gain, abs_tol=0.05):
                        raise ValueError('actual gain differs from segment gain')
                    # Tuned RF frequency is deliberately not part of the fixed receiver context.
                    fixed = {k: v for k, v in ctx.items() if k not in RETUNED_KEYS}
                    identity = (
                        json.dumps(fixed, sort_keys=True),
                        m.get('sample_rate_hz'),
                        len(r['power']),
                        r['step_hz'],
                        r.get('unit'),
                    )
                    if context is None:
                        context = identity
                    if identity != context:
                        raise ValueError('mixed receiver contexts within segment')
                    a = datetime.datetime.fromisoformat(r['time_start'])
                    b = datetime.datetime.fromisoformat(r['time_stop'])
                    if a.tzinfo is None or b.tzinfo is None:
                        raise ValueError('timezone-free acquisition timestamps')
                    a, b = a.timestamp(), b.timestamp()
                    if not math.isfinite(a + b) or b <= a or (previous is not None and a < previous):
                        raise ValueError('invalid or overlapping record intervals')
                    gap = 0 if previous is None else a - previous
                    if gap <= max_gap_s:
                        credit += gap
                    else:
                        excluded += gap
                    credit += b - a
                    dwell += b - a
                    previous = b
                    first = a if first is None else first
                    last = b
                    count += 1
                    dest.write(line)
        dest.flush()
        os.fsync(dest.fileno())
    return dict(
        records=count,
        credited_seconds=credit,
        rf_dwell_seconds=dwell,
        excluded_gap_seconds=excluded,
        first_unix=first,
        last_unix=last,
        trailing_fragment=fragment,
        raw_sha256=digest.hexdigest(),
    )


def supervise(
    session,
    command,
    seconds,
    gain,
    *,
    min_gain=20.0,
    gain_step=6.0,
    max_attempts=20,
    transport_retries=4,
    stall_s=60.0,
    backoff_s=2.0,
    max_gap_s=5.0,
    execute=run_child,
    sleep=time.sleep,
):
    session = Path(session)
    session.mkdir(parents=True, exist_ok=True)
    manifest = session / 'captures.json'
    if manifest.exists():
        raise ValueError('fresh supervisor session required; refusing to overwrite evidence')
    if (
        not command
        or not all(math.isfinite(x) for x in (seconds, gain, min_gain, gain_step, stall_s, backoff_s, max_gap_s))
        or seconds <= 0
        or not 0 <= min_gain <= gain <= 76
        or gain_step <= 0
        or stall_s <= 0
        or backoff_s < 0
        or max_gap_s < 0
        or max_attempts < 1
        or transport_retries < 0
    ):
        raise ValueError('invalid supervision policy')
    state = dict(
        version=1,
        control_version=1,
        supports_stop_request=True,
        supervisor_pid=os.getpid(),
        status='running',
        requested_seconds=seconds,
        credited_seconds=0.0,
        rf_dwell_seconds=0.0,
        paused_seconds=0.0,
        controls=[],
        control_ack=None,
        policy=dict(
            min_gain=min_gain,
            gain_step_db=gain_step,
            max_attempts=max_attempts,
            transport_retries=transport_retries,
            stall_s=stall_s,
            max_gap_s=max_gap_s,
        ),
        captures=[],
        attempts=[],
    )
    failures = 0
    last_class = 'fatal'
    result = 2
    attempt = 0
    automatic_attempts = 0
    control_file = session / 'control-request.json'

    def acknowledge(request):
        state['control_ack'] = request['id']
        state['controls'].append(
            dict(
                id=request['id'],
                action=request['action'],
                acknowledged_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                credited_seconds=state['credited_seconds'],
            )
        )

    def wait_if_paused():
        if state['status'] != 'paused':
            return
        started = time.monotonic()
        try:
            while state['status'] == 'paused':
                if (session / 'stop-request.json').exists():
                    raise KeyboardInterrupt()
                request = control_request(control_file, state['control_ack'])
                if request:
                    acknowledge(request)
                    if request['action'] in ('resume', 'retry'):
                        state['status'] = 'running'
                    publish(manifest, state)
                if state['status'] == 'paused':
                    sleep(0.1)
        finally:
            state['paused_seconds'] += time.monotonic() - started
            publish(manifest, state)

    try:
        while automatic_attempts < max_attempts:
            if (session / 'stop-request.json').exists():
                state['status'] = 'cancelled'
                result = 130
                last_class = 'cancelled'
                break
            request = control_request(control_file, state['control_ack'])
            if request:
                acknowledge(request)
                if request['action'] == 'pause':
                    state['status'] = 'paused'
                publish(manifest, state)
            try:
                wait_if_paused()
            except KeyboardInterrupt:
                state['status'] = 'cancelled'
                result = 130
                last_class = 'cancelled'
                break
            left = seconds - state['credited_seconds']
            if left <= 0:
                result = 0
                break
            attempt += 1
            raw = session / f'attempt-{attempt:03d}.jsonl'
            accepted = session / f'capture-{attempt:03d}.jsonl'
            log = session / f'attempt-{attempt:03d}.log'
            rejected = session / f'rejected-main-{attempt}'
            args = list(command) + [
                '-g',
                str(gain),
                '-e',
                str(math.ceil(left)),
                '--deadline-policy',
                'finish-hop',
                '--rejection-evidence',
                str(rejected),
                '-F',
                'json',
                '-O',
                str(raw),
            ]
            state['current_attempt'] = dict(
                number=attempt,
                gain_db=gain,
                raw_file=raw.name,
                log_file=log.name,
                started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
            publish(manifest, state)
            cancelled = False
            operator = None
            try:
                if execute is run_child:
                    code, stalled = execute(
                        args,
                        log,
                        raw,
                        stall_s,
                        cancel_file=session / 'stop-request.json',
                        control_file=control_file,
                        acknowledged=state['control_ack'],
                    )
                else:
                    code, stalled = execute(args, log, raw, stall_s)
            except CaptureControl as e:
                code, stalled, operator = -signal.SIGTERM, False, e.request
            except KeyboardInterrupt:
                code, stalled, cancelled = -signal.SIGTERM, False, True
            metrics = accepted_prefix(raw, accepted, gain, max_gap_s)
            text = log.read_text(errors='replace')
            kind = (
                'cancelled'
                if cancelled
                else 'paused'
                if operator and operator['action'] == 'pause'
                else 'manual_retry'
                if operator
                else 'transport'
                if stalled
                else classify_failure(code, text)
            )
            record = dict(
                attempt=attempt,
                gain_db=gain,
                exit=code,
                failure_class=kind,
                watchdog_stalled=stalled,
                raw_file=raw.name,
                accepted_file=accepted.name,
                log_file=log.name,
                **metrics,
            )
            state['attempts'].append(record)
            state['credited_seconds'] += metrics['credited_seconds']
            state['rf_dwell_seconds'] += metrics['rf_dwell_seconds']
            if metrics['records']:
                state['captures'].append(dict(file=accepted.name, gain_db=gain, **metrics))
            state['current_attempt'] = None
            if operator:
                acknowledge(operator)
                state['status'] = 'paused' if operator['action'] == 'pause' else 'running'
            publish(manifest, state)
            if cancelled:
                state['status'] = 'cancelled'
                last_class = 'cancelled'
                result = 130
                break
            if operator:
                # Manual boundaries do not spend the automatic recovery budget.
                # Retained complete records are credited, partial trailing records are not.
                last_class = kind
                continue
            automatic_attempts += 1
            if kind == 'fatal':
                last_class = kind
                break
            if state['credited_seconds'] >= seconds:
                result = 0
                break
            last_class = kind
            if kind == 'headroom':
                index = json.loads((rejected / 'index.json').read_text())
                if not index.get('records'):
                    raise ValueError('headroom failure without retained IQ evidence')
                for entry in index['records']:
                    meta_path = rejected / entry['metadata_file']
                    if (
                        meta_path.parent.resolve() != rejected.resolve()
                        or json.loads(meta_path.read_text()).get('reason') != 'headroom_rejected'
                    ):
                        raise ValueError('missing typed headroom rejection')
                    path = rejected / entry['iq_file']
                    if (
                        path.parent.resolve() != rejected.resolve()
                        or hashlib.sha256(path.read_bytes()).hexdigest() != entry['iq_sha256']
                    ):
                        raise ValueError('rejected IQ integrity failure')
                if gain <= min_gain:
                    break
                gain = max(min_gain, gain - gain_step)
                failures = 0
            elif kind == 'transport':
                failures = 0 if metrics['records'] else failures + 1
                if failures > transport_retries:
                    break
            elif kind == 'finished' and metrics['credited_seconds'] > 0:
                failures = 0
            else:
                break
            # Keep controls responsive even during exponential retry backoff.
            delay = min(30.0, backoff_s * 2 ** min(failures, 4))
            while delay > 0:
                if (session / 'stop-request.json').exists() or control_request(control_file, state['control_ack']):
                    break
                step = min(0.1, delay)
                sleep(step)
                delay -= step

    except BaseException as e:
        state['error'] = str(e)
        state['status'] = 'cancelled' if isinstance(e, (KeyboardInterrupt, SystemExit)) else 'failed'
        raise
    finally:
        if state['status'] == 'cancelled':
            state['controls'].append(
                dict(
                    id=None,
                    action='abort',
                    acknowledged_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    credited_seconds=state['credited_seconds'],
                    source='stop_request' if (session / 'stop-request.json').exists() else 'interrupt',
                )
            )
        if state['status'] == 'running':
            state['status'] = 'completed' if result == 0 else 'incomplete'
        state['current_attempt'] = None
        state['remaining_seconds'] = max(0.0, seconds - state['credited_seconds'])
        state['last_failure_class'] = last_class
        publish(manifest, state)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('session')
    p.add_argument('--seconds', type=float, required=True)
    p.add_argument('--gain', type=float, required=True)
    p.add_argument('--min-gain', type=float, default=20)
    p.add_argument('--gain-step', type=float, default=6)
    p.add_argument('--stall-seconds', type=float, default=60)
    p.add_argument('--backoff-seconds', type=float, default=2)
    if '--' not in sys.argv:
        p.error('capture command must follow --')
    separator = sys.argv.index('--')
    a = p.parse_args(sys.argv[1:separator])
    command = sys.argv[separator + 1 :]

    def terminate(signum, frame):
        raise KeyboardInterrupt()

    old = signal.signal(signal.SIGTERM, terminate)
    try:
        with awake():
            return supervise(
                a.session,
                command,
                a.seconds,
                a.gain,
                min_gain=min(a.min_gain, a.gain),
                gain_step=a.gain_step,
                stall_s=a.stall_seconds,
                backoff_s=a.backoff_seconds,
            )
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGTERM, old)


if __name__ == '__main__':
    sys.exit(main())
