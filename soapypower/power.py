#!/usr/bin/env python3

import sys, time, datetime, math, logging, signal, gc, collections

import numpy
import simplesoapy
from simplespectral import zeros

from soapypower import psd, writer, rf_state
from soapypower.acquisition_quality import QualityPolicy, RejectionEvidence, HeadroomRejected, IQEvidenceBuffer
from soapypower.scan_profile import ScanProfile
from soapypower.coverage import masked_frequency_plan, coverage_gaps
from soapypower.telemetry import TelemetryMonitor, TelemetryPolicy, digest
from soapypower.frontend import Frontend
from soapypower.calibration import _compare
from soapypower.integrity import ContinuityTracker, shifted
from soapypower.numerics import crop_bins, finite, integer

logger = logging.getLogger(__name__)
_shutdown = False


def _shutdown_handler(sig, frame):
    """Set global _shutdown flag when receiving SIGTERM or SIGINT signals"""
    global _shutdown
    _shutdown = True


def install_signal_handlers():
    """Install CLI handlers explicitly, leaving library users' handlers intact."""
    global _shutdown
    _shutdown = False
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)
    if sys.platform == 'win32':
        signal.signal(signal.SIGBREAK, _shutdown_handler)


class SoapyPower:
    """SoapySDR spectrum analyzer"""
    _acquisition_stage = 'idle'  # class default: _read_chunk reads it before any acquisition has started
    def __init__(self, soapy_args='', sample_rate=2.00e6, bandwidth=0, corr=0, gain=20.7,
                 auto_gain=False, channel=0, antenna='', settings=None,
                 force_sample_rate=False, force_bandwidth=False,
                 output=sys.stdout, output_format='rtl_power', dc_correction='default',
                 iq_correction='default', tune_args=None, manual_dc=None, manual_iq=None,
                 manual_tolerance=None, clock_source=None, time_source=None, master_clock_rate=None,
                 stream_args=None):
        finite(sample_rate, 'sample_rate', positive=True)
        finite(bandwidth, 'bandwidth', nonnegative=True)
        integer(channel, 'channel')
        if output_format not in writer.formats:
            raise ValueError('unknown output format')
        self._soapy_args = soapy_args
        self._enumerated_identity = None
        self._stream_args = dict(stream_args or {})
        if any(not isinstance(k, str) or not k or not isinstance(v, str)
               for k, v in self._stream_args.items()):
            raise ValueError('stream arguments require nonempty string keys and string values')
        self.device = simplesoapy.SoapyDevice(
            soapy_args=soapy_args, sample_rate=sample_rate, bandwidth=bandwidth, corr=0,
            gain=None, auto_gain=False, channel=channel, antenna=antenna, settings=settings,
            force_sample_rate=force_sample_rate, force_bandwidth=force_bandwidth
        )
        compact = ''.join(str(soapy_args or '').split()).lower()
        if 'driver=uhd' in compact and 'num_recv_frames' not in compact:
            # UHD sizes its host receive ring when the device is opened (device string, NOT --stream-args).
            # The default (~16 frames, ~31 ms) overflows on any host stall longer than that.
            logger.info('UHD device string has no num_recv_frames: the default host ring (~31 ms) can overflow on '
                        'host stalls; consider -d "%s,num_recv_frames=512" (device-open argument)', soapy_args)
        if self._stream_args:
            # Use the binding's native map. Some SWIG/Python combinations lose
            # values when a Python dict is passed directly to setupStream().
            native = simplesoapy.SoapySDR.KwargsFromString('')
            for key, value in self._stream_args.items():
                native[key] = value
            self.device.stream_args = native

        actual_channel = getattr(self.device, 'channel', channel)
        if actual_channel != channel:
            raise ValueError('requested RX channel is unavailable; refusing fallback channel')
        self._frontend = Frontend(self.device, getattr(simplesoapy.SoapySDR, 'SOAPY_SDR_RX', 1), channel,
                                  dc_correction, iq_correction, manual_dc, manual_iq, manual_tolerance)
        self._frontend.configure_clocking(clock_source, time_source, master_clock_rate)
        if clock_source is not None or master_clock_rate is not None:
            # Clock reconfiguration can alter/coerce the delivered rate and filters.
            self.device.sample_rate = sample_rate
            if bandwidth:
                self.device.bandwidth = bandwidth
        self._requested_gain = dict(gain) if isinstance(gain, dict) else gain
        self._requested_agc = bool(auto_gain)
        self._apply_gain()
        self._frontend.frequency_correction(corr)
        self._frontend.apply_corrections()
        self._device_settings = dict(settings or {})
        self._tune_args = dict(tune_args or {})
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in self._tune_args.items()):
            raise ValueError('tuning arguments must be strings')
        self._tune_args_applied = False
        finite(self.device.sample_rate, 'device sample_rate', positive=True)
        self._deadline = None
        self._read_tail = numpy.empty(0, dtype=numpy.complex64)
        self._next_sample_time = None
        self._burst_ended = False
        self._output = output
        self._output_format = output_format

        self._buffer = None
        self._buffer_repeats = None
        self._base_buffer_size = None
        self._max_buffer_size = None
        self._bins = None
        self._repeats = None
        self._tune_delay = None
        self._reset_stream = None
        self._psd = None
        self._writer = None

    def _apply_gain(self):
        self._frontend.gain_mode(self._requested_agc)
        if not self._requested_agc:
            if isinstance(self._requested_gain, dict):
                for element, value in self._requested_gain.items():
                    if element == 'overall':
                        self.device.gain = finite(value, 'gain')
                    else:
                        self.device.set_gain(element, finite(value, 'gain'))
            elif self._requested_gain is not None:
                self.device.gain = finite(self._requested_gain, 'gain')

    def measurement_context(self):
        context = self._frontend.context(self._device_settings, self._tune_args,
                                         self._lnb_lo, self._lnb_high_side, self._iq_conjugate)
        if (getattr(self, '_receiver_profile', None) is not None and context['serial'] is None
                and str(context['driver']).lower() == 'rtlsdr'):
            if self._enumerated_identity is None:
                selection = simplesoapy.SoapySDR.KwargsFromString(self._soapy_args)
                candidates = list(simplesoapy.SoapySDR.Device.enumerate(selection))
                if len(candidates) != 1 or not candidates[0].get('serial'):
                    raise ValueError('RTL receiver profile requires one unambiguously enumerated serial')
                observed = dict(candidates[0])
                if selection.get('serial') and selection['serial'] != observed['serial']:
                    raise ValueError('enumerated serial differs from requested receiver')
                if str(observed.get('driver','')).lower() != 'rtlsdr':
                    raise ValueError('enumerated identity driver differs from the open receiver')
                index = context['hardware_info'].get('index')
                if index is not None and str(observed.get('rtl', observed.get('index'))) != str(index):
                    raise ValueError('RTL open-device index and enumeration identity differ')
                self._enumerated_identity = observed
            context['serial'] = self._enumerated_identity['serial']
            context['hardware_info']['soapypower_identity_enumeration'] = dict(self._enumerated_identity)
        if self._stream_args:
            # Preserve the fixed top-level context schema used by calibrated
            # references; transport provenance belongs in hardware_info.
            context['hardware_info']['soapypower_stream_args'] = dict(self._stream_args)
        return context

    def nearest_freq(self, freq, bin_size):
        """Return nearest frequency based on bin size"""
        return round(freq / bin_size) * bin_size

    def nearest_bins(self, bins, even=False, pow2=False):
        """Return nearest number of FFT bins (even or power of two)"""
        bins = integer(bins, 'bins', 2)
        if pow2:
            bins_log2 = (bins - 1).bit_length()
            if bins & (bins - 1):
                bins = 1 << bins_log2
                logger.warning('number of FFT bins should be power of two, changing to {}'.format(bins))
        elif even:
            if bins % 2 != 0:
                bins = math.ceil(bins / 2) * 2
                logger.warning('number of FFT bins should be even, changing to {}'.format(bins))

        return bins

    def nearest_overlap(self, overlap, bins):
        """Return nearest overlap/crop factor based on number of bins"""
        count = crop_bins(bins, overlap)
        actual = count / bins
        if not math.isclose(actual, overlap, rel_tol=1e-14, abs_tol=1e-16):
            logger.info('overlap/crop quantized to %d bins (%.8f)', count, actual)
        return actual

    def bin_size_to_bins(self, bin_size):
        """Convert bin size [Hz] to number of FFT bins"""
        if not math.isfinite(bin_size) or bin_size <= 0:
            raise ValueError('bin_size must be finite and positive')
        return max(2, math.ceil(self.device.sample_rate / bin_size))

    def bins_to_bin_size(self, bins):
        """Convert number of FFT bins to bin size [Hz]"""
        return finite(self.device.sample_rate, 'sample_rate', positive=True) / integer(bins, 'bins', 2)

    def time_to_repeats(self, bins, integration_time):
        """Convert integration time to number of repeats"""
        if not math.isfinite(integration_time) or integration_time <= 0:
            raise ValueError('integration_time must be finite and positive')
        self.nearest_bins(bins)
        return math.ceil((self.device.sample_rate * integration_time) / bins)

    def repeats_to_time(self, bins, repeats):
        """Convert number of repeats to integration time"""
        return (repeats * bins) / self.device.sample_rate

    def freq_plan(self, min_freq, max_freq, bins, overlap=0, quiet=False, frequency_sign=1):
        """Returns list of frequencies for frequency hopping"""
        if not all(math.isfinite(f) for f in (min_freq, max_freq)) or max_freq < min_freq:
            raise ValueError('frequency range must be finite and ascending')
        self.nearest_bins(bins)
        overlap = self.nearest_overlap(overlap, bins)
        bin_size = self.bins_to_bin_size(bins)
        removed = crop_bins(bins, overlap)
        bins_crop = bins - removed
        sample_rate_crop = bins_crop * bin_size

        freq_range = max_freq - min_freq
        hopping = True if freq_range >= sample_rate_crop else False
        hop_size = sample_rate_crop
        hops = math.ceil(freq_range / hop_size) if hopping else 1
        if frequency_sign not in (-1, 1):
            raise ValueError('frequency_sign must be +1 or -1')
        # Mirroring an even FFT changes which Nyquist endpoint is retained.
        first_bin = -(bins // 2) if frequency_sign == 1 else -((bins - 1) // 2)
        first_bin_offset = (first_bin + removed // 2) * bin_size
        if hopping:
            min_center_freq = min_freq - first_bin_offset
        elif freq_range:
            min_center_freq = (min_freq + max_freq) / 2 - first_bin_offset - hop_size / 2
        else:
            min_center_freq = min_freq
        max_center_freq = min_center_freq + ((hops - 1) * hop_size)

        freq_list = [min_center_freq + (i * hop_size) for i in range(hops)]

        if not quiet:
            logger.info('overlap: {:.5f}'.format(overlap))
            logger.info('bin_size: {:.2f} Hz'.format(bin_size))
            logger.info('bins: {}'.format(bins))
            logger.info('bins (after crop): {}'.format(bins_crop))
            logger.info('sample_rate: {:.3f} MHz'.format(self.device.sample_rate / 1e6))
            logger.info('sample_rate (after crop): {:.3f} MHz'.format(sample_rate_crop / 1e6))
            logger.info('freq_range: {:.3f} MHz'.format(freq_range / 1e6))
            logger.info('hopping: {}'.format('YES' if hopping else 'NO'))
            logger.info('hop_size: {:.3f} MHz'.format(hop_size / 1e6))
            logger.info('hops: {}'.format(hops))
            logger.info('min_center_freq: {:.3f} MHz'.format(min_center_freq / 1e6))
            logger.info('max_center_freq: {:.3f} MHz'.format(max_center_freq / 1e6))
            logger.info('min_freq (after crop): {:.3f} MHz'.format((min_center_freq + first_bin_offset) / 1e6))
            logger.info('max_freq (after crop): {:.3f} MHz'.format((max_center_freq + first_bin_offset + hop_size) / 1e6))

            logger.debug('Frequency hops table:')
            logger.debug('  {:8s}      {:8s}      {:8s}'.format('Min:', 'Center:', 'Max:'))
            for f in freq_list:
                logger.debug('  {:8.3f} MHz  {:8.3f} MHz  {:8.3f} MHz'.format(
                    (f - (self.device.sample_rate / 2)) / 1e6,
                    f / 1e6,
                    (f + (self.device.sample_rate / 2)) / 1e6,
                ))

        return freq_list

    def create_buffer(self, bins, repeats, base_buffer_size, max_buffer_size=0):
        """Create buffer for reading samples"""
        bins = integer(bins, 'bins', 2)
        repeats = integer(repeats, 'repeats', 1)
        base_buffer_size = integer(base_buffer_size, 'base_buffer_size', 1)
        max_buffer_size = integer(max_buffer_size, 'max_buffer_size', -1)
        samples = bins * repeats
        target = samples
        buffer_target = ((samples + base_buffer_size - 1) // base_buffer_size) * base_buffer_size
        if not max_buffer_size:
            # About 100 ms of IQ by default, capped at 100 MiB. This bounds
            # read latency and queued copies; contiguous framing preserves math.
            limit = min(100 * 1024**2 // 8,
                        max(base_buffer_size, math.ceil(self.device.sample_rate * 0.1)))
            max_buffer_size = max(base_buffer_size, (limit // base_buffer_size) * base_buffer_size)
        if max_buffer_size > 0:
            max_buffer_size = (max_buffer_size // base_buffer_size) * base_buffer_size
            if max_buffer_size < base_buffer_size:
                raise ValueError('maximum buffer must fit one device block')
            buffer_size = min(buffer_target, max_buffer_size)
        else:
            buffer_size = buffer_target
        buffer_repeats = (target + buffer_size - 1) // buffer_size
        logger.info('repeats: {}'.format(repeats))
        logger.info('samples: {} (time: {:.5f} s)'.format(samples, samples / self.device.sample_rate))
        if max_buffer_size > 0:
            logger.info('max_buffer_size (samples): {} (repeats: {:.2f}, time: {:.5f} s)'.format(
                max_buffer_size, max_buffer_size / bins, max_buffer_size / self.device.sample_rate
            ))
        else:
            logger.info('max_buffer_size (samples): UNLIMITED')
        logger.info('buffer_size (samples): {} (repeats: {:.2f}, time: {:.5f} s)'.format(
            buffer_size, buffer_size / bins, buffer_size / self.device.sample_rate
        ))
        logger.info('buffer_repeats: {}'.format(buffer_repeats))

        return (buffer_repeats, zeros(buffer_size, numpy.complex64))

    def setup(self, bins, repeats, base_buffer_size=0, max_buffer_size=0, fft_window='hann',
              fft_overlap=0.5, crop_factor=0, log_scale=True, remove_dc=False, detrend=None,
              lnb_lo=0, tune_delay=0, reset_stream=False, max_threads=0, max_queue_size=0,
              precision='double', iq_conjugate=False, lnb_high_side=False,
              dc_mask_width=0, dc_offset=0, calibration=None, fft_threads=1, partial_policy=None, acquisition_rounding='exact',
              settle_samples=0, lock_sensor=None, lock_timeout=2.0, timestamp_tolerance_samples=0.25,
              require_timestamps=False, workspace_budget_bytes=64 * 1024**2, allow_estimated_dc=False,
              reference_lock_sensor=None, reopen_stream=False, quality_policy=None, receiver_profile=None,
              observe_rf_state=False, telemetry_policy=None, settling_model=None):
        """Prepare samples buffer and start streaming samples from device"""
        bins = integer(bins, 'bins', 2)
        repeats = integer(repeats, 'repeats', 1)
        base_buffer_size = integer(base_buffer_size, 'base_buffer_size')
        max_buffer_size = integer(max_buffer_size, 'max_buffer_size', -1)
        if not math.isfinite(tune_delay) or tune_delay < 0:
            raise ValueError('tune_delay must be finite and nonnegative')
        if not 0 <= fft_overlap < 1 or not 0 <= crop_factor < 1:
            raise ValueError('overlap and crop must be in [0, 1)')
        if self._psd is not None or self.device.is_streaming:
            self.stop()

        if max_queue_size < 0:
            raise ValueError('live acquisition requires a bounded DSP queue')
        if partial_policy not in (None, 'emit', 'reject', 'legacy'):
            raise ValueError('partial policy must be emit, reject or legacy')
        if acquisition_rounding not in ('exact', 'block'):
            raise ValueError('acquisition rounding must be exact or block')
        self._partial_policy = partial_policy or ('reject' if calibration is not None else 'emit')
        self._settle_samples = integer(settle_samples, 'settle samples')
        self._lock_sensor = lock_sensor
        self._reference_lock_sensor = reference_lock_sensor
        self._lock_timeout = finite(lock_timeout, 'lock timeout', positive=True)
        self._timestamp_tolerance = finite(timestamp_tolerance_samples, 'timestamp tolerance', nonnegative=True)
        self._require_timestamps = bool(require_timestamps)
        self._acquisition_rounding = acquisition_rounding
        self._calibration = calibration
        self._lnb_lo, self._lnb_high_side, self._iq_conjugate = lnb_lo, lnb_high_side, iq_conjugate
        self._quality_policy = QualityPolicy(quality_policy) if quality_policy is not None else None
        prior_profile = getattr(self, '_receiver_profile', None)
        managed_dc = (prior_profile is not None and bool(prior_profile.anchors))
        self._receiver_profile = (receiver_profile if isinstance(receiver_profile, ScanProfile) else
                                  ScanProfile(receiver_profile)) if receiver_profile is not None else None
        if self._receiver_profile is not None and self._receiver_profile.telemetry_policy is not None:
            bound = self._receiver_profile.telemetry_policy
            if telemetry_policy is not None:
                requested = telemetry_policy if isinstance(telemetry_policy, TelemetryPolicy) else TelemetryPolicy(telemetry_policy)
                if requested.sha256 != bound.sha256:
                    raise ValueError('receiver profile telemetry policy cannot be overridden')
            telemetry_policy = bound
        if telemetry_policy is not None:
            parsed_policy = telemetry_policy if isinstance(telemetry_policy, TelemetryPolicy) else TelemetryPolicy(telemetry_policy)
            if parsed_policy.require_temperature and not reopen_stream:
                raise ValueError('required thermal validity needs --reopen-stream for safe polling')
        self._observe_rf_state = bool(observe_rf_state or self._quality_policy is not None)
        self._telemetry = TelemetryMonitor(self._frontend,telemetry_policy) if self._observe_rf_state or telemetry_policy is not None else None
        self._observe_rf_state = self._telemetry is not None
        if settling_model is not None:
            from soapypower.telemetry_models import validate_settling
            settling_model=validate_settling(settling_model)
        self._settling_model = settling_model
        self._read_latencies = []
        self._init_rx_diagnostics()
        self._read_errors = {}
        self._host_reads = 0
        self._gain_cache = {}
        self._quality_chunks = None
        self._rejection_evidence = None
        if self._receiver_profile is not None:
            if self._quality_policy is None:
                raise ValueError('receiver profiles require a headroom policy and rejected-IQ evidence')
            if calibration is not None or self._requested_agc or (self._frontend.manual_dc is not None and not managed_dc) or self._frontend.manual_iq is not None:
                raise ValueError('receiver profiles require fixed gain and cannot combine with absolute calibration or manual correction options')
            self._receiver_profile.validate_context(self.measurement_context())
            if managed_dc:
                self._frontend.manual_dc = [0.,0.]
                self._frontend.apply_corrections()
            if self._receiver_profile.anchors and self._frontend.dc_correction != 'off':
                raise ValueError('profile DC anchors require --dc-correction off')
        self._need_context = self._telemetry is not None or self._quality_policy is not None or calibration is not None or self._output_format in ('json', 'rtl_power_fftw', 'soapy_power_bin_v3')
        if self._frontend.manual_dc is not None or self._frontend.manual_iq is not None:
            self._frontend.apply_corrections()
        if calibration is not None:
            calibration.validate_context(self.measurement_context(), before_tune=True)
        self._burst_ended = False
        self._coverage_request = None
        try:
            if self._telemetry is not None:self._telemetry.closed_slot()
            base_buffer = self.device.start_stream(buffer_size=base_buffer_size)
            block_size = integer(len(base_buffer), 'device block size', 1)
            self._bins = bins
            self._repeats = repeats
            self._requested_samples = bins * repeats
            self._target_samples = (self._requested_samples if acquisition_rounding == 'exact' else
                                    ((self._requested_samples + block_size - 1) // block_size) * block_size)
            if self._quality_policy is not None:
                self._quality_policy.check_budget(self._target_samples)
                self._rejection_evidence = RejectionEvidence(self._quality_policy.directory)
            self._base_buffer_size = len(base_buffer)
            self._max_buffer_size = max_buffer_size
            self._buffer_repeats, self._buffer = self.create_buffer(
                bins, repeats, self._base_buffer_size, self._max_buffer_size
            )
            self._tune_delay = tune_delay
            self._reset_stream = reset_stream
            self._reopen_stream = bool(reopen_stream)
            self._psd = psd.PSD(bins, self.device.sample_rate, fft_window=fft_window, fft_overlap=fft_overlap,
                                crop_factor=crop_factor, log_scale=log_scale, remove_dc=remove_dc, detrend=detrend,
                                lnb_lo=lnb_lo, max_threads=max_threads, max_queue_size=max_queue_size or 32,
                                precision=precision, iq_conjugate=iq_conjugate,
                                frequency_sign=-1 if lnb_high_side else 1,
                                dc_mask_width=dc_mask_width, dc_offset=dc_offset, calibration=calibration,
                                fft_threads=fft_threads, nonblocking=True, input_format='complex',
                                workspace_budget_bytes=workspace_budget_bytes, allow_estimated_dc=allow_estimated_dc)
            self._writer = writer.formats[self._output_format](
                self._output, log_scale=log_scale, allow_nan=bool(dc_mask_width),
                power_unit='mW' if calibration is not None else 'input')
            self._writer.nonblocking = True
            logger.info('FFT bin spacing: %.8g Hz; window ENBW: %.8g Hz (%.6g bins)',
                        self._psd.bin_width, self._psd.enbw, self._psd.enbw / self._psd.bin_width)
            if not self._gc_observing:
                gc.callbacks.append(self._gc_observe)
                self._gc_observing = True
        except BaseException:
            self.stop()
            raise

    def stop(self, cancel=False):
        """Stop streaming samples from device and delete samples buffer"""
        if getattr(self, '_gc_observing', False):
            try:
                gc.callbacks.remove(self._gc_observe)
            except ValueError:
                pass
            self._gc_observing = False
        psd_worker, output_writer = self._psd, self._writer
        self._bins = None
        self._repeats = None
        self._base_buffer_size = None
        self._max_buffer_size = None
        self._buffer_repeats = None
        self._buffer = None
        self._tune_delay = None
        self._reset_stream = None
        self._psd = None
        self._writer = None
        try:
            if self.device.is_streaming:
                self.device.stop_stream()
        finally:
            try:
                if psd_worker is not None:
                    psd_worker.close(cancel=cancel)
            finally:
                if output_writer is not None:
                    try:
                        output_writer.close()
                    except BaseException:
                        if not cancel:
                            raise

    def _init_rx_diagnostics(self):
        """(Re)create RX gap / GC / settle-overflow counters; also called lazily so _read_chunk works on bare objects."""
        self._read_latency_max_ms = 0.0
        self._diagnostic_started = time.monotonic()
        self._previous_read_end = None
        self._rx_gap_warn_ms = 10.0
        self._rx_gaps = collections.deque(maxlen=4096)
        self._rx_gap_max_ms = 0.0
        self._rx_gap_last_ms = None
        self._rx_gap_count = 0
        self._measurement_rx_gaps = collections.deque(maxlen=4096)
        self._measurement_rx_gap_max_ms = 0.0
        self._measurement_read_latency_max_ms = 0.0
        self._gc_pause_max_ms = 0.0
        self._gc_pause_last_ms = None
        self._gc_pause_count = 0
        self._gc_pause_started = None
        self._gc_observing = False
        self._settle_overflow_count = 0
        self._settle_burst_end_count = 0
        self._settle_finished = time.monotonic()
        self._measurement_start_gap_ms = 0.0

    def _gc_observe(self, phase, info):
        """Record stop-the-world GC pauses that can starve the RX loop."""
        if phase == 'start':
            self._gc_pause_started = time.monotonic()
            return
        started = getattr(self, '_gc_pause_started', None)
        if started is None:
            return
        self._gc_pause_started = None
        elapsed_ms = max(0.0, (time.monotonic() - started) * 1000.0)
        self._gc_pause_last_ms = elapsed_ms
        if elapsed_ms >= self._rx_gap_warn_ms:
            self._gc_pause_count += 1
            self._gc_pause_max_ms = max(self._gc_pause_max_ms, elapsed_ms)
            if self._gc_pause_count <= 20:
                logger.warning('Python GC pause: %.3f ms at +%.3f s (generation %s, collected %s)',
                               elapsed_ms, time.monotonic() - self._diagnostic_started,
                               info.get('generation'), info.get('collected'))

    def _cancelled(self):
        if self._writer is not None:
            self._writer.check_error()
        return _shutdown or (self._deadline is not None and time.monotonic() >= self._deadline)

    def _read_chunk(self, output):
        """Read contiguous IQ; expose partial packets, overflow and time gaps.

        SimpleSoapy's bulk reader hides overflows. Read its public packet API
        directly, preserving unused samples when a short packet ends a chunk.
        """
        constants = simplesoapy.SoapySDR
        if not hasattr(self, '_rx_gaps'):
            self._init_rx_diagnostics()
        if not hasattr(self, '_tracker'):
            self._reset_packet_state()
        position = 0
        if self._read_tail.size:
            count = min(len(output), len(self._read_tail))
            self._record_times(self._read_tail_time, count)
            self._read_tail_time = shifted(self._read_tail_time, count)
            output[:count] = self._read_tail[:count]
            if getattr(self, '_quality_chunks', None) is not None and self._acquisition_stage == 'measurement':
                self._quality_chunks.append(output[:count])
            self._read_tail = self._read_tail[count:]
            position = count
        while position < len(output) and not self._cancelled() and not self._burst_ended:
            now = time.monotonic()
            previous = self._previous_read_end
            if previous is not None:
                gap_ms = max(0.0, (now - previous) * 1000.0)
                self._rx_gaps.append(gap_ms)
                self._rx_gap_last_ms = gap_ms
                self._rx_gap_max_ms = max(self._rx_gap_max_ms, gap_ms)
                if self._acquisition_stage == 'measurement':
                    self._measurement_rx_gaps.append(gap_ms)
                    self._measurement_rx_gap_max_ms = max(self._measurement_rx_gap_max_ms, gap_ms)
                if gap_ms >= self._rx_gap_warn_ms:
                    self._rx_gap_count += 1
                    if self._rx_gap_count <= 20:
                        logger.warning('RX scheduling gap: %.3f ms at +%.3f s during %s',
                                       gap_ms, now - self._diagnostic_started,
                                       getattr(self, '_acquisition_stage', 'unknown'))
                    elif self._rx_gap_count == 21:
                        logger.warning('RX scheduling gaps >= %.1f ms: further messages suppressed',
                                       self._rx_gap_warn_ms)
            read_started = now
            try:
                result = self.device.read_stream()
            except BaseException:
                self._read_errors['exception']=self._read_errors.get('exception',0)+1
                raise
            finally:
                read_finished = time.monotonic()
                self._previous_read_end = read_finished
                read_ms = max(0, read_finished - read_started) * 1000
                self._read_latency_max_ms = max(self._read_latency_max_ms, read_ms)
                if self._acquisition_stage == 'measurement':
                    self._measurement_read_latency_max_ms = max(
                        self._measurement_read_latency_max_ms, read_ms)
                self._read_latencies.append(read_ms)
                if len(self._read_latencies)>256:del self._read_latencies[0]
            self._host_reads+=1
            if result.ret<=0:self._read_errors[str(result.ret)]=self._read_errors.get(str(result.ret),0)+1
            count = result.ret
            if count == constants.SOAPY_SDR_OVERFLOW:
                self.device.buffer_overflow_count += 1
                stage = getattr(self, '_acquisition_stage', 'unknown')
                if stage == 'measurement' and self._measurement_rx_gaps:
                    gaps = self._measurement_rx_gaps
                    gap_max = self._measurement_rx_gap_max_ms
                    read_max = self._measurement_read_latency_max_ms
                else:
                    gaps = self._rx_gaps
                    gap_max = self._rx_gap_max_ms
                    read_max = self._read_latency_max_ms
                detail = ''
                if gaps:
                    previous_read = (self._read_latencies[-2] if len(self._read_latencies) > 1
                                     else self._read_latencies[-1])
                    detail = (' (stage {}, last gap {:.3f} ms, p99 gap {:.3f} ms, max gap {:.3f} ms, '
                              'last read {:.3f} ms, previous read {:.3f} ms, max read {:.3f} ms)').format(
                        stage, self._rx_gap_last_ms, float(numpy.percentile(gaps, 99)),
                        gap_max, self._read_latencies[-1], previous_read, read_max)
                raise RuntimeError('SDR overflow: samples were dropped; refusing discontinuous IQ' + detail)
            if count <= 0:
                raise RuntimeError('SDR read failed or timed out: {}'.format(count))
            if count > len(self.device.buffer):
                raise RuntimeError('SDR returned an invalid sample count')
            if result.flags & constants.SOAPY_SDR_END_ABRUPT:
                raise RuntimeError('SDR stream ended abruptly; IQ continuity is lost')
            packet_time = self._tracker.observe(count, bool(result.flags & constants.SOAPY_SDR_HAS_TIME), result.timeNs)
            copied = min(count, len(output) - position)
            self._record_times(packet_time, copied)
            output[position:position + copied] = self.device.buffer[:copied]
            if getattr(self, '_quality_chunks', None) is not None and self._acquisition_stage == 'measurement':
                self._quality_chunks.append(output[position:position + copied])
            position += copied
            if copied < count:
                self._read_tail = self.device.buffer[copied:count].copy()
                self._read_tail_time = shifted(packet_time, copied)
            if result.flags & constants.SOAPY_SDR_END_BURST:
                self._burst_ended = True
        return position

    def _reset_packet_state(self):
        self._tracker = ContinuityTracker(self._psd._sample_rate, self._timestamp_tolerance)
        self._read_tail_time = None
        self._time_first = self._time_last = None
        self._time_started = False

    def _record_times(self, first, count):
        if not count:
            return
        if not self._time_started:
            self._time_first = first
            self._time_started = True
        self._time_last = shifted(first, count - 1)

    def _settle(self, tuned_at):
        """Discard samples until the receiver has settled.

        Settling IQ never enters a PSD. A transport overflow here therefore
        cannot corrupt the measurement; it only proves that the host ring
        filled while the receiver was being tuned. Restart the settle window
        so measurement begins with a complete run of continuous samples.
        """
        discarded = 0
        window_samples = 0
        window_started = tuned_at
        while True:
            locked = True
            if self._lock_sensor:
                value = self._frontend.optional('readSensor', self._frontend.direction,
                                                self._frontend.channel, self._lock_sensor)
                if value is None:
                    raise ValueError('requested RX lock sensor is unavailable')
                locked = str(value).strip().lower() in ('true', '1', 'yes', 'locked')
            if self._reference_lock_sensor:
                value = self._frontend.optional('readSensor', self._reference_lock_sensor)
                if value is None:
                    raise ValueError('requested motherboard reference-lock sensor is unavailable')
                locked = locked and str(value).strip().lower() in ('true', '1', 'yes', 'locked')
            now = time.monotonic()
            if (window_samples >= self._settle_samples
                    and now >= window_started + self._active_tune_delay and locked):
                break
            if self._cancelled():
                self._settle_finished = time.monotonic()
                return discarded
            if now - tuned_at >= max(self._active_tune_delay, self._lock_timeout):
                raise RuntimeError('tuning settle timeout')
            try:
                count = self._read_chunk(self._buffer)
            except RuntimeError as error:
                if 'SDR overflow' not in str(error):
                    raise
                self._settle_overflow_count += 1
                if self._settle_overflow_count <= 20:
                    logger.warning('SDR overflow during settling; restarting settle window')
                count = 0
                self._burst_ended = False
                self._read_tail = numpy.empty(0, dtype=numpy.complex64)
                self._reset_packet_state()
                window_samples = 0
                window_started = time.monotonic()
            discarded += count
            window_samples += count
            if self._burst_ended:
                self._settle_burst_end_count += 1
                if self._settle_burst_end_count <= 20:
                    logger.warning('Burst boundary during settling; restarting settle window')
                self._burst_ended = False
                self._read_tail = numpy.empty(0, dtype=numpy.complex64)
                self._reset_packet_state()
                window_samples = 0
                window_started = time.monotonic()
        discarded += int(self._read_tail.size)
        # Settle samples and their clock anchors never form part of a PSD.
        self._read_tail = numpy.empty(0, dtype=numpy.complex64)
        self._reset_packet_state()
        self._settle_finished = time.monotonic()
        return discarded

    def psd(self, freq):
        """Publish only complete headroom-safe hops when a quality policy is active."""
        policy = getattr(self, '_quality_policy', None)
        if policy is None:
            return self._acquire_psd(freq)
        freq = finite(freq, 'freq')
        profile = self._receiver_profile
        source = None
        if profile is not None:
            seed, source = profile.select_gain(freq)
            gain = self._gain_cache.get(freq, seed).copy()
        for attempt in range(policy.retries + 1):
            self._quality_chunks = IQEvidenceBuffer(self._target_samples)
            self._acquisition_stage = 'receiver_controls'
            self._last_context = self._last_integrity = None
            try:
                if profile is not None:
                    # Check identity/state before writing any empirical controls.
                    profile.validate_context(self.measurement_context())
                    self._requested_gain = gain.copy()
                    self._apply_gain()
                    if profile.anchors:
                        self._frontend.manual_dc = profile.correction(freq, gain)[0]
                result = self._acquire_psd(freq)
                if result[0] is not None:
                    result[0].result()  # Drain this hop before accepting or retrying.
                raw = self._quality_chunks.samples()
                if raw.size != self._target_samples:
                    raise RuntimeError('headroom evidence does not contain every requested raw sample')
                if not self._last_integrity or self._last_integrity['measurement_status'] != 'complete':
                    raise RuntimeError('headroom mode requires a complete acquisition')
                self._acquisition_stage = 'raw_headroom'
                metrics, safe = policy.evaluate(raw)
            except BaseException as error:
                raw = self._quality_chunks.samples()
                try:
                    observed_context = self.measurement_context()
                except BaseException as context_error:
                    observed_context = {'readback_error':str(context_error)}
                self._rejection_evidence.save(raw, dict(requested_center_hz=freq, attempt=attempt,
                    acquisition_stage=self._acquisition_stage, observed_context_at_failure=observed_context,
                    requested_gains=self._requested_gain,
                    reason=type(error).__name__ + ': ' + str(error), context=self._last_context,
                    integrity=self._last_integrity, profile_sha256=profile.sha256 if profile else None))
                self._quality_chunks = None
                raise
            self._quality_chunks = None
            if safe:
                self._last_integrity['headroom'] = dict(metrics, accepted=True, attempt=attempt,
                    maximum_limit=policy.maximum, p999_limit=policy.percentile,
                    evaluated_samples=int(raw.size), uncentered_components=True)
                if self._telemetry is not None:
                    self._last_integrity['rssi_iq_consistency']=self._telemetry.qualify_iq(self._last_context,self._last_integrity['rf_state']['after'],metrics)
                if profile is not None:
                    self._gain_cache[freq] = gain.copy()
                    self._last_integrity['receiver_profile'] = dict(sha256=profile.sha256,
                        gain_source=source, accepted_gains=gain.copy(),
                        dc_anchor_hz=profile.correction(float(self.device.freq), self._last_context['gains'])[1])
                return result
            # Unsafe raw data is durable BEFORE backoff, retry or exception.
            self._rejection_evidence.save(raw, dict(requested_center_hz=freq, attempt=attempt,
                reason='headroom_rejected', metrics=metrics, context=self._last_context,
                integrity=self._last_integrity, profile_sha256=profile.sha256 if profile else None))
            lower = profile.lower_gain(gain) if profile is not None and attempt < policy.retries else None
            if lower is None or self._cancelled():
                raise HeadroomRejected('headroom rejected; original IQ retained in ' + str(policy.directory))
            gain = lower
        raise RuntimeError('unreachable headroom retry state')

    def _acquire_psd(self, freq):
        """Acquire one center frequency using continuous Welch framing."""
        if self._psd is None or (not self.device.is_streaming and not self._reopen_stream):
            raise RuntimeError('Streaming is not initialized, you must run setup() first!')
        freq = finite(freq, 'freq')
        t_freq = time.monotonic()
        previous_center = finite(self.device.freq, 'previous device frequency')
        self._acquisition_stage = 'tuning'
        self._read_tail = numpy.empty(0, dtype=numpy.complex64)
        self._next_sample_time = None
        self._reset_packet_state()
        if self._reopen_stream:
            # A new stream avoids queued END_BURST packets and buffers captured
            # while UHD is retuning. Never reopen after a failed tuning call.
            if self.device.is_streaming:
                self.device.stop_stream()
            self._burst_ended = False
        if self._reopen_stream or self.device.freq != freq or (self._tune_args and not self._tune_args_applied):
            if self._reset_stream and not self._reopen_stream:
                status = self.device.device.deactivateStream(self.device.stream)
                if status:
                    raise RuntimeError('SDR stream deactivation failed: {}'.format(status))
            try:
                if self._tune_args:
                    if self._frontend.raw is None:
                        raise ValueError('device does not expose tuning arguments')
                    self._frontend.raw.setFrequency(self._frontend.direction, self._frontend.channel,
                                                    freq, self._tune_args)
                    self._tune_args_applied = True
                else:
                    self.device.freq = freq
                self._apply_gain()
                self._frontend.apply_corrections()
            finally:
                if self._reset_stream and not self._reopen_stream:
                    status = self.device.device.activateStream(self.device.stream)
                    if status:
                        raise RuntimeError('SDR stream activation failed: {}'.format(status))
        if self._telemetry is not None:
            self._telemetry.closed_slot()
            self._telemetry.await_warmup(cancelled=self._cancelled)
        self._active_tune_delay=self._tune_delay
        self._settling_decision={'status':'configured_default','delay_s':self._tune_delay}
        if self._settling_model is not None:
            from soapypower.telemetry_models import evaluate_settling, ModelContextMismatch
            step=digest(dict(context=self.measurement_context(),previous_center_hz=previous_center,center_hz=float(self.device.freq)))
            try:
                self._active_tune_delay=evaluate_settling(self._settling_model,step)
                self._settling_decision={'status':'validated_reference_model','delay_s':self._active_tune_delay,'step_context_sha256':step}
            except ModelContextMismatch as error:
                # An untrained RF/gain/clock step keeps the conservative configured policy.
                self._settling_decision.update(status='unmatched_model_fallback',reason=str(error))
        if self._reopen_stream:
            self.device.start_stream(buffer_size=self._base_buffer_size)
        if self._receiver_profile is not None:
            self._frontend.apply_corrections()
        # Read back the tuned state and apply profile corrections BEFORE the
        # final settle. Driver readbacks can take tens of milliseconds while
        # the RX stream is not being drained; doing them between the settle and
        # measurement lets the device ring overflow, and the first measurement
        # read then reports dropped IQ.
        if not math.isclose(finite(self.device.sample_rate, 'actual sample rate', positive=True),
                            self._psd._sample_rate, rel_tol=1e-12, abs_tol=1e-9):
            raise RuntimeError('SDR sample rate changed after DSP setup')
        actual_freq = finite(self.device.freq, 'actual device frequency')
        if not math.isclose(actual_freq, freq, rel_tol=0, abs_tol=self._psd.bin_width * 1e-6):
            logger.warning('Requested center %.9g Hz; device tuned %.9g Hz. Output uses actual tuning.',
                           freq, actual_freq)
        if self._receiver_profile is not None:
            profile = self._receiver_profile
            profile.validate_context(self.measurement_context())
            observed_gains = self.measurement_context()['gains']
            profile.check_gains(observed_gains, self._requested_gain)
            if profile.anchors:
                correction, _ = profile.correction(actual_freq, observed_gains)
                if correction != self._frontend.manual_dc:
                    self._frontend.manual_dc = correction
                    self._frontend.apply_corrections()
        context = self.measurement_context() if self._need_context else None
        if self._calibration is not None:
            self._calibration.validate_context(context)
        state = self._psd.set_center_freq(actual_freq)
        self._last_state = state
        self._last_context = context
        if getattr(self, '_coverage_request', None) is not None:
            lo, hi, previous = self._coverage_request
            ordered = state['freq_array'][state['freq_order']]
            half = self._psd._crop_bins_half
            retained = ordered[half:-half] if half else ordered
            actual_lo, actual_hi = float(retained[0]), float(retained[-1] + self._psd.bin_width)
            tolerance = self._psd.bin_width * 1e-6
            if (previous is None and actual_lo > lo + tolerance) or (previous is not None and actual_lo > previous + tolerance):
                raise ValueError('actual tuning leaves a frequency coverage gap')
            self._coverage_request = (lo, hi, max(previous or actual_hi, actual_hi))
        before_rf = rf_state.observe(self._frontend,self._telemetry) if self._observe_rf_state else None
        if self._telemetry is not None:self._telemetry.require_valid(before_rf)
        # Settling is the last operation before measurement: it drains the
        # device ring after all tuning, correction and readback work.
        self._acquisition_stage = 'settling'
        self._previous_read_end = None
        settled_samples = self._settle(time.monotonic())
        self._acquisition_stage = 'measurement'
        self._previous_read_end = None
        self._measurement_start_gap_ms = max(0.0, (time.monotonic() - self._settle_finished) * 1000.0)
        self._measurement_rx_gaps.clear()
        self._measurement_rx_gap_max_ms = 0.0
        self._measurement_read_latency_max_ms = 0.0
        start = stop = None
        self._acquired_samples = 0
        while (self._acquired_samples < self._target_samples and not self._cancelled() and
               (not self._burst_ended or self._read_tail.size)):
            length = min(len(self._buffer), self._target_samples - self._acquired_samples)
            if start is None:
                start = datetime.datetime.now(datetime.timezone.utc)
            quality_chunks_before = len(self._quality_chunks) if self._quality_chunks is not None else None
            count = self._read_chunk(self._buffer[:length])
            stop = datetime.datetime.now(datetime.timezone.utc)
            if not count:
                break
            self._acquired_samples += count
            if self._quality_chunks is not None and len(self._quality_chunks) == quality_chunks_before:
                # Custom reader overrides may not implement packet-level evidence.
                self._quality_chunks.append(self._buffer[:count])
            self._psd.update_contiguous_async(state, self._buffer[:count])
        acquired = self._acquired_samples
        if self._reopen_stream:
            # Do not fill the USB FIFO while RF readbacks, queued FFT work,
            # headroom statistics, evidence publication or output run. Each
            # next hop explicitly starts a fresh stream after tuning.
            self.device.stop_stream()
        status = ('complete' if acquired == self._target_samples else
                  'partial_cancelled' if _shutdown else 'partial_deadline' if self._cancelled() else
                  'partial_end_burst' if self._burst_ended else 'partial')
        clock = self._tracker.metadata()
        first_ns, last_ns = self._tracker.timestamp(self._time_first), self._tracker.timestamp(self._time_last)
        driver_first_ns, driver_last_ns = first_ns, last_ns
        software_clock = str(self._frontend.optional('getDriverKey') or '').lower() == 'rtlsdr'
        if software_clock:
            # SoapyRTLSDR timestamps count samples delivered to its host callback.
            # HAS_TIME alone cannot establish independent hardware continuity.
            clock.update(driver_reported_continuity=clock['continuity'],
                         continuity='unverifiable_software_clock',
                         timestamp_source='driver_sample_counter')
            first_ns = last_ns = None
        self._last_integrity = dict(clock, measurement_status=status, requested_samples=self._requested_samples,
                                    target_samples=self._target_samples, acquired_samples=acquired,
                                    completion_fraction=acquired / self._target_samples,
                                    acquisition_rounding=self._acquisition_rounding,
                                    stream_reopened_per_hop=self._reopen_stream,
                                    stream_closed_after_acquisition=self._reopen_stream,
                                    end_burst_seen=self._burst_ended,
                                    terminal_packet_fully_processed=self._burst_ended and not self._read_tail.size,
                                    full_burst_received=None,
                                    full_burst_processed=(False if self._burst_ended and (self._read_tail.size or settled_samples) else None),
                                    burst_samples_unprocessed=int(self._read_tail.size) if self._burst_ended else 0,
                                    packet_tail_unprocessed=int(self._read_tail.size),
                                    hardware_first_sample_ns=first_ns, hardware_last_sample_ns=last_ns,
                                    driver_first_sample_ns=driver_first_ns, driver_last_sample_ns=driver_last_ns,
                                    hardware_time_is_utc=False, settled_discarded_samples=settled_samples,
                                    settle_policy={'min_time_s': self._tune_delay, 'min_samples': self._settle_samples,
                                                   'lock_sensor': self._lock_sensor, 'reference_lock_sensor': self._reference_lock_sensor,
                                                   'timeout_s': self._lock_timeout},
                                    time_basis={'host_read_bounds_utc': True, 'hardware_sample_time': first_ns is not None,
                                                'driver_sample_counter_time': software_clock and driver_first_ns is not None})
        if before_rf is not None:
            self._telemetry.hops+=1
            self._telemetry.closed_slot()
            after_rf = rf_state.observe(self._frontend,self._telemetry)
            self._last_integrity['rf_state'] = dict(before=before_rf,after=after_rf)
            rf_state.require_stable(before_rf,after_rf,self._psd.bin_width*1e-6)
            self._telemetry.require_valid(after_rf)
            self._last_integrity['lo_mapping']=rf_state.lo_mapping(after_rf,actual_freq,self.device.sample_rate,
                -1 if self._lnb_high_side else 1,self._lnb_lo,self._iq_conjugate)
            self._last_integrity['telemetry_statistics']=self._telemetry.statistics()
        rf_state.require_lock(self._frontend,self._lock_sensor,self._reference_lock_sensor)
        self._last_integrity['settling_decision']=dict(self._settling_decision)
        self._last_integrity['host_io']=dict(read_calls=self._host_reads,error_counts=dict(self._read_errors),
            read_latency_median_ms=float(numpy.median(self._read_latencies)) if self._read_latencies else None,
            read_latency_p95_ms=float(numpy.percentile(self._read_latencies,95)) if self._read_latencies else None,
            read_latency_max_ms=float(self._read_latency_max_ms),
            measurement_read_latency_max_ms=float(self._measurement_read_latency_max_ms),
            measurement_start_gap_ms=float(getattr(self, '_measurement_start_gap_ms', 0.0)),
            rx_scheduling_gap_max_ms=float(self._rx_gap_max_ms),
            rx_scheduling_gap_p95_ms=float(numpy.percentile(self._rx_gaps,95)) if self._rx_gaps else None,
            rx_scheduling_gap_p99_ms=float(numpy.percentile(self._rx_gaps,99)) if self._rx_gaps else None,
            measurement_rx_scheduling_gap_max_ms=float(self._measurement_rx_gap_max_ms),
            measurement_rx_scheduling_gap_p99_ms=(float(numpy.percentile(self._measurement_rx_gaps,99))
                                                  if self._measurement_rx_gaps else None),
            rx_scheduling_gap_last_ms=(None if self._rx_gap_last_ms is None else float(self._rx_gap_last_ms)),
            rx_scheduling_gap_count=int(self._rx_gap_count),
            gc_pause_max_ms=float(self._gc_pause_max_ms),
            gc_pause_last_ms=(None if self._gc_pause_last_ms is None else float(self._gc_pause_last_ms)),
            gc_pause_count=int(self._gc_pause_count),
            settle_overflow_count=int(self._settle_overflow_count),
            settle_burst_end_count=int(self._settle_burst_end_count),
            usb_packet_count_observed=False)
        if self._require_timestamps and clock['continuity'] != 'verified':
            raise RuntimeError('hardware timestamp continuity is not verified')
        if status != 'complete':
            if self._partial_policy == 'reject' or (self._partial_policy == 'emit' and self._output_format not in ('json', 'rtl_power_fftw', 'soapy_power_bin_v3')):
                raise RuntimeError('incomplete measurement: ' + status)
            logger.warning('Emitting explicitly partial measurement: %s (%d/%d samples)', status, acquired, self._target_samples)
        if self._acquired_samples < self._bins:
            # A cancelled partial hop has no full FFT and cannot yield a PSD.
            return None, start, stop
        if not math.isclose(finite(self.device.sample_rate, 'actual sample rate', positive=True),
                            self._psd._sample_rate, rel_tol=1e-12, abs_tol=1e-9):
            raise RuntimeError('SDR sample rate changed during acquisition')
        if not math.isclose(finite(self.device.freq, 'actual device frequency'),actual_freq,
                            rel_tol=0,abs_tol=self._psd.bin_width*1e-6):
            raise RuntimeError('SDR tuning changed during acquisition')
        if context is not None:
            final_context = self.measurement_context()
            if self._calibration is not None:
                self._calibration.validate_context(final_context)
            elif context['agc'] is False:
                # Raw JSON is also used as future calibration evidence.
                # Never label an internally changed fixed state as one context.
                _compare(context, final_context, 'fixed acquisition context')
        self._acquisition_stage = 'dsp_completion'
        future = self._psd.result_async(state)
        logger.debug('Acquired %d samples at %.9g Hz in %.3f s',
                     self._acquired_samples, actual_freq, time.monotonic() - t_freq)
        return future, start, stop

    def sweep(self, min_freq, max_freq, bins, repeats, runs=0, time_limit=0, overlap=0,
              fft_window='hann', fft_overlap=0.5, crop=False, log_scale=True, remove_dc=False, detrend=None, lnb_lo=0,
              tune_delay=0, reset_stream=False, base_buffer_size=0, max_buffer_size=0, max_threads=0, max_queue_size=0,
              precision='double', iq_conjugate=False, lnb_high_side=False,
              dc_mask_width=0, dc_offset=0, calibration=None, fft_threads=1, partial_policy=None,
              acquisition_rounding='exact', settle_samples=0, lock_sensor=None, lock_timeout=2.0,
              timestamp_tolerance_samples=0.25, require_timestamps=False,
              workspace_budget_bytes=64 * 1024**2, allow_estimated_dc=False, reference_lock_sensor=None,
              reopen_stream=False, deadline_policy='interrupt', quality_policy=None, receiver_profile=None,
              ensure_coverage=False, usable_halfwidth=None, observe_rf_state=False, telemetry_policy=None, settling_model=None):
        """Sweep spectrum using frequency hopping"""
        if deadline_policy not in ('interrupt', 'finish-hop'):
            raise ValueError('deadline policy must be interrupt or finish-hop')
        runs = integer(runs, 'runs')
        time_limit = finite(time_limit or 0, 'time_limit', nonnegative=True)
        overlap = self.nearest_overlap(overlap, bins)
        lnb_lo = finite(lnb_lo, 'lnb_lo')
        direction = -1 if lnb_high_side else 1
        rf_centers = self.freq_plan(min_freq, max_freq, bins, overlap, quiet=True,
                                    frequency_sign=direction)
        coverage_halfwidth = None
        if ensure_coverage:
            df = self.device.sample_rate / bins
            retained_halfwidth = (bins-crop_bins(bins,overlap if crop else 0))*df/2
            coverage_halfwidth = retained_halfwidth if usable_halfwidth is None else finite(usable_halfwidth,'usable halfwidth',positive=True)
            if coverage_halfwidth > retained_halfwidth:
                raise ValueError('usable halfwidth exceeds the retained FFT region')
            if abs(dc_offset)+dc_mask_width/2 >= self.device.sample_rate/2:
                raise ValueError('coverage planner requires a non-wrapping DC exclusion')
            tuner = self.device.get_frequency_range() if hasattr(self.device,'get_frequency_range') else None
            rf_range = sorted(lnb_lo+direction*x for x in tuner) if tuner is not None else None
            rf_centers = masked_frequency_plan(min_freq,max_freq,coverage_halfwidth,df,
                dc_mask_width, direction*(-dc_offset if iq_conjugate else dc_offset),rf_range)
        elif usable_halfwidth is not None:
            raise ValueError('usable halfwidth requires ensure_coverage')
        freq_list = [direction * (center - lnb_lo) for center in rf_centers]
        if hasattr(self.device, 'get_frequency_range'):
            low, high = self.device.get_frequency_range()
            if any(center < low or center > high for center in freq_list):
                raise ValueError('planned center frequency lies outside the device tuning range')
        t_start = time.monotonic()
        self._deadline = t_start + time_limit if time_limit else None
        try:
            self.setup(
                bins, repeats, base_buffer_size, max_buffer_size,
                fft_window=fft_window, fft_overlap=fft_overlap, crop_factor=overlap if crop else 0,
                log_scale=log_scale, remove_dc=remove_dc, detrend=detrend, lnb_lo=lnb_lo,
                tune_delay=tune_delay, reset_stream=reset_stream, reopen_stream=reopen_stream,
                max_threads=max_threads, max_queue_size=max_queue_size,
                precision=precision, iq_conjugate=iq_conjugate, lnb_high_side=lnb_high_side,
                dc_mask_width=dc_mask_width, dc_offset=dc_offset, calibration=calibration, fft_threads=fft_threads,
                partial_policy=partial_policy, acquisition_rounding=acquisition_rounding,
                settle_samples=settle_samples, lock_sensor=lock_sensor, lock_timeout=lock_timeout,
                reference_lock_sensor=reference_lock_sensor,
                timestamp_tolerance_samples=timestamp_tolerance_samples, require_timestamps=require_timestamps,
                workspace_budget_bytes=workspace_budget_bytes, allow_estimated_dc=allow_estimated_dc,
                quality_policy=quality_policy, receiver_profile=receiver_profile, observe_rf_state=observe_rf_state,
                telemetry_policy=telemetry_policy, settling_model=settling_model
            )
            if ensure_coverage:
                logger.info('Mask-aware plan: %d centers, usable halfwidth %.9g Hz',len(rf_centers),coverage_halfwidth)
            else:
                self.freq_plan(min_freq, max_freq, bins, overlap, frequency_sign=direction)
            run = 0
            write_next_future = None
            while not self._cancelled() and not self._burst_ended and (runs == 0 or run < runs):
                run += 1
                t_run_start = time.monotonic()
                logger.debug('Run: {}'.format(run))

                self._coverage_request = (min_freq, max_freq, None) if max_freq > min_freq else None
                accepted_centers = []
                for freq in freq_list:
                    if self._cancelled():
                        break
                    # Tune to new frequency, acquire samples and compute Power Spectral Density
                    deadline = self._deadline
                    if deadline_policy == 'finish-hop':
                        self._deadline = None
                    try:
                        psd_future, acq_time_start, acq_time_stop = self.psd(freq)
                    finally:
                        self._deadline = deadline

                    # Write PSD to stdout (in another thread)
                    if psd_future is not None:
                        if ensure_coverage:
                            accepted_centers.append(float(self._last_state['rf_origin_hz']))
                            self._last_integrity['coverage_plan'] = dict(mask_aware=True,
                                planned_centers=len(freq_list),usable_halfwidth_hz=coverage_halfwidth,
                                positive_weight_edge_margin_hz=self._psd.bin_width)
                        def metadata(handler=self._psd, state=self._last_state,
                                     context=self._last_context, integrity=dict(self._last_integrity)):
                            result = handler.measurement_metadata(state)
                            result.update(integrity)
                            result['deadline_policy'] = deadline_policy
                            count = result['segment_count']
                            result['processed_samples'] = ((count - 1) * (result['fft_bins'] - result['overlap_samples']) + result['fft_bins']) if count else 0
                            result['unprocessed_fft_tail_samples'] = result['acquired_samples'] - result['processed_samples']
                            if context is not None:
                                result['context'] = context
                            return result
                        self._writer.write_async(psd_future, acq_time_start, acq_time_stop,
                                                 self._acquired_samples, metadata=metadata)

                    if self._cancelled() or self._burst_ended:
                        break

                if (self._coverage_request is not None and not self._cancelled() and not self._burst_ended
                        and self._coverage_request[2] < max_freq - self._psd.bin_width * 1e-6):
                    raise ValueError('actual tuning does not cover the requested upper frequency')
                if ensure_coverage and not self._cancelled() and not self._burst_ended:
                    gaps = coverage_gaps(accepted_centers,min_freq,max_freq,coverage_halfwidth,
                        dc_mask_width,direction*(-dc_offset if iq_conjugate else dc_offset),self._psd.bin_width)
                    if gaps:
                        raise ValueError('actual tuning leaves masked/zero-weight frequency support gaps')
                # Write end of measurement marker (in another thread)
                write_next_future = self._writer.write_next_async()
                t_run = time.monotonic()
                logger.debug('  Total run time: {:.3f} s'.format(t_run - t_run_start))

                # End measurement if time limit is exceeded
                if time_limit and (time.monotonic() - t_start) >= time_limit:
                    logger.info('Time limit of {} s exceeded, completed {} runs'.format(time_limit, run))
                    break

            # Wait for last write to be finished
            if write_next_future is not None:
                write_next_future.result()

            # Debug thread pool queues
            logging.debug('Number of USB buffer overflow errors: {}'.format(self.device.buffer_overflow_count))
            logging.debug('PSD worker threads: {}'.format(self._psd._executor._max_workers))
            logging.debug('Max. PSD queue size: {} / {}'.format(self._psd._executor.max_queue_size_reached,
                                                                self._psd._executor.max_queue_size))
            logging.debug('Writer worker threads: {}'.format(self._writer._executor._max_workers))
            logging.debug('Max. Writer queue size: {} / {}'.format(self._writer._executor.max_queue_size_reached,
                                                                   self._writer._executor.max_queue_size))
        finally:
            # Shutdown SDR
            self._deadline = None
            self.stop(cancel=sys.exc_info()[0] is not None)
            t_stop = time.monotonic()
            logger.info('Total time: {:.3f} s'.format(t_stop - t_start))
