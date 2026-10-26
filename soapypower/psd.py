#!/usr/bin/env python3

import math, os, logging, threading, concurrent.futures

import numpy
import simplespectral

from soapypower import threadpool
from soapypower.numerics import crop_bins, finite, integer

logger = logging.getLogger(__name__)


def periodic_window(spec, bins):
    """DFT-periodic windows, including odd lengths and Tukey endpoints."""
    if not isinstance(spec, (str, tuple)) or (isinstance(spec, tuple) and not spec):
        raise ValueError('window must be a name or nonempty parameter tuple')
    name, params = (spec[0], spec[1:]) if isinstance(spec, tuple) else (spec, ())
    if not isinstance(name, str):
        raise ValueError('window name must be a string')
    functions = {'boxcar': numpy.ones, 'hann': numpy.hanning,
                 'hamming': numpy.hamming, 'blackman': numpy.blackman,
                 'bartlett': numpy.bartlett, 'kaiser': numpy.kaiser}
    if name == 'tukey':
        if len(params) != 1 or not 0 <= params[0] <= 1:
            raise ValueError('Tukey alpha must be in [0, 1]')
        alpha = params[0]
        if alpha == 0:
            window = numpy.ones(bins)
        elif alpha == 1:
            window = numpy.hanning(bins + 1)[:-1]
        else:
            t = numpy.arange(bins, dtype=float) / bins
            window = numpy.ones(bins)
            left, right = t < alpha / 2, t > 1 - alpha / 2
            window[left] = 0.5 * (1 + numpy.cos(numpy.pi * (2 * t[left] / alpha - 1)))
            window[right] = 0.5 * (1 + numpy.cos(numpy.pi * (2 * t[right] / alpha - 2 / alpha + 1)))
    else:
        if name not in functions:
            raise ValueError('unknown FFT window: {}'.format(name))
        if name == 'kaiser' and (len(params) != 1 or not math.isfinite(params[0]) or params[0] < 0):
            raise ValueError('Kaiser beta must be finite and nonnegative')
        if name != 'kaiser' and params:
            raise ValueError('window does not accept parameters')
        window = functions[name](bins + 1, *params)[:-1]
    if not numpy.isfinite(window).all() or not numpy.any(window):
        raise ValueError('window must have finite, nonzero energy')
    return window


class PSD:
    """Two-sided Welch density for IQ, with optional continuous input framing.

    update/update_async average independent records. update_contiguous_async
    preserves segment alignment across reads of one uninterrupted stream.
    Finalize a state with result_async or wait_for_result before starting another
    measurement. Buffer ownership passes to asynchronous calls until completion.
    """
    def __init__(self, bins, sample_rate, fft_window='hann', fft_overlap=0.5,
                 crop_factor=0, log_scale=True, remove_dc=False, detrend=None,
                 lnb_lo=0, max_threads=0, max_queue_size=0, precision='double',
                 iq_conjugate=False, frequency_sign=1, dc_mask_width=0, dc_offset=0,
                 calibration=None, fft_threads=1):
        self._bins = integer(bins, 'bins', 2)
        self._sample_rate = finite(sample_rate, 'sample_rate', positive=True)
        self._fft_overlap = finite(fft_overlap, 'fft_overlap', nonnegative=True)
        if self._fft_overlap >= 1:
            raise ValueError('FFT overlap must be in [0, 1)')
        self._crop_bins_half = crop_bins(bins, crop_factor) // 2
        self._fft_window = fft_window
        self._window = periodic_window(fft_window, self._bins)
        self._window_energy = numpy.dot(self._window, self._window)
        self._window_scale = self._sample_rate * self._window_energy
        if not math.isfinite(self._window_scale) or self._window_scale <= 0:
            raise ValueError('window normalization is outside float64 range')
        if detrend not in (None, False, 'constant'):
            raise ValueError('detrend must be None or constant')
        if precision not in ('single', 'double'):
            raise ValueError('precision must be single or double')
        self._dtype = numpy.dtype(numpy.complex128 if precision == 'double' else numpy.complex64)
        if precision == 'single':
            # Normalize against the actual rounded coefficients used by the FFT.
            self._window = self._window.astype(numpy.float32).astype(numpy.float64)
            self._window_energy = numpy.dot(self._window, self._window)
            self._window_scale = self._sample_rate * self._window_energy
        self._fft_overlap_bins = math.floor(self._bins * self._fft_overlap)
        self._step = self._bins - self._fft_overlap_bins
        self._log_scale = log_scale
        if remove_dc and ((fft_window[0] if isinstance(fft_window, tuple) else fft_window) != 'boxcar' or self._bins < 3):
            raise ValueError('DC-bin interpolation requires boxcar and at least 3 bins; use DC masking otherwise')
        self._remove_dc = remove_dc
        self._dc_mask_width = finite(dc_mask_width, 'dc_mask_width', nonnegative=True)
        self._dc_offset = finite(dc_offset, 'dc_offset')
        if self._dc_mask_width >= self._sample_rate or abs(self._dc_offset) > self._sample_rate / 2:
            raise ValueError('DC exclusion must lie in the sampled baseband and be narrower than the sample rate')
        if self._dc_offset and not self._dc_mask_width:
            raise ValueError('dc_offset requires a positive DC exclusion width')
        if remove_dc and self._dc_mask_width:
            raise ValueError('choose either DC interpolation or exclusion, not both')
        self._fft_threads = integer(fft_threads, 'fft_threads', 1)
        self._calibration = calibration
        self._detrend = detrend
        self._lnb_lo = finite(lnb_lo, 'lnb_lo')
        frequency_sign = integer(frequency_sign, 'frequency_sign', -1)
        if frequency_sign not in (-1, 1):
            raise ValueError('frequency_sign must be +1 or -1')
        self._frequency_sign = frequency_sign
        self._iq_conjugate = iq_conjugate
        self._base_freq_array = numpy.fft.fftfreq(self._bins, 1 / self._sample_rate)
        self._freq_order = numpy.argsort(self._frequency_sign * self._base_freq_array)
        self._dc_mask = numpy.zeros(self._bins, dtype=bool)
        if self._dc_mask_width:
            offset = -self._dc_offset if self._iq_conjugate else self._dc_offset
            distance = abs((self._base_freq_array - offset + self._sample_rate / 2) %
                           self._sample_rate - self._sample_rate / 2)
            self._dc_mask = distance <= self._dc_mask_width / 2
            if not self._dc_mask.any():
                raise ValueError('DC exclusion contains no FFT bin centers; increase its width')
            kept = self._dc_mask[self._freq_order]
            half = self._crop_bins_half
            if numpy.all(kept[half:-half] if half else kept):
                raise ValueError('DC exclusion would remove all retained bins')
        workers = integer(max_threads, 'max_threads') or min(4, os.cpu_count() or 1)
        queue_size = integer(max_queue_size, 'max_queue_size', -1) or workers
        self._executor = threadpool.ThreadPoolExecutor(
            max_workers=workers, max_queue_size=queue_size, thread_name_prefix='PSD_thread')

    @property
    def bin_width(self):
        return self._sample_rate / self._bins

    @property
    def enbw(self):
        """Equivalent noise bandwidth in Hz, distinct from FFT bin spacing."""
        coherent_sum = self._window.sum()
        return self._sample_rate * self._window_energy / coherent_sum**2

    @property
    def coherent_gain(self):
        return self._window.mean()

    def measurement_metadata(self, state):
        order = state['freq_order']
        half = self._crop_bins_half
        mask = self._dc_mask[order]
        if half:
            mask = mask[half:-half]
        return {'fft_bins': self._bins, 'sample_rate_hz': self._sample_rate,
                'bin_width_hz': self.bin_width, 'window_enbw_hz': self.enbw,
                'window': self._fft_window, 'segment_count': state['repeats'],
                'overlap_samples': self._fft_overlap_bins, 'detrend': self._detrend,
                'dc_interpolated': bool(self._remove_dc),
                'dc_mask_width_hz': self._dc_mask_width, 'dc_offset_hz': self._dc_offset,
                'masked_bins': numpy.flatnonzero(mask).tolist(),
                'fft_threads': self._fft_threads,
                'precision': 'double' if self._dtype == numpy.dtype(numpy.complex128) else 'single',
                'iq_conjugate': bool(self._iq_conjugate), 'frequency_sign': self._frequency_sign,
                'calibration': self._calibration.metadata() if self._calibration is not None else None}

    def set_center_freq(self, center_freq):
        """Start a measurement; never carry overlap across retuning or gaps."""
        center_freq = finite(center_freq, 'center_freq')
        freqs = self._lnb_lo + self._frequency_sign * (self._base_freq_array + center_freq)
        order = self._freq_order
        shifted = freqs[order]
        if not numpy.isfinite(freqs).all() or not numpy.all(numpy.diff(shifted) > 0):
            raise ValueError('frequency bins cannot be represented distinctly in float64')
        return {
            'repeats': 0, 'freq_array': freqs, 'freq_order': order, 'pwr_array': None,
            'compensation': numpy.zeros(self._bins, dtype=numpy.float64),
            'update_lock': threading.Lock(), 'input_lock': threading.Lock(),
            'futures': set(), 'error': None, 'result_future': None,
            'completing': False, 'tail': numpy.empty(0, dtype=self._dtype),
            'input_mode': None,
        }

    def result(self, psd_state):
        """Snapshot a completed linear average, then apply display operations."""
        with psd_state['update_lock']:
            if psd_state['futures']:
                raise RuntimeError('PSD updates are still running; use wait_for_result')
            if psd_state['error'] is not None:
                raise psd_state['error']
            if not psd_state['repeats']:
                raise ValueError('no complete FFT segments have been accumulated')
            power = psd_state['pwr_array'].copy()
        if self._remove_dc:
            power[0] = power[1] / 2 + power[-1] / 2
        freqs = psd_state['freq_array'][psd_state['freq_order']]
        power = power[psd_state['freq_order']]
        half = self._crop_bins_half
        if half:
            freqs, power = freqs[half:-half], power[half:-half]
        mask = self._dc_mask[psd_state['freq_order']]
        if half:
            mask = mask[half:-half]
        if self._calibration is not None:
            baseband = self._base_freq_array.copy()
            if self._iq_conjugate:
                baseband = (-baseband + self._sample_rate / 2) % self._sample_rate - self._sample_rate / 2
            baseband = baseband[psd_state['freq_order']]
            if half:
                baseband = baseband[half:-half]
            valid = ~mask
            corrected = numpy.full_like(power, numpy.nan)
            corrected[valid] = self._calibration.apply(freqs[valid], power[valid], self._log_scale,
                                                       baseband_freqs=baseband[valid])
            power = corrected
        elif self._log_scale:
            with numpy.errstate(divide='ignore'):
                power = 10 * numpy.log10(power)
        power[mask] = numpy.nan
        return freqs, power

    def _complete_result(self, state):
        with state['update_lock']:
            future = state['result_future']
            if future is None or state['futures'] or state['completing']:
                return
            state['completing'] = True
        if future.set_running_or_notify_cancel():
            try:
                result = self.result(state)
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(result)

    def _update_done(self, state, future):
        error = (concurrent.futures.CancelledError() if future.cancelled() else future.exception())
        with state['update_lock']:
            state['futures'].discard(future)
            if error is not None and state['error'] is None:
                state['error'] = error
        self._complete_result(state)

    def wait_for_result(self, psd_state):
        return self.result_async(psd_state).result()

    def result_async(self, psd_state):
        """Seal a measurement; completion uses callbacks, never a waiting worker."""
        with psd_state['input_lock']:
            with psd_state['update_lock']:
                if psd_state['result_future'] is None:
                    psd_state['result_future'] = concurrent.futures.Future()
                future = psd_state['result_future']
        self._complete_result(psd_state)
        return future

    def close(self):
        self._executor.shutdown(wait=True)

    def _samples(self, samples):
        samples = numpy.asarray(samples)
        if samples.ndim != 1 or samples.dtype.kind not in 'iufc':
            raise ValueError('samples must be a one-dimensional numeric IQ array')
        samples = numpy.asarray(samples, dtype=self._dtype)
        if not numpy.isfinite(samples).all():
            raise ValueError('IQ samples must be finite')
        return samples

    def _compute(self, samples):
        segments = 1 + (samples.size - self._bins) // self._step
        if segments < 1:
            raise ValueError('independent records must contain at least bins samples')
        window = self._window.astype(samples.real.dtype)
        views = numpy.lib.stride_tricks.as_strided(
            samples, shape=(segments, self._bins),
            strides=(self._step * samples.strides[0], samples.strides[0]), writeable=False)
        batch_size = max(1, (8 * 1024**2) // (self._bins * samples.itemsize))
        total = numpy.zeros(self._bins, dtype=numpy.float64)
        # Normalize before squaring/summing. This avoids intermediate overflow
        # when the final mean density is representable but raw FFT power is not.
        fft_scale = (1 / math.sqrt(self._window_scale)) / math.sqrt(segments)
        for start in range(0, segments, batch_size):
            batch = views[start:start + batch_size]
            if self._detrend:
                batch = batch - batch.mean(axis=-1, keepdims=True)
            batch = batch * window
            if self._iq_conjugate:
                numpy.conjugate(batch, out=batch)
            options = {}
            if getattr(simplespectral, 'fft_pyfftw', False) and simplespectral.use_pyfftw:
                options['threads'] = self._fft_threads
            transformed = simplespectral.fft(batch, n=self._bins, axis=-1, **options)
            # Float64 scaling also prevents single-precision power overflow.
            real = numpy.asarray(transformed.real, dtype=numpy.float64) * fft_scale
            imag = numpy.asarray(transformed.imag, dtype=numpy.float64) * fft_scale
            with numpy.errstate(over='ignore', invalid='ignore'):
                power = numpy.square(real) + numpy.square(imag)
                total += power.sum(axis=0, dtype=numpy.float64)
        if not numpy.isfinite(total).all():
            raise ValueError('PSD exceeds float64 range; rescale input IQ')
        return segments, total

    def _accumulate(self, state, samples):
        segments, total = self._compute(samples)
        with state['update_lock']:
            previous_count = state['repeats']
            total_count = previous_count + segments
            if state['pwr_array'] is None:
                updated = total
                compensation = state['compensation']
            else:
                # A weighted running mean avoids overflow of a long sum.
                # Carry the previous rounding residual with its remaining weight.
                weight = segments / total_count
                old = state['pwr_array']
                corrected = (total - old) * weight - state['compensation'] * (1 - weight)
                updated = old + corrected
                compensation = (updated - old) - corrected
            if not numpy.isfinite(updated).all():
                raise ValueError('averaged PSD exceeds float64 range')
            state['pwr_array'] = updated
            state['compensation'] = compensation
            state['repeats'] = total_count

    def _check_open(self, state, mode):
        if state['result_future'] is not None:
            raise RuntimeError('measurement is finalized')
        if state['error'] is not None:
            raise state['error']
        if state['input_mode'] not in (None, mode):
            raise ValueError('do not mix independent records and contiguous IQ in one state')
        state['input_mode'] = mode

    def update(self, psd_state, samples_array):
        """Average one independent record; no overlap is shared with other calls."""
        with psd_state['input_lock']:
            self._check_open(psd_state, 'independent')
            self._accumulate(psd_state, self._samples(samples_array))

    def _submit(self, state, samples):
        future = self._executor.submit(self._accumulate, state, samples)
        with state['update_lock']:
            state['futures'].add(future)
        future.add_done_callback(lambda done: self._update_done(state, done))
        return future

    def update_async(self, psd_state, samples_array):
        """Queue an independent record, with bounded backpressure."""
        with psd_state['input_lock']:
            self._check_open(psd_state, 'independent')
            return self._submit(psd_state, self._samples(samples_array))

    def update_contiguous_async(self, psd_state, samples_array):
        """Append uninterrupted IQ; retain fewer than bins samples for next read.

        Empty or short chunks are allowed. Incomplete final segments are dropped
        without padding, just as in Welch on the concatenated whole record.
        """
        with psd_state['input_lock']:
            self._check_open(psd_state, 'contiguous')
            samples = self._samples(samples_array)
            if psd_state['tail'].size:
                samples = numpy.concatenate((psd_state['tail'], samples))
            segments = max(0, 1 + (samples.size - self._bins) // self._step)
            consumed = segments * self._step
            psd_state['tail'] = samples[consumed:].copy()
            if not segments:
                future = concurrent.futures.Future()
                future.set_result(None)
                return future
            end = consumed + self._fft_overlap_bins
            return self._submit(psd_state, samples[:end])
