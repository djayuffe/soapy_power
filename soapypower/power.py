#!/usr/bin/env python3

import sys, time, datetime, math, logging, signal

import numpy
import simplesoapy
from simplespectral import zeros

from soapypower import psd, writer
from soapypower.frontend import Frontend
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
    def __init__(self, soapy_args='', sample_rate=2.00e6, bandwidth=0, corr=0, gain=20.7,
                 auto_gain=False, channel=0, antenna='', settings=None,
                 force_sample_rate=False, force_bandwidth=False,
                 output=sys.stdout, output_format='rtl_power', dc_correction='default',
                 iq_correction='default', tune_args=None, manual_dc=None, manual_iq=None,
                 manual_tolerance=None, clock_source=None, time_source=None, master_clock_rate=None):
        finite(sample_rate, 'sample_rate', positive=True)
        finite(bandwidth, 'bandwidth', nonnegative=True)
        integer(channel, 'channel')
        if output_format not in writer.formats:
            raise ValueError('unknown output format')
        self.device = simplesoapy.SoapyDevice(
            soapy_args=soapy_args, sample_rate=sample_rate, bandwidth=bandwidth, corr=0,
            gain=None, auto_gain=False, channel=channel, antenna=antenna, settings=settings,
            force_sample_rate=force_sample_rate, force_bandwidth=force_bandwidth
        )

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
                    self.device.set_gain(element, finite(value, 'gain'))
            elif self._requested_gain is not None:
                self.device.gain = finite(self._requested_gain, 'gain')

    def measurement_context(self):
        return self._frontend.context(self._device_settings, self._tune_args,
                                      self._lnb_lo, self._lnb_high_side, self._iq_conjugate)

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
              reference_lock_sensor=None):
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
        self._need_context = calibration is not None or self._output_format in ('json', 'rtl_power_fftw', 'soapy_power_bin_v3')
        if self._frontend.manual_dc is not None or self._frontend.manual_iq is not None:
            self._frontend.apply_corrections()
        if calibration is not None:
            calibration.validate_context(self.measurement_context(), before_tune=True)
        self._burst_ended = False
        self._coverage_request = None
        try:
            base_buffer = self.device.start_stream(buffer_size=base_buffer_size)
            block_size = integer(len(base_buffer), 'device block size', 1)
            self._bins = bins
            self._repeats = repeats
            self._requested_samples = bins * repeats
            self._target_samples = (self._requested_samples if acquisition_rounding == 'exact' else
                                    ((self._requested_samples + block_size - 1) // block_size) * block_size)
            self._base_buffer_size = len(base_buffer)
            self._max_buffer_size = max_buffer_size
            self._buffer_repeats, self._buffer = self.create_buffer(
                bins, repeats, self._base_buffer_size, self._max_buffer_size
            )
            self._tune_delay = tune_delay
            self._reset_stream = reset_stream
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
        except BaseException:
            self.stop()
            raise

    def stop(self, cancel=False):
        """Stop streaming samples from device and delete samples buffer"""
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

    def _cancelled(self):
        return _shutdown or (self._deadline is not None and time.monotonic() >= self._deadline)

    def _read_chunk(self, output):
        """Read contiguous IQ; expose partial packets, overflow and time gaps.

        SimpleSoapy's bulk reader hides overflows. Read its public packet API
        directly, preserving unused samples when a short packet ends a chunk.
        """
        constants = simplesoapy.SoapySDR
        if not hasattr(self, '_tracker'):
            self._reset_packet_state()
        position = 0
        if self._read_tail.size:
            count = min(len(output), len(self._read_tail))
            self._record_times(self._read_tail_time, count)
            self._read_tail_time = shifted(self._read_tail_time, count)
            output[:count] = self._read_tail[:count]
            self._read_tail = self._read_tail[count:]
            position = count
        while position < len(output) and not self._cancelled() and not self._burst_ended:
            result = self.device.read_stream()
            count = result.ret
            if count == constants.SOAPY_SDR_OVERFLOW:
                self.device.buffer_overflow_count += 1
                raise RuntimeError('SDR overflow: samples were dropped; refusing discontinuous IQ')
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
        discarded = 0
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
            if discarded >= self._settle_samples and now >= tuned_at + self._tune_delay and locked:
                break
            if self._cancelled():
                return discarded
            if now - tuned_at >= max(self._tune_delay, self._lock_timeout):
                raise RuntimeError('tuning settle timeout')
            count = self._read_chunk(self._buffer)
            discarded += count
            if self._burst_ended:
                raise RuntimeError('burst ended during settling')
        discarded += int(self._read_tail.size)
        # Settle samples and their clock anchors never form part of a PSD.
        self._read_tail = numpy.empty(0, dtype=numpy.complex64)
        self._reset_packet_state()
        return discarded

    def psd(self, freq):
        """Acquire one center frequency using continuous Welch framing."""
        if not self.device.is_streaming:
            raise RuntimeError('Streaming is not initialized, you must run setup() first!')
        freq = finite(freq, 'freq')
        t_freq = time.monotonic()
        self._read_tail = numpy.empty(0, dtype=numpy.complex64)
        self._next_sample_time = None
        self._reset_packet_state()
        tuned_at = time.monotonic()
        if self.device.freq != freq or (self._tune_args and not self._tune_args_applied):
            if self._reset_stream:
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
                if self._reset_stream:
                    status = self.device.device.activateStream(self.device.stream)
                    if status:
                        raise RuntimeError('SDR stream activation failed: {}'.format(status))
        settled_samples = self._settle(tuned_at)
        actual_freq = finite(self.device.freq, 'actual device frequency')
        if not math.isclose(actual_freq, freq, rel_tol=0, abs_tol=self._psd.bin_width * 1e-6):
            logger.warning('Requested center %.9g Hz; device tuned %.9g Hz. Output uses actual tuning.',
                           freq, actual_freq)
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
        start = stop = None
        self._acquired_samples = 0
        while (self._acquired_samples < self._target_samples and not self._cancelled() and
               (not self._burst_ended or self._read_tail.size)):
            length = min(len(self._buffer), self._target_samples - self._acquired_samples)
            if start is None:
                start = datetime.datetime.now(datetime.timezone.utc)
            count = self._read_chunk(self._buffer[:length])
            stop = datetime.datetime.now(datetime.timezone.utc)
            if not count:
                break
            self._acquired_samples += count
            self._psd.update_contiguous_async(state, self._buffer[:count].copy())
        acquired = self._acquired_samples
        status = ('complete' if acquired == self._target_samples else
                  'partial_cancelled' if _shutdown else 'partial_deadline' if self._cancelled() else
                  'partial_end_burst' if self._burst_ended else 'partial')
        clock = self._tracker.metadata()
        first_ns, last_ns = self._tracker.timestamp(self._time_first), self._tracker.timestamp(self._time_last)
        self._last_integrity = dict(clock, measurement_status=status, requested_samples=self._requested_samples,
                                    target_samples=self._target_samples, acquired_samples=acquired,
                                    completion_fraction=acquired / self._target_samples,
                                    acquisition_rounding=self._acquisition_rounding,
                                    end_burst_seen=self._burst_ended,
                                    terminal_packet_fully_processed=self._burst_ended and not self._read_tail.size,
                                    full_burst_received=None,
                                    full_burst_processed=(False if self._burst_ended and (self._read_tail.size or settled_samples) else None),
                                    burst_samples_unprocessed=int(self._read_tail.size) if self._burst_ended else 0,
                                    packet_tail_unprocessed=int(self._read_tail.size),
                                    hardware_first_sample_ns=first_ns, hardware_last_sample_ns=last_ns,
                                    hardware_time_is_utc=False, settled_discarded_samples=settled_samples,
                                    settle_policy={'min_time_s': self._tune_delay, 'min_samples': self._settle_samples,
                                                   'lock_sensor': self._lock_sensor, 'reference_lock_sensor': self._reference_lock_sensor,
                                                   'timeout_s': self._lock_timeout},
                                    time_basis={'host_read_bounds_utc': True, 'hardware_sample_time': first_ns is not None})
        if self._require_timestamps and clock['continuity'] != 'verified':
            raise RuntimeError('hardware timestamp continuity is not verified')
        if status != 'complete':
            if self._partial_policy == 'reject' or (self._partial_policy == 'emit' and self._output_format not in ('json', 'rtl_power_fftw', 'soapy_power_bin_v3')):
                raise RuntimeError('incomplete measurement: ' + status)
            logger.warning('Emitting explicitly partial measurement: %s (%d/%d samples)', status, acquired, self._target_samples)
        if self._acquired_samples < self._bins:
            # A cancelled partial hop has no full FFT and cannot yield a PSD.
            return None, start, stop
        if self._calibration is not None:
            # A mode/gain/rate change during acquisition invalidates this hop.
            self._calibration.validate_context(self.measurement_context())
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
              workspace_budget_bytes=64 * 1024**2, allow_estimated_dc=False, reference_lock_sensor=None):
        """Sweep spectrum using frequency hopping"""
        runs = integer(runs, 'runs')
        time_limit = finite(time_limit or 0, 'time_limit', nonnegative=True)
        overlap = self.nearest_overlap(overlap, bins)
        lnb_lo = finite(lnb_lo, 'lnb_lo')
        direction = -1 if lnb_high_side else 1
        rf_centers = self.freq_plan(min_freq, max_freq, bins, overlap, quiet=True,
                                    frequency_sign=direction)
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
                tune_delay=tune_delay, reset_stream=reset_stream,
                max_threads=max_threads, max_queue_size=max_queue_size,
                precision=precision, iq_conjugate=iq_conjugate, lnb_high_side=lnb_high_side,
                dc_mask_width=dc_mask_width, dc_offset=dc_offset, calibration=calibration, fft_threads=fft_threads,
                partial_policy=partial_policy, acquisition_rounding=acquisition_rounding,
                settle_samples=settle_samples, lock_sensor=lock_sensor, lock_timeout=lock_timeout,
                reference_lock_sensor=reference_lock_sensor,
                timestamp_tolerance_samples=timestamp_tolerance_samples, require_timestamps=require_timestamps,
                workspace_budget_bytes=workspace_budget_bytes, allow_estimated_dc=allow_estimated_dc
            )
            self.freq_plan(min_freq, max_freq, bins, overlap, frequency_sign=direction)
            run = 0
            write_next_future = None
            while not self._cancelled() and not self._burst_ended and (runs == 0 or run < runs):
                run += 1
                t_run_start = time.monotonic()
                logger.debug('Run: {}'.format(run))

                self._coverage_request = (min_freq, max_freq, None) if max_freq > min_freq else None
                for freq in freq_list:
                    if self._cancelled():
                        break
                    # Tune to new frequency, acquire samples and compute Power Spectral Density
                    psd_future, acq_time_start, acq_time_stop = self.psd(freq)

                    # Write PSD to stdout (in another thread)
                    if psd_future is not None:
                        def metadata(handler=self._psd, state=self._last_state,
                                     context=self._last_context, integrity=dict(self._last_integrity)):
                            result = handler.measurement_metadata(state)
                            result.update(integrity)
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
