"""Delivered-IQ headroom and explicit receiver-state guards, not RF calibration."""
import math
import numpy as np

def headroom_metrics(samples):
    z=np.asarray(samples)
    if z.ndim!=1 or z.dtype.kind!='c' or not len(z) or not np.isfinite(z).all():
        raise ValueError('headroom requires finite one-dimensional complex IQ')
    # Test uncentered components: removing a DC mean before the guard can hide
    # a saturated analog/ADC path. Centering is used only for AC RMS reporting.
    component=np.maximum(np.abs(z.real),np.abs(z.imag))
    mean=np.mean(z,dtype=np.complex128)
    centered=z-mean
    return dict(max_component=float(component.max()),
                p999_component=float(np.quantile(component,.999)),
                threshold_095_fraction=float(np.mean(component>=.95)),
                mean_i=float(mean.real),mean_q=float(mean.imag),
                mean_power=float(abs(mean)**2),
                ac_rms=float(np.sqrt(np.mean(abs(centered)**2))))

def headroom_passes(metrics,max_component=.85,p999_component=.60):
    for v in (max_component,p999_component):
        if not math.isfinite(v) or not 0<v<=1:raise ValueError('invalid headroom limit')
    for key in ('max_component','p999_component','threshold_095_fraction'):
        value=metrics.get(key)
        if not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
            raise ValueError('invalid headroom metrics')
    return bool(metrics['max_component']<max_component and
                metrics['p999_component']<p999_component and
                metrics['threshold_095_fraction']==0)

def require_lms_calibration_state(expected,hardware_info):
    """Bind a manual DC profile to its observed LMS state, fail closed."""
    if not isinstance(expected,str) or not expected:
        raise ValueError('profile must specify its LMS calibration state')
    actual=hardware_info.get('lms_dc_state_at_open')
    if actual!=expected:
        raise ValueError('LMS calibration state is missing or differs from the fitted profile')
    return actual

def lower_bladerf1_gain(gains):
    """One bounded headroom backoff: late VGA, early VGA, then RF LNA."""
    if set(gains)!= {'lna','rxvga1','rxvga2'}:
        raise ValueError('bladeRF1 requires explicit lna/rxvga1/rxvga2 stages')
    lna,vga1,vga2=(gains[k] for k in ('lna','rxvga1','rxvga2'))
    if lna not in (0,3,6) or not isinstance(vga1,(int,float)) or not 5<=vga1<=30 or int(vga1)!=vga1 or vga2 not in range(0,31,3):
        raise ValueError('invalid bladeRF1 gain state')
    result=dict(gains)
    if vga2>0:result['rxvga2']=max(0,vga2-6)
    elif vga1>5:result['rxvga1']=max(5,vga1-6)
    elif lna>0:result['lna']=max(0,lna-3)
    else:return None
    return result

def matching_dc_anchor(anchors,gains,center_hz,max_distance_hz=150e6):
    """Never carry DC values across gain changes or XB mix/bypass domains."""
    if not math.isfinite(center_hz) or not math.isfinite(max_distance_hz) or max_distance_hz<0:
        raise ValueError('invalid DC anchor coordinate')
    choices=[a for a in anchors if a.get('validated') is True and a.get('gains')==gains
             and (a['center_hz']<237.5e6)==(center_hz<237.5e6)
             and abs(a['center_hz']-center_hz)<=max_distance_hz]
    return min(choices,key=lambda a:abs(a['center_hz']-center_hz)) if choices else None
