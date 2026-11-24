"""Inventory hardware telemetry and build/apply explicitly reference-validated models."""
import argparse
import copy
import io
import json
import numpy as np
from soapypower.files import load_json,loads_json,write_json,_atomic_write,EvidenceSnapshot
from soapypower.telemetry import TelemetryMonitor,TelemetryPolicy,digest,optional_call
from soapypower.telemetry_models import fit_thermal,fit_settling,ThermalModel
from soapypower.filter_model import digital_response


def plain(value):
    if value is None or isinstance(value,(str,int,float,bool)):return value
    if isinstance(value,complex):return [value.real,value.imag]
    if hasattr(value,'minimum') and hasattr(value,'maximum'):return dict(minimum=value.minimum(),maximum=value.maximum(),step=value.step())
    if hasattr(value,'key') and hasattr(value,'description'):
        return {k:plain(getattr(value,k)) for k in ('key','name','description','units','type','range','options','optionNames') if hasattr(value,k)}
    if isinstance(value,dict) or hasattr(value,'items'):return {str(k):plain(v) for k,v in value.items()}
    try:return [plain(v) for v in value]
    except TypeError:return str(value)


def measurement_signature(context,frequency_edges_hz,processing):
    from soapypower.calibration import require_reference_context
    from soapypower.spectral_comparison import edges
    require_reference_context(context)
    grid=edges(frequency_edges_hz)
    if len(grid)>1000000:raise ValueError('reference grid exceeds budget')
    if not isinstance(processing,dict) or not processing:raise ValueError('reference processing identity required')
    return digest(dict(context=context,frequency_edges_hz=grid.tolist(),processing=processing))


def apply_thermal(model,archive,observation):
    from soapypower.numerics import finite
    if observation.get('fresh') is not True:raise ValueError('fresh temperature evidence is required')
    age=finite(observation.get('age_s'),'temperature age',nonnegative=True)
    if age>min(model.max_age,finite(observation.get('max_age_s'),'temperature maximum age',positive=True)):raise ValueError('stale temperature cannot apply a correction')
    meta=loads_json(str(archive['metadata'].item()))
    signature=measurement_signature(meta['context'],archive['frequency_edges_hz'],meta['processing'])
    if signature!=observation.get('context_sha256'):raise ValueError('spectrum/reference observation identity mismatch')
    evaluation=model.evaluate(observation['temperature_c'],signature,observation['sensor_key'])
    if model.value['source']['response_scope']=='single_reference_channel' and len(archive['frequency_edges_hz'])!=2:raise ValueError('single-channel reference cannot correct a wider grid')
    p=np.asarray(archive['density'],dtype=float)
    if p.shape!=(len(archive['frequency_edges_hz'])-1,) or np.isinf(p).any() or np.any(p<0):raise ValueError('invalid linear spectrum density')
    if 'thermal_model' in meta:raise ValueError('thermal model was already applied')
    factor=10**(evaluation['correction_db']/10)
    with np.errstate(over='ignore',under='ignore'):corrected=p*factor
    if np.isinf(corrected).any() or np.any((p>0)&(corrected==0)):raise ValueError('thermal correction exceeds numeric representation')
    result={k:archive[k] for k in archive.files};result.update(density=corrected,raw_density=p.copy())
    meta=copy.deepcopy(meta);meta['thermal_model']=dict(evaluation,temperature_age_s=age,raw_density_preserved=True,units_changed=False)
    result['metadata']=np.array(json.dumps(meta,allow_nan=False,sort_keys=True))
    return result


def inventory(sdr):
    monitor=sdr._telemetry or TelemetryMonitor(sdr._frontend)
    if sdr.device.is_streaming:sdr.device.stop_stream()
    monitor.closed_slot();snapshot=monitor.snapshot();entries=[]
    for name,args in [('getHardwareInfo',()),('getClockSource',()),('listClockSources',()),('getTimeSource',()),('listTimeSources',()),('hasHardwareTime',()),('getHardwareTime',()),('getHardwareTime',('PPS',)),('listRegisterInterfaces',()),('listGPIOBanks',()),('listUARTs',()),('getStreamArgsInfo',(sdr._frontend.direction,sdr._frontend.channel)),('getGainRange',(sdr._frontend.direction,sdr._frontend.channel)),('getFrequencyRange',(sdr._frontend.direction,sdr._frontend.channel)),('getSampleRateRange',(sdr._frontend.direction,sdr._frontend.channel)),('getBandwidthRange',(sdr._frontend.direction,sdr._frontend.channel))]:
        result=optional_call(sdr._frontend.raw,name,*args);entries.append(dict(method=name,arguments=list(args),status=result['status'],value=plain(result['value']),error=result['error']))
    return dict(version=1,context=sdr.measurement_context(),snapshot=snapshot,capabilities=entries,
                firmware_writes=False,transmit_stream=False,unknown_registers_read=False)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command')
    p=commands.add_parser('inventory');p.add_argument('--device',required=True);p.add_argument('--sample-rate',type=float,default=2e6);p.add_argument('--bandwidth',type=float,default=0);p.add_argument('--gain',type=float,default=20);p.add_argument('--antenna',default='');p.add_argument('--master-clock-rate',type=float);p.add_argument('--policy');p.add_argument('-O','--output',required=True);p.add_argument('--overwrite',action='store_true')
    for name in ('fit-thermal','fit-settling','filter-model','signature'):
        p=commands.add_parser(name);p.add_argument('input');p.add_argument('output');p.add_argument('--overwrite',action='store_true')
        if name=='filter-model':p.add_argument('--frequency-grid',required=True)
    p=commands.add_parser('apply-thermal');p.add_argument('model');p.add_argument('input');p.add_argument('observation');p.add_argument('output');p.add_argument('--overwrite',action='store_true')
    args=parser.parse_args(argv)
    if args.command is None:parser.error('choose a telemetry command')
    try:
        if args.command=='inventory':
            from soapypower.power import SoapyPower
            policy=TelemetryPolicy(args.policy) if args.policy else None
            receiver=SoapyPower(soapy_args=args.device,sample_rate=args.sample_rate,bandwidth=args.bandwidth,gain=args.gain,antenna=args.antenna,master_clock_rate=args.master_clock_rate,output=io.BytesIO(),output_format='json')
            try:
                receiver.setup(64,1,reopen_stream=True,observe_rf_state=True,telemetry_policy=policy)
                result=inventory(receiver)
                write_json(args.output,result,overwrite=args.overwrite)
            finally:receiver.stop()
            return
        paths=[args.input]
        for field in ('model','observation','frequency_grid'):
            if getattr(args,field,None):paths.append(getattr(args,field))
        snapshot=EvidenceSnapshot(paths);snapshot.protect_output(args.output)
        if args.command in ('fit-thermal','fit-settling'):
            result=(fit_thermal if args.command=='fit-thermal' else fit_settling)(load_json(args.input))
            write_json(args.output,result,overwrite=args.overwrite,before_publish=snapshot.verify)
        elif args.command=='filter-model':
            chain=load_json(args.input);result=digital_response(chain,load_json(args.frequency_grid))
            arrays={k:v for k,v in result.items() if isinstance(v,np.ndarray)}
            arrays['metadata']=np.array(json.dumps({k:v for k,v in result.items() if k not in arrays},allow_nan=False))
            _atomic_write(args.output,lambda f:np.savez_compressed(f,**arrays),args.overwrite,before_publish=snapshot.verify)
        elif args.command=='signature':
            with np.load(args.input,allow_pickle=False) as archive:
                meta=loads_json(str(archive['metadata'].item()));result=measurement_signature(meta['context'],archive['frequency_edges_hz'],meta['processing'])
            write_json(args.output,dict(context_sha256=result),args.overwrite,before_publish=snapshot.verify)
        else:
            model=ThermalModel(args.model)
            with np.load(args.input,allow_pickle=False) as archive:result=apply_thermal(model,archive,load_json(args.observation))
            _atomic_write(args.output,lambda f:np.savez_compressed(f,**result),args.overwrite,before_publish=snapshot.verify)
    except (ValueError,RuntimeError,OSError,KeyError,TypeError,ImportError) as error:
        parser.error(str(error))


if __name__=='__main__':main()
