"""Conservative scan planning for positive IF weight and an excluded origin.

A one-bin edge margin keeps zero-weight taper edges out of the support proof.
The guarantee describes requested IF coordinates; unknown analog LO spurs are
not inferred. Actual tuner coercion must still be checked by the caller.
"""
import math
from bisect import bisect_left
from soapypower.numerics import finite


def support_intervals(center, halfwidth, mask_width=0, mask_offset=0, margin=0):
    left, right = center-halfwidth+margin, center+halfwidth-margin
    if not left < right:
        raise ValueError('usable IF region is empty')
    if not mask_width:
        return [(left,right)]
    lo,hi = center+mask_offset-mask_width/2-margin, center+mask_offset+mask_width/2+margin
    return [(a,b) for a,b in ((left,min(right,lo)),(max(left,hi),right)) if a < b]


def coverage_gaps(centers, start, stop, halfwidth, mask_width=0, mask_offset=0, margin=0):
    intervals = sorted(v for c in centers for v in support_intervals(c,halfwidth,mask_width,mask_offset,margin))
    covered = start
    gaps = []
    for left,right in intervals:
        if right <= covered:
            continue
        if left > covered:
            gaps.append((covered,min(left,stop)))
        covered = max(covered,right)
        if covered >= stop:
            break
    if covered < stop:
        gaps.append((covered,stop))
    return [(a,b) for a,b in gaps if a < b]


def masked_frequency_plan(start, stop, halfwidth, bin_width, mask_width=0,
                          mask_offset=0, tune_range=None, max_centers=1000000, preferred_centers=None):
    start,stop = finite(start,'start'),finite(stop,'stop')
    h = finite(halfwidth,'usable IF halfwidth',positive=True)
    df = finite(bin_width,'bin width',positive=True)
    mask = finite(mask_width,'mask width',nonnegative=True)
    offset = finite(mask_offset,'mask offset')
    if start >= stop:
        raise ValueError('coverage planning requires a nonempty ascending band')
    # Each origin hole is seen strictly inside a neighbour's usable passband.
    step = math.floor((h-abs(offset)-mask/2-3*df)/df)*df
    if step <= 0:
        raise ValueError('no positive mask-aware hop remains')
    low,high = (start-h,stop+h) if tune_range is None else tune_range
    low,high = finite(low,'tuning minimum'),finite(high,'tuning maximum')
    if low >= high:
        raise ValueError('invalid tuning range')
    first,last = max(low,start-h+2*df),min(high,stop+h-2*df)
    if first > last:
        raise ValueError('requested band is unreachable')
    count = int(math.ceil((last-first)/step))+1
    if count > max_centers:
        raise ValueError('coverage plan exceeds center budget')
    centers = [first+i*step for i in range(count-1)] + [last]
    if coverage_gaps(centers,start,stop,h,mask,offset,df):
        raise ValueError('requested band cannot be covered with positive weight outside the configured mask')
    if preferred_centers is not None:
        # Keep the proven coverage grid intact; requested overlap adds views.
        # Never quantize/move coverage centers or bridge their origin holes.
        added = set()
        for value in preferred_centers:
            value = finite(value, 'preferred center')
            if not low <= value <= high:
                continue
            index = bisect_left(centers, value)
            neighbours = centers[max(0, index-1):index+1]
            if all(abs(value-other) > df/2 for other in neighbours):
                added.add(value)
            if len(centers) + len(added) > max_centers:
                raise ValueError('coverage and overlap plan exceeds center budget')
        centers = sorted(set(centers) | added)
    return centers
