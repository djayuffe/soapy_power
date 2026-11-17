"""Reference-driven thermal and settling policies with independent preservation gates.

These models consume declared known-reference measurements. Software cannot
verify a reference certificate or make uncontrolled antenna traffic causal.
"""
import copy
import math
import numpy as np
from soapypower.files import load_json
from soapypower.numerics import finite,integer
from soapypower.telemetry import digest


def _source(value):
    if not isinstance(value,dict) or not {'reference','reference_sha256','controlled_input','sensor_key','context_sha256'}<=set(value) or set(value)-{'reference','reference_sha256','controlled_input','sensor_key','context_sha256','max_sensor_age_s','response_scope'}:
        raise ValueError('model requires explicit reference, sensor and context provenance')
    if value['controlled_input'] is not True:raise ValueError('uncontrolled antenna input cannot fit a receiver correction')
    if not isinstance(value['reference'],str) or not value['reference'].strip() or not isinstance(value['sensor_key'],str) or not value['sensor_key']:
        raise ValueError('reference and sensor identifiers required')
    if 'max_sensor_age_s' in value:finite(value['max_sensor_age_s'],'reference sensor age',positive=True)
    for key in ('reference_sha256','context_sha256'):
        if not isinstance(value[key],str) or len(value[key])!=64 or any(c not in '0123456789abcdef' for c in value[key]):raise ValueError('invalid '+key)


def _rows(value,fields):
    if not isinstance(value,list) or not value or len(value)>100000:raise ValueError('invalid model measurements/budget')
    seen=set();rows=[]
    for row in value:
        if not isinstance(row,dict) or set(row)!=fields|{'capture_sha256','partition'}:raise ValueError('invalid model measurement fields')
        key=row['capture_sha256']
        if not isinstance(key,str) or len(key)!=64 or any(c not in '0123456789abcdef' for c in key) or key in seen:raise ValueError('model train/validation captures must be unique')
        seen.add(key)
        if row['partition'] not in ('train','validation'):raise ValueError('independent train/validation partition required')
        rows.append(dict(row,**{k:finite(row[k],k) for k in fields}))
    return rows


def fit_thermal(manifest):
    fields={'version','type','source','measurements','max_validation_error_db','max_correction_db','reference_uncertainty_db','temperature_uncertainty_c'}
    if not isinstance(manifest,dict) or set(manifest)!=fields or type(manifest['version']) is not int or manifest['version']!=1 or manifest['type']!='thermal_reference_measurements':raise ValueError('invalid thermal measurement schema')
    _source(manifest['source'])
    if manifest['source'].get('response_scope') not in ('whole_grid_scalar','single_reference_channel'):
        raise ValueError('thermal reference must declare its scalar response scope')
    rows=_rows(manifest['measurements'],{'temperature_c','correction_db'})
    train=[r for r in rows if r['partition']=='train'];validation=[r for r in rows if r['partition']=='validation']
    if len(train)<4 or len(validation)<3 or len({r['temperature_c'] for r in train})<3 or len({r['temperature_c'] for r in validation})<2:raise ValueError('thermal model needs independent temperature-spanning repeats')
    t=np.array([r['temperature_c'] for r in train]);y=np.array([r['correction_db'] for r in train]);origin=float(np.mean(t));x=t-origin
    if np.ptp(t)<1:raise ValueError('thermal training span is too narrow')
    if any(not t.min()<=r['temperature_c']<=t.max() for r in validation):raise ValueError('thermal holdout exceeds trained temperature domain')
    limit=finite(manifest['max_validation_error_db'],'thermal validation tolerance',positive=True)
    maximum=finite(manifest['max_correction_db'],'maximum thermal correction',positive=True)
    uncertainty=finite(manifest['reference_uncertainty_db'],'reference uncertainty',nonnegative=True)
    temperature_uncertainty=finite(manifest['temperature_uncertainty_c'],'temperature uncertainty',nonnegative=True)
    design=np.column_stack([np.ones(len(x)),x]);coef=np.linalg.lstsq(design,y,rcond=None)[0]
    prediction=np.array([coef[0]+coef[1]*(r['temperature_c']-origin) for r in validation]);truth=np.array([r['correction_db'] for r in validation]);error=prediction-truth
    baseline=float(np.sqrt(np.mean((np.mean(y)-truth)**2)));rms=float(np.sqrt(np.mean(error**2)))
    if float(np.max(abs(error)))>limit or rms>=baseline:raise ValueError('thermal model failed independent preservation/improvement gates')
    extrema=[coef[0]+coef[1]*(v-origin) for v in [t.min(),t.max()]]
    if max(abs(v) for v in extrema)>maximum:raise ValueError('thermal correction exceeds declared bound')
    residual=y-design@coef;variance=float(residual@residual/(len(train)-2));cov=variance*np.linalg.inv(design.T@design)
    model=dict(version=1,type='validated_thermal_response',source=copy.deepcopy(manifest['source']),
        temperature_domain_c=[float(t.min()),float(t.max())],origin_c=origin,coefficients_db=coef.tolist(),coefficient_covariance=cov.tolist(),
        max_correction_db=maximum,reference_uncertainty_db=uncertainty,temperature_uncertainty_c=temperature_uncertainty,validation_max_error_db=float(np.max(abs(error))),
        validation_rmse_db=rms,baseline_rmse_db=baseline,capture_sha256=[r['capture_sha256'] for r in rows],
        training_manifest_sha256=digest(manifest),absolute_power_calibration=False,
        domain='Only the bound receiver/RF/IF/gain/clock/filter context; no frequency or gain extrapolation')
    model['model_sha256']=digest(model)
    return model


class ThermalModel:
    def __init__(self,value):
        if isinstance(value,str):value=load_json(value)
        if not isinstance(value,dict) or value.get('type')!='validated_thermal_response' or type(value.get('version')) is not int or value['version']!=1:raise ValueError('invalid validated thermal model')
        self.value=copy.deepcopy(value);claimed=self.value.pop('model_sha256',None)
        if digest(self.value)!=claimed:raise ValueError('thermal model digest mismatch')
        _source(value['source']);self.sha256=claimed
        self.domain=[finite(v,'thermal domain') for v in value['temperature_domain_c']]
        if len(self.domain)!=2 or self.domain[0]>=self.domain[1]:raise ValueError('invalid thermal temperature domain')
        self.origin=finite(value['origin_c'],'thermal origin');self.coef=np.asarray(value['coefficients_db'],float);self.cov=np.asarray(value['coefficient_covariance'],float)
        if self.coef.shape!=(2,) or self.cov.shape!=(2,2) or not np.isfinite(self.coef).all() or not np.isfinite(self.cov).all() or not np.allclose(self.cov,self.cov.T) or np.linalg.eigvalsh(self.cov).min()<-1e-12:raise ValueError('invalid thermal coefficients/uncertainty')
        self.maximum=finite(value['max_correction_db'],'thermal correction maximum',positive=True)
        self.uncertainty=finite(value['reference_uncertainty_db'],'thermal reference uncertainty',nonnegative=True)
        self.temperature_uncertainty=finite(value['temperature_uncertainty_c'],'temperature uncertainty',nonnegative=True)
        self.validation_error=finite(value['validation_max_error_db'],'thermal validation maximum error',nonnegative=True)
        if value['source'].get('response_scope') not in ('whole_grid_scalar','single_reference_channel'):raise ValueError('invalid thermal response scope')
        self.max_age=finite(value['source'].get('max_sensor_age_s',3),'model maximum sensor age',positive=True)
        if finite(value['validation_rmse_db'],'thermal validation RMS',nonnegative=True)>=finite(value['baseline_rmse_db'],'baseline RMS',positive=True):raise ValueError('thermal validation does not improve baseline')

    def evaluate(self,temperature_c,context_sha256,sensor_key):
        t=finite(temperature_c,'observed thermal model temperature')
        if context_sha256!=self.value['source']['context_sha256'] or sensor_key!=self.value['source']['sensor_key']:raise ValueError('thermal reference context/sensor mismatch')
        if not self.domain[0]<=t<=self.domain[1]:raise ValueError('thermal model extrapolation forbidden')
        v=np.array([1,t-self.origin]);correction=float(v@self.coef)
        if abs(correction)>self.maximum:raise ValueError('thermal correction bound exceeded')
        uncertainty=math.sqrt(max(0,float(v@self.cov@v))+self.uncertainty**2+(self.coef[1]*self.temperature_uncertainty)**2)+self.validation_error
        return dict(correction_db=correction,conditional_uncertainty_db=uncertainty,model_sha256=self.sha256,
                    absolute_power_calibration=False,temperature_c=t)


def fit_settling(manifest):
    if not isinstance(manifest,dict) or set(manifest)!={'version','type','source','measurements','max_amplitude_error_db','max_frequency_error_hz','minimum_delay_s'} or manifest.get('version')!=1 or type(manifest.get('version')) is not int or manifest.get('type')!='settling_reference_measurements':raise ValueError('invalid settling measurement schema')
    _source(manifest['source']);rows=_rows(manifest['measurements'],{'delay_s','amplitude_error_db','frequency_error_hz'})
    a=finite(manifest['max_amplitude_error_db'],'amplitude tolerance',positive=True);f=finite(manifest['max_frequency_error_hz'],'frequency tolerance',positive=True);minimum=finite(manifest['minimum_delay_s'],'minimum settling delay',nonnegative=True)
    if any(r['delay_s']<0 for r in rows):raise ValueError('negative settling time')
    train=[r for r in rows if r['partition']=='train'];hold=[r for r in rows if r['partition']=='validation']
    candidates=sorted({r['delay_s'] for r in train if r['delay_s']>=minimum})
    for delay in candidates:
        tr=[r for r in train if r['delay_s']>=delay];va=[r for r in hold if r['delay_s']>=delay]
        if sum(r['delay_s']==delay for r in tr)<3 or sum(r['delay_s']==delay for r in va)<3:continue
        if all(abs(r['amplitude_error_db'])<=a and abs(r['frequency_error_hz'])<=f for r in tr+va):
            value=dict(version=1,type='validated_settling_policy',source=copy.deepcopy(manifest['source']),delay_s=delay,
                max_amplitude_error_db=a,max_frequency_error_hz=f,capture_sha256=[r['capture_sha256'] for r in rows],training_manifest_sha256=digest(manifest),
                validated_context_only=True,extrapolation_allowed=False)
            value['model_sha256']=digest(value);return value
    raise ValueError('no independently validated settling delay meets the preservation gates')


class ModelContextMismatch(ValueError):
    pass


def validate_settling(value):
    if isinstance(value,str):value=load_json(value)
    if not isinstance(value,dict) or value.get('type')!='validated_settling_policy' or type(value.get('version')) is not int or value['version']!=1:raise ValueError('invalid settling policy')
    copy_=dict(value);claimed=copy_.pop('model_sha256',None)
    if digest(copy_)!=claimed:raise ValueError('settling policy digest mismatch')
    _source(value['source'])
    finite(value['delay_s'],'validated settling delay',nonnegative=True)
    return copy.deepcopy(value)


def evaluate_settling(value,context_sha256):
    value=validate_settling(value)
    if context_sha256!=value['source']['context_sha256']:raise ModelContextMismatch('settling state/step context mismatch')
    return value['delay_s']
