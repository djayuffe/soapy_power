"""Interpret recorded density grids for reports without fabricating masked bins."""
import numpy as np


def density(record, usable=.35, dc_hz=.5e6, start_hz=70e6, stop_hz=6e9):
    metadata=record.get('metadata') or {}
    power=np.asarray(record['power'],float).copy()
    first=float(record['first_bin_center_hz'])
    step=float(record.get('step_hz',record.get('bin_width_hz')))
    if power.ndim!=1 or not np.isfinite([first,step]).all() or step<=0:
        raise ValueError('Invalid recorded density grid')
    for index in set(record.get('masked_bins',[]))|set(record.get('estimated_bins',[])):
        if type(index) is not int or not 0<=index<len(power):raise ValueError('Invalid masked/estimated bin')
        power[index]=np.nan
    center=metadata.get('tuned_center_hz')
    if center is None:center=.5*(record['coverage_start_hz']+record['coverage_stop_hz'])
    center=float(center)
    half=metadata.get('sample_rate_hz')
    half=float(half)/2 if half else .5*(record['coverage_stop_hz']-record['coverage_start_hz'])
    frequency=first+step*np.arange(len(power))
    unit=record.get('unit','dB(input power/Hz)')
    if unit not in ('dB(input power/Hz)','dBm/Hz','input power/Hz','mW/Hz'):
        raise ValueError('Unsupported density unit: '+str(unit))
    with np.errstate(over='ignore',invalid='ignore'):
        linear=np.power(10.,power/10) if unit.startswith('dB') else power
    keep=np.isfinite(linear)&(linear>=0)&(np.abs(frequency-center)<=usable*2*half)&(np.abs(frequency-center)>=dc_hz)&(frequency>=start_hz)&(frequency<stop_hz)
    return frequency,linear,keep,center
