"""Relative IF tomography of stable, high-SNR overlapping RF observations.

Unknown received RF power is a nuisance parameter, never an absolute reference.
The bipartite graph, temporal repeats and independent validation must support
identification. Conditional fit errors cannot bound propagation/model bias.
"""
import copy
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

from soapypower.calibration import CONTEXT_FIELDS, _compare, require_fixed_gain
from soapypower.calibration_workflows import (_sha256, _check_coordinates, average_capture,
                                             band_summary, DSP_FIELDS)
from soapypower.files import load_json
from soapypower.numerics import finite, integer


def require_observable_state(context):
    require_fixed_gain(context)
    for field in ('hardware','driver','antenna','sample_rate','bandwidth','gains','channel','corr_ppm'):
        if context.get(field) is None:
            raise ValueError('passive references require known '+field)
    gains=context['gains']
    if not isinstance(gains,dict) or not gains or any(not isinstance(name,str) or not name for name in gains):
        raise ValueError('passive references require actual gain-register readbacks')
    for name,value in gains.items():finite(value,'actual gain '+name)
    finite(context['sample_rate'],'actual sample rate',positive=True)
    finite(context['bandwidth'],'actual bandwidth',nonnegative=True)
    finite(context['corr_ppm'],'frequency correction readback')
    integer(context['channel'],'actual RX channel')
    if context['sample_format']!='CF32':raise ValueError('passive capture requires known CF32 stream format')


def _solve(signal_ids, positions, values_db, standard_db, anchor, huber=1.5):
    signals, knots = sorted(set(signal_ids)), sorted(set(positions))
    if len(signals) < 2 or len(knots) < 2 or anchor not in knots:
        raise ValueError('relative fit needs two RF sources, two IF positions and an observed anchor')
    if len(signals)+len(knots) > 512:
        raise ValueError('overlap graph exceeds 512 source/IF nodes')
    edges = set(zip(signal_ids,positions))
    for signal in signals:
        if len({bb for sid,bb in edges if sid==signal}) < 2:
            raise ValueError('each RF source must link at least two IF positions')
    for knot in knots:
        if len({sid for sid,bb in edges if bb==knot}) < 2:
            raise ValueError('each IF position needs at least two independent RF sources')
    n = len(signals)+len(knots)-1
    if len(edges) <= n:
        raise ValueError('overlap graph needs redundant cycles, not a just-determined tree')
    signal_col = {value:i for i,value in enumerate(signals)}
    position_col = {value:len(signals)+i for i,value in enumerate(k for k in knots if k!=anchor)}
    design = np.zeros((len(values_db),n))
    for row,(signal,position) in enumerate(zip(signal_ids,positions)):
        design[row,signal_col[signal]]=1
        if position!=anchor:design[row,position_col[position]]=1
    y, sigma = np.asarray(values_db,dtype=float),np.asarray(standard_db,dtype=float)
    if not np.isfinite(y).all() or not np.isfinite(sigma).all() or np.any(sigma<=0):
        raise ValueError('relative observations need finite dB values and positive standard errors')
    if float(sigma.max()/sigma.min()) > 1e12:
        raise ValueError('relative measurement weight range is ill-conditioned')
    robust=np.ones(len(y));beta=None;inverse=None
    for iteration in range(50):
        weight=np.sqrt(robust)/sigma
        matrix=design*weight[:,None]
        u,s,vt=np.linalg.svd(matrix,full_matrices=False)
        if len(s)!=n or s[-1] <= s[0]*1e-10:
            raise ValueError('disconnected or ill-conditioned overlap graph')
        inverse=(vt.T/s)@u.T
        next_beta=inverse@(y*weight)
        standardized=abs((y-design@next_beta)/sigma)
        next_robust=np.ones(len(y));large=standardized>huber
        next_robust[large]=huber/standardized[large]
        if beta is not None and np.max(abs(next_beta-beta))<1e-9:
            beta=next_beta;robust=next_robust;break
        beta=next_beta;robust=next_robust
    else:
        raise ValueError('robust overlap solver did not converge')
    # Final covariance is explicitly conditional on final fixed Huber weights.
    weight=np.sqrt(robust)/sigma
    matrix=design*weight[:,None]
    u,s,vt=np.linalg.svd(matrix,full_matrices=False)
    inverse=(vt.T/s)@u.T;beta=inverse@(y*weight)
    covariance=(inverse*robust[None,:])@inverse.T
    residual=y-design@beta
    response=[0.0 if k==anchor else float(beta[position_col[k]]) for k in knots]
    std=[0.0 if k==anchor else math.sqrt(max(0,float(covariance[position_col[k],position_col[k]]))) for k in knots]
    return {'baseband_hz':knots,'response_db':response,'conditional_standard_db':std,
            'conditional_interval_95_db':[[v-1.96*s,v+1.96*s] for v,s in zip(response,std)],
            'residual_db':residual.tolist(),'robust_weights':robust.tolist(),
            'downweighted_observations':np.flatnonzero(robust<1-1e-8).tolist(),
            'rank':n,'condition_number':float(s[0]/s[-1]),'unique_edges':len(edges),
            'signal_levels_db':{sid:float(beta[signal_col[sid]]) for sid in signals},
            'uncertainty_scope':'conditional known independent dB errors and fixed final robust weights; propagation bias and robust-selection uncertainty excluded'}


def measure_channel(capture, rf, bb, start, stop, background_start, background_stop):
    """Adjacent-background subtraction is explicit, high-SNR and conditional."""
    _check_coordinates(capture,rf,bb)
    if not start <= rf < stop or start>=stop or background_start>=background_stop:
        raise ValueError('invalid signal/background integration bands')
    if max(start,background_start)<min(stop,background_stop):
        raise ValueError('background band must not overlap signal band')
    signal=band_summary(capture,start,stop)
    background=band_summary(capture,background_start,background_stop)
    noise=background['mean_density']*signal['measured_bandwidth_hz']
    net=signal['power']-noise
    if net<=0:
        raise ValueError('received channel does not exceed adjacent background')
    snr=None if noise==0 else 10*(math.log10(net)-math.log10(noise))
    return {'net_raw_power':net,'power_db':10*math.log10(net),'snr_db':snr,
            'signal':signal,'background':background,
            'background_model':'locally representative adjacent density; not measured receiver noise figure'}


def build_overlap(manifest_path):
    manifest=load_json(manifest_path)
    allowed={'version','reference','stationarity_confirmed','context_unchanged_confirmed',
             'reference_baseband_hz','min_segments','min_snr_db','min_repeats',
             'max_repeat_span_db','max_fit_residual_db','max_validation_residual_db',
             'narrowband_response_confirmed','max_band_fraction','cells'}
    if (not isinstance(manifest,dict) or set(manifest)-allowed or type(manifest.get('version')) is not int
            or manifest['version']!=1):raise ValueError('invalid overlap manifest version or fields')
    if not isinstance(manifest.get('reference'),str) or not manifest['reference'].strip():
        raise ValueError('overlap reference must identify signals and measurement procedure')
    if manifest.get('stationarity_confirmed') is not True or manifest.get('context_unchanged_confirmed') is not True:
        raise ValueError('overlap fitting requires stationarity and unchanged-path confirmations')
    if manifest.get('narrowband_response_confirmed') is not True:
        raise ValueError('relative IF fitting requires verified locally flat response across each measured channel band')
    band_fraction=finite(manifest.get('max_band_fraction',0.1),'maximum signal bandwidth / IF spacing',positive=True)
    if band_fraction>0.25:raise ValueError('signal bandwidth fraction must not exceed 0.25')
    anchor=finite(manifest.get('reference_baseband_hz'),'relative IF anchor')
    minimum=integer(manifest.get('min_segments',32),'minimum segments',1)
    repeats=integer(manifest.get('min_repeats',2),'minimum repeats',2)
    snr_limit=finite(manifest.get('min_snr_db',10),'minimum SNR dB',nonnegative=True)
    stability=finite(manifest.get('max_repeat_span_db',0.5),'repeat span dB',positive=True)
    fit_limit=finite(manifest.get('max_fit_residual_db',0.25),'fit residual dB',positive=True)
    validation_limit=finite(manifest.get('max_validation_residual_db',0.25),'validation residual dB',positive=True)
    cells=manifest.get('cells')
    if not isinstance(cells,list) or not cells or len(cells)>20000:
        raise ValueError('overlap manifest needs 1..20000 observations')
    root=Path(manifest_path).resolve().parent
    context,dsp=None,None
    rows=[];hashes={};split_hashes={'train':set(),'validation':set()};source_coordinates={};groups=defaultdict(list)
    for cell in cells:
        fields={'capture','signal_id','rf_hz','baseband_hz','start_hz','stop_hz',
                'background_start_hz','background_stop_hz','standard_error_db','repeat_id','split'}
        if not isinstance(cell,dict) or set(cell)!=fields:raise ValueError('invalid overlap observation fields')
        sid=cell['signal_id'];repeat=cell['repeat_id'];split=cell['split'];name=cell['capture']
        if not all(isinstance(x,str) and x.strip() for x in (sid,repeat,name)) or split not in split_hashes:
            raise ValueError('overlap observations need signal/repeat/capture identifiers and train/validation split')
        rf,bb,start,stop,bgstart,bgstop=[finite(cell[k],k) for k in ('rf_hz','baseband_hz','start_hz','stop_hz','background_start_hz','background_stop_hz')]
        source=(rf,start,stop,bgstart,bgstop)
        if sid in source_coordinates and source_coordinates[sid]!=source:
            raise ValueError('one signal identifier must retain the same RF and integration band')
        source_coordinates[sid]=source
        error=finite(cell['standard_error_db'],'measurement standard error dB',positive=True)
        capture=average_capture(root/name,minimum,require_complete=True)
        candidate=capture['metadata']['context'];require_observable_state(candidate)
        current_dsp={k:capture['metadata'][k] for k in DSP_FIELDS}
        if context is None:context,dsp=copy.deepcopy(candidate),current_dsp
        else:_compare(context,candidate);_compare(dsp,current_dsp,'overlap DSP')
        measured=measure_channel(capture,rf,bb,start,stop,bgstart,bgstop)
        if measured['snr_db'] is not None and measured['snr_db']<snr_limit:
            raise ValueError('overlap observation has insufficient net SNR')
        if any(r['repeat_id']==repeat for r in groups[(split,sid,bb)]):
            raise ValueError('duplicate repeat identifier at one overlap edge')
        digest=capture['capture_sha256'];hashes[name]=digest
        if any(r['capture_sha256']==digest for r in groups[(split,sid,bb)]):
            raise ValueError('repeat captures for one RF/IF edge must not reuse the same data')
        split_hashes[split].add(digest)
        row=dict(cell,rf_hz=rf,baseband_hz=bb,standard_error_db=error,
                 capture_sha256=digest,power_db=measured['power_db'],measurement=measured,
                 capture_time_start=capture.get('time_start'),capture_time_stop=capture.get('time_stop'))
        rows.append(row);groups[(split,sid,bb)].append(row)
    if split_hashes['train'] & split_hashes['validation']:
        raise ValueError('training and validation captures must be disjoint')
    for group in groups.values():
        if len({r['repeat_id'] for r in group})<repeats:
            raise ValueError('each signal/IF/split edge needs independent repeated observations')
        values=[r['power_db'] for r in group]
        if max(values)-min(values)>stability:
            raise ValueError('received signal is not stable across repeated observations')
    train=[r for r in rows if r['split']=='train'];validation=[r for r in rows if r['split']=='validation']
    if not train or not validation:raise ValueError('separate training and validation observations are mandatory')
    result=_solve([r['signal_id'] for r in train],[r['baseband_hz'] for r in train],
                  [r['power_db'] for r in train],[r['standard_error_db'] for r in train],anchor)
    if max(abs(x) for x in result['residual_db'])>fit_limit:
        raise ValueError('overlap fit residual exceeds acceptance; no automatic outlier deletion')
    knots=result['baseband_hz'];responses=result['response_db']
    max_width=float(np.min(np.diff(knots)))*band_fraction
    if any(value[2]-value[1]>max_width for value in source_coordinates.values()):
        raise ValueError('channel integration is too broad for pointwise IF-response modeling; use narrower reference components')
    if set(r['baseband_hz'] for r in validation)!=set(knots):
        raise ValueError('independent validation must cover every measured IF knot')
    validation_groups=defaultdict(list)
    for row in validation:
        validation_groups[row['signal_id']].append(row)
    validation_errors=[]
    for sid,group in validation_groups.items():
        if len({r['baseband_hz'] for r in group})<2:
            raise ValueError('each validation signal must span multiple IF positions')
        corrected=np.array([r['power_db']-responses[knots.index(r['baseband_hz'])] for r in group])
        sigma=np.array([r['standard_error_db'] for r in group]);weight=(sigma.min()/sigma)**2
        level=float(np.sum(weight*corrected)/np.sum(weight))
        validation_errors.extend((corrected-level).tolist())
    if max(abs(x) for x in validation_errors)>validation_limit:
        raise ValueError('independent overlap validation failed; relative profile not published')
    profile={'kind':'relative_if_response','version':1,'reference':manifest['reference'],'context':context,
             'processing':{'detrend':None},'reference_baseband_hz':anchor,
             'baseband_hz':knots,'response_db':responses,
             'rf_range_hz':[min(v[0] for v in source_coordinates.values()),max(v[0] for v in source_coordinates.values())],
             'method_details':{'manifest_sha256':_sha256(manifest_path),'capture_sha256':hashes,
                 'fit':result,'validation_residual_db':validation_errors,'observations':rows,
                 'acceptance':{'max_repeat_span_db':stability,'max_fit_residual_db':fit_limit,
                               'max_validation_residual_db':validation_limit},
                 'narrowband_response_confirmed':True,'max_band_fraction':band_fraction,
                 'maximum_channel_bandwidth_hz':max_width,
                 'absolute_power_identifiable':False,'noise_figure_identifiable':False,
                 'phase_identifiable':False,'sample_clock_identifiable':False,
                 'model':'M(signal,IF)=unknown_received_signal_level+relative_IF_response; RF/LO separability assumed',
                 'limitations':['Common time/tuning-correlated fading can imitate receiver response despite residual checks.',
                                'Finite channel bands measure spectrum-weighted response; pointwise use assumes local flatness.',
                                'Adjacent-background subtraction is conditional and cannot establish NF.',
                                'Conditional fit intervals are not a complete metrology uncertainty budget.',
                                'RF/IF interpolation inside the measured domain still needs physical validation.']}}
    return RelativeIFCalibration(profile)


class RelativeIFCalibration:
    """Relative-only response interface for offline PSD equalization, never dBm."""
    def __init__(self,profile):
        required={'kind','version','reference','context','processing','reference_baseband_hz',
                  'baseband_hz','response_db','rf_range_hz','method_details'}
        if not isinstance(profile,dict) or set(profile)!=required or profile['kind']!='relative_if_response' or type(profile['version']) is not int or profile['version']!=1:
            raise ValueError('invalid relative IF profile schema')
        if not isinstance(profile['reference'],str) or not profile['reference'].strip():raise ValueError('relative profile requires reference')
        ctx=profile['context']
        if not isinstance(ctx,dict) or set(ctx)!=CONTEXT_FIELDS:raise ValueError('relative profile needs complete context')
        require_observable_state(ctx)
        if profile['processing']!={'detrend':None}:raise ValueError('relative profiles require raw undetrended measurements')
        x=np.asarray(profile['baseband_hz'],dtype=float);y=np.asarray(profile['response_db'],dtype=float);rf=np.asarray(profile['rf_range_hz'],dtype=float)
        if (x.ndim!=1 or len(x)<2 or y.shape!=x.shape or not np.isfinite(x).all() or not np.isfinite(y).all()
                or np.any(np.diff(x)<=0) or rf.shape!=(2,) or not np.isfinite(rf).all() or rf[0]>=rf[1]):
            raise ValueError('invalid relative response domain')
        anchor=finite(profile['reference_baseband_hz'],'relative anchor')
        if anchor not in x or abs(float(np.interp(anchor,x,y)))>1e-9:raise ValueError('relative response anchor must be observed and zero')
        rate=finite(ctx['sample_rate'],'relative sample rate',positive=True)
        if np.any(abs(x)>rate/2):raise ValueError('relative IF grid exceeds Nyquist')
        self._profile=copy.deepcopy(profile);self._x=x.copy();self._y=y.copy();self._rf=rf.copy()
        self.profile_id=hashlib.sha256(json.dumps(profile,sort_keys=True,allow_nan=False,separators=(',',':')).encode()).hexdigest()
    @property
    def profile(self):return copy.deepcopy(self._profile)
    @property
    def context(self):return copy.deepcopy(self._profile['context'])
    @property
    def domain(self):return {'rf_hz':tuple(self._rf),'raw_baseband_hz':(float(self._x[0]),float(self._x[-1]))}
    def validate_context(self,context):_compare(self._profile['context'],context);require_fixed_gain(context)
    def validate_processing(self,detrend,remove_dc,allow_estimated_dc=False):
        if detrend is not None or remove_dc:raise ValueError('relative response requires no detrending or DC interpolation')
    def offsets_db(self,freqs,baseband_freqs=None):
        rf=np.asarray(freqs,dtype=float)
        bb=np.full_like(rf,self._profile['reference_baseband_hz']) if baseband_freqs is None else np.asarray(baseband_freqs,dtype=float)
        if (rf.shape!=bb.shape or not np.isfinite(rf).all() or not np.isfinite(bb).all() or
                np.any(rf<self._rf[0]) or np.any(rf>self._rf[1]) or np.any(bb<self._x[0]) or np.any(bb>self._x[-1])):
            raise ValueError('relative IF lookup outside measured RF/IF domain; no extrapolation')
        return -np.interp(bb,self._x,self._y)
    def metadata(self):
        return {'profile_sha256':self.profile_id,'reference':self._profile['reference'],'model':'relative_if_only',
                'reference_plane':'relative received-path comparison; absolute input level unknown',
                'reference_baseband_hz':self._profile['reference_baseband_hz'],
                'absolute_power_calibration':False,'uncertainty_evaluation':{'status':'conditional_model_only'},
                'method_details':copy.deepcopy(self._profile['method_details'])}


def load_response_profile(path):
    profile=load_json(path)
    if isinstance(profile,dict) and profile.get('kind')=='relative_if_response':
        return RelativeIFCalibration(profile)
    from soapypower.calibration import PowerCalibration
    return PowerCalibration(profile)
