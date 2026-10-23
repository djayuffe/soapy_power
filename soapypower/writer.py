#!/usr/bin/env python3

import os, sys, logging, struct, collections, io, threading, math, concurrent.futures

import numpy

from soapypower import threadpool

if sys.platform == 'win32':
    import msvcrt

logger = logging.getLogger(__name__)


class BaseWriter:
    """Power Spectral Density writer base class"""
    def __init__(self, output=sys.stdout, log_scale=True):
        self._log_scale = log_scale
        self._close_output = False
        self._write_error = None
        self._error_lock = threading.Lock()
        self._closed = False
        self._wrapped_output = False

        if isinstance(output, (str, bytes, os.PathLike)):
            output = open(output, 'wb')
            self._close_output = True

        # If output is integer, assume it is file descriptor and open it
        if isinstance(output, int):
            self._close_output = True
            if sys.platform == 'win32':
                msvcrt.setmode(output, os.O_BINARY)
            output = open(output, 'wb', closefd=False)

        # Get underlying buffered file object
        try:
            self.output = output.buffer
        except AttributeError:
            self.output = output

        # Use only one writer thread to preserve sequence of written frequencies
        self._executor = threadpool.ThreadPoolExecutor(
            max_workers=1,
            max_queue_size=100,
            thread_name_prefix='Writer_thread'
        )

    def write(self, psd_data_or_future, time_start, time_stop, samples):
        """Write PSD of one frequency hop"""
        raise NotImplementedError

    def write_async(self, psd_data_or_future, time_start, time_stop, samples):
        """Write PSD of one frequncy hop (asynchronously in another thread)"""
        return self._submit(self.write, psd_data_or_future, time_start, time_stop, samples)

    def write_next(self):
        """Write marker for next run of measurement"""
        raise NotImplementedError

    def write_next_async(self):
        """Write marker for next run of measurement (asynchronously in another thread)"""
        return self._submit(self.write_next)

    def _record_error(self, future):
        error = concurrent.futures.CancelledError() if future.cancelled() else future.exception()
        if error is not None:
            with self._error_lock:
                if self._write_error is None:
                    self._write_error = error

    def _submit(self, fn, *args):
        if self._write_error is not None:
            raise self._write_error
        future = self._executor.submit(fn, *args)
        future.add_done_callback(self._record_error)
        return future

    def close(self):
        """Drain writes, release wrappers and propagate background failures."""
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=True)
        try:
            self.output.flush()
        finally:
            if self._close_output:
                self.output.close()
            elif self._wrapped_output:
                self.output.detach()
        if self._write_error is not None:
            raise self._write_error


def spectrum_data(data_or_future, log_scale):
    if hasattr(data_or_future, 'result'):
        data_or_future = data_or_future.result()
    freqs, power = (numpy.asarray(array) for array in data_or_future)
    if (freqs.ndim != 1 or power.ndim != 1 or len(freqs) != len(power) or
            len(freqs) < 2 or freqs.dtype.kind not in 'iuf' or power.dtype.kind not in 'iuf'):
        raise ValueError('spectrum must contain matching real frequency/power vectors with >= 2 bins')
    freqs = freqs.astype(numpy.float64, copy=False)
    differences = numpy.diff(freqs)
    tolerance = max(abs(numpy.spacing(numpy.abs(freqs).max())) * 8, abs(differences[0]) * 1e-10)
    if (not numpy.isfinite(freqs).all() or not numpy.all(differences > 0) or
            not numpy.allclose(differences, differences[0], rtol=1e-10, atol=tolerance)):
        raise ValueError('output frequency grid must be finite, increasing and uniform')
    if log_scale:
        valid = numpy.isfinite(power) | numpy.isneginf(power)
    else:
        valid = numpy.isfinite(power) & (power >= 0)
    if not valid.all():
        raise ValueError('invalid PSD values for selected linear/logarithmic scale')
    return freqs, power


class SoapyPowerBinFormat:
    """Power Spectral Density binary file format"""
    header_struct = struct.Struct('<BdddddQQ2x')
    header = collections.namedtuple('Header', 'version time_start time_stop start stop step samples size')
    magic = b'SDRFF'
    version = 2

    def __init__(self, max_payload_bytes=256 * 1024**2):
        self.max_payload_bytes = max_payload_bytes

    @staticmethod
    def _validate_geometry(time_start, time_stop, start, stop, step, size):
        if (not all(math.isfinite(x) for x in (time_start, time_stop, start, stop, step)) or
                time_stop < time_start or step <= 0 or stop <= start or size < 8 or size % 4):
            raise ValueError('invalid binary record geometry')
        expected_stop = start + (size // 4) * step
        tolerance = max(abs(numpy.spacing(max(abs(start), abs(stop)))) * 8, step * 1e-8)
        if not math.isclose(stop, expected_stop, rel_tol=0, abs_tol=tolerance):
            raise ValueError('binary record frequency span does not match its payload')

    def read(self, f):
        """Read data from file-like object"""
        magic = f.read(1)
        if not magic:
            return None
        magic += self._read_exact(f, len(self.magic) - 1)
        if magic != self.magic:
            raise ValueError('Magic bytes not found! Read data: {}'.format(magic))

        header_data = self._read_exact(f, self.header_struct.size)
        header = self.header._make(self.header_struct.unpack(header_data))
        if header.version != self.version:
            raise ValueError('unsupported binary format version')
        self._validate_geometry(header.time_start, header.time_stop, header.start,
                                header.stop, header.step, header.size)
        if self.max_payload_bytes is not None and header.size > self.max_payload_bytes:
            raise ValueError('binary payload exceeds configured size limit')
        pwr_array = numpy.frombuffer(self._read_exact(f, header.size), dtype='<f4').copy()
        return (header, pwr_array)

    @staticmethod
    def _read_exact(f, size):
        chunks = []
        while size:
            chunk = f.read(min(size, 1024 * 1024))
            if not chunk:
                raise ValueError('truncated binary record')
            chunks.append(chunk)
            size -= len(chunk)
        return b''.join(chunks)

    def write(self, f, time_start, time_stop, start, stop, step, samples, pwr_array):
        """Write data to file-like object"""
        original = numpy.asarray(pwr_array)
        if original.dtype.kind not in 'iuf':
            raise ValueError('power must be real numeric data')
        with numpy.errstate(over='ignore'):
            pwr_array = numpy.asarray(original, dtype='<f4')
        if numpy.any(numpy.isfinite(original) & ~numpy.isfinite(pwr_array)):
            raise ValueError('power exceeds float32 range of binary v2')
        if pwr_array.ndim != 1:
            raise ValueError('power array must be one-dimensional')
        self._validate_geometry(time_start, time_stop, start, stop, step, pwr_array.nbytes)
        header_bytes = self.header_struct.pack(
            self.version, time_start, time_stop, start, stop, step, samples, pwr_array.nbytes
        )
        f.write(self.magic)
        f.write(header_bytes)
        f.write(pwr_array.tobytes())
        f.flush()

    def header_size(self):
        """Return total size of header"""
        return len(self.magic) + self.header_struct.size


class SoapyPowerBinWriter(BaseWriter):
    """Write Power Spectral Density to stdout or file (in soapy_power binary format)"""
    def __init__(self, output=sys.stdout, log_scale=True):
        super().__init__(output=output, log_scale=log_scale)
        self.formatter = SoapyPowerBinFormat()

    def write(self, psd_data_or_future, time_start, time_stop, samples):
        """Write PSD of one frequency hop"""
        f_array, pwr_array = spectrum_data(psd_data_or_future, self._log_scale)

        step = (f_array[-1] - f_array[0]) / (len(f_array) - 1)
        self.formatter.write(
            self.output,
            time_start.timestamp(),
            time_stop.timestamp(),
            f_array[0],
            f_array[-1] + step,
            step,
            samples,
            pwr_array
        )

    def write_next(self):
        """Write marker for next run of measurement"""
        pass


class RtlPowerFftwWriter(BaseWriter):
    """Write Power Spectral Density to stdout or file (in rtl_power_fftw format)"""
    def __init__(self, output=sys.stdout, log_scale=True):
        super().__init__(output=output, log_scale=log_scale)
        if not isinstance(self.output, io.TextIOBase):
            self.output = io.TextIOWrapper(self.output, encoding='utf-8', newline='\n')
            self._wrapped_output = True

    def write(self, psd_data_or_future, time_start, time_stop, samples):
        """Write PSD of one frequency hop"""
        f_array, pwr_array = spectrum_data(psd_data_or_future, self._log_scale)

        self.output.write('# soapy_power output\n')
        self.output.write('# Acquisition start: {}\n'.format(time_start))
        self.output.write('# Acquisition end: {}\n'.format(time_stop))
        self.output.write('#\n')
        units = 'dB(input power/Hz)' if self._log_scale else 'input power/Hz'
        self.output.write('# frequency [Hz] power spectral density [{}]\n'.format(units))

        for f, pwr in zip(f_array, pwr_array):
            self.output.write('{} {}\n'.format(f, pwr))

        self.output.write('\n')
        self.output.flush()

    def write_next(self):
        """Write marker for next run of measurement"""
        self.output.write('\n')
        self.output.flush()


class RtlPowerWriter(BaseWriter):
    """Write Power Spectral Density to stdout or file (in rtl_power format)"""
    def __init__(self, output=sys.stdout, log_scale=True):
        super().__init__(output=output, log_scale=log_scale)
        if not isinstance(self.output, io.TextIOBase):
            self.output = io.TextIOWrapper(self.output, encoding='utf-8', newline='\n')
            self._wrapped_output = True

    def write(self, psd_data_or_future, time_start, time_stop, samples):
        """Write PSD of one frequency hop"""
        f_array, pwr_array = spectrum_data(psd_data_or_future, self._log_scale)

        step = (f_array[-1] - f_array[0]) / (len(f_array) - 1)
        row = [
            time_stop.strftime('%Y-%m-%d'), time_stop.strftime('%H:%M:%S'),
            f_array[0], f_array[-1] + step, step, samples
        ]
        row += list(pwr_array)
        self.output.write('{}\n'.format(', '.join(str(x) for x in row)))
        self.output.flush()

    def write_next(self):
        """Write marker for next run of measurement"""
        pass


formats = {
    'soapy_power_bin': SoapyPowerBinWriter,
    'rtl_power_fftw': RtlPowerFftwWriter,
    'rtl_power': RtlPowerWriter,
}
