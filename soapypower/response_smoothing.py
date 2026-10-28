"""Optional constrained, uncertainty-weighted baseband smoothing of measured grids.

All RF rows are treated separately. Explicit breakpoints block curvature penalties
across known edges; fixed endpoints/protected knots and anchors remain unchanged.
Independent tone holdouts are mandatory before publishing a candidate profile.
"""
import copy
import math
import numpy as np

from soapypower.calibration import PowerCalibration
from soapypower.calibration_workflows import _sha256
from soapypower.files import load_json
from soapypower.numerics import finite
from soapypower.response_validation import validate_holdout, response_sha256


def smooth_row(baseband_hz, correction_db, standard_db, strength,
               reference_baseband_hz, breakpoints_hz=(), protected_hz=()):
    x = np.asarray(baseband_hz, dtype=float)
    y = np.asarray(correction_db, dtype=float)
    sigma = np.asarray(standard_db, dtype=float)
    if (x.ndim != 1 or len(x) < 3 or len(x) > 512 or y.shape != x.shape or sigma.shape != x.shape
            or not np.isfinite(x).all() or not np.isfinite(y).all() or not np.isfinite(sigma).all()
            or not np.isfinite(np.diff(x)).all() or np.any(np.diff(x) <= 0) or np.any(sigma <= 0)):
        raise ValueError('smoothing requires 3..512 increasing knots, finite corrections and positive standard errors')
    strength = finite(strength, 'smoothing strength', nonnegative=True)
    reference = finite(reference_baseband_hz, 'reference baseband')
    if not x[0] <= reference <= x[-1]:
        raise ValueError('reference outside smoothing grid')
    breaks = np.asarray(breakpoints_hz, dtype=float)
    protected = np.asarray(protected_hz, dtype=float)
    if (breaks.ndim != 1 or protected.ndim != 1 or not np.isfinite(breaks).all()
            or not np.isfinite(protected).all() or len(np.unique(breaks)) != len(breaks)
            or np.any(breaks <= x[0]) or np.any(breaks >= x[-1])):
        raise ValueError('breakpoints must be unique finite interior frequencies')
    if any(not np.any(x == value) for value in protected):
        raise ValueError('protected frequencies must be measured knots')
    if any(np.any(x == value) for value in breaks):
        raise ValueError('put breakpoints strictly between measured knots')
    fixed = sorted({0, len(x)-1} | {int(np.flatnonzero(x == value)[0]) for value in protected})
    constraints = [np.eye(len(x))[i] for i in fixed]
    # Exact linear interpolation constraint at the original reference offset.
    ref_weights = np.zeros(len(x))
    if reference in x:
        ref_weights[int(np.flatnonzero(x == reference)[0])] = 1
    else:
        i = int(np.searchsorted(x, reference)-1)
        f = (reference-x[i])/(x[i+1]-x[i])
        ref_weights[i:i+2] = [1-f, f]
    constraints.append(ref_weights)
    constraint = np.asarray(constraints)
    _, singular, vt = np.linalg.svd(constraint, full_matrices=True)
    rank = int(np.count_nonzero(singular > singular[0]*1e-12))
    null = vt[rank:].T
    # Solve only for increments in the nullspace: all constraints stay exact.
    gaps = np.diff(x);typical = float(np.median(gaps))
    penalty = []
    for i in range(1, len(x)-1):
        if np.any((breaks > x[i-1]) & (breaks < x[i+1])):
            continue
        row = np.zeros(len(x));a, b = typical/gaps[i-1], typical/gaps[i]
        row[i-1:i+2] = [a, -a-b, b]
        penalty.append(row)
    p = np.asarray(penalty).reshape(-1, len(x))
    if not np.isfinite(p).all() or np.any(abs(p) > 1e12):
        raise ValueError('smoothing knot spacing is numerically ill-conditioned')
    # Normalize the objective by min(sigma)^2 to avoid huge inverse weights.
    scale = float(sigma.min())
    weight = scale/sigma
    if np.any(weight < 1e-12) or not math.isfinite(math.sqrt(strength)*scale):
        raise ValueError('smoothing weight dynamic range is too large')
    if null.shape[1] and strength and len(p):
        penalty_scale = math.sqrt(strength)*scale
        matrix = np.vstack((weight[:,None]*null, penalty_scale*(p@null)))
        rhs = np.r_[np.zeros(len(x)), -penalty_scale*(p@y)]
        solution, _, fitted_rank, _ = np.linalg.lstsq(matrix, rhs, rcond=1e-12)
        if fitted_rank != null.shape[1]:
            raise ValueError('ill-conditioned constrained smoothing')
        fitted = y + null@solution
    else:
        fitted = y.copy()
    if not np.isfinite(fitted).all():
        raise ValueError('smoothed response outside float64 range')
    if not np.allclose(constraint@(fitted-y),0,rtol=0,atol=1e-9):
        raise ValueError('smoothing failed protected-knot constraints')
    return fitted


def smooth_profile(calibration, model_path, holdout_manifest):
    model = load_json(model_path)
    if not isinstance(model, dict) or set(model) != {'version', 'standard_uncertainty_db', 'strength',
            'max_change_db', 'breakpoints_hz', 'protected_baseband_hz', 'uncertainty_assumption'}:
        raise ValueError('invalid smoothing model fields')
    if type(model['version']) is not int or model['version'] != 1:
        raise ValueError('expected smoothing model version 1')
    if model['uncertainty_assumption'] != 'independent_knot_standard_errors':
        raise ValueError('smoothing requires explicit independent-knot standard-error assumption')
    profile = calibration.profile
    x = profile['baseband_hz']
    sigma = np.asarray(model['standard_uncertainty_db'], dtype=float)
    original = np.array(profile['baseband_correction_db']) + np.array(profile['points'])[:,1,None]
    if sigma.shape != original.shape:
        raise ValueError('smoothing standard errors must match RF-by-baseband grid')
    strength = finite(model['strength'], 'smoothing strength', nonnegative=True)
    limit = finite(model['max_change_db'], 'maximum smoothing change dB', nonnegative=True)
    rows = np.array([smooth_row(x,row,std,strength,profile['reference_baseband_hz'],
                     model['breakpoints_hz'],model['protected_baseband_hz']) for row,std in zip(original,sigma)])
    delta = rows-original
    if float(np.max(abs(delta))) > limit:
        raise ValueError('smoothing exceeds declared maximum change; candidate rejected')
    anchors = [float(np.interp(profile['reference_baseband_hz'],x,row)) for row in rows]
    profile['points'] = [[p[0],anchor] for p,anchor in zip(profile['points'],anchors)]
    profile['baseband_correction_db'] = (rows-np.asarray(anchors)[:,None]).tolist()
    previous_uncertainty = {k:copy.deepcopy(profile[k]) for k in ('uncertainty','uncertainty_db') if k in profile}
    profile.pop('uncertainty_db',None)
    if profile['version'] == 2:
        # Existing interpolation error cannot certify a new fitted response.
        profile['uncertainty']['components']['interpolation_db'] = None
    details = copy.deepcopy(profile.get('method_details',{}))
    if not isinstance(details, dict):
        raise ValueError('invalid source profile method details')
    details['response_smoothing'] = {'source_profile_sha256':calibration.profile_id,
        'source_response_sha256':response_sha256(calibration), 'model_sha256':_sha256(model_path),
        'model':model, 'per_knot_change_db':delta.tolist(), 'max_abs_change_db':float(np.max(abs(delta))),
        'previous_uncertainty_declaration':previous_uncertainty,
        'uncertainty_status':'new smoothing/model error not quantified; previous budget does not certify it',
        'scope':'baseband only, independently per RF row; endpoints, protected knots and reference held fixed'}
    profile['method_details'] = details
    candidate = PowerCalibration(profile)
    # Unseen tones validate the frozen candidate; never tune strength to these data here.
    validation = validate_holdout(candidate,holdout_manifest)
    if len({(cell['rf_hz'],cell['baseband_hz']) for cell in validation['cells']}) < 2:
        raise ValueError('smoothing requires at least two distinct independent holdout coordinates')
    if not validation['passed']:
        raise ValueError('independent holdout validation failed; smoothed profile not published')
    details['response_smoothing']['holdout_validation'] = validation
    final = PowerCalibration(profile)
    if response_sha256(final) != validation['response_sha256']:
        raise ValueError('response changed after holdout validation')
    return final
