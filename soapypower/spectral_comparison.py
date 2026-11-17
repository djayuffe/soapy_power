"""Support-preserving linear-density comparisons; never interpolate RF holes."""
import numpy as np
from soapypower.numerics import numeric_array


def edges(value):
    a = numeric_array(value,'frequency edges')
    if a.ndim != 1 or len(a) < 2 or not np.isfinite(a).all() or not np.isfinite(np.diff(a)).all() or not np.all(np.diff(a)>0):
        raise ValueError('frequency edges must be finite and strictly increasing')
    return a


def rebin_density(source_edges, density, target_edges, support=None):
    """Area-overlap mean density and measured fraction of each target cell.

    Input is linear density in a single consistent unit. NaN denotes missing;
    zero is measured zero. Fractional support describes uniformly distributed
    coverage within a source cell, not the unknown location of subcell holes.
    Use finer source cells when the position of a hole matters.
    """
    src,dst = edges(source_edges),edges(target_edges)
    p = numeric_array(density,'density')
    if p.shape != (len(src)-1,) or np.isinf(p).any() or np.any(p<0):
        raise ValueError('density must be nonnegative finite or NaN, one value per cell')
    fraction = np.ones(len(p)) if support is None else numeric_array(support,'support')
    if fraction.shape != p.shape or not np.isfinite(fraction).all() or np.any((fraction<0)|(fraction>1)):
        raise ValueError('support fractions must be in [0,1]')
    result = np.full(len(dst)-1,np.nan)
    covered = np.zeros(len(result))
    i = 0
    for j,(lo,hi) in enumerate(zip(dst[:-1],dst[1:])):
        while i < len(p) and src[i+1] <= lo:
            i += 1
        k,total,mean = i,0.,0.
        while k < len(p) and src[k] < hi:
            w = max(0.,min(hi,src[k+1])-max(lo,src[k]))*fraction[k]
            if w > 0 and np.isfinite(p[k]):
                new = total+w
                # Convex update avoids cumsum cancellation and overflowing p*Hz.
                mean = mean*(total/new)+p[k]*(w/new)
                total = new
            k += 1
        if total > 0:
            result[j] = mean
            covered[j] = min(1.,total/(hi-lo))
    return result,covered


def fuse_density(densities, weights):
    """Fuse co-registered linear densities using only positive finite weights.

    Callers must explicitly bring units/reference planes/gain states into the
    same convention first. No cross-receiver absolute calibration is inferred.
    """
    p,w = numeric_array(densities,'densities'),numeric_array(weights,'weights')
    if p.ndim != 2 or p.shape != w.shape or not len(p) or np.isinf(p).any() or np.any(p<0):
        raise ValueError('densities and weights require matching nonnegative row arrays')
    if not np.isfinite(w).all() or np.any(w<0):
        raise ValueError('fusion weights must be finite and nonnegative')
    w = np.where(np.isfinite(p),w,0)
    scale = w.max(axis=0)
    normalized = np.divide(w,scale,out=np.zeros_like(w),where=scale>0)
    total = normalized.sum(axis=0)
    normalized = np.divide(normalized,total,out=np.zeros_like(w),where=total>0)
    result = np.sum(np.where(np.isfinite(p),p,0)*normalized,axis=0)
    result[total==0] = np.nan
    return result, np.sum(w>0,axis=0)


def fuse_observations(observations, target_edges, reference_time_s=None, max_age_s=None,
                      max_cells=1000000):
    """Fuse explicitly normalized hops and export sparse per-cell provenance.

    Observation keys: id, frequency_edges_hz, density, units,
    reference_convention, start_s, stop_s; optional supported_fraction and
    weight. Time values share a caller-declared time basis. Aging is opt-in.
    Peak output is the largest contributing cell mean, not a burst detector.
    """
    from soapypower.numerics import finite, integer
    dst = edges(target_edges)
    if not observations or len(observations)*(len(dst)-1)>integer(max_cells,'fusion cell budget',1):
        raise ValueError('fusion requires observations within its cell budget')
    if max_age_s is not None:
        max_age_s = finite(max_age_s,'maximum age',nonnegative=True)
        reference_time_s = finite(reference_time_s,'reference time')
    ids,means,weights,starts,stops = [],[],[],[],[]
    convention = None
    for row in observations:
        identity = row.get('id')
        unit,reference = row.get('units'),row.get('reference_convention')
        if not isinstance(identity,str) or not identity or identity in ids:
            raise ValueError('hop IDs must be nonempty and unique')
        if not isinstance(unit,str) or not unit or not isinstance(reference,str) or not reference:
            raise ValueError('fusion requires explicit units and normalization/reference convention')
        if convention is None:convention = (unit,reference)
        if (unit,reference) != convention:
            raise ValueError('cannot fuse different units or gain/reference conventions')
        start,stop = finite(row['start_s'],'hop start'),finite(row['stop_s'],'hop stop')
        if start>stop:raise ValueError('hop time interval is reversed')
        if reference_time_s is not None and stop>reference_time_s:
            raise ValueError('hop lies after the declared reference time')
        value,support = rebin_density(row['frequency_edges_hz'],row['density'],dst,row.get('supported_fraction'))
        weight = finite(row.get('weight',1),'hop weight',nonnegative=True)*support
        if max_age_s is not None and reference_time_s-stop>max_age_s:weight[:]=0
        ids.append(identity);means.append(value);weights.append(weight);starts.append(start);stops.append(stop)
    means,weights = np.array(means),np.array(weights)
    mean,count = fuse_density(means,weights)
    positive = weights>0
    peak = np.max(np.where(positive,means,-np.inf),axis=0)
    peak[count==0] = np.nan
    earliest = np.min(np.where(positive,np.asarray(starts)[:,None],np.inf),axis=0)
    latest = np.max(np.where(positive,np.asarray(stops)[:,None],-np.inf),axis=0)
    earliest[count==0] = latest[count==0] = np.nan
    # Compressed sparse row: one row per output frequency cell, values are hop indices.
    cell,hop = np.nonzero(positive.T)
    indptr = np.r_[0,np.cumsum(count)]
    return dict(frequency_edges_hz=dst,density=mean,support_count=count,
        peak_contributing_mean_density=peak,first_acquisition_s=earliest,last_acquisition_s=latest,
        contributor_indptr=indptr,contributor_hop_index=hop,
        contributor_weight=weights[hop,cell],hop_ids=np.asarray(ids),
        units=convention[0],reference_convention=convention[1])
