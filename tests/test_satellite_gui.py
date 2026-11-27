"""Satellite GUI/scan integration: deterministic orbit mocks and real local IPC; no RF."""

import datetime as dt
import json
from pathlib import Path
import time
from unittest import mock

import numpy as np
import pytest
from soapypower import gui_jobs, satellites as sat

ROOT = Path(__file__).resolve().parents[1]


def tle():
    now = dt.datetime.now(dt.timezone.utc)
    day = (now - dt.datetime(now.year, 1, 1, tzinfo=dt.timezone.utc)).total_seconds() / 86400 + 1
    first = f'1 99001U 26001A   {now.year % 100:02d}{day:012.8f}  .00000000  00000-0  00000-0 0  999'
    second = '2 99001  97.5000  40.0000 0001000   0.0000   0.0000 15.20000000    1'

    def checksum(line):
        line = line[:68].ljust(68)
        return line + str(sum(int(c) if c.isdigit() else int(c == '-') for c in line) % 10)

    return checksum(first), checksum(second)


def cache(root, frequency=145825000):
    root.mkdir(parents=True, exist_ok=True)
    l1, l2 = tle()
    files = {
        'tle': [{'norad': 99001, 'name': 'Synthetic test orbit', 'l1': l1, 'l2': l2}],
        'satellites': [{'norad_cat_id': 99001, 'name': 'Synthetic test orbit', 'status': 'alive'}],
        'transmitters': [
            {
                'norad_cat_id': 99001,
                'uuid': 'test-tx',
                'downlink_low': frequency,
                'downlink_high': frequency,
                'mode': 'BPSK',
                'baud': 1200,
                'alive': True,
                'status': 'active',
            },
            {
                'norad_cat_id': 99001,
                'uuid': 'inactive',
                'downlink_low': frequency,
                'alive': False,
                'status': 'inactive',
            },
        ],
        'meta': {'fetched_utc': '2026-11-27T00:00:00Z', 'sources': {'transmitters': 'synthetic fixture'}},
    }
    for name, data in files.items():
        (root / (name + '.json')).write_text(json.dumps(data))
    return root


@pytest.fixture
def local(tmp_path):
    cache(tmp_path / 'satdb')
    (tmp_path / 'qth.json').write_text(json.dumps(dict(lat_deg=0.0, lon_deg=0.0, alt_m=0.0, min_elevation_deg=0)))
    return tmp_path


def patch_orbits(monkeypatch):
    sd = sat.backend(ROOT)
    monkeypatch.setattr(sd, 'tle_eligibility', lambda *a, **k: {'eligible': True})

    def predict(db, qth, start, stop, bands, **kwargs):
        s = next(iter(db.values()))
        times = np.array([start, start + 30, start + 60, start + 90, start + 120])
        return [
            dict(
                norad=99001,
                name=s['name'],
                t=times,
                el=np.array([10, 30, -5, 20, 10]),
                tle_epoch_utc='2026-11-27T00:00:00Z',
                transmitters=s['transmitters'],
            )
        ]

    monkeypatch.setattr(sd, 'predict', predict)

    def topo(l1, l2, times, qth):
        n = len(times)
        return np.full(n, 20.0), np.full(n, 45.0), np.full(n, 800e3), np.full(n, 1000.0)

    monkeypatch.setattr(sd, 'topo', topo)
    return sd


def spec(session=None):
    return dict(session=session, lo_hz=100e6, hi_hz=500e6, seconds=120, now=1000.0, frame_time=1030.0)


def test_passes_split_and_inactive_transmitters_excluded(local, monkeypatch):
    patch_orbits(monkeypatch)
    result = sat.compute(ROOT, local, spec())
    assert len(result['catalog']) == 1
    assert len(result['passes']) == 2
    assert result['passes'][0]['set_utc'] < result['passes'][1]['rise_utc']
    assert result['identity_confirmed'] is False
    assert result['curves'][0]['range_rate_m_s'][0] == 1000
    json.dumps(result, allow_nan=False)


def test_frozen_record_uses_its_own_frequency_and_survives_missing_current_qth(local, monkeypatch):
    sd = patch_orbits(monkeypatch)
    folder = local / 'superscan-20261127-000000'
    folder.mkdir()
    sd.snapshot(local / 'satdb', folder / 'reference', local / 'qth.json')
    cache(local / 'satdb', frequency=435100000)
    result = sat.compute(ROOT, local, spec(folder.name))
    assert result['catalog'][0]['f_hz'] == 435100000
    assert result['curves'][0]['transmitters'][0]['f_hz'] == 145825000
    assert result['frame_reference'] == 'frozen session'
    (local / 'qth.json').unlink()
    result = sat.compute(ROOT, local, spec(folder.name))
    assert result['status'] == 'observer_required'
    assert result['curves']


def test_corrupted_reference_never_falls_back_to_current_orbit(local, monkeypatch):
    sd = patch_orbits(monkeypatch)
    folder = local / 'superscan-20261127-000000'
    folder.mkdir()
    sd.snapshot(local / 'satdb', folder / 'reference', local / 'qth.json')
    (folder / 'reference' / 'satdb' / 'tle.json').write_text('[]')
    result = sat.compute(ROOT, local, spec(folder.name))
    assert result['frame_error'] and not result['curves']
    assert result['catalog']


def test_prepare_freezes_reference_before_rx_and_rejects_changed_cache(local, monkeypatch):
    patch_orbits(monkeypatch)
    payload = sat.prepare(ROOT, local, dict(norad=99001, transmitter_id='test-tx', seconds=300))
    assert payload['satellite']['norad'] == 99001
    assert payload['values']['ensure_coverage'] and payload['values']['overlap'] == 75
    low, high = map(float, payload['values']['freq'].split(':'))
    assert low < 145825000 < high
    planned = gui_jobs.plan(payload, local)
    assert planned['satellite'] == payload['satellite']
    folder = local / 'expert-20261127-000000-12345678'
    folder.mkdir()
    sat.freeze(ROOT, local, folder, payload['satellite'])
    assert (folder / 'satellite-target.json').exists()
    assert (folder / 'reference' / 'bundle-manifest.json').exists()
    cache(local / 'satdb', frequency=435100000)
    with pytest.raises(ValueError, match='changed'):
        sat.freeze(ROOT, local, folder, payload['satellite'])


def test_reference_escape_rejected(local, tmp_path):
    folder = local / 'superscan-20261127-000000'
    folder.mkdir()
    (folder / 'reference').symlink_to('/tmp')
    with pytest.raises(ValueError, match='escaped'):
        sat.sources(local, folder.name)


def test_async_calculation_and_cache_are_bounded(local):
    service = sat.SatelliteService(ROOT, local)
    value = dict(session=None, lo_hz=100e6, hi_hz=500e6, seconds=120, frame_time=time.time())
    try:
        start = time.monotonic()
        answer = service.snapshot(value)
        assert time.monotonic() - start < 0.2 and answer['status'] == 'loading'
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            answer = service.snapshot(value)
            if answer['status'] != 'loading':
                break
            time.sleep(0.03)
        assert answer['status'] == 'ready', answer
        assert answer['catalog'][0]['uuid'] == 'test-tx'
        assert len(service.results) <= 4
    finally:
        service.close()


def test_refresh_failure_preserves_prior_cache_and_publishes_status(local):
    sd = sat.backend(ROOT)
    before = sat.identity(local / 'satdb')
    with mock.patch.object(sd, 'refresh', side_effect=RuntimeError('network unavailable')):
        with pytest.raises(RuntimeError):
            sat.refresh_worker(ROOT, local)
    assert sat.identity(local / 'satdb') == before
    assert json.loads((local / 'satnogs-refresh.json').read_text())['state'] == 'failed'


def test_refresh_uses_satnogs_tle_without_unnecessary_fallback(local):
    sd = sat.backend(ROOT)
    l1, l2 = tle()
    calls = []

    def get(url):
        calls.append(url)
        if '/tle/' in url:
            return json.dumps([{'norad_cat_id': 99001, 'tle1': l1, 'tle2': l2}]).encode()
        return b'[{"norad_cat_id":99001}]'

    with mock.patch.object(sd, '_get', side_effect=get):
        sd.refresh(local / 'satdb')
    assert len(calls) == 3 and all('db.satnogs.org' in url for url in calls)


def test_target_metadata_cannot_inject_reference_paths(local):
    with pytest.raises(ValueError):
        gui_jobs.plan({'command': 'acquisition', 'satellite': {'reference': '/tmp'}}, local)


def test_browser_prediction_math_and_context_suppression(tmp_path):
    import shutil, subprocess

    node = shutil.which('node')
    if not node:
        pytest.skip('Node needed for browser-side calculation test')
    script = r'''
const vm=require('vm'),fs=require('fs'),assert=require('assert');
const fields=new Map();
const field=id=>{if(!fields.has(id))fields.set(id,{checked:true,addEventListener(){}});return fields.get(id);};
const context={finite:Number.isFinite,$:field,setTimeout(){},model:{selected:'session'}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
vm.runInContext(`
const curve={name:'Synthetic',norad:99001,times_s:[1000,1002],el_deg:[20,20],az_deg:[359,1],range_rate_m_s:[1000,1000],transmitters:[{f_hz:100e6,f_high_hz:100e6,mode:'BPSK'}]};
satelliteUI.data={frame_session:'session',frame_min_el_deg:0,curves:[curve]};
const live={start:new Date(1000000).toISOString(),stop:new Date(1002000).toISOString(),frequencies_hz:[99.9e6,100.1e6]};
globalThis.answer={az:satelliteAt(curve,1001).az,frequency:satelliteMarkers(live)[0].hz,count:satelliteMarkers(live).length};
model.selected='other';answer.wrongContext=satelliteMarkers(live).length;
model.selected='session';live.stop=new Date(2000000).toISOString();live.start=live.stop;answer.stale=satelliteMarkers(live).length;
`,context);
assert.equal(context.answer.az,0);assert.equal(context.answer.count,1);
assert(context.answer.frequency<100e6);assert.equal(context.answer.wrongContext,0);assert.equal(context.answer.stale,0);
'''
    path = tmp_path / 'check.cjs'
    path.write_text(script)
    subprocess.run([node, str(path), str(ROOT / 'soapypower/web/satellites.js')], check=True, timeout=10)


def test_stale_tle_is_catalog_only_and_cannot_start_a_target(local):
    path = local / 'satdb' / 'tle.json'
    rows = json.loads(path.read_text())
    line = rows[0]['l1']
    line = line[:18] + '20001.00000000' + line[32:68]
    rows[0]['l1'] = line + str(sum(int(c) if c.isdigit() else int(c == '-') for c in line) % 10)
    path.write_text(json.dumps(rows))
    value = dict(session=None, lo_hz=100e6, hi_hz=500e6, seconds=120, now=time.time(), frame_time=time.time())
    result = sat.compute(ROOT, local, value)
    assert result['catalog'] and result['stale_or_invalid_tles'] == 1
    assert not result['passes'] and not result['curves']
    with pytest.raises(ValueError, match='fresh eligible TLE'):
        sat.prepare(ROOT, local, dict(norad=99001, transmitter_id='test-tx', seconds=300))


def test_queued_target_expiry_is_rechecked_before_snapshot(local, monkeypatch):
    sd = patch_orbits(monkeypatch)
    payload = sat.prepare(ROOT, local, dict(norad=99001, transmitter_id='test-tx', seconds=300))
    monkeypatch.setattr(sd, 'tle_eligibility', lambda *a, **k: {'eligible': False})
    folder = local / 'expert-20261127-000000-12345678'
    folder.mkdir()
    with pytest.raises(ValueError, match='expired while queued'):
        sat.freeze(ROOT, local, folder, payload['satellite'])
    assert not (folder / 'reference').exists()


def test_satellite_refresh_can_run_while_receiver_lease_is_held(local):
    import fcntl

    sd = sat.backend(ROOT)
    with (local / '.receiver.lock').open('w') as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with mock.patch.object(sd, 'refresh', return_value={'fetched_utc': '2026-11-27T00:00:00Z'}):
            sat.refresh_worker(ROOT, local)
    assert json.loads((local / 'satnogs-refresh.json').read_text())['state'] == 'finished'


def test_sky_tracks_refined_bounded_and_separate_from_frame_curves(local, monkeypatch):
    patch_orbits(monkeypatch)
    result = sat.compute(ROOT, local, spec())
    for p in result['passes']:
        track = p['sky_track']
        assert 2 <= len(track['times_s']) <= 96
        assert track['times_s'][0] == p['rise_utc'] and track['times_s'][-1] == p['set_utc']
        assert all(np.isfinite(track[k]).all() for k in ('el_deg', 'az_deg', 'range_m', 'range_rate_m_s'))
        assert np.all(np.diff(track['times_s']) > 0)
    assert result['curves'][0]['times_s'] != result['passes'][0]['sky_track']['times_s']


def test_sky_projection_playback_and_receiver_clock_independence(tmp_path):
    import shutil, subprocess

    node = shutil.which('node')
    if not node:
        pytest.skip('Node needed for sky-view calculations')
    script = r'''
const vm=require('vm'),fs=require('fs'),assert=require('assert');
const fields=new Map(),drawing=new Proxy({},{get:(o,k)=>o[k]||(()=>{}),set:(o,k,v)=>(o[k]=v,true)});
function field(id){if(!fields.has(id))fields.set(id,{checked:true,value:'',addEventListener(){},setAttribute(){},replaceChildren(){},append(){},scrollIntoView(){},getBoundingClientRect(){return{width:400,height:400,left:0,top:0};},getContext(){return drawing;}});return fields.get(id);}
const context={finite:Number.isFinite,$:field,setTimeout(){},clearTimeout(){},requestAnimationFrame(){return 1;},cancelAnimationFrame(){},
 document:{hidden:false,addEventListener(){},createElement(){return{append(){}};}},window:{devicePixelRatio:1},
 IntersectionObserver:class{observe(){}},ResizeObserver:class{observe(){}},
 model:{selected:'recording',detail:{live:{stop:'original-RF-timestamp'}}},format:(v)=>String(v),put:(id,v)=>field(id).textContent=v};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
vm.runInContext(fs.readFileSync(process.argv[3],'utf8'),context);
vm.runInContext(`
const north=skyProjection(0,0,100,100,90),east=skyProjection(90,0,100,100,90),zenith=skyProjection(220,90,100,100,90);
const pass={name:'Synthetic',norad:99001,rise_utc:1000,set_utc:2000,max_el_deg:60,max_el_utc:1500,transmitters:[{f_hz:100e6}],sky_track:{times_s:[1000,1500,2000],el_deg:[1,60,1],az_deg:[350,0,10],range_m:[1e6,500e3,1e6],range_rate_m_s:[-1000,0,1000]}};
const data={cache_identity:'a',observer:{lat_deg:0,min_elevation_deg:0},lo_hz:1e8,hi_hz:5e8,passes:[pass]};
syncSkyView(data,true);skySeek(.2);skyView.playing=true;skyView.speed=30;drawSkyView(1000);drawSkyView(1500);
const advanced=skyView.time;
document.hidden=true;drawSkyView(2000);const hidden=skyView.time;
document.hidden=false;drawSkyView(3000);const returned=skyView.time;
const changed={...data,passes:[{...pass,rise_utc:1100}]};syncSkyView(changed);
const stable=skyView.passes[0].rise_utc;
const current=skyState(pass.sky_track,1500),outside=skyState(pass.sky_track,2500);
globalThis.answer={north,east,zenith,advanced,hidden,returned,stable,current,outside,rf:model.detail.live.stop};
`,context);
const r=context.answer;
assert(Math.abs(r.north.x-100)<1e-8&&Math.abs(r.north.y-10)<1e-8);
assert(Math.abs(r.east.x-190)<1e-8&&Math.abs(r.east.y-100)<1e-8);
assert.equal(r.zenith.x,100);assert.equal(r.zenith.y,100);
assert.equal(r.advanced,1215);assert.equal(r.hidden,1215);assert.equal(r.returned,1215);
assert.equal(r.stable,1000);assert.equal(r.current.az,0);assert.equal(r.current.range,500000);
assert.equal(r.outside,null);assert.equal(r.rf,'original-RF-timestamp');
'''
    path = tmp_path / 'sky.cjs'
    path.write_text(script)
    subprocess.run(
        [node, str(path), str(ROOT / 'soapypower/web/satellites.js'), str(ROOT / 'soapypower/web/skyview.js')],
        check=True,
        timeout=10,
    )
