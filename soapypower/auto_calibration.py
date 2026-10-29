"""Capability-driven guided calibration; no inferred RF reference levels."""
import copy
import datetime
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

import numpy as np

from soapypower.calibration import _compare, require_reference_context, PowerCalibration
from soapypower.calibration_workflows import (average_capture, band_summary, build_manifest,
    thermal_reference, effective_temperature, noise_solution, _check_coordinates, _sha256)
from soapypower.characterization import validate_cw
from soapypower.files import load_json, loads_json, write_json, EvidenceSnapshot
from soapypower.numerics import finite, integer
from soapypower.response_validation import validate_holdout, response_sha256

# Guidance is advisory; capability/readback and measured gates decide execution.
FAMILIES = {
    'rtlsdr': ('tuner', ['Check tuner model/band and digital AGC.',
        'V4 HF uses upconversion, not the V3 direct-sampling recipe.']),
    'airspy': ('low_if', ['Preserve tuner filtering, gain stages and decimation.']),
    'airspyhf': ('if_sampling', ['Embedded digital gain cannot be assumed fixed; absolute automation is blocked.']),
    'sdrplay': ('model_dependent_if', ['Verify port impedance, LNA/gain-reduction semantics, IF mode, HDR/notches and API version.']),
    'hackrf': ('mixer_frontend', ['Preserve AMP/LNA/VGA distribution; check blockers and compression.',
        'One and Pro modes are not interchangeable; inspect firmware/plugin support.']),
    'bladerf': ('model_dependent_rfic', ['Original and 2.0 micro use different native correction paths; calibrate each RX separately.']),
    'lime': ('direct_conversion_rfic', ['Preserve RF port, LNA/TIA/PGA, analog filters and digital rate stages.']),
    'pluto': ('ad936x', ['Preserve RFIC gain/tracking/FIR state and actual board/transport identity.']),
    'uhd': ('frontend_dependent', ['Identify daughterboard/subdevice and native UHD procedure.',
        'X410 IF, X420 correction and X440 direct sampling require different preparation.',
        'Do not combine UHD power-reference conversion with an application profile.']),
    'redpitaya': ('direct_adc', ['Verify exact input impedance, coupling/range, FPGA image and external anti-alias filters.']),
    'hermes': ('direct_adc', ['Preserve board filter/attenuator state, FPGA and network path.']),
    'fcd': ('audio_iq', ['Verify OS audio scaling, resampling, filters and model; keep the full IQ path fixed.']),
    'remote': ('wrapper', ['Identify underlying hardware; preserve server gain/scaling, transport format and metadata.']),
    'unknown': ('unknown', ['Do not infer topology, input impedance or calibrated scaling from a model name.']),
}


def install_signal_handlers():
    """Interrupt prompts/hooks as well as acquisition; never swallow Ctrl-C."""
    import signal
    from soapypower import power
    power.install_signal_handlers()  # Reset the live acquisition cancellation flag.
    def interrupt(sig, frame):
        raise KeyboardInterrupt('calibration interrupted by signal {}'.format(sig))
    signal.signal(signal.SIGINT,interrupt)
    signal.signal(signal.SIGTERM,interrupt)
    if hasattr(signal,'SIGBREAK'):signal.signal(signal.SIGBREAK,interrupt)


def family(device, context=None):
    text = str(device).lower() + ' ' + str((context or {}).get('driver', '')).lower() + ' ' + str((context or {}).get('hardware', '')).lower()
    for key, aliases in (
        ('remote', ('driver=remote',)), ('airspyhf', ('airspyhf', 'hf+', 'hf plus')),
        ('rtlsdr', ('rtlsdr', 'rtl-sdr')), ('sdrplay', ('sdrplay', 'rspduo', 'rspdx')),
        ('bladerf', ('bladerf',)), ('hackrf', ('hackrf',)), ('airspy', ('airspy',)),
        ('lime', ('lime', 'lms7002')), ('pluto', ('pluto', 'ad936')),
        ('uhd', ('driver=uhd', 'usrp', 'b200', 'b210', 'n3xx', 'x4xx')),
        ('redpitaya', ('redpitaya', 'red pitaya', 'stemlab')),
        ('hermes', ('hermes',)), ('fcd', ('funcube', 'driver=fcd'))):
        if any(alias in text for alias in aliases):return key
    return 'unknown'


def _text(value, name):
    if not isinstance(value, str) or not value.strip():raise ValueError(name+' must be a nonempty string')
    return value


def _axis(value, name, minimum):
    if not isinstance(value, list):raise ValueError(name+' must be a list')
    result = [finite(x, name) for x in value]
    if len(result)<minimum or result!=sorted(set(result)):raise ValueError(name+' must be sorted, unique and sufficiently long')
    return result


def _table(value, rf, name='path loss dB'):
    if not isinstance(value, list):return finite(value, name, nonnegative=True)
    if len(value)<2 or any(not isinstance(row,list) or len(row)!=2 for row in value):
        raise ValueError(name+' must be a scalar or increasing [RF Hz, value] pairs')
    x = [finite(row[0], name+' RF') for row in value]
    y = [finite(row[1], name, nonnegative=True) for row in value]
    if x!=sorted(set(x)) or not x[0]<=rf<=x[-1]:raise ValueError(name+' table must cover every RF; no extrapolation')
    return float(np.interp(rf,x,y))


def _loss(value, rf):
    return _table(value,rf)


def _temperatures(source, rf):
    cold=finite(_table(source.get('cold_temperature_k'),rf,'cold temperature K'),'cold temperature',positive=True)
    hot=finite(_table(source.get('hot_temperature_k'),rf,'hot temperature K'),'hot temperature',positive=True)
    if hot<=cold:raise ValueError('hot temperature must exceed cold at every measured RF')
    return cold,hot


def configuration(value):
    c = copy.deepcopy(value)
    allowed = {'version','receiver','source','rf_hz','baseband_hz','reference_baseband_hz',
        'holdouts','reference','reference_plane','termination','acquisition','acceptance',
        'physical_setup_confirmed'}
    if not isinstance(c,dict) or set(c)-allowed or type(c.get('version')) is not int or c['version']!=1:
        raise ValueError('invalid auto-calibration configuration version/fields')
    r = c.get('receiver')
    keys = {'device','channel','antenna','sample_rate','bandwidth','gain','settings',
            'dc_correction','iq_correction','lnb_lo','lnb_high_side','iq_conjugate',
            'clock_source','time_source','master_clock_rate','corr_ppm'}
    if not isinstance(r,dict) or set(r)-keys:raise ValueError('invalid receiver fields')
    _text(r.get('device'),'receiver device')
    r['sample_rate'] = finite(r.get('sample_rate'),'sample rate',positive=True)
    r['bandwidth'] = finite(r.get('bandwidth',0),'bandwidth',nonnegative=True)
    r['channel'] = integer(r.get('channel',0),'channel')
    r.setdefault('antenna','');r.setdefault('settings',{})
    if not isinstance(r['antenna'],str) or not isinstance(r['settings'],dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in r['settings'].items()):
        raise ValueError('antenna/settings must be strings/string mapping')
    gain = r.get('gain')
    if isinstance(gain,dict):
        if not gain or any(not isinstance(k,str) or not k for k in gain):raise ValueError('gain stages must have names')
        r['gain'] = {k:finite(v,'stage gain') for k,v in gain.items()}
    else:r['gain'] = finite(gain,'explicit fixed gain')
    for key in ('dc_correction','iq_correction'):
        r.setdefault(key,'auto')
        if r[key] not in ('auto','default','on','off'):raise ValueError('invalid correction policy')
    r['lnb_lo'] = finite(r.get('lnb_lo',0),'converter LO')
    for key in ('lnb_high_side','iq_conjugate'):
        r.setdefault(key,False)
        if type(r[key]) is not bool:raise ValueError(key+' must be boolean')
    for key in ('clock_source','time_source'):
        if r.get(key) is not None:_text(r[key],key)
    if r.get('master_clock_rate') is not None:r['master_clock_rate']=finite(r['master_clock_rate'],'master clock rate',positive=True)
    r['corr_ppm']=finite(r.get('corr_ppm',0),'frequency correction ppm')
    c['rf_hz'] = _axis(c.get('rf_hz'),'RF axis',1)
    c['baseband_hz'] = _axis(c.get('baseband_hz'),'baseband axis',1)
    if len(c['rf_hz'])*len(c['baseband_hz'])>256:raise ValueError('auto grid exceeds 256 cells')
    c['reference_baseband_hz'] = finite(c.get('reference_baseband_hz',c['baseband_hz'][0]),'reference offset')
    if c['reference_baseband_hz'] not in c['baseband_hz']:raise ValueError('reference offset must be measured')
    source = c.get('source')
    method = 'baseline' if source is None else source.get('type') if isinstance(source,dict) else None
    if source is not None and method=='baseline':raise ValueError('use source=null for a terminated baseline')
    if method not in ('baseline','tone','hot-cold'):raise ValueError('source type must be tone or hot-cold, or null for baseline')
    if method!='baseline':
        _text(c.get('reference'),'source reference');_text(c.get('reference_plane'),'reference plane')
        if len(c['rf_hz'])<2 or len(c['baseband_hz'])<2:raise ValueError('absolute grid requires at least 2 RF and 2 baseband points')
        if set(source)-{'type','levels_dbm','path_loss_db','cold_temperature_k','hot_temperature_k',
                       'path_temperature_k','source_ohms','receiver_ohms','linearity_confirmed','max_receiver_input_dbm'}:
            raise ValueError('unknown reference source fields')
        for rf in c['rf_hz']:_loss(source.get('path_loss_db',0),rf)
        if method=='tone':
            source['levels_dbm'] = _axis(source.get('levels_dbm'),'known source levels',3)
            if len(source['levels_dbm'])>16:raise ValueError('at most 16 source levels are supported')
            source['max_receiver_input_dbm']=finite(source.get('max_receiver_input_dbm'),'verified receiver input ceiling dBm')
            if source['levels_dbm'][-1]-source['levels_dbm'][0]<1:raise ValueError('source levels must span at least 1 dB')
        else:
            for key in ('source_ohms','receiver_ohms'):
                source[key] = finite(source.get(key),key,positive=True)
            for rf in c['rf_hz']:_temperatures(source,rf)
            if source['source_ohms']!=source['receiver_ohms']:raise ValueError('automatic noise calibration requires matched source/receiver impedances')
            source['path_temperature_k'] = finite(source.get('path_temperature_k',290),'path temperature',positive=True)
    holdouts = c.setdefault('holdouts',[])
    if not isinstance(holdouts,list) or len(holdouts)>64:raise ValueError('holdouts must be a list of at most 64 coordinates')
    coordinates = []
    for row in holdouts:
        if not isinstance(row,dict) or set(row)!= {'rf_hz','baseband_hz'}:raise ValueError('invalid holdout coordinate')
        rf,bb = finite(row['rf_hz'],'holdout RF'),finite(row['baseband_hz'],'holdout offset')
        row.update(rf_hz=rf,baseband_hz=bb)
        if not c['rf_hz'][0]<=rf<=c['rf_hz'][-1] or not c['baseband_hz'][0]<=bb<=c['baseband_hz'][-1]:raise ValueError('holdout outside measured grid')
        if method!='baseline':_loss(source.get('path_loss_db',0),rf)
        if method=='hot-cold':_temperatures(source,rf)
        coordinates.append((rf,bb))
    if method!='baseline' and len(set(coordinates))<2:raise ValueError('absolute automation requires two distinct independent holdout coordinates')
    if method=='tone':
        for rf in c['rf_hz']+[p['rf_hz'] for p in holdouts]:
            if source['levels_dbm'][-1]-_loss(source.get('path_loss_db',0),rf)>source['max_receiver_input_dbm']:
                raise ValueError('requested reference exceeds the declared receiver input ceiling')
    a = c.setdefault('acquisition',{})
    if not isinstance(a,dict) or set(a)-{'fft_bins','segments','fft_overlap','integration_width_hz','tune_delay','settle_samples','lock_timeout','lock_sensor','reference_lock_sensor','require_timestamps'}:raise ValueError('invalid acquisition fields')
    a['fft_bins']=integer(a.get('fft_bins',4096),'FFT bins',16)
    a['segments']=integer(a.get('segments',64),'segments',32)
    if a['fft_bins']>262144 or a['segments']>10000:raise ValueError('auto acquisition work limits exceeded')
    a['fft_overlap']=finite(a.get('fft_overlap',0.5),'FFT overlap',nonnegative=True)
    if a['fft_overlap']>=1:raise ValueError('FFT overlap must be <1')
    a['integration_width_hz']=finite(a.get('integration_width_hz',r['sample_rate']/100),'integration width',positive=True)
    a['tune_delay']=finite(a.get('tune_delay',0.1),'tune delay',nonnegative=True)
    a['settle_samples']=integer(a.get('settle_samples',32768),'settle samples')
    a['lock_timeout']=finite(a.get('lock_timeout',2),'lock timeout',positive=True)
    for key in ('lock_sensor','reference_lock_sensor'):
        a.setdefault(key,None)
        if a[key] is not None:_text(a[key],key)
    a.setdefault('require_timestamps',False)
    if type(a['require_timestamps']) is not bool:raise ValueError('require_timestamps must be boolean')
    limits = c.setdefault('acceptance',{})
    if not isinstance(limits,dict) or set(limits)-{'min_snr_db','min_y_db','max_abs_error_db','max_ripple_db','max_cw_residual_db','cw_slope_tolerance'}:raise ValueError('invalid acceptance fields')
    for key,default in (('min_snr_db',10),('min_y_db',0.5),('max_abs_error_db',0.3),('max_ripple_db',0.3),('max_cw_residual_db',0.1),('cw_slope_tolerance',0.01)):
        limits[key]=finite(limits.get(key,default),key,positive=True)
    if method=='baseline':
        t=c.get('termination')
        if not isinstance(t,dict) or set(t)!={'temperature_k','source_ohms','receiver_ohms'}:raise ValueError('baseline requires termination temperature and actual impedances')
        thermal_reference(**t)
    return c


def _geometry(c, rate):
    a,r=c['acquisition'],c['receiver'];width=a['integration_width_hz'];sign=-1 if r['lnb_high_side'] else 1
    points=[(rf,bb) for rf in c['rf_hz'] for bb in c['baseband_hz']]+[(p['rf_hz'],p['baseband_hz']) for p in c['holdouts']]
    for rf,bb in points:
        if abs(bb)-width/2 < 2*rate/a['fft_bins'] or abs(bb)+width/2>=0.45*rate:
            raise ValueError('reference bands must avoid DC by two bins and lie inside 90% of Nyquist bandwidth')
        tune=sign*(rf-r['lnb_lo'])-(-bb if r['iq_conjugate'] else bb)
        if not math.isfinite(tune):raise ValueError('nonfinite planned tuning')
    return points


def prepare(c, factory=None):
    """Discover real capabilities; only set documented, advertised controls."""
    if factory is None:
        from soapypower.power import SoapyPower
        factory=SoapyPower
    r=c['receiver'];sdr=factory(soapy_args=r['device'],sample_rate=r['sample_rate'],bandwidth=r['bandwidth'],
        gain=r['gain'],channel=r['channel'],antenna=r['antenna'],settings=r['settings'],output_format='json',
        clock_source=r.get('clock_source'),time_source=r.get('time_source'),master_clock_rate=r.get('master_clock_rate'),corr=r['corr_ppm'],
        dc_correction='default' if r['dc_correction']=='auto' else r['dc_correction'],
        iq_correction='default' if r['iq_correction']=='auto' else r['iq_correction'])
    try:
        f=sdr._frontend;changes={};capabilities={}
        for key,suffix in (('dc_correction','DCOffsetMode'),('iq_correction','IQBalanceMode')):
            capable=f.supported('has'+suffix);capabilities[key]=capable
            selection=('on' if capable else 'default') if r[key]=='auto' else r[key]
            setattr(f,key,selection);changes[key]=selection
        f.apply_corrections()
        for item in f.optional('getSettingInfo') or []:
            key=getattr(item,'key',None)
            if key not in ('digital_agc','if_agc','testmode','biastee','bias_tee'):continue
            if key in r['settings'] and r['settings'][key].strip().lower() not in ('false','off','0'):
                raise ValueError('automatic calibration requires '+key+' off; conflicting requested setting')
            choices=list(getattr(item,'options',[]) or [])
            disabled=next((choice for choice in choices if choice.lower() in ('false','off','0')),'false')
            if not hasattr(f.raw,'writeSetting'):raise ValueError('cannot disable advertised '+key)
            f.raw.writeSetting(key,disabled)
            if str(f.optional('readSetting',key)).strip().lower() not in ('false','off','0'):
                raise ValueError('cannot verify '+key+' disabled')
            changes[key]=disabled
        sdr._lnb_lo=r['lnb_lo'];sdr._lnb_high_side=r['lnb_high_side'];sdr._iq_conjugate=r['iq_conjugate']
        capabilities['gain_elements']=list(getattr(sdr.device,'list_gains',lambda:[])())
        for label,method,args in (('clock_sources','listClockSources',()),('time_sources','listTimeSources',()),
                ('rx_lock_sensors','listSensors',(f.direction,f.channel)),('reference_lock_sensors','listSensors',())):
            values=f.optional(method,*args)
            capabilities[label]=None if values is None else list(values)
        for key,label in (('lock_sensor','rx_lock_sensors'),('reference_lock_sensor','reference_lock_sensors')):
            selected=c['acquisition'][key]
            if selected is not None and (capabilities[label] is None or selected not in capabilities[label]):
                raise ValueError('requested '+key+' is not advertised by this device')
        context=sdr.measurement_context();points=_geometry(c,finite(sdr.device.sample_rate,'actual rate',positive=True))
        if hasattr(sdr.device,'get_frequency_range'):
            low,high=sdr.device.get_frequency_range();sign=-1 if r['lnb_high_side'] else 1
            for rf,bb in points:
                tune=sign*(rf-r['lnb_lo'])-(-bb if r['iq_conjugate'] else bb)
                if not low<=tune<=high:raise ValueError('planned tune exceeds actual hardware range')
        return sdr,context,changes,capabilities
    except BaseException:
        sdr.stop(cancel=True);raise


def plan(config, offline=False, factory=None):
    c=configuration(config);context=None;changes={};capabilities={};sdr=None
    try:
        if not offline:sdr,context,changes,capabilities=prepare(c,factory)
        else:_geometry(c,c['receiver']['sample_rate'])
        name=family(c['receiver']['device'],context);architecture,notes=FAMILIES[name]
        method='baseline' if c.get('source') is None else c['source']['type'];blocked=[]
        if not offline:
            try:require_reference_context(context)
            except ValueError as error:blocked.append(str(error))
            if method!='baseline' and not context.get('serial'):blocked.append('absolute automation requires a known receiver serial identity')
        if method!='baseline' and (name=='airspyhf' or (name=='remote' and family(c['receiver']['device'].replace('driver=remote',''),context)=='airspyhf')):blocked.append('HF+ hidden digital gain is not a verified fixed-gain absolute reference')
        if method=='hot-cold' and c['source'].get('linearity_confirmed') is not True:
            blocked.append('hot/cold requires independently verified linear operation at both source levels')
        if c.get('physical_setup_confirmed') is not True:blocked.append('confirm reference plane, impedance, DC isolation/bias-off, source limits and warm-up in physical_setup_confirmed')
        return {'kind':'auto_calibration_plan','version':1,'configuration':c,'method':method,
            'family':name,'architecture':architecture,'guidance':notes,'capabilities':capabilities,
            'applied_controls':changes,'context':context,'blockers':blocked,
            'status':'unverified_offline' if offline else 'blocked' if blocked else 'ready',
            'steps':(['terminated captures','baseline diagnostics'] if method=='baseline' else
                ['source-level linearity sweep','rectangular on/off grid','independent CW holdouts','publish validated profile'] if method=='tone' else
                ['rectangular hot/cold grid','independent hot/cold holdouts','publish checked conditional profile']),
            'native_calibration':'advertised automatic RX DC/IQ modes only; vendor TX/EEPROM/native calibration utilities are not invoked',
            'bias_policy':'advertised bias tee is disabled; external LNAs must be powered separately with appropriate DC isolation',
            'hardware_verified':False}
    finally:
        if sdr is not None:sdr.stop(cancel=True)


class SourceController:
    """Explicit CLI hook or terminal confirmation; configuration never executes code."""
    def __init__(self, hook=None, timeout=30, input_stream=None, output_stream=None):
        self.hook=None if hook is None else Path(hook).resolve()
        self.timeout=finite(timeout,'source hook timeout',positive=True)
        self.input=input_stream or sys.stdin;self.output=output_stream or sys.stderr
        if self.hook is not None and not self.hook.is_file():raise ValueError('source hook must be an existing executable file')
        if self.hook is None and not self.input.isatty():raise ValueError('guided calibration needs a terminal or an explicit --source-hook')
        self.hook_sha256=None if self.hook is None else _sha256(self.hook)
        self.automatic=self.hook is not None

    def __call__(self, state, rf, level=None):
        request={'version':1,'request_id':uuid.uuid4().hex,'state':state,'rf_hz':rf,'input_dbm':level}
        if self.hook is None:
            print('Set source/connection: {}. Verify safe physical setup, then type READY:'.format(request),file=self.output,flush=True)
            if self.input.readline().strip()!='READY':raise ValueError('source state was not explicitly confirmed')
            return dict(request,confirmed=True,mode='operator')
        if _sha256(self.hook)!=self.hook_sha256:raise ValueError('source hook changed during calibration')
        # No shell, no command read from config, bounded diagnostic output read.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            import json
            try:
                completed=subprocess.run([str(self.hook)],input=(json.dumps(request)+'\n').encode(),
                    stdout=stdout,stderr=stderr,timeout=self.timeout,check=False)
            except subprocess.TimeoutExpired:raise ValueError('source hook timed out; verify physical source state') from None
            if completed.returncode:raise ValueError('source hook failed; verify physical source state')
            stdout.seek(0);reply=stdout.read(65537)
            if len(reply)>65536:raise ValueError('source hook reply exceeds 64 KiB')
        ack=loads_json(reply.decode('utf-8'))
        if not isinstance(ack,dict) or ack.get('confirmed') is not True:raise ValueError('source hook must confirm applied state')
        for key,value in request.items():
            if type(ack.get(key)) is not type(value) or ack.get(key)!=value:raise ValueError('source hook acknowledgment mismatch: '+key)
        return dict(request,confirmed=True,mode='hook',hook_sha256=self.hook_sha256)


def run_session(config_path, directory, controller=None, factory=None):
    """Capture, check and publish; a failed session never publishes a profile."""
    evidence=EvidenceSnapshot([config_path]);c=configuration(load_json(config_path))
    readiness=plan(c,factory=factory)
    if readiness['blockers']:raise ValueError('calibration blocked: '+'; '.join(readiness['blockers']))
    if controller is None:
        if readiness['method']=='baseline':
            # A declared passive termination needs no generator or switching.
            def controller(state,rf,level=None):
                if state!='terminated':raise ValueError('passive-load session cannot switch a source')
                return {'state':state,'rf_hz':rf,'input_dbm':None,'confirmed':True,
                        'mode':'operator_configured_passive_termination'}
        else:controller=SourceController()
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    evidence.protect_output(directory/'session.json')
    report={'kind':'auto_calibration_session','version':1,'status':'running','plan':readiness,
        'configuration_sha256':_sha256(config_path),'events':[],
        'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'hardware_verified':False,'absolute_profile_published':False}
    write_json(directory/'session.json',report)
    pending_profile=None
    sdr=None;last_state=None;current_rf=c['rf_hz'][0];method=readiness['method'];source=c.get('source');counter=0
    def state(name,rf,level=None):
        nonlocal last_state,current_rf
        current_rf=rf;last_state='unknown'
        ack=controller(name,rf,level);report['events'].append(ack);last_state=name
        write_json(directory/'session.json',report,overwrite=True)
    try:
        sdr,context,controls,capabilities=prepare(c,factory)
        _compare(readiness['context'],context,'preflight vs acquisition context')
        a,r=c['acquisition'],c['receiver'];rate=finite(sdr.device.sample_rate,'actual sample rate',positive=True)
        needed=a['fft_bins']+(a['segments']-1)*(a['fft_bins']-math.floor(a['fft_bins']*a['fft_overlap']))
        repeats=(needed+a['fft_bins']-1)//a['fft_bins']
        def capture(rf,bb,name,level=None):
            nonlocal counter
            state(name,rf,level);counter+=1;filename='capture-{:04d}-{}.jsonl'.format(counter,name)
            sdr._output=str(directory/filename)
            sign=-1 if r['lnb_high_side'] else 1
            # sweep accepts RF origins and maps the converter LO internally.
            # Do not pass an already converted hardware tune (double mapping).
            origin=rf-sign*(-bb if r['iq_conjugate'] else bb)
            sdr.sweep(origin,origin,a['fft_bins'],repeats,runs=1,fft_overlap=a['fft_overlap'],
                log_scale=False,partial_policy='reject',tune_delay=a['tune_delay'],settle_samples=a['settle_samples'],
                lock_timeout=a['lock_timeout'],lock_sensor=a['lock_sensor'],reference_lock_sensor=a['reference_lock_sensor'],
                lnb_lo=r['lnb_lo'],lnb_high_side=r['lnb_high_side'],iq_conjugate=r['iq_conjugate'],
                require_timestamps=a['require_timestamps'])
            evidence.add(directory/filename)
            measured=average_capture(directory/filename,a['segments'],require_complete=True)
            _check_coordinates(measured,rf,bb)
            _compare(context,measured['metadata']['context'],'session fixed context')
            width=a['integration_width_hz'];summary=band_summary(measured,rf-width/2,rf+width/2)
            return filename,summary
        def pair(rf,bb,level=None):
            row={'rf_hz':rf,'baseband_hz':bb,'start_hz':rf-a['integration_width_hz']/2,'stop_hz':rf+a['integration_width_hz']/2}
            summaries={}
            for name in (('on','off') if method=='tone' else ('hot','cold')):
                row[name],summaries[name]=capture(rf,bb,name,level if name=='on' else None)
            loss=_loss(source.get('path_loss_db',0),rf)
            if method=='tone':row.update(input_dbm=level,path_loss_db=loss)
            else:
                cold,hot=_temperatures(source,rf)
                row.update(cold_temperature_k=cold,hot_temperature_k=hot,
                    source_ohms=source['source_ohms'],receiver_ohms=source['receiver_ohms'],path_loss_db=loss,path_temperature_k=source['path_temperature_k'])
            return row,summaries
        if method=='baseline':
            rows=[]
            for rf in c['rf_hz']:
                for bb in c['baseband_hz']:
                    filename,summary=capture(rf,bb,'terminated')
                    rows.append({'rf_hz':rf,'baseband_hz':bb,'capture':filename,'measured_baseline':summary})
            report['baseline']={'thermal_model':thermal_reference(**c['termination']),'cells':rows,'absolute_calibration':False}
        else:
            limits=c['acceptance'];level=None
            if method=='tone':level=source['levels_dbm'][len(source['levels_dbm'])//2]
            manifest={'version':1,'method':method,'reference':c['reference'],'reference_plane':c['reference_plane'],
                'reference_baseband_hz':c['reference_baseband_hz'],'min_segments':a['segments'],'assumptions_confirmed':True,
                'min_snr_db':limits['min_snr_db'],'min_y_db':limits['min_y_db'],'cells':[]}
            linearity_cells=[]
            for rf in c['rf_hz']:
                for bb in c['baseband_hz']:
                    if method=='tone':
                        powers=[];measured_rows=[]
                        for step_level in source['levels_dbm']:
                            row,summaries=pair(rf,bb,step_level);measured_rows.append(row)
                            net=summaries['on']['power']-summaries['off']['power'];off=summaries['off']['power']
                            if net<=0 or (off>0 and 10*(math.log10(net)-math.log10(off))<limits['min_snr_db']):
                                raise ValueError('linearity reference has insufficient net SNR')
                            powers.append(net)
                        check=validate_cw([x-_loss(source.get('path_loss_db',0),rf) for x in source['levels_dbm']],powers,
                            limits['max_cw_residual_db'],limits['cw_slope_tolerance'])
                        check.update(rf_hz=rf,baseband_hz=bb,captures=measured_rows,net_raw_powers=powers)
                        linearity_cells.append(check)
                        report['linearity']={'accepted':all(x['accepted'] for x in linearity_cells),'cells':linearity_cells}
                        if not check['accepted']:raise ValueError('source level-linearity checks failed; adjust safe input/gain and start a new session')
                        manifest['cells'].append(measured_rows[len(measured_rows)//2])
                    else:
                        row,_=pair(rf,bb,level);manifest['cells'].append(row)
            write_json(directory/'measurements.json',manifest)
            evidence.add(directory/'measurements.json')
            candidate=build_manifest(directory/'measurements.json');checks=[];holdouts=[]
            for point in c['holdouts']:
                rf,bb=point['rf_hz'],point['baseband_hz']
                if method=='tone':
                    for holdout_level in source['levels_dbm']:
                        row,_=pair(rf,bb,holdout_level)
                        row['input_dbm']-=row.pop('path_loss_db');holdouts.append(row)
                else:
                    row,summaries=pair(rf,bb)
                    loss=row['path_loss_db'];path_k=row['path_temperature_k']
                    solution=noise_solution(summaries['cold']['mean_density'],summaries['hot']['mean_density'],
                        effective_temperature(row['cold_temperature_k'],loss,path_k),
                        effective_temperature(row['hot_temperature_k'],loss,path_k),limits['min_y_db'])
                    error=float(candidate.offsets_db([rf],[bb])[0])-solution['offset_db']
                    checks.append(dict(row,offset_error_db=error,passed=abs(error)<=limits['max_abs_error_db']))
            if method=='tone':
                holdout={'version':1,'reference':c['reference'],'independent_measurements_confirmed':True,
                    'min_segments':a['segments'],'min_snr_db':limits['min_snr_db'],'max_abs_error_db':limits['max_abs_error_db'],
                    'max_ripple_db':limits['max_ripple_db'],'cells':holdouts}
                write_json(directory/'holdouts.json',holdout);evidence.add(directory/'holdouts.json');validation=validate_holdout(candidate,directory/'holdouts.json')
            else:
                errors=[x['offset_error_db'] for x in checks];ripple=max(errors)-min(errors)
                validation={'kind':'independent_hot_cold_validation','cells':checks,'ripple_db':ripple,
                    'passed':all(x['passed'] for x in checks) and ripple<=limits['max_ripple_db'],
                    'scope':'conditional thermal model; identical downstream gain and independently verified linearity required'}
            report['validation']=validation;write_json(directory/'validation.json',validation);evidence.add(directory/'validation.json')
            if not validation['passed']:raise ValueError('independent calibration validation failed')
            evidence.verify()
            # Capture pairs finish off/cold before an absolute artifact is published.
            if last_state not in ('off','cold'):raise ValueError('reference source is not in its final off/cold state')
            profile=candidate.profile
            profile['method_details']['auto_calibration']={
                'configuration_sha256':report['configuration_sha256'],'family':readiness['family'],
                'applied_controls':controls,'validation_sha256':_sha256(directory/'validation.json'),
                'validated_response_sha256':response_sha256(candidate),
                'linearity_scope':'all measured grid coordinates and declared source levels' if method=='tone' else 'operator-confirmed source-state linearity',
                'uncertainty_status':'not automatically evaluated; RF source/path/match/drift uncertainties remain required'}
            pending_profile=PowerCalibration(profile).profile
        evidence.verify();report['status']='completed'
    except BaseException as error:
        report['status']='failed';report['error']='{}: {}'.format(type(error).__name__,error)
        raise
    finally:
        try:
            if sdr is not None:sdr.stop(cancel=True)
        except BaseException as cleanup_error:
            report['status']='failed';report['cleanup_error']=str(cleanup_error)
            raise
        finally:
            report['last_source_state']=last_state
            if last_state in ('on','hot','unknown'):
                if getattr(controller,'automatic',False):
                    try:
                        safe='cold' if method=='hot-cold' else 'off'
                        ack=controller(safe,current_rf,None);report['events'].append(ack)
                        report['last_source_state']=safe
                    except BaseException as cleanup_error:
                        report['source_cleanup_error']=str(cleanup_error)
                if report['last_source_state'] in ('on','hot','unknown'):
                    print('Calibration stopped: source state may be active/unknown. Set it OFF/COLD and verify connections.',file=sys.stderr)
            report['finished_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
            write_json(directory/'session.json',report,overwrite=True)
    if pending_profile is not None:
        published=False
        try:
            write_json(directory/'profile.json',pending_profile,before_publish=evidence.verify)
            published=True;report['absolute_profile_published']=True
            report['profile_sha256']=PowerCalibration(pending_profile).profile_id
            write_json(directory/'session.json',report,overwrite=True)
        except BaseException as error:
            if published:(directory/'profile.json').unlink()
            report['absolute_profile_published']=False;report['status']='failed';report['error']=str(error)
            write_json(directory/'session.json',report,overwrite=True)
            raise
    return report
