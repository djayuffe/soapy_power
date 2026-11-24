"""Measured-dwell geometry shared by scientific waterfall renderers."""

import functools
import warnings

import numpy as np

NAN_GAP_WARNINGS = 'All-NaN slice encountered|Mean of empty slice|Degrees of freedom <= 0 for slice'


def nan_gaps_expected(fn):
    """Masked DC bins and missing hops are NaN by design: silence numpy's empty-slice RuntimeWarnings for
    this call only (also where a caller such as a test runner has reset module-level filters)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', message=NAN_GAP_WARNINGS, category=RuntimeWarning)
            return fn(*args, **kwargs)

    return wrapper


def dwell_mesh(values, starts, stops):
    """Insert masked rows between actual dwells, never hold through revisit gaps."""
    values = np.asarray(values)
    starts = np.asarray(starts, float)
    stops = np.asarray(stops, float)
    if values.ndim != 2 or starts.shape != stops.shape or starts.shape != (values.shape[0],):
        raise ValueError('waterfall dwell/value shapes differ')
    keep = np.isfinite(starts) & np.isfinite(stops)
    if not keep.any():
        return np.empty(0), np.empty((0, values.shape[1]))
    a, b, z = starts[keep], stops[keep], values[keep]
    if np.any(b <= a) or np.any(a[1:] < b[:-1]):
        raise ValueError('invalid or overlapping waterfall dwells')
    edges = np.column_stack([a, b]).ravel()
    result = np.full((2 * len(a) - 1, z.shape[1]), np.nan)
    result[::2] = z
    return edges, result
