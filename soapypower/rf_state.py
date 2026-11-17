"""Optional RF readbacks, distinct from continuous transport/phase validity."""
import math
import time


def observe(frontend, monitor=None):
    """Read only advertised channel tuning components and sensors.

    An absent sensor is unknown. Before/after snapshots cannot establish lock
    between reads, RF settling accuracy or calibrated amplitude/phase.
    """
    if monitor is not None:
        return monitor.snapshot()
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


def lo_mapping(snapshot, overall_hz, sample_rate_hz, frequency_sign=1, lnb_lo=0, iq_conjugate=False):
    """Locate controller-reported analog LO in the displayed RF/FFT coordinates."""
    from soapypower.numerics import finite
    overall_hz=finite(overall_hz,'receiver center')
    sample_rate_hz=finite(sample_rate_hz,'LO mapping sample rate',positive=True)
    lnb_lo=finite(lnb_lo,'LNB LO')
    if frequency_sign not in (-1,1) or isinstance(frequency_sign,bool):raise ValueError('frequency sign must be +1 or -1')
    if type(iq_conjugate) is not bool:raise ValueError('IQ conjugation must be boolean')
    parts=snapshot.get('tuning_components_hz',{})
    lo=parts.get('RF');bb=parts.get('BB')
    if lo is None or bb is None:return dict(status='unknown',automatic_exclusion=False)
    lo=finite(lo,'analog LO');bb=finite(bb,'digital downconversion')
    offset=lo-overall_hz
    baseband=-offset if iq_conjugate else offset
    displayed=lnb_lo+frequency_sign*(overall_hz+baseband)
    wrapped=(baseband+sample_rate_hz/2)%sample_rate_hz-sample_rate_hz/2
    return dict(status='controller_mapping',analog_lo_hz=lo,ddc_hz=bb,
        displayed_origin_hz=lnb_lo+frequency_sign*overall_hz,
        analog_lo_display_hz=displayed,analog_lo_baseband_hz=wrapped,
        rx_rf_minus_bb_residual_hz=overall_hz-(lo-bb),
        independently_measured_frequency=False,automatic_exclusion=False)
