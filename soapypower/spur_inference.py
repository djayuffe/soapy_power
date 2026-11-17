"""Inference for the mixed signal/spur model.

The observation is a set of spectra taken at several LO settings and/or drive
levels.  A real tone follows ``f_if = f_rf - f_lo`` (slope -1 against LO), an
IQ image follows ``+1`` and a harmonic response follows ``+/-n``.  This module
provides the statistical pieces used by :mod:`soapypower.spur_pipeline`:

* a conjugate inverse-gamma posterior for the mean noise floor of
  Gamma-distributed PSD bins (with a two-component mixture that treats spurs
  as an upper tail instead of deleting them silently);
* Bayesian classification of one track under all mixing laws;
* robust (Huber) line fitting and CRLB-based identifiability;
* nonnegative L1/ISTA and orthogonal matching pursuit for sparse
  signal/spur decomposition.

Everything is pure numpy except the optional SciPy path used by
:mod:`soapypower.floor_estimation` for the Gamma distribution.
"""
import math

import numpy as np

from soapypower.floor_estimation import _quantile
from soapypower.spur_model import mixing_products


# ------------------------------------------------------------------ Gamma floor

def _ig_summary(shape, rate, confidence, dof, bins, extra=None):
    """Summarize an inverse-gamma posterior for the mean floor."""
    shape, rate = float(shape), float(rate)
    if shape <= 0 or rate <= 0:
        raise ValueError('inverse-gamma shape and rate must be positive')
    mean = rate / (shape - 1.0) if shape > 1.0 else float('inf')
    median = rate / _quantile(shape, 0.5)
    alpha = (1.0 - float(confidence)) / 2.0
    # P(mu <= x) = P(Gamma(shape,1) >= rate/x); invert as x = rate/G^{-1}(1-p).
    lo = rate / _quantile(shape, 1.0 - alpha)
    hi = rate / _quantile(shape, alpha)
    out = {
        'method': 'inverse_gamma',
        'mean_linear': float(mean),
        'mean_db': 10.0 * math.log10(mean) if mean > 0 and math.isfinite(mean) else None,
        'median_linear': float(median),
        'median_db': 10.0 * math.log10(median),
        'ci_db': [10.0 * math.log10(lo), 10.0 * math.log10(hi)],
        'confidence': float(confidence),
        'dof': float(dof),
        'shape': shape,
        'rate': rate,
        'bins': int(bins),
        'scope': 'relative mean noise power; posterior assumes independent bins',
    }
    if extra:
        out.update(extra)
    return out


def gamma_floor_posterior(power_linear, dof, prior_shape=1.0, prior_rate=None,
                          confidence=0.95, mask=None):
    """Posterior for the mean floor when bins are Gamma(k=dof/2, scale=theta).

    ``mu = k*theta`` is the mean PSD of the noise.  With an inverse-gamma prior
    ``mu ~ IG(a, b)`` and ``n`` independent bins with sum ``S``, the posterior
    is ``IG(a + n*k, b + k*S)``.  The default prior is weak and proper
    (``a=1``, ``b`` one millionth of the data scale), so the data dominate but
    the posterior is always normalizable.

    Returns the posterior mean (which exists because ``a+n*k > 1``), median,
    credible interval, and the same fields as
    :func:`soapypower.floor_estimation.floor_estimate` where they are useful.
    """
    p = np.asarray(power_linear, dtype=float)
    if mask is not None:
        p = p[np.asarray(mask, dtype=bool)]
    p = p[np.isfinite(p) & (p > 0)]
    if p.size < 2:
        raise ValueError('gamma_floor_posterior needs at least two positive bins')
    dof = float(dof)
    if not math.isfinite(dof) or dof <= 0:
        raise ValueError('dof must be positive')
    k = dof / 2.0
    n = int(p.size)
    total = float(np.sum(p))
    if prior_rate is None:
        prior_rate = 1e-6 * max(k * float(np.median(p)), 1e-300)
    prior_shape = float(prior_shape)
    if prior_shape <= 0 or prior_rate < 0:
        raise ValueError('prior_shape must be positive and prior_rate nonnegative')
    a = prior_shape + n * k
    b = float(prior_rate) + k * total
    return _ig_summary(a, b, confidence, dof, n,
                       {'prior_shape': prior_shape, 'prior_rate': float(prior_rate)})


def _gamma_log_pdf(p, shape, scale):
    """Log density of Gamma(shape, scale) for positive p."""
    p = np.asarray(p, dtype=float)
    return ((shape - 1.0) * np.log(np.maximum(p, 1e-300))
            - p / scale - shape * math.log(scale) - math.lgamma(shape))


def mixture_floor_estimate(power_linear, dof, components=2, iterations=200,
                           tolerance=1e-10, prior_weight=1.0, mask=None):
    """Two-component Gamma mixture: noise floor plus an upper-tail spur component.

    Both components share the PSD shape ``k=dof/2``; the lower-scale component
    is the noise floor and the upper one collects spurs and other excess
    power.  EM returns the posterior responsibility of every bin and a
    conjugate posterior for the floor mean using only the floor component.
    This replaces "delete every bin above X dB" with a soft, explicit model.
    """
    p = np.asarray(power_linear, dtype=float)
    keep = np.isfinite(p) & (p > 0)
    if mask is not None:
        keep &= np.asarray(mask, dtype=bool)
    p = p[keep]
    if p.size < 8:
        raise ValueError('mixture_floor_estimate needs at least eight positive bins')
    components = int(components)
    if components < 2:
        raise ValueError('components must be at least 2')
    dof = float(dof)
    if dof <= 0:
        raise ValueError('dof must be positive')
    k = dof / 2.0
    n = p.size
    q = np.quantile(p, np.linspace(0.05, 0.95, components))
    scales = np.maximum(q / k, 1e-12)
    weights = np.full(components, 1.0 / components)
    logp = np.empty((components, n))
    resp = np.empty((components, n))
    for _ in range(int(iterations)):
        for j in range(components):
            logp[j] = math.log(max(weights[j], 1e-300)) + _gamma_log_pdf(p, k, scales[j])
        top = np.max(logp, axis=0)
        e = np.exp(logp - top)
        denom = e.sum(axis=0)
        resp = e / denom
        new_weights = (resp.sum(axis=1) + prior_weight - 1.0) / (n + components * (prior_weight - 1.0))
        new_scales = np.maximum((resp * p).sum(axis=1) / (k * np.maximum(resp.sum(axis=1), 1e-300)), 1e-12)
        change = max(float(np.max(np.abs(new_scales - scales)) / max(scales.max(), 1e-300)),
                     float(np.max(np.abs(new_weights - weights))))
        weights, scales = new_weights, new_scales
        if change < tolerance:
            break
    order = np.argsort(scales)
    scales, weights, resp = scales[order], weights[order], resp[order]
    floor_resp = resp[0]
    floor_n = float(floor_resp.sum())
    floor_sum = float((floor_resp * p).sum())
    # Conjugate pseudo-posterior for the floor mean: fractional responsibilities
    # enter as effective bin counts, not as repeated samples (which would
    # understate the credible interval by orders of magnitude).
    post = _ig_summary(1.0 + max(floor_n, 1e-9) * k,
                       max(k * floor_sum, 1e-300) + 1e-9 * max(k * np.median(p), 1e-300),
                       0.95, dof, p.size)
    post.update({
        'method': 'gamma_mixture_em',
        'components': components,
        'component_weights': [float(x) for x in weights],
        'component_scales': [float(x) for x in scales],
        'component_means': [float(k * x) for x in scales],
        'floor_responsibility': floor_resp,
        'spur_probability': 1.0 - floor_resp,
        'spur_bins': int(np.sum(floor_resp < 0.5)),
        'effective_floor_bins': floor_n,
        'effective_floor_power': floor_sum,
        'bins': n,
        'dof': dof,
        'scope': 'relative mean noise power; the upper component is not a calibrated signal level',
    })
    post['mean_db'] = 10.0 * math.log10(post['mean_linear'])
    return post


# ----------------------------------------------------- Bayesian track classification

def _laws(orders):
    orders = tuple(int(n) for n in orders)
    if not orders or any(n < 1 or n % 2 == 0 for n in orders):
        raise ValueError('orders must be nonempty odd positive integers')
    out = [(1, 1, 'signal'), (1, -1, 'image')]
    for n in orders:
        if n == 1:
            continue
        out.append((n, 1, 'lo_harmonic'))
        out.append((n, -1, 'lo_harmonic'))
    return out


def _weighted_line(x, y, sigma):
    w = 1.0 / np.maximum(np.asarray(sigma, dtype=float), 1e-12) ** 2
    xm = np.sum(w * x) / np.sum(w)
    ym = np.sum(w * y) / np.sum(w)
    sxx = np.sum(w * (x - xm) ** 2)
    if sxx <= 0:
        raise ValueError('LO settings must not all be identical')
    slope = np.sum(w * (x - xm) * (y - ym)) / sxx
    intercept = ym - slope * xm
    resid = y - (intercept + slope * x)
    return slope, intercept, resid, w


def bayes_classify_track(lo_hz, if_hz, orders=(1, 3, 5, 7), resolution_hz=None,
                         sigma_hz=None, prior=None, signal_prior=0.5,
                         image_prior=0.05):
    """Posterior over signal/image/LO-harmonic laws for one tracked peak.

    ``lo_hz`` and ``if_hz`` are paired measurements of the same feature.  The
    likelihood is Gaussian in the IF residuals with a common standard
    deviation, so this is a compact Bayesian form of the LO-diversity test.
    The result is explicitly a probability, not a hard "spur" verdict; a tone
    that cannot be separated by the chosen LO span is returned as
    ``unidentifiable`` even if one law happens to fit best.
    """
    lo = np.asarray(lo_hz, dtype=float)
    if_hz = np.asarray(if_hz, dtype=float)
    if lo.ndim != 1 or lo.size != if_hz.size or lo.size < 1:
        raise ValueError('lo_hz and if_hz must be equal-length one-dimensional arrays')
    if not np.all(np.isfinite(lo)) or not np.all(np.isfinite(if_hz)):
        raise ValueError('LO and IF values must be finite')
    sigma = float(sigma_hz) if sigma_hz is not None else (
        max(float(resolution_hz), 1e-9) / math.sqrt(12.0) if resolution_hz else 1e-6)
    if sigma <= 0:
        raise ValueError('sigma_hz must be positive')
    span = float(np.ptp(lo)) if lo.size > 1 else 0.0
    res = float(resolution_hz) if resolution_hz else sigma * math.sqrt(12.0)
    if lo.size < 3 or span < 4.0 * max(res, 1e-12):
        return {
            'status': 'unidentifiable',
            'reason': 'needs at least three LO settings spanning four resolution bins',
            'lo_points': int(lo.size), 'lo_span_hz': span,
            'resolution_hz': res, 'posterior': {},
        }
    laws = _laws(orders)
    n_orders = sum(1 for law in laws if law[2] == 'lo_harmonic')
    if prior is None:
        priors = {'signal': float(signal_prior), 'image': float(image_prior)}
        each = max(0.0, 1.0 - priors['signal'] - priors['image']) / max(n_orders, 1)
        priors.update({'lo_harmonic': each})
    else:
        priors = dict(prior)
    log_ev = []
    fits = []
    for n, s, kind in laws:
        slope_expected = -s * n
        # The evidence is evaluated under the *fixed* law slope.  Fitting a free
        # line for every law would make all laws equally likely and destroy the
        # LO-diversity test.  A free fit is reported only as a diagnostic.
        weights = np.full(lo.size, 1.0 / sigma ** 2)
        intercept = float(np.sum(weights * (if_hz - slope_expected * lo)) / np.sum(weights))
        resid = if_hz - (intercept + slope_expected * lo)
        rms = float(np.sqrt(np.mean(resid ** 2)))
        ll = -0.5 * float(np.sum((resid / sigma) ** 2))
        free_slope, _, _, _ = _weighted_line(lo, if_hz, np.full(lo.size, sigma))
        log_ev.append(ll + math.log(max(priors.get(kind, 1e-12), 1e-300)))
        fits.append({'order': n, 'sign': s, 'kind': kind,
                     'slope_hz_per_hz': float(slope_expected),
                     'fitted_slope_hz_per_hz': float(free_slope),
                     'expected_slope_hz_per_hz': slope_expected,
                     'intercept_hz': float(intercept),
                     'rms_residual_hz': rms,
                     'source_hz': float(np.mean(s * if_hz + n * lo)),
                     'log_likelihood': float(ll)})
    log_ev = np.asarray(log_ev)
    log_ev -= np.max(log_ev)
    post = np.exp(log_ev)
    post /= post.sum()
    best_i = int(np.argmax(post))
    order = np.argsort(post)[::-1]
    best = fits[best_i]
    second = fits[int(order[1])] if len(order) > 1 else None
    best['posterior_probability'] = float(post[best_i])
    for fit, probability in zip(fits, post):
        fit['posterior_probability'] = float(probability)
    signal_probability = float(sum(f['posterior_probability'] for f in fits if f['kind'] == 'signal'))
    spur_probability = float(1.0 - signal_probability)
    status = 'classified'
    if best['posterior_probability'] < 0.6:
        status = 'ambiguous'
    if best['rms_residual_hz'] > max(2.0 * sigma, 1.5 * res):
        status = 'no_law_fits'
    return {
        'status': status,
        'kind': best['kind'],
        'order': int(best['order']),
        'sign': int(best['sign']),
        'source_hz': float(best['source_hz']),
        'slope_hz_per_hz': float(best['slope_hz_per_hz']),
        'expected_slope_hz_per_hz': float(best['expected_slope_hz_per_hz']),
        'signal_probability': signal_probability,
        'spur_probability': spur_probability,
        'best': best,
        'runner_up': dict(second) if second else None,
        'fits': fits,
        'posterior': {('signal' if f['kind'] == 'signal' else
                       'image' if f['kind'] == 'image' else
                       'lo_harmonic_{}_{}'.format(f['order'], 'p' if f['sign'] > 0 else 'm')):
                      float(f['posterior_probability']) for f in fits},
        'lo_points': int(lo.size),
        'lo_span_hz': span,
        'resolution_hz': res,
        'sigma_hz': sigma,
        'scope': 'posterior depends on the declared priors and Gaussian residual model',
    }


# ------------------------------------------------------------------ robust fit

def robust_line_fit(x, y, sigma=None, huber=1.345, iterations=25):
    """Huber IRLS line fit; useful when one LO visit is contaminated."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if x.ndim != 1 or x.size != y.size or x.size < 2:
        raise ValueError('x and y must be equal-length one-dimensional arrays')
    if np.ptp(x) <= 0:
        raise ValueError('x must span more than one value')
    if np.any(~np.isfinite(x)) or np.any(~np.isfinite(y)):
        raise ValueError('x and y must be finite')
    base_sigma = np.asarray(sigma, dtype=float) if sigma is not None else np.ones_like(x)
    if np.any(base_sigma <= 0):
        raise ValueError('sigma must be positive')
    # Theil-Sen start: the median pairwise slope ignores a single gross outlier,
    # which a plain OLS start can let dominate the first Huber scale estimate.
    if x.size <= 200:
        pairwise = [(y[j] - y[i]) / (x[j] - x[i])
                    for i in range(x.size) for j in range(i + 1, x.size)
                    if x[j] != x[i]]
        slope0 = float(np.median(pairwise))
        intercept0 = float(np.median(y - slope0 * x))
    else:
        slope0, intercept0 = np.polyfit(x, y, 1)
    weights = 1.0 / base_sigma ** 2
    slope, intercept = slope0, intercept0
    resid = y - (slope * x + intercept)
    for _ in range(int(iterations)):
        scale = 1.4826 * float(np.median(np.abs(resid - np.median(resid))))
        if scale <= 1e-12:
            break
        r = np.abs(resid) / scale
        robust = np.where(r <= huber, 1.0, huber / np.maximum(r, 1e-300))
        new_weights = robust / base_sigma ** 2
        if np.max(np.abs(new_weights - weights)) < 1e-12:
            weights = new_weights
            break
        weights = new_weights
        slope, intercept, resid, _ = _weighted_line(x, y, 1.0 / np.sqrt(weights))
        intercept0 = intercept
    scale = 1.4826 * float(np.median(np.abs(resid - np.median(resid))))
    return {
        'slope_hz_per_hz': float(slope),
        'intercept_hz': float(intercept),
        'rms_residual_hz': float(np.sqrt(np.mean(resid ** 2))),
        'robust_scale_hz': scale,
        'weights': [float(w) for w in weights],
        'points': int(x.size),
        'method': 'huber_irls',
    }


def crlb_slope(lo_hz, sigma_hz):
    """Cramer-Rao lower bound on the slope of IF versus LO for a line fit."""
    x = np.asarray(lo_hz, dtype=float)
    if x.ndim != 1 or x.size < 2 or np.ptp(x) <= 0:
        raise ValueError('need at least two distinct LO settings')
    sigma = float(sigma_hz)
    if sigma <= 0:
        raise ValueError('sigma_hz must be positive')
    sxx = float(np.sum((x - x.mean()) ** 2))
    return {
        'sd_slope_hz_per_hz': sigma / math.sqrt(sxx),
        'sd_intercept_hz': sigma * math.sqrt(1.0 / x.size + x.mean() ** 2 / sxx),
        'lo_span_hz': float(np.ptp(x)),
        'sigma_hz': sigma,
        'points': int(x.size),
    }


def slope_separation(lo_hz, sigma_hz, first=1, second=3):
    """Whether two mixing orders are statistically separable for a given LO set."""
    x = np.asarray(lo_hz, dtype=float)
    crlb = crlb_slope(x, sigma_hz)
    difference = abs(float(first) - float(second))
    return {
        'difference_in_slope': difference,
        'sd_slope': crlb['sd_slope_hz_per_hz'],
        'separation_sigma': difference / max(crlb['sd_slope_hz_per_hz'], 1e-300),
        'separable_3sigma': bool(difference >= 3.0 * crlb['sd_slope_hz_per_hz']),
        'separable_5sigma': bool(difference >= 5.0 * crlb['sd_slope_hz_per_hz']),
    }


# --------------------------------------------------------------- coherent stack

def coherent_stack(spectra, method='median', reference=0):
    """Phase-align complex spectra and combine them without a min-hold bias.

    ``spectra`` has shape (sweeps, bins).  Each sweep is rotated by the phase
    of its complex correlation with the reference sweep, then the real and
    imaginary parts are combined by median or mean.  For noisy data the
    amplitude median still has the usual median bias; use
    :func:`soapypower.floor_estimation.median_to_mean_db` when interpreting
    the level, or use ``method='mean'`` for an unbiased linear average.
    """
    stack = np.asarray(spectra, dtype=np.complex128)
    if stack.ndim != 2 or stack.shape[0] < 2 or stack.shape[1] < 8:
        raise ValueError('spectra must be a 2-D complex array with at least two sweeps')
    reference = int(reference)
    if not 0 <= reference < stack.shape[0]:
        raise ValueError('reference sweep is out of range')
    phases = np.zeros(stack.shape[0], dtype=float)
    aligned = np.empty_like(stack)
    for i, row in enumerate(stack):
        correlation = np.sum(row * np.conj(stack[reference]))
        phases[i] = float(np.angle(correlation)) if correlation != 0 else 0.0
        aligned[i] = row * np.exp(-1j * phases[i])
    if method == 'mean':
        combined = aligned.mean(axis=0)
    elif method == 'median':
        combined = np.median(aligned.real, axis=0) + 1j * np.median(aligned.imag, axis=0)
    else:
        raise ValueError('method must be mean or median')
    return {'combined': combined, 'phases_rad': phases, 'method': method,
            'reference': reference, 'sweeps': int(stack.shape[0]),
            'scope': 'phase alignment uses the reference sweep; median retains median bias'}


# --------------------------------------------------------------- sparse recovery

def soft_threshold(values, threshold):
    """Elementwise soft threshold, the proximal operator of the L1 norm."""
    v = np.asarray(values, dtype=float)
    return np.sign(v) * np.maximum(np.abs(v) - float(threshold), 0.0)


def ista(y, dictionary, lam=0.0, iterations=500, tolerance=1e-10,
         nonnegative=False):
    """Nonnegative L1 (ISTA) sparse reconstruction ``y ~= A x``.

    ``dictionary`` must have one column per candidate signal/spur atom.  The
    step size is the reciprocal of the squared spectral norm, estimated by
    power iteration; no SciPy dependency is needed.
    """
    y = np.asarray(y, dtype=float)
    A = np.asarray(dictionary, dtype=float)
    if y.ndim != 1 or A.ndim != 2 or A.shape[0] != y.size:
        raise ValueError('y must be one-dimensional and A must have one row per y value')
    if lam < 0:
        raise ValueError('lam must be nonnegative')
    x = np.zeros(A.shape[1], dtype=float)
    v = np.ones(A.shape[1], dtype=float)
    for _ in range(50):
        v = A.T @ (A @ v)
        nv = float(np.linalg.norm(v))
        if nv <= 0:
            break
        v /= nv
    L = max(float(np.linalg.norm(A @ v)), 1e-12) ** 2
    for _ in range(int(iterations)):
        gradient = A.T @ (y - A @ x)
        candidate = soft_threshold(x + gradient / L, float(lam) / L)
        if nonnegative:
            candidate = np.maximum(candidate, 0.0)
        if np.max(np.abs(candidate - x)) <= tolerance * max(1.0, np.max(np.abs(x))):
            x = candidate
            break
        x = candidate
    residual = y - A @ x
    return {
        'x': x,
        'residual': residual,
        'residual_l2': float(np.linalg.norm(residual)),
        'objective': float(0.5 * np.sum(residual ** 2) + lam * np.sum(np.abs(x))),
        'active': np.flatnonzero(np.abs(x) > 1e-12),
        'method': 'fista_ista',
    }


def orthogonal_matching_pursuit(y, dictionary, components=5, tolerance=1e-10):
    """Orthogonal matching pursuit for a small number of atoms."""
    y = np.asarray(y, dtype=float)
    A = np.asarray(dictionary, dtype=float)
    if y.ndim != 1 or A.ndim != 2 or A.shape[0] != y.size:
        raise ValueError('y must be one-dimensional and A must have one row per y value')
    components = min(int(components), A.shape[1])
    residual = y.copy()
    active = []
    for _ in range(components):
        corr = np.abs(A.T @ residual)
        if active:
            corr[active] = -1.0
        index = int(np.argmax(corr))
        if corr[index] <= tolerance:
            break
        active.append(index)
        coef, *_ = np.linalg.lstsq(A[:, active], y, rcond=None)
        residual = y - A[:, active] @ coef
    x = np.zeros(A.shape[1], dtype=float)
    if active:
        coef, *_ = np.linalg.lstsq(A[:, active], y, rcond=None)
        x[active] = coef
    return {
        'x': x,
        'residual': residual,
        'residual_l2': float(np.linalg.norm(residual)),
        'active': np.asarray(active, dtype=int),
        'method': 'omp',
    }


def build_spur_dictionary(if_hz, lo_hz, rf_grid_hz, orders=(1, 3, 5, 7),
                          image_rejection_db=35.0, sigma_hz=None,
                          include_dc=True, max_columns=200000):
    """Gaussian point-spread dictionary over candidate RF frequencies and laws.

    Each column is a normalized IF response centred at
    ``s*(rf - n*lo)``.  The result also contains the RF/law label for every
    column so an active atom can be interpreted instead of treated as an
    anonymous coefficient.
    """
    if_hz = np.asarray(if_hz, dtype=float)
    if if_hz.ndim != 1 or if_hz.size < 8:
        raise ValueError('if_hz must be a one-dimensional array of at least eight bins')
    rf_grid_hz = np.asarray(rf_grid_hz, dtype=float)
    if rf_grid_hz.ndim != 1 or rf_grid_hz.size == 0:
        raise ValueError('rf_grid_hz must be nonempty')
    step = float(np.median(np.diff(if_hz))) if if_hz.size > 1 else 1.0
    sigma = float(sigma_hz) if sigma_hz else max(abs(step), 1e-9)
    if sigma <= 0:
        raise ValueError('sigma_hz must be positive')
    columns, labels = [], []
    def add(center, label):
        center = float(center)
        if center < if_hz[0] - 4 * sigma or center > if_hz[-1] + 4 * sigma:
            return
        column = np.exp(-0.5 * ((if_hz - center) / sigma) ** 2)
        norm = float(np.linalg.norm(column))
        if norm <= 0:
            return
        columns.append(column / norm)
        labels.append(label)
    if include_dc:
        add(0.0, {'kind': 'dc', 'order': 0, 'sign': 1, 'rf_hz': float(lo_hz),
                  'if_hz': 0.0})
    for rf in rf_grid_hz:
        for product in mixing_products(float(rf), float(lo_hz), orders=orders,
                                       image_rejection_db=image_rejection_db):
            add(product['if_hz'], {'kind': product['kind'], 'order': product['order'],
                                   'sign': product['sign'], 'rf_hz': float(rf),
                                   'if_hz': float(product['if_hz'])})
            if len(columns) >= max_columns:
                raise ValueError('dictionary exceeds max_columns; coarsen the RF grid')
    if not columns:
        raise ValueError('no dictionary atom falls inside the IF band')
    return {'matrix': np.column_stack(columns), 'labels': labels,
            'if_hz': if_hz, 'lo_hz': float(lo_hz), 'sigma_hz': sigma,
            'columns': len(columns)}


def sparse_decompose_spectrum(power, if_hz, lo_hz, rf_grid_hz, orders=(1, 3, 5, 7),
                              image_rejection_db=35.0, sigma_hz=None, lam=None,
                              max_components=32):
    """Decompose a spectrum into candidate real tones and harmonic responses.

    The baseline floor is subtracted before solving, so the atoms describe
    excess power only.  Active atoms are returned with their RF/law labels;
    this is a diagnostic decomposition, never an automatic notch list.
    """
    p = np.asarray(power, dtype=float)
    if_hz = np.asarray(if_hz, dtype=float)
    if p.ndim != 1 or p.size != if_hz.size:
        raise ValueError('power and if_hz must be equal-length one-dimensional arrays')
    finite = np.isfinite(p)
    if finite.sum() < 8:
        raise ValueError('need at least eight finite bins')
    baseline = float(np.median(p[finite]))
    y = np.maximum(p - baseline, 0.0)
    dictionary = build_spur_dictionary(if_hz, lo_hz, rf_grid_hz, orders=orders,
                                       image_rejection_db=image_rejection_db,
                                       sigma_hz=sigma_hz)
    A = dictionary['matrix']
    if lam is None:
        lam = max(float(np.std(y)) * 0.05, 1e-12)
    result = ista(y, A, lam=lam, nonnegative=True, iterations=1000)
    order = np.argsort(result['x'])[::-1]
    atoms = []
    for index in order[:int(max_components)]:
        if result['x'][index] <= 1e-12:
            break
        label = dict(dictionary['labels'][int(index)])
        label['coefficient'] = float(result['x'][int(index)])
        atoms.append(label)
    return {
        'baseline_linear': baseline,
        'atoms': atoms,
        'active': int(result['active'].size),
        'residual_l2': result['residual_l2'],
        'lam': float(lam),
        'columns': dictionary['columns'],
        'scope': 'linear excess power; atoms overlap and are not calibrated amplitudes',
    }
