"""Selected-interval and isolated-tone diagnostics, without protocol decoding."""
import math
from pathlib import Path
import numpy as np
from soapypower.numerics import finite, integer
from soapypower.calibration_workflows import _sha256
from soapypower.files import load_json


def _iq(path):
    samples=np.load(path,mmap_mode='r',allow_pickle=False)
    if not isinstance(samples,np.ndarray):
        if hasattr(samples,'close'):samples.close()
        raise ValueError('IQ diagnostics require one .npy array')
    if samples.ndim!=1 or samples.dtype.kind!='c' or samples.dtype.itemsize not in (8,16):
        raise ValueError('IQ must be one-dimensional complex64/complex128')
    return samples


def tone_audit(path,sample_rate,expected_baseband_hz,reference_rf_hz,frame_size=4096,frames=128,
               search_hz=500,min_tone_fraction=0.8):
    """Lag-one phase estimate with coherent single-tone validation per frame.

    Select an isolated tone interval before running. No burst discovery, GSM
    identity, TETRA synchronization, absolute clock, or correction sign is inferred.
    """
    fs=finite(sample_rate,'sample rate',positive=True)
    expected=finite(expected_baseband_hz,'expected raw baseband frequency')
    rf=finite(reference_rf_hz,'nominal RF reference',positive=True)
    search=finite(search_hz,'frequency search radius',positive=True)
    fraction=finite(min_tone_fraction,'minimum coherent tone fraction',positive=True)
    n=integer(frame_size,'tone frame size',16);maximum=integer(frames,'maximum tone frames',2)
    if maximum>10000 or n>1048576:raise ValueError('tone work limits exceeded')
    if fraction>1 or abs(expected)+search>=fs/2 or abs(expected)<=search:
        raise ValueError('tone search must avoid DC and Nyquist and use a fraction <=1')
    samples=_iq(path);count=min(maximum,len(samples)//n)
    if count<2:raise ValueError('at least two complete tone frames required')
    times=[];estimates=[];fractions=[];rejected=[]
    for i in range(count):
        x=np.asarray(samples[i*n:(i+1)*n],dtype=np.complex128)
        if not np.isfinite(x).all():raise ValueError('selected IQ samples must be finite')
        scale=float(np.max(abs(x)))
        if not math.isfinite(scale):raise ValueError('IQ amplitude outside float64 range')
        if not scale:rejected.append(i);continue
        x=x/scale
        correlation=np.vdot(x[:-1],x[1:])
        measured=float(np.angle(correlation))*fs/(2*math.pi)
        error=(measured-expected+fs/2)%fs-fs/2
        carrier=np.exp(-2j*math.pi*(measured/fs)*np.arange(n))
        total=float(np.mean(abs(x)**2))
        coherent=float(abs(np.mean(x*carrier))**2)/total
        coherent=min(1.0,max(0.0,coherent))
        if abs(error)>search or coherent<fraction:
            rejected.append(i);continue
        times.append((i*n+(n-1)/2)/fs);estimates.append(error);fractions.append(coherent)
    if len(estimates)<2:raise ValueError('insufficient validated isolated-tone frames; do not treat modulated carriers as CW')
    t=np.asarray(times);e=np.asarray(estimates);center=t-float(t.mean())
    beta=np.linalg.lstsq(np.column_stack((center,np.ones(len(t)))),e,rcond=None)[0]
    residual=e-(beta[0]*center+beta[1])
    return {'kind':'passive_tone_diagnostic','version':1,'input_sha256':_sha256(path),
            'sample_rate_hz':fs,'expected_baseband_hz':expected,'reference_rf_hz':rf,
            'frame_size':n,'examined_frames':count,'accepted_frames':len(estimates),'rejected_frame_indices':rejected,
            'unexamined_samples':len(samples)-count*n,'times_from_start_s':times,
            'apparent_frequency_errors_hz':estimates,'coherent_tone_fractions':fractions,
            'mean_apparent_frequency_error_hz':float(np.mean(e)),
            'apparent_reference_error_ppm':float(np.mean(e))/rf*1e6,
            'apparent_drift_hz_per_s':float(beta[0]),'residual_rms_hz':float(np.sqrt(np.mean(residual**2))),
            'automatic_frequency_correction_applied':False,'protocol_identified':False,
            'limitations':['Observed error combines receiver LO/sample clock, transmitter frequency, Doppler and path effects.',
                           'Positive reported error means measured raw-IQ frequency exceeds expected; it is not a driver PPM correction sign.',
                           'Coherent interference or a wrong reference can pass these waveform checks.',
                           'Frame variation is a diagnostic, not a traceable frequency uncertainty.']}


def interval_audit(path,manifest_path):
    """Compare operator-identified reduced-activity and active intervals only."""
    manifest=load_json(manifest_path)
    if not isinstance(manifest,dict) or set(manifest)!={'version','reference','intervals'} or type(manifest['version']) is not int or manifest['version']!=1:
        raise ValueError('invalid interval manifest')
    if not isinstance(manifest['reference'],str) or not manifest['reference'].strip():raise ValueError('interval reference required')
    intervals=manifest['intervals']
    if not isinstance(intervals,list) or not intervals:raise ValueError('interval list required')
    samples=_iq(path);rows=[];groups={'active':[],'quiet':[]};previous=0
    for interval in intervals:
        if not isinstance(interval,dict) or set(interval)!={'start_sample','stop_sample','label'}:raise ValueError('invalid interval fields')
        start=integer(interval['start_sample'],'interval start');stop=integer(interval['stop_sample'],'interval stop',1)
        label=interval['label']
        if label not in groups or start<previous or stop<=start or stop>len(samples):
            raise ValueError('intervals must be sorted, disjoint, in bounds and labeled active/quiet')
        previous=stop;power=0.0;count=0
        for offset in range(start,stop,65536):
            x=np.asarray(samples[offset:min(stop,offset+65536)],dtype=np.complex128)
            if not np.isfinite(x).all():raise ValueError('selected interval samples must be finite')
            with np.errstate(over='ignore',invalid='ignore'):
                mean=float(np.mean(x.real*x.real+x.imag*x.imag))
            if not math.isfinite(mean):raise ValueError('interval power outside float64 range; rescale IQ')
            power+=(mean-power)*(len(x)/(count+len(x)));count+=len(x)
        rows.append(dict(interval,mean_raw_power=power,sample_count=count));groups[label].append((count,power))
    if any(not value for value in groups.values()):raise ValueError('both active and quiet intervals are required')
    totals={key:sum(count for count,_ in value) for key,value in groups.items()}
    means={key:math.fsum(power*(count/totals[key]) for count,power in value) for key,value in groups.items()}
    return {'kind':'reduced_activity_diagnostic','version':1,'reference':manifest['reference'],
            'input_sha256':_sha256(path),'manifest_sha256':_sha256(manifest_path),'intervals':rows,
            'sample_weighted_mean_power':means,
            'active_over_quiet_db':10*(math.log10(means['active'])-math.log10(means['quiet'])) if min(means.values())>0 else None,
            'noise_figure_identifiable':False,'absolute_power_identifiable':False,'protocol_identified':False,
            'limitations':['Quiet intervals may contain TII, other transmitters, interference, propagation tails and receiver transients.',
                           'Operator-selected intervals are not a protocol decoder or a calibrated noise-source state.']}
