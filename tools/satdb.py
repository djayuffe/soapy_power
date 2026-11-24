#!/usr/bin/env python3
"""Satellite database, pass/Doppler prediction and identification of observed tracks.

  satdb.py refresh CACHE_DIR            fetch SatNOGS transmitters + satellites + TLEs (fallback: CelesTrak TLE groups)
  satdb.py passes CACHE_DIR QTH.json START_UTC SECONDS [--lo-mhz X --hi-mhz Y]   list predicted in-band passes

Data: SatNOGS DB (db.satnogs.org, CC BY-SA) for transmitters (downlink, mode, baud, status) and satellites;
TLEs from SatNOGS DB or CelesTrak. Propagation with the `sgp4` package (TEME -> ECEF via GMST, WGS84 observer).
Doppler: f_rx = f_tx * (1 - range_rate / c). Identification compares an observed track f(t) with every
predicted in-band curve, allowing a constant transmitter offset; the residual must follow the curve.
Format: the transmitter's mode/baud gives an expected occupied bandwidth that is compared with the observed one
(PSD only: modulation is inferred from spectral shape and timing, nothing is demodulated or decoded)."""
import datetime, json, math, sys, urllib.request
from pathlib import Path
import numpy as np
from report_bundle import bundle, verify
import shutil

DEFAULT_MAX_TLE_AGE_DAYS = 14.0

C = 299792458.0
WGS_A, WGS_F = 6378137.0, 1 / 298.257223563
OMEGA_E = 7.2921150e-5
UA = {'User-Agent': 'soapy_power-satdb/1 (+https://github.com/djayuffe/soapy_power)'}
SATNOGS = 'https://db.satnogs.org/api/{}/?format=json'
CELESTRAK = 'https://celestrak.org/NORAD/elements/gp.php?GROUP={}&FORMAT=tle'
CELESTRAK_GROUPS = ('amateur', 'cubesat', 'weather', 'noaa', 'orbcomm', 'satnogs', 'active')


# ------------------------------------------------------------------------------------------------ fetch
def _get(url, timeout=90):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def catalog_number(text):
    text = text.strip()
    if text.isdigit(): return int(text)
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
    if len(text) == 5 and text[0] in alphabet and text[1:].isdigit():
        return (10 + alphabet.index(text[0]))*10000 + int(text[1:])
    raise ValueError('invalid TLE catalog number')


def valid_tle(l1, l2):
    try:
        if not (len(l1)>=69 and len(l2)>=69 and l1.startswith('1 ') and l2.startswith('2 ')): return False
        if catalog_number(l1[2:7]) != catalog_number(l2[2:7]): return False
        return all(line[68].isdigit() and sum(int(x) if x.isdigit() else int(x=='-') for x in line[:68])%10==int(line[68]) for line in (l1,l2))
    except (ValueError,TypeError): return False


def validate_qth(qth):
    lat, lon, alt = [float(qth.get(k, 0.0)) for k in ('lat_deg','lon_deg','alt_m')]
    if not {'lat_deg','lon_deg'} <= qth.keys() or not all(math.isfinite(v) for v in (lat,lon,alt)) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('observer position must have finite WGS84 latitude/longitude and altitude')
    return qth


def tle_eligibility(l1, l2, t0, t1, max_age_days=DEFAULT_MAX_TLE_AGE_DAYS):
    if not math.isfinite(max_age_days) or max_age_days <= 0:
        raise ValueError('max TLE age must be finite and positive')
    result = dict(eligible=False, max_age_days=max_age_days, reason='invalid TLE epoch', tle_epoch_utc=None, tle_age_days_at_scan_midpoint=None)
    try:
        if not valid_tle(l1,l2): return result
        from sgp4.api import Satrec
        sat = Satrec.twoline2rv(l1,l2)
        epoch = (sat.jdsatepoch + sat.jdsatepochF - 2440587.5)*86400
        if not math.isfinite(epoch) or not l1.startswith('1 ') or not l2.startswith('2 '): return result
        age = ((t0+t1)/2-epoch)/86400
        valid = max(abs(t0-epoch),abs(t1-epoch))/86400 <= max_age_days
        result.update(eligible=valid, reason='within configured epoch-age window' if valid else 'outside configured epoch-age window',
                      tle_epoch_utc=datetime.datetime.fromtimestamp(epoch,datetime.timezone.utc).isoformat(), tle_age_days_at_scan_midpoint=age)
    except (ValueError, AttributeError, OverflowError): pass
    return result


def snapshot(cache, destination, qth_path, max_age_days=DEFAULT_MAX_TLE_AGE_DAYS):
    cache = Path(cache).resolve(); qth_path = Path(qth_path).resolve()
    validate_qth(json.loads(qth_path.read_text()))
    if not math.isfinite(max_age_days) or max_age_days <= 0: raise ValueError('invalid TLE age policy')
    files = [cache/(n+'.json') for n in ('tle','satellites','transmitters','meta')]
    if not all(p.is_file() for p in files): raise ValueError('incomplete satellite cache')
    if not all(isinstance(json.loads(p.read_text()),list) and json.loads(p.read_text()) for p in files[:3]): raise ValueError('empty/invalid satellite cache')
    with bundle(destination, files+[qth_path]) as out:
        db = out/'satdb'; db.mkdir()
        for p in files: shutil.copyfile(p,db/p.name)
        shutil.copyfile(qth_path,out/'qth.json')
        (out/'policy.json').write_text(json.dumps(dict(max_tle_age_days=max_age_days, identity_confirmed_by_psd=False)))
    return verify(destination)


def parse_tle_text(text):
    out, lines = [], [l.rstrip() for l in text.splitlines() if l.strip()]
    i = 0
    while i < len(lines) - 1:
        if lines[i].startswith('1 ') and lines[i + 1].startswith('2 '):
            name, l1, l2 = None, lines[i], lines[i + 1]; i += 2
        elif i + 2 < len(lines) and lines[i + 1].startswith('1 ') and lines[i + 2].startswith('2 '):
            name, l1, l2 = lines[i].lstrip('0 ').strip(), lines[i + 1], lines[i + 2]; i += 3
        else:
            i += 1; continue
        if not valid_tle(l1,l2): continue
        out.append(dict(name=name or l1[2:7].strip(), norad=catalog_number(l1[2:7]), l1=l1, l2=l2))
    return out


def _fetch_generation(cache):
    cache = Path(cache); cache.mkdir(parents=True, exist_ok=True); meta = dict(fetched_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(), sources={})
    failures = []
    for what in ('transmitters', 'satellites'):
        try:
            data = json.loads(_get(SATNOGS.format(what)))
            if not isinstance(data, list) or not data: raise ValueError('empty or malformed response')
            (cache / f'{what}.json').write_text(json.dumps(data)); meta['sources'][what] = f'satnogs ({len(data)})'
        except Exception as e:
            failures.append(what)
            meta['sources'][what] = f'failed: {e}'
    tles = {}
    try:
        for t in json.loads(_get(SATNOGS.format('tle'))):
            if t.get('tle1') and t.get('tle2') and valid_tle(t['tle1'],t['tle2']):
                tles[int(t['norad_cat_id'])] = dict(name=(t.get('tle0') or '').lstrip('0 ').strip(), norad=int(t['norad_cat_id']), l1=t['tle1'], l2=t['tle2'])
        meta['sources']['tle_satnogs'] = len(tles)
    except Exception as e:
        meta['sources']['tle_satnogs'] = f'failed: {e}'
    for g in CELESTRAK_GROUPS:
        try:
            got = parse_tle_text(_get(CELESTRAK.format(g)).decode('ascii', 'replace'))
            for t in got:
                tles.setdefault(t['norad'], t)
            meta['sources'][f'tle_celestrak_{g}'] = len(got)
        except Exception as e:
            meta['sources'][f'tle_celestrak_{g}'] = f'failed: {e}'
        if g == 'satnogs' and len(tles) > 500:
            break                                   # 'active' is large; only needed when the others failed
    if failures or not tles:
        raise RuntimeError('incomplete satellite refresh; previous generation preserved: '+','.join(failures or ['tle']))
    if tles:
        (cache / 'tle.json').write_text(json.dumps(sorted(tles.values(), key=lambda t: t['norad'])))
    meta['tle_count'] = len(tles)
    (cache / 'meta.json').write_text(json.dumps(meta, indent=1))
    return meta


def refresh(cache):
    with bundle(cache) as stage:
        meta = _fetch_generation(stage)
    return meta


def load(cache):
    """norad -> dict(name, status, l1, l2, transmitters=[...]) for satellites with a TLE and >= 1 downlink."""
    cache = Path(cache).resolve()
    if not (cache / 'tle.json').exists():
        return {}
    tles = {t['norad']: t for t in json.loads((cache / 'tle.json').read_text())}
    sats = {}
    if (cache / 'satellites.json').exists():
        for s in json.loads((cache / 'satellites.json').read_text()):
            if s.get('norad_cat_id'):
                sats[int(s['norad_cat_id'])] = s
    tx = json.loads((cache / 'transmitters.json').read_text()) if (cache / 'transmitters.json').exists() else []
    db = {}
    for t in tx:
        n = t.get('norad_cat_id'); f = t.get('downlink_low')
        if not n or not f or int(n) not in tles:
            continue
        n = int(n); s = sats.get(n, {})
        e = db.setdefault(n, dict(norad=n, name=s.get('name') or tles[n]['name'], sat_status=s.get('status', 'unknown'), l1=tles[n]['l1'], l2=tles[n]['l2'], transmitters=[]))
        e['transmitters'].append(dict(description=t.get('description') or '', f_hz=float(f), f_high_hz=float(t['downlink_high']) if t.get('downlink_high') else None,
                                      mode=t.get('mode') or '', baud=float(t['baud']) if t.get('baud') else None, alive=bool(t.get('alive', True)),
                                      status=t.get('status', ''), type=t.get('type', ''), service=t.get('service', ''), drift=t.get('downlink_drift')))
    return db


def load_tles(cache):
    """Every cached TLE (also satellites without a listed transmitter), name from SatNOGS when known."""
    cache = Path(cache).resolve()
    if not (cache / 'tle.json').exists():
        return []
    names = {}; statuses = {}
    if (cache / 'satellites.json').exists():
        statuses = {int(s['norad_cat_id']): s.get('status','unknown') for s in json.loads((cache / 'satellites.json').read_text()) if s.get('norad_cat_id')}
        names = {int(s['norad_cat_id']): s.get('name') for s in json.loads((cache / 'satellites.json').read_text()) if s.get('norad_cat_id')}
    return [dict(t, name=names.get(t['norad']) or t['name'],sat_status=statuses.get(t['norad'],'unknown')) for t in json.loads((cache / 'tle.json').read_text())]


def match_orbit(times, freqs, tles, qth, track_rms, max_rms=350.0, max_tle_age_days=DEFAULT_MAX_TLE_AGE_DAYS):
    """Which orbit produced an observed Doppler curve, independent of any transmitter list: the curve is compared
    with f_c * (1 - range_rate/c) of every satellite above the horizon (f_c = observed centre, constant offset free)."""
    times = np.asarray(times, float); freqs = np.asarray(freqs, float); fc = float(np.median(freqs)); out = []
    k = np.argsort(times); times, freqs = times[k], freqs[k]
    for t in tles:
        if not eligible_satellite(t): continue
        if not tle_eligibility(t['l1'],t['l2'],float(times.min()),float(times.max()),max_tle_age_days).get('eligible'): continue
        try:
            el, az, rng, rr = topo(t['l1'], t['l2'], times, qth)
        except Exception:
            continue
        if not np.isfinite(el).all() or (el > -2).sum() < 0.8 * len(el):
            continue
        fp = fc * (1 - rr / C); d = freqs - fp; off = float(np.median(d)); res = d - off
        rms = float(np.sqrt(np.mean(res ** 2))); ps = float(fp[-1] - fp[0]); os_ = float(freqs[-1] - freqs[0])
        if abs(ps) < 1e3 or abs(os_ - ps) > max(500.0, 0.3 * abs(ps)):
            continue
        out.append(dict(norad=t['norad'], name=t['name'], rms_hz=rms, offset_hz=off, pred_shift_hz=ps, obs_shift_hz=os_, max_el=float(np.nanmax(el))))
    out.sort(key=lambda c: c['rms_hz'])
    return [c for c in out if c['rms_hz'] < max(max_rms, 2.0 * track_rms)]


# ------------------------------------------------------------------------------------------- propagation
def _gmst(jd_ut1):
    T = (jd_ut1 - 2451545.0) / 36525.0
    s = 67310.54841 + (876600 * 3600 + 8640184.812866) * T + 0.093104 * T ** 2 - 6.2e-6 * T ** 3
    return np.radians((s % 86400) / 240.0)


def observer_ecef(lat, lon, alt):
    la, lo = math.radians(lat), math.radians(lon); e2 = WGS_F * (2 - WGS_F); N = WGS_A / math.sqrt(1 - e2 * math.sin(la) ** 2)
    return np.array([(N + alt) * math.cos(la) * math.cos(lo), (N + alt) * math.cos(la) * math.sin(lo), (N * (1 - e2) + alt) * math.sin(la)])


def look(target_ecef, qth):
    """Elevation and azimuth (deg, azimuth from true north through east) of ECEF targets seen from the
    observer, in the observer's geodetic (WGS84 ellipsoid-normal) east-north-up frame; also range (m) and the
    line-of-sight vectors. Exact geometry: valid at any latitude, no flat-earth or spherical approximation."""
    o = observer_ecef(qth['lat_deg'], qth['lon_deg'], qth.get('alt_m', 0.0)); d = np.atleast_2d(target_ecef) - o; rng = np.linalg.norm(d, axis=1)
    la, lo = math.radians(qth['lat_deg']), math.radians(qth['lon_deg'])
    E = np.array([-math.sin(lo), math.cos(lo), 0]); Nn = np.array([-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)])
    U = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
    el = np.degrees(np.arcsin(np.clip(d @ U / rng, -1, 1))); az = np.degrees(np.arctan2(d @ E, d @ Nn)) % 360
    return el, az, rng, d


def topo(l1, l2, t_unix, qth):
    """elevation (deg), azimuth (deg), range (m), range-rate (m/s) for unix times t_unix (array)."""
    from sgp4.api import Satrec
    sat = Satrec.twoline2rv(l1, l2); t = np.atleast_1d(np.asarray(t_unix, float))
    jd = t / 86400.0 + 2440587.5; jd_i = np.floor(jd); fr = jd - jd_i
    err, r, v = sat.sgp4_array(jd_i, fr); r = r * 1e3; v = v * 1e3
    th = _gmst(jd); c, s = np.cos(th), np.sin(th)
    re = np.stack([c * r[:, 0] + s * r[:, 1], -s * r[:, 0] + c * r[:, 1], r[:, 2]], 1)
    ve = np.stack([c * v[:, 0] + s * v[:, 1], -s * v[:, 0] + c * v[:, 1], v[:, 2]], 1)
    ve = ve - np.cross(np.array([0, 0, OMEGA_E]), re)
    el, az, rng, d = look(re, qth)
    rr = np.sum(d * ve, 1) / rng
    bad = err != 0; el[bad] = np.nan; rr[bad] = np.nan
    return el, az, rng, rr


def in_band(f, bands, margin=25e3):
    return any(b['lo_hz'] - margin <= f <= b['hi_hz'] + margin for b in bands)


def predict(db, qth, t0, t1, bands, step=5.0, min_el=None, max_tle_age_days=DEFAULT_MAX_TLE_AGE_DAYS):
    """Satellites above the horizon in [t0, t1] with >= 1 downlink inside a scanned band."""
    if not all(math.isfinite(x) for x in (t0,t1,step)) or t1 < t0 or step <= 0: raise ValueError('invalid prediction interval/step')
    min_el = qth.get('min_elevation_deg', 0.0) if min_el is None else min_el
    t = np.arange(t0 - 60, t1 + 60 + step, step); out = []
    for n, s in db.items():
        eligibility = tle_eligibility(s['l1'],s['l2'],t0,t1,max_tle_age_days)
        if not eligibility['eligible']: continue
        txs = [x for x in s['transmitters'] if in_band(x['f_hz'], bands)]
        if not txs:
            continue
        try:
            el, az, rng, rr = topo(s['l1'], s['l2'], t, qth)
        except Exception:
            continue
        window=(t>=t0)&(t<=t1)&np.isfinite(el)
        vis = window & (np.nan_to_num(el,nan=-90)>min_el)
        if not vis.any():
            continue
        indices=np.flatnonzero(window);k=int(indices[np.argmax(el[window])])
        epoch_utc=None;age_days=None
        try:
            from sgp4.api import Satrec
            elements=Satrec.twoline2rv(s['l1'],s['l2'])
            epoch=(elements.jdsatepoch+elements.jdsatepochF-2440587.5)*86400
            epoch_utc=datetime.datetime.fromtimestamp(epoch,datetime.timezone.utc).isoformat()
            age_days=((t0+t1)/2-epoch)/86400
        except (ValueError,AttributeError):pass
        out.append(dict(norad=n, name=s['name'], sat_status=s['sat_status'], tle_epoch_utc=epoch_utc,tle_age_days_at_scan_midpoint=age_days,t=t, el=el, az=az, rng=rng, rr=rr, max_el=float(el[k]), t_max_el=float(t[k]),
                        t_rise=float(t[vis][0]), t_set=float(t[vis][-1]), transmitters=txs))
    return sorted(out, key=lambda p: -p['max_el'])


def doppler(p, f_tx, t):
    return f_tx * (1 - np.interp(t, p['t'], p['rr']) / C)


# ------------------------------------------------------------------------------------------------- format
def expected_bw_hz(mode, baud):
    """Rough occupied (≈ -10 dB) bandwidth for a SatNOGS mode string; None if unknown."""
    m = (mode or '').upper(); b = baud or 0.0
    if m in ('CW', 'MORSE'):
        return (30.0, 400.0)
    if m in ('APT',):
        return (25e3, 45e3)
    if m in ('LRPT', 'HRPT', 'OQPSK') and b >= 30e3:
        return (0.9 * b, 2.2 * b)
    if 'LORA' in m:
        return (60e3, 520e3)
    if m in ('FM', 'FMN', 'SSTV', 'DUV', 'AFSK', 'AFSK1K2', 'AFSK S-NET', 'AFSK SALSAT', 'FSK AX.100 MODE 5'):
        return (6e3, 20e3)
    if m in ('USB', 'LSB', 'SSB', 'LINEAR', 'TRANSPONDER'):
        return (1e3, 60e3)
    if any(k in m for k in ('GMSK', 'GFSK', 'FSK', 'MSK')):
        return (0.6 * b, 2.5 * b + 2e3) if b else (1e3, 40e3)
    if any(k in m for k in ('BPSK', 'QPSK', 'PSK')):
        return (0.7 * b, 2.6 * b + 1e3) if b else (300.0, 40e3)
    return None


def observed_format(bw_hz, duty, peaks, peak_sep_hz, step_hz):
    """Spectral/temporal class of an observed signal (PSD only)."""
    if bw_hz <= RES_BINS * step_hz:
        cls = f'carrier / CW-like (<= {RES_BINS} bins, at the resolution limit)'
    elif peaks >= 2 and peak_sep_hz and bw_hz < 30e3:
        cls = f'FSK-like ({peaks} tones, spacing {peak_sep_hz / 1e3:.1f} kHz)'
    elif bw_hz < 5e3:
        cls = 'narrowband digital (PSK/FSK, < 5 kHz)'
    elif bw_hz < 22e3:
        cls = 'FM / AFSK / 9k6-class digital (5-22 kHz)'
    elif bw_hz < 60e3:
        cls = 'wideband (22-60 kHz, APT-class)'
    else:
        cls = 'very wide (> 60 kHz, LRPT/LoRa-class)'
    return cls + (', bursty' if duty < 0.5 else ', continuous' if duty > 0.85 else ', intermittent')


RES_BINS = 7          # -10 dB width of a pure carrier after Blackman window, 0.5 s Doppler smear and +-0.5 bin alignment


def format_consistency(tx, bw_hz, step_hz):
    """Observed -10 dB bandwidth vs the mode's expected range, widened to the measurement's resolution limit."""
    exp = expected_bw_hz(tx.get('mode'), tx.get('baud'))
    if not exp:
        return 'unknown mode'
    res = RES_BINS * step_hz; lo, hi = exp; lo = 0.0 if lo < res else lo; hi = max(hi, res); slack = 2 * step_hz
    return 'consistent' if lo - slack <= bw_hz <= hi + slack else f'inconsistent (expected {lo / 1e3:.1f}-{hi / 1e3:.1f} kHz)'


# --------------------------------------------------------------------------------------------- identify
def eligible_transmitter(tx):
    return tx.get('alive') is True and str(tx.get('status','')).lower() in ('active','')


def eligible_satellite(p):
    return str(p.get('sat_status','')).lower() not in ('dead','re-entered','decayed')


def match_track(times, freqs, preds, max_offset_hz=8e3, min_overlap_s=20.0):
    """Best predicted (satellite, transmitter) for an observed track. Returns list of candidates sorted by score."""
    times = np.asarray(times, float); freqs = np.asarray(freqs, float); cands = []
    for p in preds:
        if not eligible_satellite(p):continue
        el = np.interp(times, p['t'], p['el'])
        up = el > -2.0
        if up.sum() < 4 or np.ptp(times[up]) < min_overlap_s:
            continue
        for tx in p['transmitters']:
            if not eligible_transmitter(tx):continue
            fp = doppler(p, tx['f_hz'], times[up]); d = freqs[up] - fp
            off = float(np.median(d))
            if abs(off) > max_offset_hz:
                continue
            res = d - off; rms = float(np.sqrt(np.mean(res ** 2)))
            shape = float(np.ptp(fp))                 # how much Doppler the prediction itself has over the overlap
            tu = times[up]; k = np.argsort(tu)
            pred_shift = float(fp[k][-1] - fp[k][0]); obs_shift = float(freqs[up][k][-1] - freqs[up][k][0])
            cands.append(dict(norad=p['norad'], name=p['name'], tx=tx, offset_hz=off, rms_hz=rms, predicted_shift_hz=shape,
                              pred_shift_hz=pred_shift, obs_shift_hz=obs_shift,
                              overlap_s=float(np.ptp(times[up])), max_el=p['max_el'], score=rms + abs(off) / 20.0))
    return sorted(cands, key=lambda c: c['score'])


def confidence(c, track_rms, observed_shift_hz):
    """strong: Doppler shape followed; probable: frequency + rough shape; frequency-only: nothing to test the shape on.
    The observed track must move with the prediction over the overlap: a steady line next to a moving curve is no match."""
    ps, os_ = c.get('pred_shift_hz', 0.0), c.get('obs_shift_hz', observed_shift_hz)
    follows = abs(ps) > 1.0e3 and abs(os_ - ps) < max(500.0, 0.3 * abs(ps))
    if follows and abs(ps) > 1.5e3 and c['rms_hz'] < max(350.0, 2.5 * track_rms) and abs(c['offset_hz']) < 4e3:
        return 'strong'
    if follows and c['rms_hz'] < max(800.0, 4 * track_rms) and abs(c['offset_hz']) < 8e3:
        return 'probable'
    return 'frequency-only'


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__); return 2
    if a[0] == 'refresh':
        print(json.dumps(refresh(a[1]), indent=1)); return 0
    if a[0] == 'snapshot':
        age = float(a[a.index('--max-tle-age-days')+1]) if '--max-tle-age-days' in a else DEFAULT_MAX_TLE_AGE_DAYS
        print(json.dumps(snapshot(a[1],a[2],a[a.index('--qth')+1],age),indent=1)); return 0
    if a[0] == 'passes':
        db = load(a[1]); qth = json.loads(Path(a[2]).read_text())
        t0 = datetime.datetime.fromisoformat(a[3]).timestamp(); t1 = t0 + float(a[4])
        lo = float(a[a.index('--lo-mhz') + 1]) * 1e6 if '--lo-mhz' in a else 0; hi = float(a[a.index('--hi-mhz') + 1]) * 1e6 if '--hi-mhz' in a else 1e12
        for p in predict(db, qth, t0, t1, [dict(lo_hz=lo, hi_hz=hi)]):
            print(f"{p['name'][:28]:28s} {p['norad']:6d} max el {p['max_el']:5.1f} at {datetime.datetime.fromtimestamp(p['t_max_el'], datetime.timezone.utc):%H:%M:%S}  "
                  + '; '.join(f"{x['f_hz'] / 1e6:.4f} {x['mode']} {int(x['baud'] or 0)}" for x in p['transmitters'][:3]))
        return 0
    print(__doc__); return 2


if __name__ == '__main__':
    sys.exit(main())
