"""Local SatNOGS integration. HTTP-side code uses only the standard library.

Orbital calculations run in one bounded CPU-only child. Network refresh runs
independently of RX and atomically preserves the last complete DB generation.
"""

import argparse
from collections import OrderedDict
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

C = 299792458.0


def backend(repo):
    sys.path.insert(0, str(Path(repo) / 'tools'))
    import satdb

    return satdb


def read(path):
    path = Path(path)
    if path.stat().st_size > 64 * 1024**2:
        raise ValueError('Satellite input exceeds size budget')
    return json.loads(path.read_text())


def instant(value):
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('A timezone-aware acquisition timestamp is required')
    return parsed.timestamp()


def identity(cache):
    cache = Path(cache).resolve()
    digest = hashlib.sha256()
    for name in ('meta.json', 'satellites.json', 'transmitters.json', 'tle.json'):
        p = cache / name
        if p.exists():
            with p.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
    return digest.hexdigest()


def sources(sessions, session=None):
    sessions = Path(sessions).resolve()
    cache = (sessions / 'satdb').resolve()
    qth = sessions / 'qth.json'
    frozen = False
    if session:
        from soapypower.gui import NAME

        if not isinstance(session, str) or not NAME.fullmatch(session):
            raise ValueError('Invalid satellite session')
        folder = sessions / session
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError('Unknown satellite session')
        reference = folder / 'reference'
        if reference.exists():
            reference = reference.resolve()
            if not reference.is_relative_to(folder.resolve()):
                raise ValueError('Reference escaped its session')
            cache, qth, frozen = reference / 'satdb', reference / 'qth.json', True
    if not cache.is_relative_to(sessions) or not qth.resolve().is_relative_to(sessions):
        raise ValueError('Satellite reference escaped sessions')
    return cache, qth, frozen


def transmitters(sd, cache):
    db = sd.load(cache)
    result = {}
    for n, sat in sorted(db.items()):
        if (
            not sd.eligible_satellite(sat)
            or not sd.valid_tle(sat['l1'], sat['l2'])
            or sd.catalog_number(sat['l1'][2:7]) != n
        ):
            continue
        good = []
        for tx in sat['transmitters']:
            if not sd.eligible_transmitter(tx):
                continue
            low = float(tx['f_hz'])
            high = float(tx.get('f_high_hz') or low)
            if not all(math.isfinite(x) and 0 < x <= 1e12 for x in (low, high)) or high < low:
                continue
            item = dict(tx, f_high_hz=high)
            item['id'] = (
                item.get('uuid')
                or hashlib.sha256(json.dumps([n, low, high, item['mode'], item['description']]).encode()).hexdigest()[
                    :24
                ]
            )
            good.append(item)
        if good:
            result[n] = dict(sat, transmitters=good)
    return result


def observer(sd, path):
    value = read(path)
    sd.validate_qth(value)
    for key, low, high in [('alt_m', -500, 10000), ('min_elevation_deg', 0, 90)]:
        number = value.get(key, 0)
        if (
            isinstance(number, bool)
            or not isinstance(number, (float, int))
            or not math.isfinite(number)
            or not low <= number <= high
        ):
            raise ValueError('Invalid observer ' + key)
    return value


def compute(repo, sessions, spec):
    import numpy as np

    sd = backend(repo)
    low, high = spec['lo_hz'], spec['hi_hz']
    now = spec['now']
    cache = (Path(sessions) / 'satdb').resolve()
    if not (cache / 'meta.json').is_file():
        raise ValueError('No local satellite cache. Use Update SatNOGS data.')
    meta = read(cache / 'meta.json')
    db = transmitters(sd, cache)
    selected = {}
    catalog = []
    for n, sat in db.items():
        txs = [x for x in sat['transmitters'] if x['f_hz'] <= high and x['f_high_hz'] >= low]
        if txs:
            selected[n] = dict(sat, transmitters=txs)
            catalog.extend(dict(norad=n, name=sat['name'], **x) for x in txs)
    eligible = sum(
        sd.tle_eligibility(s['l1'], s['l2'], now, now + spec['seconds'])['eligible'] for s in selected.values()
    )
    result = dict(
        status='ready',
        generated_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        fetched_utc=meta.get('fetched_utc'),
        cache_identity=identity(cache),
        attribution='SatNOGS DB · CC BY-SA; orbital sources listed in cache metadata',
        sources=meta.get('sources', {}),
        catalog=catalog[:5000],
        catalog_truncated=len(catalog) > 5000,
        active_satellites=len(selected),
        eligible_tles=eligible,
        stale_or_invalid_tles=len(selected) - eligible,
        passes=[],
        curves=[],
        lo_hz=low,
        hi_hz=high,
        frame_session=spec.get('session'),
        identity_confirmed=False,
        notes=[
            'Predictions and frequency proximity do not confirm reception or identity.',
            'Pass boundaries sampled at 30 s; Doppler curves interpolated at 5 s.',
        ],
    )
    try:
        qth = observer(sd, Path(sessions) / 'qth.json')
    except (OSError, ValueError, TypeError) as error:
        result.update(status='observer_required', error=str(error))
        qth = None
    if qth is not None:
        result['observer'] = {k: qth.get(k, 0) for k in ('lat_deg', 'lon_deg', 'alt_m', 'min_elevation_deg')}
    import sgp4  # noqa: F401 -- explicit capability failure instead of empty passes

    result['propagation'] = dict(
        model='SGP4 / TEME to ECEF / WGS84', sgp4_version=sgp4.__version__, max_tle_age_days=sd.DEFAULT_MAX_TLE_AGE_DAYS
    )
    chosen = dict(list(selected.items())[:4096])
    result['prediction_truncated'] = len(selected) > 4096
    predictions = (
        sd.predict(chosen, qth, now, now + spec['seconds'], [dict(lo_hz=0, hi_hz=1e12)], step=30)
        if qth is not None
        else []
    )
    minimum = (qth or {}).get('min_elevation_deg', 0)
    for p in predictions:
        t = np.asarray(p['t'])
        elevation = np.asarray(p['el'])
        visible = (t >= now) & (t <= now + spec['seconds']) & np.isfinite(elevation) & (elevation > minimum)
        edges = np.flatnonzero(np.diff(np.r_[False, visible, False]))
        for a, b in zip(edges[::2], edges[1::2]):
            peak = a + int(np.argmax(elevation[a:b]))
            result['passes'].append(
                dict(
                    sky_track=None,
                    norad=p['norad'],
                    name=p['name'],
                    rise_utc=float(t[a]),
                    set_utc=float(t[b - 1]),
                    max_el_deg=float(elevation[peak]),
                    max_el_utc=float(t[peak]),
                    clipped_start=bool(a == int(np.flatnonzero(t >= now)[0])),
                    clipped_end=bool(b - 1 == int(np.flatnonzero(t <= now + spec['seconds'])[-1])),
                    tle_epoch_utc=p['tle_epoch_utc'],
                    transmitters=p['transmitters'],
                )
            )
    result['passes'].sort(key=lambda p: (p['rise_utc'], -p['max_el_deg']))
    result['passes_truncated'] = len(result['passes']) > 256
    result['passes'] = result['passes'][:256]
    # Refine only the bounded displayed set, not every candidate orbit.
    for item in result['passes']:
        rise, setting = item['rise_utc'], item['set_utc']
        if setting <= rise:
            continue
        sample_times = np.linspace(rise, setting, min(96, max(2, int(math.ceil((setting - rise) / 5)) + 1)))
        try:
            orbit = chosen[item['norad']]
            sky_el, sky_az, sky_range, sky_rr = sd.topo(orbit['l1'], orbit['l2'], sample_times, qth)
            if all(np.isfinite(v).all() for v in (sky_el, sky_az, sky_range, sky_rr)):
                item['sky_track'] = dict(
                    times_s=np.round(sample_times, 3).tolist(),
                    el_deg=np.round(sky_el, 4).tolist(),
                    az_deg=np.round(sky_az, 4).tolist(),
                    range_m=np.round(sky_range, 1).tolist(),
                    range_rate_m_s=np.round(sky_rr, 3).tolist(),
                )
                maximum = int(np.argmax(sky_el))
                item['sky_peak_el_deg'] = float(sky_el[maximum])
                item['sky_peak_utc'] = float(sample_times[maximum])
        except (ValueError, RuntimeError, OverflowError):
            pass
    # The selected record uses its frozen reference and acquisition timestamp.
    # Curves cover a short interval, allowing the browser to interpolate current
    # frames without requiring a process launch for every half-second RF hop.
    try:
        frame_cache, frame_qth, frozen = sources(sessions, spec.get('session'))
        if frozen:
            from report_bundle import verify

            verify(frame_cache.parent)
        frame_db = transmitters(sd, frame_cache)
        frame_observer = observer(sd, frame_qth)
    except (OSError, ValueError, TypeError, KeyError) as error:
        result['frame_error'] = str(error)
        return result
    anchor = spec.get('frame_time')
    anchor = now if anchor is None else float(anchor)
    times = np.arange(math.floor(anchor / 30) * 30 - 15, math.floor(anchor / 30) * 30 + 110, 5, dtype=float)
    result.update(
        frame_reference='frozen session' if frozen else 'current cache; not frozen',
        frame_cache_identity=identity(frame_cache),
        frame_min_el_deg=frame_observer.get('min_elevation_deg', 0),
        frame_time_start=float(times[0]),
        frame_time_stop=float(times[-1]),
    )
    count = 0
    for n, sat in frame_db.items():
        txs = [x for x in sat['transmitters'] if x['f_hz'] <= high and x['f_high_hz'] >= low]
        if not txs or not sd.tle_eligibility(sat['l1'], sat['l2'], float(times[0]), float(times[-1]))['eligible']:
            continue
        if count >= 4096:
            result['curves_truncated'] = True
            break
        count += 1
        try:
            el, az, _, rr = sd.topo(sat['l1'], sat['l2'], times, frame_observer)
            if not (np.isfinite(el).all() and np.isfinite(rr).all() and np.isfinite(az).all()):
                continue
            if np.max(el) <= frame_observer.get('min_elevation_deg', 0):
                continue
            result['curves'].append(
                dict(
                    norad=n,
                    name=sat['name'],
                    transmitters=txs,
                    times_s=times.tolist(),
                    el_deg=el.tolist(),
                    az_deg=az.tolist(),
                    range_rate_m_s=rr.tolist(),
                )
            )
        except (ValueError, RuntimeError, OverflowError):
            continue
    return result


def prepare(repo, sessions, payload):
    if set(payload) != {'norad', 'transmitter_id', 'seconds'}:
        raise ValueError('Choose a transmitter and a scan duration')
    n = payload['norad']
    seconds = payload['seconds']
    if type(n) is not int or type(seconds) is not int or not 5 <= seconds <= 3600:
        raise ValueError('Invalid NORAD number or duration')
    sd = backend(repo)
    cache = (Path(sessions) / 'satdb').resolve()
    observer(sd, Path(sessions) / 'qth.json')
    sat = transmitters(sd, cache).get(n)
    tx = next((t for t in sat['transmitters'] if t['id'] == payload['transmitter_id']), None) if sat else None
    if tx is None:
        raise ValueError('Transmitter is no longer active in this cache')
    if not sd.tle_eligibility(sat['l1'], sat['l2'], time.time(), time.time() + seconds)['eligible']:
        raise ValueError('The selected satellite needs a fresh eligible TLE')
    margin = max(25000, tx['f_high_hz'] * 4e-5)
    bw = sd.expected_bw_hz(tx.get('mode'), tx.get('baud'))
    if tx['f_high_hz'] == tx['f_hz'] and bw:
        margin += bw[1] / 2
    start, stop = tx['f_hz'] - margin, tx['f_high_hz'] + margin
    return dict(
        command='acquisition',
        runtime='b200',
        seconds=seconds,
        anti_spur=True,
        satellite=dict(norad=n, transmitter_id=tx['id'], cache_identity=identity(cache)),
        values=dict(
            device='driver=uhd,type=b200,recv_frame_size=16360,num_recv_frames=1024',
            antenna='RX2',
            freq='{}:{}'.format(start, stop),
            rate='2M',
            bins=8192,
            gain=30,
            time=0.25,
            overlap=75,
            fft_window='blackman',
            dc_mask_width='10000',
            ensure_coverage=True,
            rf_state=True,
            headroom=True,
            headroom_max=0.5,
            headroom_p999=0.2,
            partial_policy='reject',
            tune_delay=0.05,
            max_threads=2,
            max_queue_size=4,
        ),
    )


def freeze(repo, sessions, folder, target, seconds=300):
    sd = backend(repo)
    cache = (Path(sessions) / 'satdb').resolve()
    if identity(cache) != target['cache_identity']:
        raise ValueError('Satellite cache changed; prepare and review the target again')
    sat = transmitters(sd, cache).get(target['norad'])
    tx = next((t for t in sat['transmitters'] if t['id'] == target['transmitter_id']), None) if sat else None
    if tx is None:
        raise ValueError('Target transmitter is unavailable')
    if not sd.tle_eligibility(sat['l1'], sat['l2'], time.time(), time.time() + seconds)['eligible']:
        raise ValueError('Target TLE expired while queued; prepare a fresh reference')
    sd.snapshot(cache, Path(folder) / 'reference', Path(sessions) / 'qth.json')
    if identity((Path(folder) / 'reference').resolve() / 'satdb') != target['cache_identity']:
        raise ValueError('Satellite reference changed before freezing; RX was not started')
    from soapypower.gui_jobs import publish

    publish(
        Path(folder) / 'satellite-target.json',
        dict(
            target,
            name=sat['name'],
            transmitter=tx,
            frozen_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
            identity_confirmed=False,
        ),
    )


def refresh_worker(repo, sessions):
    from soapypower.gui_jobs import publish
    from soapypower.gui_service import file_lock

    root = Path(sessions)
    state = root / 'satnogs-refresh.json'
    with file_lock(root / '.satnogs-refresh.lock', blocking=False):
        publish(state, dict(state='running', pid=os.getpid(), started_utc=dt.datetime.now(dt.timezone.utc).isoformat()))
        try:
            meta = backend(repo).refresh(root / 'satdb')
            publish(state, dict(state='finished', fetched_utc=meta['fetched_utc']))
        except Exception as error:
            publish(state, dict(state='failed', error=str(error), previous_cache_preserved=True))
            raise


class SatelliteService:
    def __init__(self, repo, sessions):
        self.repo, self.sessions = Path(repo), Path(sessions)
        self.lock = threading.Lock()
        self.child = None
        self.pending = None
        self.running = False
        self.closed = False
        self.results = OrderedDict()
        self.requested = None

    def python(self):
        candidate = self.sessions / '.venv/bin/python'
        return str(candidate) if candidate.is_file() else sys.executable

    def command(self, mode):
        return [
            self.python(),
            '-m',
            'soapypower.satellites',
            mode,
            '--repo',
            str(self.repo),
            '--sessions',
            str(self.sessions),
        ]

    def prepare(self, payload):
        result = subprocess.run(
            self.command('prepare'),
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=12,
            env=dict(os.environ, PYTHONPATH=str(self.repo)),
        )
        if result.returncode:
            raise ValueError(result.stderr[-2500:] or 'Could not prepare satellite scan')
        return json.loads(result.stdout)

    def refresh_status(self):
        try:
            value = read(self.sessions / 'satnogs-refresh.json')
        except FileNotFoundError:
            return {}
        except (ValueError, OSError):
            return dict(state='invalid', error='Refresh status is unreadable; cached data are retained')
        if not isinstance(value, dict):
            return dict(state='invalid', error='Invalid refresh status')
        if value.get('state') == 'running':
            from soapypower.gui_service import file_lock

            try:
                with file_lock(self.sessions / '.satnogs-refresh.lock', blocking=False):
                    value = dict(
                        value, state='interrupted', error='Refresh worker ended; previous cache remains available'
                    )
            except BlockingIOError:
                pass
        return value

    def refresh(self):
        # Dedicated file-only worker: no receiver lease, queue or device discovery.
        from soapypower.gui_service import file_lock

        try:
            with file_lock(self.sessions / '.satnogs-refresh.lock', blocking=False):
                pass
        except BlockingIOError:
            return dict(state='running')
        with (self.sessions / 'satnogs-refresh.log').open('ab') as log:
            child = subprocess.Popen(
                self.command('refresh'),
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                env=dict(os.environ, PYTHONPATH=str(self.repo)),
                start_new_session=True,
            )
        # Reap our child without keeping the GUI alive; its own durable state and
        # file lock remain authoritative after a GUI restart.
        threading.Thread(target=child.wait, daemon=True).start()
        return dict(state='starting', pid=child.pid)

    def snapshot(self, spec):
        cache, qth, frozen = sources(self.sessions, spec.get('session'))
        current = (self.sessions / 'satdb').resolve()
        paths = [current / 'meta.json', self.sessions / 'qth.json', cache / 'meta.json', qth]
        signature = []
        for p in paths:
            try:
                signature.append((str(p), p.stat().st_mtime_ns, p.stat().st_size))
            except FileNotFoundError:
                signature.append((str(p), None, None))
        context = json.dumps([spec.get('session'), spec['lo_hz'], spec['hi_hz'], spec['seconds'], signature])
        request = dict(spec, now=math.floor(time.time() / 30) * 30)
        if request.get('frame_time') is not None:
            request['frame_time'] = math.floor(request['frame_time'] / 30) * 30
        key = json.dumps([context, request['now'], request.get('frame_time')])
        with self.lock:
            if self.closed:
                raise RuntimeError('Satellite service is closed')
            saved = self.results.get(context)
            if not saved or saved[0] != key:
                self.pending = (context, key, request)
                if not self.running:
                    self.running = True
                    threading.Thread(target=self._work, daemon=True).start()
            answer = dict(saved[1]) if saved else dict(status='loading', passes=[], curves=[], catalog=[])
            answer['updating'] = not saved or saved[0] != key
            return answer

    def _work(self):
        while True:
            with self.lock:
                if self.closed or self.pending is None:
                    self.running = False
                    return
                context, key, spec = self.pending
                self.pending = None
            try:
                child = subprocess.Popen(
                    self.command('compute'),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=dict(os.environ, PYTHONPATH=str(self.repo)),
                )
                with self.lock:
                    self.child = child
                    if self.closed:
                        child.terminate()
                try:
                    stdout, stderr = child.communicate(json.dumps(spec), timeout=30)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate()
                    raise ValueError('Satellite prediction exceeded 30 s CPU worker budget')
                if child.returncode:
                    message = stderr.strip().splitlines()[-1] if stderr.strip() else 'Satellite prediction failed'
                    if 'No module named' in message and 'sgp4' in message:
                        message = 'Orbital prediction requires the optional sgp4 package in the GUI Python environment'
                    raise ValueError(message)
                if len(stdout) > 4 * 1024**2:
                    raise ValueError('Satellite response exceeded budget')
                value = json.loads(stdout)
            except Exception as error:
                value = dict(status='unavailable', error=str(error), passes=[], curves=[], catalog=[])
            with self.lock:
                self.child = None
                if not self.closed:
                    self.results[context] = (key, value)
                    self.results.move_to_end(context)
                    while len(self.results) > 4:
                        self.results.popitem(last=False)

    def close(self):
        with self.lock:
            self.closed = True
            if self.child and self.child.poll() is None:
                try:
                    self.child.terminate()
                except ProcessLookupError:
                    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['compute', 'refresh', 'prepare'])
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--sessions', required=True, type=Path)
    args = parser.parse_args()
    if args.mode == 'refresh':
        refresh_worker(args.repo, args.sessions)
    elif args.mode == 'prepare':
        print(json.dumps(prepare(args.repo, args.sessions, json.load(sys.stdin)), allow_nan=False))
    else:
        print(json.dumps(compute(args.repo, args.sessions, json.load(sys.stdin)), allow_nan=False))


if __name__ == '__main__':
    main()
