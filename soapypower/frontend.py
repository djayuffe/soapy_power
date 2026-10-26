"""Capability-checked SoapySDR controls; never infer architecture from a model name."""
from soapypower.numerics import finite


class Frontend:
    def __init__(self, wrapper, direction, channel, dc_correction='default', iq_correction='default'):
        self.wrapper = wrapper
        self.raw = getattr(wrapper, 'device', None)
        self.direction, self.channel = direction, channel
        if dc_correction not in ('default', 'on', 'off') or iq_correction not in ('default', 'on', 'off'):
            raise ValueError('hardware corrections must be default, on or off')
        self.dc_correction, self.iq_correction = dc_correction, iq_correction

    def supported(self, capability):
        method = getattr(self.raw, capability, None)
        return bool(method(self.direction, self.channel)) if method is not None else False

    def gain_mode(self, automatic):
        if self.supported('hasGainMode'):
            self.raw.setGainMode(self.direction, self.channel, bool(automatic))
            if bool(self.raw.getGainMode(self.direction, self.channel)) != bool(automatic):
                raise RuntimeError('SDR did not apply requested gain mode')
        elif automatic:
            raise ValueError('selected channel does not support AGC')

    def frequency_correction(self, ppm):
        ppm = finite(ppm, 'frequency correction')
        if self.supported('hasFrequencyCorrection'):
            self.raw.setFrequencyCorrection(self.direction, self.channel, ppm)
        elif 'CORR' in getattr(self.wrapper, 'list_frequencies', lambda: [])():
            self.wrapper.corr = ppm
        elif ppm:
            raise ValueError('selected channel does not support frequency correction')

    def apply_corrections(self):
        # Some drivers restore their own per-LO calibration when tuning.
        for selection, suffix in ((self.dc_correction, 'DCOffsetMode'), (self.iq_correction, 'IQBalanceMode')):
            if selection == 'default':
                continue
            if not self.supported('has' + suffix):
                raise ValueError('selected channel does not support hardware ' + suffix)
            enabled = selection == 'on'
            getattr(self.raw, 'set' + suffix)(self.direction, self.channel, enabled)
            actual = getattr(self.raw, 'get' + suffix)(self.direction, self.channel)
            if bool(actual) != enabled:
                raise RuntimeError('SDR did not apply requested hardware ' + suffix)

    def optional(self, method, *args):
        function = getattr(self.raw, method, None)
        if function is None:
            return None
        try:
            return function(*args)
        except (RuntimeError, NotImplementedError):
            return None

    def correction_state(self, suffix):
        if not self.supported('has' + suffix):
            return None
        value = self.optional('get' + suffix, self.direction, self.channel)
        return None if value is None else bool(value)

    def context(self, device_settings, tune_args, lnb_lo, lnb_high_side, iq_conjugate):
        gains = None
        names = getattr(self.wrapper, 'list_gains', lambda: [])()
        if self.raw is not None and hasattr(self.raw, 'getGain'):
            if names:
                gains = {name: self.optional('getGain', self.direction, self.channel, name) for name in names}
                if any(value is None for value in gains.values()):
                    gains = None
            else:
                value = self.optional('getGain', self.direction, self.channel)
                gains = {'overall': value} if value is not None else None
        info = self.optional('getHardwareInfo') or {}
        if self.supported('hasFrequencyCorrection'):
            ppm = self.optional('getFrequencyCorrection', self.direction, self.channel)
        else:
            ppm = getattr(self.wrapper, 'corr', None)
        settings = {key: self.optional('readSetting', key) for key in device_settings}
        gain_mode = (self.optional('getGainMode', self.direction, self.channel)
                     if self.supported('hasGainMode') else False)
        return {
            'hardware': getattr(self.wrapper, 'hardware', None),
            'driver': self.optional('getDriverKey'), 'serial': info.get('serial'),
            'hardware_info': dict(info), 'clock_source': self.optional('getClockSource'),
            'time_source': self.optional('getTimeSource'), 'master_clock_rate': self.optional('getMasterClockRate'),
            'channel': getattr(self.wrapper, 'channel', self.channel),
            'antenna': getattr(self.wrapper, 'antenna', None),
            'sample_rate': getattr(self.wrapper, 'sample_rate', None),
            'bandwidth': getattr(self.wrapper, 'bandwidth', None), 'gains': gains,
            'agc': None if gain_mode is None else bool(gain_mode),
            'dc_correction': self.correction_state('DCOffsetMode'),
            'iq_correction': self.correction_state('IQBalanceMode'),
            'device_settings': settings, 'tune_args': dict(tune_args),
            'lnb_lo': lnb_lo, 'lnb_high_side': bool(lnb_high_side),
            'iq_conjugate': bool(iq_conjugate), 'corr_ppm': ppm, 'sample_format': 'CF32',
        }
