#!/usr/bin/env python3

import os, sys, logging, struct, collections, io, threading

import numpy

from soapypower import threadpool

if sys.platform == 'win32':
    import msvcrt

logger = logging.getLogger(__name__)


class BaseWriter:
    """Power Spectral Density writer base class"""
    def __init__(self, output=sys.stdout):
        self._close_output = False
        self._write_error = None
        self._error_lock = threading.Lock()
        self._closed = False
        self._wrapped_output = False

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
        error = future.exception()
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


class SoapyPowerBinFormat:
    """Power Spectral Density binary file format"""
    header_struct = struct.Struct('<BdddddQQ2x')
    header = collections.namedtuple('Header', 'version time_start time_stop start stop step samples size')
    magic = b'SDRFF'
    version = 2

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
        if header.size % 4:
            raise ValueError('payload size must be a multiple of float32 size')
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
        pwr_array = numpy.asarray(pwr_array, dtype='<f4')
        if pwr_array.ndim != 1:
            raise ValueError('power array must be one-dimensional')
        f.write(self.magic)
        f.write(self.header_struct.pack(
            self.version, time_start, time_stop, start, stop, step, samples, pwr_array.nbytes
        ))
        #pwr_array.tofile(f)
        f.write(pwr_array.tobytes())
        f.flush()

    def header_size(self):
        """Return total size of header"""
        return len(self.magic) + self.header_struct.size


class SoapyPowerBinWriter(BaseWriter):
    """Write Power Spectral Density to stdout or file (in soapy_power binary format)"""
    def __init__(self, output=sys.stdout):
        super().__init__(output=output)
        self.formatter = SoapyPowerBinFormat()

    def write(self, psd_data_or_future, time_start, time_stop, samples):
        """Write PSD of one frequency hop"""
        if hasattr(psd_data_or_future, 'result'):
            f_array, pwr_array = psd_data_or_future.result()
        else:
            f_array, pwr_array = psd_data_or_future

        step = f_array[1] - f_array[0]
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
    def __init__(self, output=sys.stdout):
        super().__init__(output=output)
        if not isinstance(self.output, io.TextIOBase):
            self.output = io.TextIOWrapper(self.output, encoding='utf-8', newline='\n')
            self._wrapped_output = True

    def write(self, psd_data_or_future, time_start, time_stop, samples):
        """Write PSD of one frequency hop"""
        if hasattr(psd_data_or_future, 'result'):
            f_array, pwr_array = psd_data_or_future.result()
        else:
            f_array, pwr_array = psd_data_or_future

        self.output.write('# soapy_power output\n')
        self.output.write('# Acquisition start: {}\n'.format(time_start))
        self.output.write('# Acquisition end: {}\n'.format(time_stop))
        self.output.write('#\n')
        self.output.write('# frequency [Hz] power spectral density [dB/Hz]\n')

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
    def __init__(self, output=sys.stdout):
        super().__init__(output=output)
        if not isinstance(self.output, io.TextIOBase):
            self.output = io.TextIOWrapper(self.output, encoding='utf-8', newline='\n')
            self._wrapped_output = True

    def write(self, psd_data_or_future, time_start, time_stop, samples):
        """Write PSD of one frequency hop"""
        if hasattr(psd_data_or_future, 'result'):
            f_array, pwr_array = psd_data_or_future.result()
        else:
            f_array, pwr_array = psd_data_or_future

        step = f_array[1] - f_array[0]
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
