"""Validated native processing-chain metadata and explicitly partial digital models."""
import copy
import numpy as np
from soapypower.numerics import finite,integer


def validate_chain(value):
    if not isinstance(value,dict) or value.get('schema')!='soapypower_native_uhd_v1':
        raise ValueError('invalid native chain schema')
    result=copy.deepcopy(value)
    for key in ('sample_rate_hz','bandwidth_hz','master_clock_hz'):
        result[key]=finite(value.get(key),'native '+key,positive=True)
    if not isinstance(value.get('native_format'),str) or not value['native_format']:
        raise ValueError('native format required')
    result['native_full_scale']=finite(value.get('native_full_scale'),'native full scale',positive=True)
    filters=result.get('filters')
    if filters is not None:
        if not isinstance(filters,list) or len(filters)>128:raise ValueError('invalid filter list/budget')
        names=set();total=0
        for f in filters:
            if not isinstance(f,dict) or not isinstance(f.get('name'),str) or not f['name'] or f['name'] in names:
                raise ValueError('native filters require unique names')
            names.add(f['name'])
            if type(f.get('bypassed')) is not bool:raise ValueError('filter bypass must be known boolean')
            if 'taps' in f:
                if not isinstance(f['taps'],list):raise ValueError('filter taps require array')
                total+=len(f['taps'])
                if total>65536 or len(f['taps'])>8192:raise ValueError('filter coefficient budget exceeded')
                f['input_rate_hz']=finite(f.get('input_rate_hz'),'filter input rate',positive=True)
                f['decimation']=integer(f.get('decimation'),'filter decimation',1)
                f['interpolation']=integer(f.get('interpolation'),'filter interpolation',1)
                f['full_scale']=finite(f.get('full_scale'),'filter scale',positive=True)
                f['taps']=[finite(v,'filter coefficient') for v in f['taps']]
                if not f['bypassed'] and not f['taps']:raise ValueError('active digital filter has no taps')
            if 'cutoff_hz' in f:finite(f['cutoff_hz'],'analog nominal cutoff',positive=True)
    for key in ('iq_tracking','dc_tracking','has_power_reference'):
        if value.get(key) is not None and type(value[key]) is not bool:raise ValueError('invalid native '+key)
    return result


def digital_response(chain,frequency_hz):
    """DC-normalized exposed digital response, never a whole-receiver equalizer."""
    from scipy.signal import freqz
    chain=validate_chain(chain);f=np.asarray(frequency_hz,dtype=float)
    if f.ndim!=1 or not len(f) or len(f)>1000000 or not np.isfinite(f).all():raise ValueError('invalid filter frequency grid/budget')
    h=np.ones(len(f),complex);delay=0.;used=[]
    for stage in chain.get('filters') or []:
        if stage['bypassed'] or 'taps' not in stage:continue
        taps=np.asarray(stage['taps']);scale=float(np.sum(taps))
        if not scale:raise ValueError('cannot DC-normalize a zero-sum filter')
        if np.any(abs(f)>=stage['input_rate_hz']/2):raise ValueError('filter model queried beyond a stage Nyquist limit')
        _,response=freqz(taps,1,worN=f,fs=stage['input_rate_hz']);h*=response/scale
        if delay is not None and np.allclose(taps,taps[::-1],rtol=0,atol=1e-12):delay+=(len(taps)-1)/2/stage['input_rate_hz']
        else:delay=None
        used.append(stage['name'])
    if not used:raise ValueError('no exposed active digital filters')
    return dict(frequency_hz=f,response=h,exposed_linear_phase_delay_s=delay,filters=used,
                complete_receiver_model=False,absolute_power_calibrated=False,
                rf_phase_calibrated=False,inverse_correction_authorized=False)
