"""Capability-checked SoapySDR controls; never infer architecture from a model name."""
import math
from soapypower.numerics import finite


class Frontend:
    def __init__(self, wrapper, direction, channel, dc_correction='default', iq_correction='default', manual_dc=None, manual_iq=None, manual_tolerance=None):
        self._capabilities = {}
        self.wrapper = wrapper
        self.raw = getattr(wrapper, 'device', None)
        self.direction, self.channel = direction, channel
        if dc_correction not in ('default', 'on', 'off') or iq_correction not in ('default', 'on', 'off'):
            raise ValueError('hardware corrections must be default, on or off')
        self.dc_correction, self.iq_correction = dc_correction, iq_correction
        self.manual_dc = self._manual_pair(manual_dc, 'DC offset')
        self.manual_iq = self._manual_pair(manual_iq, 'IQ balance')
        self.manual_tolerance = (None if manual_tolerance is None else
                                 finite(manual_tolerance, 'manual correction tolerance', nonnegative=True))

    @staticmethod
    def _manual_pair(value, name):
        if value is None:
            return None
        try:
            if isinstance(value, (str, bytes)) or len(value) != 2:
                raise ValueError
            result = tuple(finite(component, name) for component in value)
        except (TypeError, ValueError):
            raise ValueError(name + ' requires two finite normalized components') from None
        if any(abs(component) > 1 for component in result):
            raise ValueError(name + ' components must be in [-1, 1]')
        return result

    def supported(self, capability):
        # Some drivers probe gain-mode support by changing AGC temporarily.
        # Query once before acquisition, never repeatedly in measurement readback.
        if capability not in self._capabilities:
            method = getattr(self.raw, capability, None)
            self._capabilities[capability] = bool(method(self.direction, self.channel)) if method is not None else False
        return self._capabilities[capability]

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

        for value, suffix, mode in ((self.manual_dc, 'DCOffset', self.dc_correction),
                                    (self.manual_iq, 'IQBalance', self.iq_correction)):
            if value is None:
                continue
            if not self.supported('has' + suffix):
                raise ValueError('selected RX channel does not support manual ' + suffix)
            if self.supported('has' + suffix + 'Mode') and mode != 'off':
                raise ValueError('manual ' + suffix + ' requires automatic correction explicitly off')
            getattr(self.raw, 'set' + suffix)(self.direction, self.channel, complex(*value))
            self._manual_readback(suffix, value)

    def _manual_readback(self, suffix, requested):
        try:
            actual = complex(getattr(self.raw, 'get' + suffix)(self.direction, self.channel))
        except (AttributeError, TypeError, ValueError):
            raise RuntimeError('manual ' + suffix + ' readback is unavailable or invalid') from None
        if not math.isfinite(actual.real) or not math.isfinite(actual.imag):
            raise RuntimeError('nonfinite manual ' + suffix + ' readback')
        driver = self.optional('getDriverKey')
        quantum = (1 / (2048 if suffix == 'DCOffset' else 4096)) if driver == 'bladerf' else 1e-9
        tolerance = quantum if self.manual_tolerance is None else self.manual_tolerance
        if any(abs(got - wanted) > tolerance + 1e-12 for got, wanted in zip((actual.real, actual.imag), requested)):
            raise RuntimeError('SDR did not apply requested manual ' + suffix)
        result = {'requested': list(requested), 'actual': [actual.real, actual.imag]}
        if self.manual_tolerance is not None:
            result['readback_tolerance'] = tolerance
        return result

    def configure_clocking(self, clock_source=None, time_source=None, master_clock_rate=None):
        for choice, suffix in ((clock_source, 'ClockSource'), (time_source, 'TimeSource')):
            if choice is None:
                continue
            if not isinstance(choice, str) or not choice.strip():
                raise ValueError('clock/time source must be a nonempty name')
            choices = self.optional('list' + suffix + 's')
            if choices is not None and choice not in choices:
                raise ValueError('unsupported ' + suffix + ': ' + choice)
            setter = getattr(self.raw, 'set' + suffix, None)
            if setter is None:
                raise ValueError('device does not expose ' + suffix)
            setter(choice)
            if self.optional('get' + suffix) != choice:
                raise RuntimeError('SDR did not apply requested ' + suffix)
        if master_clock_rate is not None:
            rate = finite(master_clock_rate, 'master clock rate', positive=True)
            setter = getattr(self.raw, 'setMasterClockRate', None)
            if setter is None:
                raise ValueError('device does not expose master clock rate')
            setter(rate)
            actual = self.optional('getMasterClockRate')
            if actual is None or not math.isclose(float(actual), rate, rel_tol=1e-10, abs_tol=1e-6):
                raise RuntimeError('SDR did not apply requested master clock rate')

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
        info = dict(self.optional('getHardwareInfo') or {})
        mapping = self.optional('getFrontendMapping', self.direction)
        if isinstance(mapping, str) and mapping.strip():
            info['soapypower_rx_frontend_mapping'] = mapping
        try:
            native = self.optional('getNativeStreamFormat', self.direction, self.channel)
        except TypeError:
            # Older SoapySDR SWIG bindings lack the double& output typemap.
            # Do not guess a scale or fail otherwise usable CF32 acquisition.
            native = None
            info['soapypower_native_stream'] = {'status': 'unavailable_python_binding',
                                               'requested_format': 'CF32'}
        if native is not None:
            if (not isinstance(native, (tuple, list)) or len(native) != 2
                    or not isinstance(native[0], str) or not native[0].strip()):
                raise ValueError('invalid native stream format/full-scale readback')
            full_scale = finite(native[1], 'native full-scale', positive=True)
            info['soapypower_native_stream'] = {'format': native[0], 'full_scale': full_scale,
                                               'requested_format': 'CF32'}
        hardware_time = self.optional('hasHardwareTime')
        if hardware_time is not None:
            info['soapypower_hardware_time_available'] = bool(hardware_time)
        manual = {}
        for value, suffix in ((self.manual_dc, 'DCOffset'), (self.manual_iq, 'IQBalance')):
            if value is not None:
                manual[suffix] = self._manual_readback(suffix, value)
        if manual:
            info['soapypower_manual_rx_corrections'] = manual
        if self.supported('hasFrequencyCorrection'):
            ppm = self.optional('getFrequencyCorrection', self.direction, self.channel)
        else:
            ppm = getattr(self.wrapper, 'corr', None)
        keys = set(device_settings)
        for info_item in self.optional('getSettingInfo') or []:
            key = getattr(info_item, 'key', None)
            if isinstance(key, str) and key:
                keys.add(key)
        settings = {key: self.optional('readSetting', key) for key in sorted(keys)}
        gain_mode = (self.optional('getGainMode', self.direction, self.channel)
                     if self.supported('hasGainMode') else False)
        return {
            'hardware': getattr(self.wrapper, 'hardware', None),
            'driver': self.optional('getDriverKey'), 'serial': info.get('serial') or info.get('mboard_serial'),
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
