"""Measured-dwell geometry shared by scientific waterfall renderers."""
import numpy as np


def dwell_mesh(values, starts, stops):
    """Insert masked rows between actual dwells, never hold through revisit gaps."""
    values=np.asarray(values);starts=np.asarray(starts,float);stops=np.asarray(stops,float)
    if values.ndim!=2 or starts.shape!=stops.shape or starts.shape!=(values.shape[0],):
        raise ValueError('waterfall dwell/value shapes differ')
    keep=np.isfinite(starts)&np.isfinite(stops)
    if not keep.any():return np.empty(0),np.empty((0,values.shape[1]))
    a,b,z=starts[keep],stops[keep],values[keep]
    if np.any(b<=a) or np.any(a[1:]<b[:-1]):raise ValueError('invalid or overlapping waterfall dwells')
    edges=np.column_stack([a,b]).ravel()
    result=np.full((2*len(a)-1,z.shape[1]),np.nan)
    result[::2]=z
    return edges,result
