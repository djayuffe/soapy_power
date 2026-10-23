"""Shared integer and finite-value checks for numerical configuration."""
import math
import operator
import numpy


def integer(value, name, minimum=0):
    try:
        if isinstance(value, (bool, numpy.bool_)):
            raise TypeError
        result = operator.index(value)
    except TypeError:
        raise ValueError('{} must be an integer >= {}'.format(name, minimum)) from None
    if result < minimum:
        raise ValueError('{} must be an integer >= {}'.format(name, minimum))
    return result


def finite(value, name, positive=False, nonnegative=False):
    try:
        if isinstance(value, (bool, numpy.bool_)):
            raise TypeError
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('{} must be finite'.format(name)) from None
    if not math.isfinite(result) or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError('{} has an invalid numerical value'.format(name))
    return result


def crop_bins(bins, fraction):
    bins = integer(bins, 'bins', 2)
    fraction = finite(fraction, 'crop/overlap', nonnegative=True)
    if fraction >= 1:
        raise ValueError('crop/overlap must be in [0, 1)')
    # Both planner and PSD must use exactly this same integer geometry.
    count = 2 * round(fraction * bins / 2)
    if bins - count < 2:
        raise ValueError('crop/overlap must retain at least two bins')
    return count
