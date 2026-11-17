"""Typed, age-qualified telemetry with bounded polling and no inferred RF calibration."""
import copy
import hashlib
import json
import math
import time
from collections import deque

from soapypower.files import load_json
from soapypower.numerics import finite, integer


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def optional_call(raw, method, *args):
    """Keep unsupported, missing and failed readbacks distinct from measured zero."""
    fn = getattr(raw,method,None)
    if fn is None:
        return dict(status='unsupported',value=None,error='API unavailable')
    try:
        return dict(status='ok',value=fn(*args),error=None)
    except (RuntimeError,NotImplementedError,TypeError,ValueError,AttributeError) as error:
        return dict(status='error',value=None,error=type(error).__name__+': '+str(error))


def native_metadata(frontend, channel=True):
    """Read the optional versioned SoapyUHD extension, never guess hidden state."""
    name = 'SOAPY_POWER_RX_CHAIN_V1' if channel else 'SOAPY_POWER_DEVICE_INFO_V1'
    args = (frontend.direction,frontend.channel) if channel else ()
    infos = optional_call(frontend.raw,'getSettingInfo',*args)
    if infos['status'] != 'ok' or not any(getattr(i,'key',None)==name for i in infos['value']):
        return dict(status='unsupported',value=None,error='native telemetry extension not advertised')
    result = optional_call(frontend.raw,'readSetting',*(args+(name,)))
    if result['status'] == 'ok':
        from soapypower.files import loads_json
        try:
            if not isinstance(result['value'],str) or len(result['value'])>2*1024**2:
                raise ValueError('native telemetry exceeds schema/size budget')
            value=loads_json(result['value'])
            if not isinstance(value,dict) or value.get('schema')!='soapypower_native_uhd_v1':
                raise ValueError('unknown native telemetry schema')
            result['value']=value
        except (ValueError,TypeError) as error:
            return dict(status='error',value=None,error=str(error))
    return result


class TelemetryPolicy:
    """Acquisition policy; numeric limits are explicit, not guessed from ArgInfo."""
    def __init__(self, value=None):
        if isinstance(value,str):value=load_json(value)
        value={} if value is None else value
        fields={'version','temperature_interval_s','temperature_max_age_s','require_temperature',
                'temperature_range_c','warmup_window_s','warmup_max_slope_c_per_min',
                'max_reference_delta_c','reference_temperature_c','max_latency_ms',
                'reference_lock','require_native_chain','expected_chain_sha256','gpio',
                'anomaly_threshold_db','history_samples','refresh_hops','warmup_timeout_s','temperature_sensor_key'}
        if not isinstance(value,dict) or set(value)-fields or type(value.get('version',1)) is not int or value.get('version',1)!=1:
            raise ValueError('invalid telemetry policy schema/version')
        self.value=copy.deepcopy(value)
        self.interval=finite(value.get('temperature_interval_s',1),'temperature interval',positive=True)
        self.max_age=finite(value.get('temperature_max_age_s',3),'temperature maximum age',positive=True)
        if self.max_age<self.interval:raise ValueError('maximum temperature age must cover its polling interval')
        self.require_temperature=value.get('require_temperature',False)
        self.require_native=value.get('require_native_chain',False)
        if type(self.require_temperature) is not bool or type(self.require_native) is not bool:
            raise ValueError('telemetry requirement flags must be boolean')
        self.temperature_range=None
        if 'temperature_range_c' in value:
            a=value['temperature_range_c']
            if not isinstance(a,list) or len(a)!=2:raise ValueError('temperature range requires two limits')
            self.temperature_range=[finite(v,'temperature limit') for v in a]
            if self.temperature_range[0]>=self.temperature_range[1]:raise ValueError('temperature limits must ascend')
            self.require_temperature=True
        self.window=finite(value.get('warmup_window_s',60),'warmup window',positive=True)
        self.max_slope=None if value.get('warmup_max_slope_c_per_min') is None else finite(value['warmup_max_slope_c_per_min'],'warmup slope',positive=True)
        self.warmup_timeout=finite(value.get('warmup_timeout_s',max(300,self.window*2)),'warmup timeout',positive=True)
        if self.warmup_timeout<self.window:raise ValueError('warmup timeout must cover the observation window')
        if self.max_slope is not None:self.require_temperature=True
        self.reference_temperature=None if value.get('reference_temperature_c') is None else finite(value['reference_temperature_c'],'reference temperature')
        self.max_delta=None if value.get('max_reference_delta_c') is None else finite(value['max_reference_delta_c'],'reference temperature difference',nonnegative=True)
        if self.max_delta is not None and self.reference_temperature is None:raise ValueError('temperature delta requires a reference temperature')
        if self.max_delta is not None:self.require_temperature=True
        self.max_latency=None if value.get('max_latency_ms') is None else finite(value['max_latency_ms'],'telemetry latency limit',positive=True)
        self.reference_lock=value.get('reference_lock','selected')
        if self.reference_lock not in ('selected','required','off'):raise ValueError('invalid reference-lock policy')
        self.history=integer(value.get('history_samples',3600),'telemetry history',3)
        if self.history>100000:raise ValueError('telemetry history exceeds budget')
        self.refresh_hops=integer(value.get('refresh_hops',100),'telemetry refresh hops',1)
        self.anomaly=finite(value.get('anomaly_threshold_db',6),'RSSI/IQ disagreement threshold',positive=True)
        self.expected=value.get('expected_chain_sha256')
        if self.expected is not None and (not isinstance(self.expected,str) or len(self.expected)!=64 or any(c not in '0123456789abcdef' for c in self.expected)):
            raise ValueError('invalid chain fingerprint')
        self.temperature_sensor_key=value.get('temperature_sensor_key')
        if self.temperature_sensor_key is not None and (not isinstance(self.temperature_sensor_key,str) or not self.temperature_sensor_key.strip()):raise ValueError('temperature sensor key must be nonempty')
        if not isinstance(value.get('gpio',[]),list) or len(value.get('gpio',[]))>128:raise ValueError('invalid GPIO policy list/budget')
        self.gpio=[]
        for item in value.get('gpio',[]):
            if not isinstance(item,dict) or set(item)!={'bank','mask','expected','reference'}:
                raise ValueError('GPIO fixture policy needs bank/mask/expected/reference')
            if not all(isinstance(item[k],str) and item[k].strip() for k in ('bank','reference')):
                raise ValueError('GPIO bank and physical mapping reference required')
            mask=integer(item['mask'],'GPIO mask',1);expected=integer(item['expected'],'GPIO expected')
            if mask>0xffffffff or expected>0xffffffff or expected&~mask:raise ValueError('invalid GPIO mask/value')
            self.gpio.append(dict(item))
        self.sha256=digest(value)


class TelemetryMonitor:
    """One-thread polling: slow sensors only in known closed-stream slots.

    Cached observations retain the original read interval and error, even when
    they are stale. No interpolated sensor values or absolute RF claims exist.
    """
    def __init__(self, frontend, policy=None, clock=None):
        self.frontend=frontend
        self.policy=policy if isinstance(policy,TelemetryPolicy) else TelemetryPolicy(policy)
        self.clock=clock or time.monotonic
        self.schema={};self.last={};self.last_good={};self.next_poll={}
        self.trend=deque(maxlen=self.policy.history);self.latencies=deque(maxlen=self.policy.history)
        self.read_counts={};self.hops=0;self.started=self.clock();self.configuration=None
        self.chain=None;self.chain_error=None;self.device=None;self.anomalies={};self.native_read_count=0;self.last_forced_hop=0;self.warmup_complete=False
        self.discover()

    def discover(self):
        f=self.frontend
        for scope,args in (('device',()),('rx',(f.direction,f.channel))):
            listing=optional_call(f.raw,'listSensors',*args)
            if listing['status']!='ok':continue
            for name in listing['value'] or []:
                key=scope+':'+str(name);info=optional_call(f.raw,'getSensorInfo',*(args+(name,)))
                descriptor=info['value'] if info['status']=='ok' else None
                kind=getattr(descriptor,'type',None)
                # Soapy ArgInfo: BOOL=0, INT=1, FLOAT=2, STRING=3.
                kind={0:'boolean',1:'integer',2:'number',3:'string'}.get(kind,'unknown')
                unit=getattr(descriptor,'units',None)
                self.schema[key]=dict(key=key,scope=scope,name=str(name),kind=kind,
                    unit=None if kind=='boolean' else unit,raw_unit=unit,
                    description=getattr(descriptor,'description',''),
                    descriptor_status=info['status'],descriptor_error=info['error'],
                    slow=('temp' in str(name).lower()),measurement_time_basis='host read bracket; sensor latch epoch unavailable')
        candidates=[k for k,d in self.schema.items() if d['slow'] and d['scope']=='rx']
        self.temperature_key=self.policy.temperature_sensor_key
        if self.temperature_key is None:
            self.temperature_key='rx:temp' if 'rx:temp' in candidates else candidates[0] if len(candidates)==1 else None
        self.device=native_metadata(f,False)
        self.refresh_chain(force=True)

    def refresh_chain(self, force=False):
        f=self.frontend
        signature=(getattr(f.wrapper,'sample_rate',None),getattr(f.wrapper,'bandwidth',None),
                   getattr(f.wrapper,'antenna',None),f.optional('getMasterClockRate'),
                   f.correction_state('DCOffsetMode'),f.correction_state('IQBalanceMode'))
        if not force and self.configuration==signature:return
        self.configuration=signature;self.native_read_count+=1
        result=f.native_chain(force=force)
        self.chain_error=result['error']
        if result['status']=='ok':
            from soapypower.filter_model import validate_chain
            try:
                value=validate_chain(result['value'])
                self.chain=dict(status='ok',sha256=digest(value),value=value,error=None)
            except ValueError as error:self.chain=dict(status='error',value=None,error=str(error),sha256=None)
        else:self.chain=dict(status=result['status'],value=None,error=result['error'],sha256=None)

    @staticmethod
    def _parse(raw, kind):
        if raw is None:raise ValueError('empty sensor value')
        text=str(raw).strip()
        if kind=='boolean':
            if text.lower() in ('true','1','yes','locked','on'):return True
            if text.lower() in ('false','0','no','unlocked','off'):return False
            raise ValueError('invalid boolean sensor value')
        if kind=='integer':
            if not text or any(c not in '-+0123456789' for c in text):raise ValueError('invalid integer sensor value')
            return int(text)
        if kind=='number':return finite(text,'sensor value')
        return text

    def _read(self,key):
        descriptor=self.schema[key];f=self.frontend;args=() if descriptor['scope']=='device' else (f.direction,f.channel)
        start=self.clock();result=optional_call(f.raw,'readSensor',*(args+(descriptor['name'],)));stop=self.clock()
        observation=dict(raw=None,value=None,status=result['status'],error=result['error'],
            read_start_monotonic_s=start,read_stop_monotonic_s=stop,latency_ms=max(0,stop-start)*1000,
            source='driver_sensor',unit=descriptor['unit'],kind=descriptor['kind'],
            polled_while_streaming=bool(getattr(f.wrapper,'is_streaming',False)))
        if result['status']=='ok':
            observation['raw']=str(result['value'])
            try:
                observation['value']=self._parse(result['value'],descriptor['kind'])
                self.last_good[key]=copy.deepcopy(observation)
                if key==self.temperature_key and isinstance(observation['value'],(int,float)) and not isinstance(observation['value'],bool):
                    self.trend.append((stop,float(observation['value'])))
            except ValueError as error:observation.update(status='error',error=str(error),value=None)
        self.last[key]=observation;self.read_counts[key]=self.read_counts.get(key,0)+1
        self.latencies.append(observation['latency_ms']);self.next_poll[key]=stop+self.policy.interval

    def poll(self, slow=False):
        for key,d in self.schema.items():
            if d['slow']!=slow:continue
            if slow and self.clock()<self.next_poll.get(key,-float('inf')):continue
            if slow and getattr(self.frontend.wrapper,'is_streaming',False):continue
            self._read(key)

    def closed_slot(self):
        if getattr(self.frontend.wrapper,'is_streaming',False):return
        force=self.hops>self.last_forced_hop and self.hops%self.policy.refresh_hops==0
        self.refresh_chain(force=force)
        if force:self.last_forced_hop=self.hops
        self.poll(slow=True)

    def await_warmup(self, sleeper=None, cancelled=None):
        """Bounded startup wait; subsequent drift rejects a hop rather than hiding it."""
        if self.policy.max_slope is None or self.warmup_complete:return
        if getattr(self.frontend.wrapper,'is_streaming',False):
            raise RuntimeError('thermal warmup requires --reopen-stream')
        sleeper=sleeper or time.sleep
        deadline=self.clock()+self.policy.warmup_timeout
        while True:
            if cancelled is not None and cancelled():raise RuntimeError('thermal warmup cancelled')
            self.closed_slot();snapshot=self.snapshot()
            failures=snapshot['validity']['failures']
            other=[x for x in failures if x!='thermal_stability_not_established']
            if other:self.require_valid(snapshot)
            if snapshot['thermal']['stable'] is True:
                self.warmup_complete=True;return
            if self.clock()>=deadline:raise RuntimeError('thermal warmup timed out without a stable observation window')
            sleeper(min(.2,self.policy.interval,max(0,deadline-self.clock())))

    def snapshot(self):
        self.poll(slow=False)
        now=self.clock();readings={};legacy={}
        for key,d in self.schema.items():
            item=copy.deepcopy(self.last.get(key,dict(status='deferred',raw=None,value=None,error='awaiting closed-stream polling slot')))
            if key in self.last_good:item['last_good']=copy.deepcopy(self.last_good[key])
            age=None if 'read_stop_monotonic_s' not in item else max(0,now-item['read_stop_monotonic_s'])
            item.update(age_s=age,fresh=bool(item['status']=='ok' and (not d['slow'] or age<=self.policy.max_age)))
            if item['status']=='ok' and not item['fresh']:item['status']='stale'
            readings[key]=item;legacy[key]=item.get('raw') if item['fresh'] else None
        tuning={}
        for name in self.frontend.optional('listFrequencies',self.frontend.direction,self.frontend.channel) or []:
            if str(name).upper() not in ('RF','BB'):continue
            value=self.frontend.optional('getFrequency',self.frontend.direction,self.frontend.channel,name)
            try:value=finite(value,'tuning readback')
            except ValueError:value=None
            tuning[str(name)]=value
        gpio=[]
        for spec in self.policy.gpio:
            result=optional_call(self.frontend.raw,'readGPIO',spec['bank'])
            valid=result['status']=='ok' and isinstance(result['value'],int) and not isinstance(result['value'],bool)
            gpio.append(dict(spec,status=result['status'] if valid else 'error',value=result['value'] if valid else None,
                matched=bool(valid and result['value']&spec['mask']==spec['expected']),error=result['error']))
        result=dict(monotonic_s=now,tuning_components_hz=tuning,tuning_source='controller_readback; backend may cache tune results',
            sensors=legacy,telemetry=readings,sensor_schema=copy.deepcopy(self.schema),gpio=gpio,
            chain=copy.deepcopy(self.chain),device_metadata=copy.deepcopy(self.device),
            clock_source=self.frontend.optional('getClockSource'),time_source=self.frontend.optional('getTimeSource'),
            thermal=self.thermal_state(now),temperature_sensor_key=self.temperature_key,telemetry_policy_sha256=self.policy.sha256,
            continuously_verified=False,calibrated_rf_state=False)
        result['validity']=self.validity(result)
        return result

    def thermal_state(self,now=None):
        now=self.clock() if now is None else now
        # Retain one sample bracketing the window boundary. Dropping it before
        # the next scheduled poll would make stable coverage flicker on/off.
        history=list(self.trend)
        points=[(t,v) for t,v in history if now-t<=self.policy.window]
        before=[(t,v) for t,v in history if now-t>self.policy.window]
        if before:points.insert(0,before[-1])
        slope=None;span=0
        if len(points)>=3:
            t,v=zip(*points);t=[x-t[0] for x in t];span=max(t)
            if span>0:
                import numpy as np
                # Median endpoint windows avoid differentiating individual noisy ADC steps.
                order=np.argsort(t);n=max(1,len(t)//3);lo,hi=order[:n],order[-n:]
                dt=float(np.median(np.asarray(t)[hi])-np.median(np.asarray(t)[lo]))
                if dt>0:slope=float((np.median(np.asarray(v)[hi])-np.median(np.asarray(v)[lo]))/dt*60)
        return dict(slope_c_per_min=slope,observed_span_s=span,samples=len(points),
            stable=None if slope is None or span<self.policy.window*.8 else
                   (True if self.policy.max_slope is None else abs(slope)<=self.policy.max_slope),
            absolute_temperature_calibrated=False)

    def validity(self,snapshot):
        failures=[];temperature=None
        temps=[snapshot['telemetry'][self.temperature_key]] if self.temperature_key in snapshot['telemetry'] and self.schema[self.temperature_key]['slow'] else []
        if temps and temps[0]['fresh'] and isinstance(temps[0].get('value'),(int,float)):temperature=float(temps[0]['value'])
        if self.policy.require_temperature and temperature is None:failures.append('fresh_temperature_required')
        if temperature is not None:
            if self.policy.temperature_range and not self.policy.temperature_range[0]<=temperature<=self.policy.temperature_range[1]:failures.append('temperature_outside_validated_domain')
            if self.policy.max_delta is not None and abs(temperature-self.policy.reference_temperature)>self.policy.max_delta:failures.append('reference_temperature_difference_exceeded')
        if self.policy.max_slope is not None and snapshot['thermal']['stable'] is not True:failures.append('thermal_stability_not_established')
        if self.policy.require_native and snapshot['chain']['status']!='ok':failures.append('native_chain_required')
        if self.policy.expected and snapshot['chain'].get('sha256')!=self.policy.expected:failures.append('processing_chain_fingerprint_mismatch')
        required=self.policy.reference_lock=='required' or (self.policy.reference_lock=='selected' and snapshot['clock_source'] in ('external','gpsdo'))
        if required:
            lock=snapshot['telemetry'].get('device:ref_locked',{})
            if not lock.get('fresh') or lock.get('value') is not True:failures.append('selected_reference_not_locked_or_unknown')
        if self.policy.max_latency is not None and any(r.get('latency_ms',0)>self.policy.max_latency for r in snapshot['telemetry'].values()):failures.append('telemetry_latency_budget_exceeded')
        if any(not r['matched'] for r in snapshot['gpio']):failures.append('fixture_feedback_mismatch_or_unknown')
        return dict(passed=not failures,failures=failures,temperature_c=temperature,reference_lock_required=required,
                    establishes_absolute_calibration=False)

    def require_valid(self,snapshot):
        if not snapshot['validity']['passed']:raise RuntimeError('telemetry policy rejected: '+', '.join(snapshot['validity']['failures']))

    def qualify_iq(self,context,snapshot,headroom):
        """Advisory matched-state RSSI/IQ disagreement, never an RF notch/AGC."""
        key=digest(context)
        key+=':'+str({k:None if v is None else round(v) for k,v in snapshot['tuning_components_hz'].items()})
        r=snapshot['telemetry'].get('rx:rssi',{});rssi=r.get('value') if r.get('fresh') else None
        power=headroom.get('ac_rms',0)
        if not isinstance(rssi,(int,float)) or isinstance(rssi,bool) or not power:return dict(status='unknown',automatic_gain_change=False)
        delta=float(rssi)-20*math.log10(power)
        previous=self.anomalies.get(key)
        if len(self.anomalies)>=4096 and key not in self.anomalies:self.anomalies.pop(next(iter(self.anomalies)))
        self.anomalies[key]=delta
        return dict(status='baseline' if previous is None else 'disagreement' if abs(delta-previous)>self.policy.anomaly else 'consistent',
                    relative_disagreement_db=None if previous is None else delta-previous,
                    automatic_gain_change=False,analog_linearity_verified=False,rssi_sample_window_aligned=False)

    def statistics(self):
        import numpy as np
        return dict(read_counts=dict(self.read_counts),native_chain_refreshes=self.native_read_count,
            sensor_latency_median_ms=float(np.median(self.latencies)) if self.latencies else None,
            sensor_latency_p95_ms=float(np.percentile(self.latencies,95)) if self.latencies else None,
            elapsed_host_s=max(0,self.clock()-self.started))
