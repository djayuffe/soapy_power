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
                 iq_conjugate=False, frequency_sign=1):
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
        self._fft_overlap_bins = math.floor(self._bins * self._fft_overlap)
        self._step = self._bins - self._fft_overlap_bins
        self._log_scale = log_scale
        self._remove_dc = remove_dc
        self._detrend = detrend
        self._lnb_lo = finite(lnb_lo, 'lnb_lo')
        if frequency_sign not in (-1, 1) or isinstance(frequency_sign, bool):
            raise ValueError('frequency_sign must be +1 or -1')
        self._frequency_sign = frequency_sign
        self._iq_conjugate = iq_conjugate
        self._base_freq_array = numpy.fft.fftfreq(self._bins, 1 / self._sample_rate)
        self._freq_order = numpy.argsort(self._frequency_sign * self._base_freq_array)
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
            power = psd_state['pwr_array'].copy() / psd_state['repeats']
        if self._remove_dc:
            power[0] = (power[1] + power[-1]) / 2
        freqs = psd_state['freq_array'][psd_state['freq_order']]
        power = power[psd_state['freq_order']]
        half = self._crop_bins_half
        if half:
            freqs, power = freqs[half:-half], power[half:-half]
        if self._log_scale:
            with numpy.errstate(divide='ignore'):
                power = 10 * numpy.log10(power)
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
        for start in range(0, segments, batch_size):
            batch = views[start:start + batch_size]
            if self._detrend:
                batch = batch - batch.mean(axis=-1, keepdims=True)
            batch = batch * window
            if self._iq_conjugate:
                numpy.conjugate(batch, out=batch)
            transformed = simplespectral.fft(batch, n=self._bins, axis=-1)
            # Square in float64 even with single-precision FFTs: avoid float32
            # power overflow/underflow and preserve weak spectral components.
            power = numpy.square(transformed.real, dtype=numpy.float64)
            power += numpy.square(transformed.imag, dtype=numpy.float64)
            total += power.sum(axis=0, dtype=numpy.float64)
        total /= self._window_scale
        if not numpy.isfinite(total).all():
            raise ValueError('PSD exceeds float64 range; rescale input IQ')
        return segments, total

    def _accumulate(self, state, samples):
        segments, total = self._compute(samples)
        with state['update_lock']:
            state['repeats'] += segments
            if state['pwr_array'] is None:
                state['pwr_array'] = total
            else:
                # Compensated summation limits loss when averaging long scans.
                corrected = total - state['compensation']
                updated = state['pwr_array'] + corrected
                state['compensation'] = (updated - state['pwr_array']) - corrected
                state['pwr_array'] = updated

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
