"""Measured receiver-control profiles, distinct from absolute power calibration."""
import bisect
import copy
import hashlib
import json
import math
from pathlib import Path

from soapypower.files import load_json
from soapypower.calibration import CONTEXT_FIELDS, require_fixed_gain, require_reference_context
from soapypower.numerics import finite
from soapypower.receiver_quality import lower_bladerf1_gain


def gains(value):
    if not isinstance(value, dict) or not value or any(not isinstance(k, str) or not k for k in value):
        raise ValueError('profile gains require named stages')
    return {k: finite(v, 'profile gain') for k, v in value.items()}


class ScanProfile:
    """Bind empirical gain/DC controls to an observable receiver and RF state.

    Points are exact/local observations; bands are explicitly approximate gain
    seeds. DC never follows a gain change unless an independently validated
    anchor matches that exact stage configuration and frequency domain.
    """
    def __init__(self, value):
        if isinstance(value, (str, Path)):
            value = load_json(value)
        fields = {'version', 'type', 'reference', 'identity', 'sample_rate_hz', 'bandwidth_hz',
                  'antenna', 'settings', 'expected_hardware_info', 'points', 'bands',
                  'point_tolerance_hz', 'dc_anchors', 'dc_max_distance_hz', 'dc_domain_edges_hz',
                  'gain_strategy', 'gain_ladder', 'gain_readback_tolerance_db', 'control_context'}
        if not isinstance(value, dict) or set(value)-fields or type(value.get('version')) is not int or value['version'] != 1 or value.get('type') != 'receiver_scan_profile':
            raise ValueError('invalid receiver scan profile schema/version')
        self.value = copy.deepcopy(value)
        if not isinstance(value.get('reference'), str) or not value['reference'].strip():
            raise ValueError('receiver profile needs its measured reference/procedure')
        identity = value.get('identity')
        if not isinstance(identity, dict) or set(identity) != {'driver', 'hardware', 'serial'} or any(not isinstance(v, str) or not v.strip() for v in identity.values()):
            raise ValueError('profile requires driver, hardware and observable serial identity')
        self.rate = finite(value.get('sample_rate_hz'), 'profile sample rate', positive=True)
        self.tolerance = finite(value.get('gain_readback_tolerance_db', .01), 'gain readback tolerance', nonnegative=True)
        self.point_tolerance = finite(value.get('point_tolerance_hz', 2), 'point tolerance', nonnegative=True)
        self.dc_distance = finite(value.get('dc_max_distance_hz', 0), 'DC maximum distance', nonnegative=True)
        for name in ('points','bands','dc_anchors','gain_ladder','dc_domain_edges_hz'):
            if not isinstance(value.get(name,[]),list):
                raise ValueError(name+' must be a list')
        self.edges = [finite(x, 'DC domain boundary') for x in value.get('dc_domain_edges_hz', [])]
        if self.edges != sorted(set(self.edges)):
            raise ValueError('DC domain boundaries must be increasing and unique')
        if identity['hardware'].lower() == 'bladerf1' and 237.5e6 not in self.edges:
            self.edges = sorted(set(self.edges + [237.5e6]))
        for name in ('settings', 'expected_hardware_info'):
            if not isinstance(value.get(name, {}), dict):
                raise ValueError(name+' must be a mapping')
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.get('settings', {}).items()):
            raise ValueError('profile settings must be string pairs')
        if 'bandwidth_hz' in value:
            finite(value['bandwidth_hz'],'profile bandwidth',nonnegative=True)
        if 'antenna' in value and (not isinstance(value['antenna'],str) or not value['antenna']):
            raise ValueError('profile antenna must be a nonempty name')
        self.points = []
        for point in value.get('points', []):
            if not isinstance(point, dict) or set(point) != {'center_hz', 'gains'}:
                raise ValueError('invalid profile point')
            self.points.append(dict(center_hz=finite(point['center_hz'], 'profile center'), gains=gains(point['gains'])))
        self.points.sort(key=lambda p:p['center_hz'])
        if len({p['center_hz'] for p in self.points}) != len(self.points):
            raise ValueError('duplicate profile center')
        self.bands = []
        for band in value.get('bands', []):
            if not isinstance(band, dict) or set(band) != {'start_hz', 'stop_hz', 'gains'}:
                raise ValueError('invalid profile band')
            lo, hi = finite(band['start_hz'], 'band start'), finite(band['stop_hz'], 'band stop')
            if lo >= hi:
                raise ValueError('profile bands must be ascending')
            self.bands.append(dict(start_hz=lo, stop_hz=hi, gains=gains(band['gains'])))
        self.bands.sort(key=lambda b:b['start_hz'])
        if any(a['stop_hz'] > b['start_hz'] for a,b in zip(self.bands,self.bands[1:])):
            raise ValueError('profile bands must not overlap')
        if not self.points and not self.bands:
            raise ValueError('profile needs measured points or explicit gain-seed bands')
        controls = CONTEXT_FIELDS-{'driver','hardware','serial','hardware_info','gains'}
        self.controls = copy.deepcopy(value.get('control_context',{}))
        if not isinstance(self.controls,dict) or set(self.controls)-controls:
            raise ValueError('invalid receiver profile control_context')
        if value.get('dc_anchors') and set(self.controls) != controls:
            raise ValueError('manual DC anchors require complete observed control_context')
        if value.get('dc_anchors'):
            require_reference_context(dict(self.controls,hardware=identity['hardware'],driver=identity['driver'],
                serial=identity['serial'],hardware_info=value.get('expected_hardware_info',{}),
                gains=(self.points+self.bands)[0]['gains']))
        self.anchors = []
        for a in value.get('dc_anchors', []):
            if not isinstance(a, dict) or set(a) != {'center_hz','gains','normalized_dc','validated'} or type(a['validated']) is not bool:
                raise ValueError('invalid DC anchor')
            pair = a['normalized_dc']
            if not isinstance(pair, list) or len(pair) != 2 or any(abs(finite(v,'DC control')) > 1 for v in pair):
                raise ValueError('DC controls require two normalized components')
            self.anchors.append(dict(center_hz=finite(a['center_hz'],'DC center'),gains=gains(a['gains']),normalized_dc=[finite(v,'DC control') for v in pair],validated=a['validated']))
        if self.anchors and identity['hardware'].lower() == 'bladerf1' and not value.get('expected_hardware_info',{}).get('lms_dc_state_at_open'):
            raise ValueError('bladeRF1 manual DC profile requires observed LMS calibration state')
        self.strategy = value.get('gain_strategy', 'reject')
        if self.strategy not in ('reject', 'ladder', 'bladerf1_stages'):
            raise ValueError('unknown profile gain strategy')
        if self.strategy == 'bladerf1_stages' and identity['hardware'].lower() != 'bladerf1':
            raise ValueError('bladeRF1 backoff requires that exact hardware')
        if self.strategy == 'bladerf1_stages':
            for seed in self.points+self.bands:
                lower_bladerf1_gain(seed['gains'])
        self.ladder = [gains(v) for v in value.get('gain_ladder', [])]
        if self.strategy == 'ladder':
            if not self.ladder or len({tuple(sorted(v.items())) for v in self.ladder}) != len(self.ladder):
                raise ValueError('gain ladder must contain unique states')
            if any(set(a) != set(b) or sum(a.values()) <= sum(b.values()) for a,b in zip(self.ladder,self.ladder[1:])):
                raise ValueError('gain ladder must use identical stages in strictly descending order')
            if any(p['gains'] not in self.ladder for p in self.points+self.bands):
                raise ValueError('every gain seed must belong to the measured ladder')
        encoded = json.dumps(value, sort_keys=True, separators=(',',':'), allow_nan=False).encode()
        self.sha256 = hashlib.sha256(encoded).hexdigest()

    def validate_context(self, context):
        require_fixed_gain(context)
        for key,expected in self.controls.items():
            if context.get(key) != expected:
                raise ValueError('receiver profile control state mismatch: '+key)
        for key, expected in self.value['identity'].items():
            actual = context.get(key)
            if (str(actual).lower() != expected.lower() if key == 'driver' else actual != expected):
                raise ValueError('receiver profile identity mismatch: '+key)
        if not math.isclose(context.get('sample_rate', 0), self.rate, rel_tol=1e-12, abs_tol=1e-6):
            raise ValueError('receiver profile sample rate mismatch')
        if context.get('agc') is not False:
            raise ValueError('receiver profile requires observed fixed gain / AGC off')
        for key in ('bandwidth_hz', 'antenna'):
            field = 'bandwidth' if key == 'bandwidth_hz' else key
            if key in self.value and context.get(field) != self.value[key]:
                raise ValueError('receiver profile mismatch: '+field)
        for key, expected in self.value.get('settings', {}).items():
            if context.get('device_settings', {}).get(key) != expected:
                raise ValueError('receiver profile device setting mismatch: '+key)
        for key, expected in self.value.get('expected_hardware_info', {}).items():
            if context.get('hardware_info', {}).get(key) != expected:
                raise ValueError('receiver profile hardware state mismatch: '+key)

    def select_gain(self, center):
        center = finite(center, 'profile RF center')
        if self.points:
            p = min(self.points, key=lambda p:abs(p['center_hz']-center))
            if abs(p['center_hz']-center) <= self.point_tolerance:
                return p['gains'].copy(), 'measured_point'
        for b in self.bands:
            if b['start_hz'] <= center < b['stop_hz']:
                return b['gains'].copy(), 'approximate_band_seed'
        raise ValueError('receiver profile has no supported gain seed at this frequency')

    def correction(self, center, actual_gains):
        domain = bisect.bisect_right(self.edges, center)
        choices = [a for a in self.anchors if a['validated'] and a['gains'] == actual_gains and
                   bisect.bisect_right(self.edges,a['center_hz']) == domain and abs(a['center_hz']-center) <= self.dc_distance]
        if not choices:
            return [0., 0.], None
        a = min(choices,key=lambda a:abs(a['center_hz']-center))
        return a['normalized_dc'].copy(), a['center_hz']

    def check_gains(self, actual, expected):
        if not isinstance(actual,dict) or set(actual) != set(expected) or any(not math.isclose(finite(actual[k],'observed gain'),expected[k],rel_tol=0,abs_tol=self.tolerance) for k in expected):
            raise ValueError('receiver profile gain readback differs from requested stages')

    def lower_gain(self, current):
        if self.strategy == 'bladerf1_stages':
            return lower_bladerf1_gain(current)
        if self.strategy == 'ladder':
            if current not in self.ladder:
                raise ValueError('actual gain is not in the measured backoff ladder')
            index = self.ladder.index(current) + 1
            return self.ladder[index].copy() if index < len(self.ladder) else None
        return None


def import_campaign_profile(value, context, reference, gain_ladder=None, point_tolerance_hz=2):
    """Convert retained bladeRF/RTL campaign controls without inventing RF support.

    The caller supplies a freshly observed context and native gain ladder.
    Dense points remain local points; sparse observations are never silently
    turned into wide RF bands. Band seeds retain their approximate semantics.
    """
    if not isinstance(value,dict) or not isinstance(context,dict):
        raise ValueError('campaign and observed context must be mappings')
    result = dict(version=1,type='receiver_scan_profile',reference=reference,
        identity={k:context.get(k) for k in ('driver','hardware','serial')},
        sample_rate_hz=context.get('sample_rate'),bandwidth_hz=context.get('bandwidth'),
        antenna=context.get('antenna'),settings=context.get('device_settings',{}),
        point_tolerance_hz=point_tolerance_hz,points=[],bands=[],dc_anchors=[])
    if 'sample_rate_hz' in value and value['sample_rate_hz'] != result['sample_rate_hz']:
        raise ValueError('campaign rate differs from observed receiver')
    if str(context.get('hardware')).lower() == 'bladerf1':
        from soapypower.receiver_quality import require_lms_calibration_state
        require_lms_calibration_state(value.get('lms_state_at_fit'),context.get('hardware_info',{}))
        if value.get('serial') != context.get('serial') or value.get('bandwidth_hz') != context.get('bandwidth'):
            raise ValueError('bladeRF campaign identity/bandwidth mismatch')
        controls = CONTEXT_FIELDS-{'driver','hardware','serial','hardware_info','gains'}
        result['control_context'] = {k:context.get(k) for k in controls}
        result.update(gain_strategy='bladerf1_stages',
            expected_hardware_info={'lms_dc_state_at_open':value['lms_state_at_fit']},
            dc_max_distance_hz=value.get('dc_max_anchor_distance_hz',0))
        for band in value.get('bands',[]):
            result['bands'].append({k:band[k] for k in ('start_hz','stop_hz','gains')})
            for a in band.get('dc_anchors',[]):
                result['dc_anchors'].append({k:a[k] for k in ('center_hz','gains','normalized_dc','validated')})
    elif str(context.get('driver')).lower() == 'rtlsdr':
        if not gain_ladder:
            raise ValueError('RTL import requires an independently observed descending native gain ladder')
        result.update(gain_strategy='ladder',gain_ladder=[{'TUNER':g} for g in gain_ladder])
        for point in value.get('points',[]):
            gain = point.get('gain_db',point.get('selected_gain_db'))
            result['points'].append(dict(center_hz=point['center_hz'],gains={'TUNER':gain}))
    else:
        raise ValueError('unsupported campaign schema; create an explicit receiver_scan_profile')
    profile = ScanProfile(result)
    profile.validate_context(context)
    return profile
