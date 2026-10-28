"""High-SNR, repeated same-RF comparison of two fixed SDR gain states."""
import copy
import math
from collections import defaultdict
from pathlib import Path
import numpy as np
from soapypower.calibration import _compare
from soapypower.calibration_workflows import average_capture, DSP_FIELDS, _sha256
from soapypower.files import load_json
from soapypower.numerics import finite, integer
from soapypower.passive_response import measure_channel, require_observable_state


def compare_gain_states(path):
    m=load_json(path)
    allowed={'version','reference','stationarity_confirmed','min_segments','min_repeats','min_snr_db',
             'max_repeat_span_db','nominal_gain_step_db','max_nominal_error_db','cells'}
    if not isinstance(m,dict) or set(m)-allowed or type(m.get('version')) is not int or m['version']!=1:
        raise ValueError('invalid gain-comparison manifest')
    if not isinstance(m.get('reference'),str) or not m['reference'].strip() or m.get('stationarity_confirmed') is not True:
        raise ValueError('gain comparison requires a reference and confirmed stable received signal')
    minimum=integer(m.get('min_segments',32),'minimum segments',1)
    repeats=integer(m.get('min_repeats',2),'minimum repeats',2)
    snr=finite(m.get('min_snr_db',10),'minimum SNR',nonnegative=True)
    limit=finite(m.get('max_repeat_span_db',0.5),'repeat span',positive=True)
    nominal=None if m.get('nominal_gain_step_db') is None else finite(m['nominal_gain_step_db'],'nominal gain step')
    tolerance=finite(m.get('max_nominal_error_db',0.5),'nominal comparison tolerance',positive=True)
    cells=m.get('cells')
    if not isinstance(cells,list) or not cells:raise ValueError('gain comparison requires repeated cells')
    root=Path(path).resolve().parent;contexts={};dsp=None;groups=defaultdict(list);hashes={};seen={}
    for cell in cells:
        keys={'rf_hz','baseband_hz','start_hz','stop_hz','background_start_hz','background_stop_hz','reference','observed','repeat_id'}
        if not isinstance(cell,dict) or set(cell)!=keys:raise ValueError('invalid gain comparison cell')
        rf,bb,start,stop,bgstart,bgstop=[finite(cell[k],k) for k in ('rf_hz','baseband_hz','start_hz','stop_hz','background_start_hz','background_stop_hz')]
        repeat=cell['repeat_id']
        if not isinstance(repeat,str) or not repeat:raise ValueError('gain comparison needs repeat identifiers')
        values={}
        for state in ('reference','observed'):
            name=cell[state]
            if not isinstance(name,str) or not name:raise ValueError('gain comparison requires capture paths')
            capture=average_capture(root/name,minimum,require_complete=True)
            context=capture['metadata']['context'];require_observable_state(context)
            current={k:capture['metadata'][k] for k in DSP_FIELDS}
            if state in contexts:_compare(contexts[state],context,state+' gain context')
            else:contexts[state]=copy.deepcopy(context)
            if len(contexts)==2:
                _compare({k:v for k,v in contexts['reference'].items() if k!='gains'},
                         {k:v for k,v in contexts['observed'].items() if k!='gains'},'gain states excluding gain registers')
            if dsp is None:dsp=current
            else:_compare(dsp,current,'gain comparison DSP')
            digest=capture['capture_sha256'];hashes[name]=digest
            key=(rf,bb,state,digest)
            if key in seen:raise ValueError('gain-state repeats cannot reuse one capture')
            seen[key]=True
            measurement=measure_channel(capture,rf,bb,start,stop,bgstart,bgstop)
            if measurement['snr_db'] is not None and measurement['snr_db']<snr:raise ValueError('gain comparison has insufficient net SNR')
            values[state]=measurement
        if any(row['repeat_id']==repeat for row in groups[(rf,bb,start,stop)]):
            raise ValueError('duplicate gain comparison repeat identifier')
        groups[(rf,bb,start,stop)].append({'repeat_id':repeat,'measurements':values,
            'gain_delta_db':values['observed']['power_db']-values['reference']['power_db']})
    points=[]
    for (rf,bb,start,stop),rows in groups.items():
        if len({r['repeat_id'] for r in rows})<repeats:raise ValueError('each gain comparison coordinate needs independent repeats')
        ratios=np.array([r['gain_delta_db'] for r in rows]);maximum=float(ratios.max())
        mean=maximum+10*math.log10(float(np.mean(10**((ratios-maximum)/10))))
        span=float(np.ptp(ratios));error=None if nominal is None else mean-nominal
        points.append({'rf_hz':rf,'baseband_hz':bb,'band_hz':[start,stop],'mean_gain_delta_db':mean,
                       'repeat_span_db':span,'repeat_scatter_db':float(np.std(ratios,ddof=1)),
                       'nominal_error_db':error,'passed':span<=limit and (error is None or abs(error)<=tolerance),
                       'observations':rows})
    return {'kind':'gain_state_comparison','version':1,'reference':m['reference'],'contexts':contexts,
            'manifest_sha256':_sha256(path),'capture_sha256':hashes,'points':points,
            'passed':all(p['passed'] for p in points),'nominal_gain_step_db':nominal,
            'absolute_power_identifiable':False,'isolated_lna_noise_figure_identifiable':False,
            'scope':'relative received conversion change between fixed gain-register configurations',
            'limitations':['Sequential source/path changes can imitate receiver gain changes.',
                           'Gain-register names do not identify internal RF topology or individual-stage noise figure.',
                           'Nominal register gain is a comparison target, not an independently calibrated reference.',
                           'Repeat scatter excludes common source, mismatch and model errors.']}
