"""Explicit conversion of native interleaved IQ; never guess ADC full scale."""
import numpy
from soapypower.numerics import finite


def iq_from_interleaved(values, full_scale, offset=0, dtype='complex64'):
    full_scale = finite(full_scale, 'native full_scale', positive=True)
    offset = finite(offset, 'native offset')
    values = numpy.asarray(values)
    if values.ndim != 1 or values.size % 2 or values.dtype.kind not in 'iuf':
        raise ValueError('native IQ must be an even-length interleaved numeric vector')
    if dtype not in ('complex64', 'complex128'):
        raise ValueError('IQ dtype must be complex64 or complex128')
    scaled = (values.astype(numpy.float64) - offset) / full_scale
    result = (scaled[::2] + 1j * scaled[1::2]).astype(dtype)
    if not numpy.isfinite(result).all():
        raise ValueError('converted IQ is outside representable range')
    return result
