"""Optional RF readbacks, distinct from continuous transport/phase validity."""
import math
import time


def observe(frontend):
    """Read only advertised channel tuning components and sensors.

    An absent sensor is unknown. Before/after snapshots cannot establish lock
    between reads, RF settling accuracy or calibrated amplitude/phase.
    """
    tuning = {}
    for name in frontend.optional('listFrequencies',frontend.direction,frontend.channel) or []:
        # CORR and backend-specific pseudo-components need not have Hz units.
        if str(name).upper() not in ('RF','BB'):
            continue
        value = frontend.optional('getFrequency',frontend.direction,frontend.channel,name)
        try:
            value = float(value)
            if not math.isfinite(value):value = None
        except (TypeError,ValueError):value = None
        tuning[str(name)] = value
    sensors = {}
    for prefix,args in (('device',()),('rx',(frontend.direction,frontend.channel))):
        for name in frontend.optional('listSensors',*args) or []:
            value = frontend.optional('readSensor',*(args+(name,)))
            sensors[prefix+':'+str(name)] = None if value is None else str(value)
    return dict(monotonic_s=time.monotonic(),tuning_components_hz=tuning,sensors=sensors,
                continuously_verified=False,calibrated_rf_state=False)


def require_stable(before, after, tolerance_hz):
    for key,value in before['tuning_components_hz'].items():
        final = after['tuning_components_hz'].get(key)
        if value is not None and final is None:
            raise RuntimeError('RF tuning component readback disappeared: '+key)
        if value is not None and final is not None and abs(value-final)>tolerance_hz:
            raise RuntimeError('RF tuning component changed during acquisition: '+key)


def require_lock(frontend, channel_sensor=None, reference_sensor=None):
    for name,args in ((channel_sensor,(frontend.direction,frontend.channel)),(reference_sensor,())):
        if not name:continue
        value = frontend.optional('readSensor',*(args+(name,)))
        if value is None or str(value).strip().lower() not in ('true','1','yes','locked'):
            raise RuntimeError('requested lock sensor not locked after acquisition: '+name)
