"""Receive-only evidence campaign. Configure driver/library environment externally."""
import argparse,gzip,io,json,os,subprocess,time,gc,math
from pathlib import Path
from datetime import datetime,timezone
import numpy as np
from soapypower.acquisition_quality import HeadroomRejected
from soapypower.files import _atomic_write,write_json

p=argparse.ArgumentParser(description=__doc__);p.add_argument('--device',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--seconds',type=float,default=300);args=p.parse_args()
if not math.isfinite(args.seconds) or args.seconds <= 5:p.error('--seconds must be finite and greater than five')
from soapypower.power import SoapyPower

class IQReceiver(SoapyPower):
    def _read_chunk(self,output):
        n=super()._read_chunk(output)
        if getattr(self,'_acquisition_stage',None)=='measurement' and getattr(self,'saved',None) is not None:
            self.saved.append(output[:n].copy())
        return n

out=args.output.resolve();out.mkdir(parents=True,exist_ok=False);(out/'iq').mkdir();
state=dict(status='preflight',started_utc=datetime.now(timezone.utc).isoformat(),receive_only=True,antenna='RX2',input='operator reports antenna connected',sample_rate_hz=28e6,bandwidth_hz=28e6,master_clock_hz=56e6,gain_seed_db=20,requested_seconds=args.seconds,iq_captures=[],records=0,absolute_calibration=False,new_flash_write=False)
def save():write_json(out/'status.json',state,overwrite=True)
save();print('OUTPUT',out,flush=True)
context=None;frequency_range=None
try:
    for label,dc,gain,centers in [('native','on',20,[70,100,400,915,2450,5800]),('retune','on',20,[910,913,917,923,925]),('dc-off','off',20,[915]),('gain-low','on',0,[915]),('gain-high','on',40,[915])]:
        sdr=IQReceiver(soapy_args=args.device,sample_rate=28e6,bandwidth=28e6,gain=gain,antenna='RX2',output=io.BytesIO(),output_format='json',dc_correction=dc,master_clock_rate=56e6,force_sample_rate=True)
        try:
            sdr.setup(8192,128,base_buffer_size=65536,max_buffer_size=262144,tune_delay=.02,settle_samples=32768,lock_sensor='lo_locked',reopen_stream=True,require_timestamps=True,partial_policy='reject',precision='single',max_threads=2,max_queue_size=4,quality_policy={'evidence_dir':str(out/(label+'-iq-rejected'))})
            if label=='native':
                context=sdr.measurement_context();frequency_range=sdr.device.get_frequency_range()
                raw=sdr.device.device;direction=sdr._frontend.direction
                probe=dict(context=context,reported_frequency_range_hz=list(frequency_range),manufacturer_nominal_range_hz=[70e6,6000e6],reported_gain_names=list(sdr.device.list_gains()),tuning_components=list(raw.listFrequencies(direction,0)),channel_sensors=list(raw.listSensors(direction,0)),device_sensors=list(raw.listSensors()))
                write_json(out/'probe.json',probe)
            for mhz in centers:
                for trial in range(2):
                    sdr.saved=[]
                    try:
                        future,start,stop=sdr.psd(mhz*1e6);frequency,density=future.result()
                    except HeadroomRejected as error:
                        state.setdefault('rejected_preflight',[]).append(dict(label=label,center_mhz=mhz,trial=trial,reason=str(error)));save()
                        if label=='gain-high':break
                        raise
                    iq=np.concatenate(sdr.saved);sdr.saved=None
                    if sdr.device.is_streaming:
                        sdr.device.stop_stream()
                    metadata=sdr._psd.measurement_metadata(sdr._last_state);metadata.update(sdr._last_integrity);metadata['context']=sdr._last_context
                    assert len(iq)==1048576 and metadata['continuity']=='verified' and metadata['measurement_status']=='complete'
                    tag='{}-{}M-trial{}'.format(label,mhz,trial)
                    _atomic_write(out/'iq'/(tag+'.npz'),lambda f:np.savez_compressed(f,iq=iq,metadata=np.array(json.dumps(metadata,allow_nan=False))))
                    write_json(out/'iq'/(tag+'.json'),dict(center_hz=mhz*1e6,gain_db=gain,dc_correction=dc,trial=trial,sample_rate_hz=28e6,context=metadata['context'],headroom=metadata['headroom'],time_start=start.isoformat(),time_stop=stop.isoformat(),integrity=metadata))
                    state['iq_captures'].append(dict(tag=tag,center_mhz=mhz,gain_db=gain,dc=dc,trial=trial,samples=len(iq),headroom_max=metadata['headroom']['max_component']));save();print('IQ',tag,'max',metadata['headroom']['max_component'],flush=True)
        finally:
            sdr.stop()
            del sdr
    del raw  # Release the probe handle before the separate CLI process opens USB.
    gc.collect()
    gains=context['gains'];assert set(gains)=={'PGA'} and gains['PGA']==20
    profile=dict(version=1,type='receiver_scan_profile',reference='This campaign: independent trial 0/1 native antenna IQ at 70,100,400,915,2450,5800 MHz, fixed 20 dB. Bands are approximate seeds, not calibrated response; every hop is guarded.',identity={k:context[k] for k in ('driver','hardware','serial')},sample_rate_hz=28e6,bandwidth_hz=28e6,antenna='RX2',settings=context['device_settings'],expected_hardware_info={k:context['hardware_info'][k] for k in ('fw_version','fpga_version','mboard_serial')},bands=[dict(start_hz=frequency_range[0],stop_hz=frequency_range[1]+1,gains={'PGA':20})],points=[dict(center_hz=f*1e6,gains={'PGA':20}) for f in [70,100,400,915,2450,5800]],gain_strategy='ladder',gain_ladder=[{'PGA':g} for g in [20,15,10,5,0]])
    write_json(out/'receiver-profile.json',profile)
    cmd=[os.sys.executable,'-m','soapypower','-d',args.device,'-A','RX2','-C','0','-r','28M','-w','28M','--master-clock-rate','56M','--force-rate','-g','20','--dc-correction','on','-b','8192','--repeats','32','--tune-delay','.02','--settle-samples','32768','--lock-sensor','lo_locked','--reopen-stream','--deadline-policy','finish-hop','--require-timestamps','--partial-policy','reject','--no-pyfftw','--precision','single','--max-threads','2','--max-queue-size','4','-s','65536','-S','262144','-F','json','--rf-state','--receiver-profile',str(out/'receiver-profile.json'),'--headroom-retries','4','--rejection-evidence',str(out/'scan-rejected'),'--ensure-coverage','--usable-halfwidth','12M','--dc-mask-width','500k','-f','{}:{}'.format(*frequency_range),'-e',str(args.seconds),'-O','/dev/stdout']
    write_json(out/'scan-command.json',cmd);state.update(status='scanning',frequency_range_hz=list(frequency_range),scan_started_utc=datetime.now(timezone.utc).isoformat(),full_sweeps=0);save();print('SCAN_START',flush=True)
    first=None;previous=None;last_save=time.monotonic();child=None
    with (out/'scan.log').open('w') as log,gzip.open(out/'scan.jsonl.gz','wb',compresslevel=1) as capture:
        child=subprocess.Popen(cmd,env=os.environ,stdout=subprocess.PIPE,stderr=log)
        try:
            for line in child.stdout:
                capture.write(line)
                if not line.strip():continue
                r=json.loads(line);m=r['metadata'];c=m['context'];h=m['headroom'];center=m['tuned_center_hz']
                assert m['measurement_status']=='complete' and m['continuity']=='verified' and m['timestamp_coverage']==1 and h['accepted']
                assert c['serial']==context['serial'] and c['antenna']=='RX2' and c['sample_rate']==28e6 and c['agc'] is False
                assert m['acquired_samples']==m['processed_samples']==m['target_samples']==262144
                if previous is not None and center<previous:state['full_sweeps']+=1
                previous=center
                if first is None:first=datetime.fromisoformat(r['time_start'])
                state.update(records=state['records']+1,last_center_hz=center,captured_span_seconds=(datetime.fromisoformat(r['time_stop'])-first).total_seconds(),max_component=max(state.get('max_component',0),h['max_component']),max_p999_component=max(state.get('max_p999_component',0),h['p999_component']))
                if time.monotonic()-last_save>5:capture.flush();save();last_save=time.monotonic();print('PROGRESS',state['records'],round(state['captured_span_seconds'],1),center/1e6,flush=True)
            code=child.wait()
        finally:
            if child.poll() is None:child.terminate();child.wait(timeout=20)
    state['scan_exit_code']=code
    if code:raise RuntimeError('Full scan failed; all accepted records and rejection evidence retained')
    if state.get('captured_span_seconds',0)<args.seconds-5:raise RuntimeError('Scan ended early')
    state.update(status='captured',finished_utc=datetime.now(timezone.utc).isoformat());save();print('CAPTURED',state['records'],flush=True)
except BaseException as error:
    state.update(status='failed',error=str(error));save();raise
