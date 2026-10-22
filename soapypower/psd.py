#!/usr/bin/env python3

import math, logging, threading, concurrent.futures

import numpy
import simplespectral

from soapypower import threadpool

logger = logging.getLogger(__name__)


def periodic_window(spec, bins):
    """DFT-periodic windows, including odd lengths and Tukey endpoints."""
    name, params = (spec[0], spec[1:]) if isinstance(spec, tuple) else (spec, ())
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
    """Compute averaged power spectral density using Welch's method"""
    def __init__(self, bins, sample_rate, fft_window='hann', fft_overlap=0.5,
                 crop_factor=0, log_scale=True, remove_dc=False, detrend=None,
                 lnb_lo=0, max_threads=0, max_queue_size=0):
        if not isinstance(bins, int) or bins < 2:
            raise ValueError('bins must be an integer >= 2')
        if not math.isfinite(sample_rate) or sample_rate <= 0:
            raise ValueError('sample_rate must be finite and positive')
        if not 0 <= fft_overlap < 1 or not 0 <= crop_factor < 1:
            raise ValueError('overlap and crop must be in [0, 1)')
        if bins - 2 * round(crop_factor * bins / 2) < 2:
            raise ValueError('crop must retain at least two bins')
        self._bins = bins
        self._sample_rate = sample_rate
        self._fft_window = fft_window
        self._window = periodic_window(fft_window, bins)
        self._window_scale = sample_rate * numpy.dot(self._window, self._window)
        if detrend not in (None, False, 'constant'):
            raise ValueError('detrend must be None or constant')
        self._fft_overlap = fft_overlap
        self._fft_overlap_bins = math.floor(self._bins * self._fft_overlap)
        self._crop_factor = crop_factor
        self._log_scale = log_scale
        self._remove_dc = remove_dc
        self._detrend = detrend
        self._lnb_lo = lnb_lo
        self._executor = threadpool.ThreadPoolExecutor(
            max_workers=max_threads,
            max_queue_size=max_queue_size,
            thread_name_prefix='PSD_thread'
        )
        self._base_freq_array = numpy.fft.fftfreq(self._bins, 1 / self._sample_rate)

    def set_center_freq(self, center_freq):
        """Set center frequency and clear averaged PSD data"""
        psd_state = {
            'repeats': 0,
            'freq_array': self._base_freq_array + self._lnb_lo + center_freq,
            'pwr_array': None,
            'update_lock': threading.Lock(),
            'futures': [],
        }
        return psd_state

    def result(self, psd_state):
        """Return freqs and averaged PSD for given center frequency"""
        if not psd_state['repeats']:
            raise ValueError('no spectra have been accumulated')
        freq_array = numpy.fft.fftshift(psd_state['freq_array'])
        pwr_array = numpy.fft.fftshift(psd_state['pwr_array'])

        crop_bins_half = round((self._crop_factor * self._bins) / 2)
        if crop_bins_half:
            freq_array = freq_array[crop_bins_half:-crop_bins_half]
            pwr_array = pwr_array[crop_bins_half:-crop_bins_half]

        if psd_state['repeats'] > 1:
            pwr_array = pwr_array / psd_state['repeats']

        if self._log_scale:
            with numpy.errstate(divide='ignore'):
                pwr_array = 10 * numpy.log10(pwr_array)

        return (freq_array, pwr_array)

    def wait_for_result(self, psd_state):
        """Wait for all PSD threads to finish and return result"""
        for future in psd_state['futures']:
            future.result()
        return self.result(psd_state)

    def result_async(self, psd_state):
        """Return freqs and averaged PSD for given center frequency (asynchronously in another thread)"""
        return self._executor.submit(self.wait_for_result, psd_state)

    def close(self):
        self._executor.shutdown(wait=True)

    def update(self, psd_state, samples_array):
        """Compute PSD from samples and update average for given center frequency"""
        samples_array = numpy.asarray(samples_array)
        if samples_array.ndim != 1 or samples_array.size < self._bins:
            raise ValueError('samples must be one-dimensional with at least bins samples')
        segments = 1 + (samples_array.size - self._bins) // (self._bins - self._fft_overlap_bins)
        # Process bounded batches rather than materializing every overlapping FFT.
        # About 8 MiB of windowed samples per worker; FFT backends add workspaces.
        dtype = numpy.result_type(samples_array.dtype, numpy.complex64)
        samples_array = numpy.asarray(samples_array, dtype=dtype)
        window = self._window.astype(samples_array.real.dtype)
        step = self._bins - self._fft_overlap_bins
        views = numpy.lib.stride_tricks.as_strided(
            samples_array, shape=(segments, self._bins),
            strides=(step * samples_array.strides[0], samples_array.strides[0]),
            writeable=False)
        batch_size = max(1, (8 * 1024**2) // (self._bins * samples_array.itemsize))
        pwr_array = numpy.zeros(self._bins, dtype=numpy.float64)
        for start in range(0, segments, batch_size):
            batch = views[start:start + batch_size]
            if self._detrend:
                batch = batch - batch.mean(axis=-1, keepdims=True)
            transformed = simplespectral.fft(batch * window, n=self._bins, axis=-1)
            power = numpy.abs(transformed)
            numpy.square(power, out=power)
            pwr_array += power.sum(axis=0, dtype=numpy.float64)
        pwr_array /= self._window_scale
        if self._remove_dc:
            pwr_array[0] = (pwr_array[1] + pwr_array[-1]) / 2

        # Sum linear periodograms in float64; normalize by segment count last.
        with psd_state['update_lock']:
            psd_state['repeats'] += segments
            if psd_state['pwr_array'] is None:
                psd_state['pwr_array'] = pwr_array
            else:
                psd_state['pwr_array'] += pwr_array

    def update_async(self, psd_state, samples_array):
        """Compute PSD from samples and update average for given center frequency (asynchronously in another thread)"""
        future = self._executor.submit(self.update, psd_state, samples_array)
        psd_state['futures'].append(future)
        return future
