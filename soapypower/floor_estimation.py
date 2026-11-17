"""Bias-free noise-floor estimation, FDR peak detection and mixing-order classification.

All spectra here are *relative* (uncalibrated). A PSD bin averaged over K independent
segments is Gamma(k=K) distributed about its mean (chi-square with 2K degrees of
freedom, `dof=2K`). Consequences this module corrects instead of ignoring:

* the sample median of a bin lies below the mean by median_to_mean_db(dof)
  (-1.59 dB for dof=2, -0.0..dB for large dof) and min-hold lies far below it;
* a fixed "N dB above the floor" threshold has a false-alarm rate that depends on dof
  and on the number of bins tested; Benjamini-Hochberg control does not;
* the order n of a mixing product follows from how its IF moves with the LO
  (slope -n, +n for an image) and from how its level moves with the drive (slope n
  for an n-th order intermodulation product).

Pure numpy; scipy is used when installed, with an exact integer-dof fallback.
"""
import math
import numpy as np

try:  # optional, only for non-integer dof
    from scipy import special as _sp
except Exception:  # pragma: no cover
    _sp = None


# ----------------------------------------------------------------- gamma helpers

def _sf(k, x):
    """P(Gamma(k, 1) > x), vectorized in x."""
    x = np.asarray(x, dtype=float)
    if _sp is not None:
        return _sp.gammaincc(k, np.maximum(x, 0.0))
    kk = int(round(k))
    if kk > 2000:  # Wilson-Hilferty
        z = ((np.maximum(x, 1e-300) / kk) ** (1 / 3) - (1 - 1 / (9 * kk))) / math.sqrt(1 / (9 * kk))
        return 0.5 * np.vectorize(math.erfc)(z / math.sqrt(2))
    xs = np.maximum(x, 0.0)
    term = np.exp(-xs)
    total = term.copy()
    for j in range(1, kk):
        term = term * xs / j
        total += term
    return np.minimum(total, 1.0)


def survival(power, floor_mean, dof):
    """P(bin >= power) for a bin whose mean is floor_mean and which averages dof/2 segments."""
    k = dof / 2.0
    return _sf(k, k * np.asarray(power, dtype=float) / floor_mean)


def _quantile(k, p, tol=1e-12):
    """x with P(Gamma(k,1) <= x) = p, by bisection on the survival function."""
    if _sp is not None:
        return float(_sp.gammaincinv(k, p))
    lo, hi = 0.0, max(1.0, k + 20 * math.sqrt(k))
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if 1.0 - float(_sf(k, mid)) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol * max(1.0, hi):
            break
    return 0.5 * (lo + hi)


def median_to_mean_db(dof):
    """10 log10(median/mean) of a bin averaged over dof/2 segments (exponential: 10 log10 ln 2)."""
    k = dof / 2.0
    return 10.0 * math.log10(_quantile(k, 0.5) / k)


def min_hold_to_mean_db(n, dof):
    """10 log10(E[min of n independent bins]/mean) for n sweeps of the same bin."""
    k = dof / 2.0
    top = k + 40 * math.sqrt(k) + 40
    x = np.linspace(0.0, top, 20001)
    s = _sf(k, x) ** n
    return 10.0 * math.log10(float(np.trapezoid(s, x)) / k) if hasattr(np, 'trapezoid') else \
        10.0 * math.log10(float(np.trapz(s, x)) / k)


def estimate_dof(power_linear, min_dof=2.0, max_dof=1e5):
    """Equivalent dof of the bins from their own spread: dof = 2 mean^2 / var, from the quiet bulk.

    Uses the central 80 % (spurs excluded) and corrects the variance lost by truncation
    by calibrating against the exact truncated gamma at the estimated k (one fixed-point step).
    """
    p = np.asarray(power_linear, dtype=float)
    p = p[np.isfinite(p) & (p > 0)]
    if p.size < 20:
        raise ValueError('too few bins to estimate dof')
    q10, q90 = np.quantile(p, [0.1, 0.9])
    core = p[(p >= q10) & (p <= q90)]
    cv2 = float(np.var(core) / np.mean(core) ** 2)
    # truncated-variance ratio at candidate k, solved by scanning log-spaced k
    ks = np.geomspace(min_dof / 2, max_dof / 2, 80)
    ratio = []
    for k in ks:
        a, b = _quantile(k, 0.1), _quantile(k, 0.9)
        x = np.linspace(a, b, 4000)
        pdf = np.exp((k - 1) * np.log(np.maximum(x, 1e-300)) - x - math.lgamma(k))
        w = pdf / pdf.sum()
        m = (w * x).sum()
        ratio.append(((w * (x - m) ** 2).sum() / m ** 2))
    ratio = np.array(ratio)
    k = float(np.interp(cv2, ratio[::-1], ks[::-1]))
    return float(np.clip(2 * k, min_dof, max_dof))


# ------------------------------------------------------------------ floor level

def floor_estimate(power_linear, dof=None, confidence=0.95, correlation_bins=1.0, spur_alpha=1e-3):
    """Mean (true) floor level with a distribution-free confidence interval.

    1. median-based start, bias-corrected for the bin dof;
    2. bins whose upper-tail probability is below spur_alpha/n are excluded (spurs), and the
       estimate is repeated on the rest with the matching truncation of the median rank;
    3. CI from order-statistic ranks of the median (binomial), mapped to the mean.
    correlation_bins > 1 shrinks the effective sample size (windowed, overlapping bins).
    Returns linear and dB values; `bias_db` is what a plain dB-median would have been off by.
    """
    p = np.asarray(power_linear, dtype=float)
    p = p[np.isfinite(p) & (p > 0)]
    n0 = p.size
    if n0 < 8:
        raise ValueError('floor_estimate needs at least 8 bins')
    dof = float(dof) if dof is not None else estimate_dof(p)
    k = dof / 2.0
    mtm = _quantile(k, 0.5) / k  # median/mean ratio
    mean_hat = float(np.median(p)) / mtm
    kept = p
    for _ in range(3):
        limit = mean_hat * _quantile(k, 1.0 - spur_alpha / max(n0, 1)) / k
        kept = p[p <= limit]
        if kept.size < 8:
            kept = p
            break
        # median of the retained, untruncated-side-equivalent: rank shift for the removed upper tail
        removed = (p.size - kept.size) / p.size
        rank = min(0.999999, 0.5 / (1.0 - removed)) if removed < 0.5 else 0.5
        mean_hat = float(np.quantile(kept, rank)) / mtm
    n_eff = max(kept.size / max(correlation_bins, 1.0), 8.0)
    z = _normal_quantile(0.5 + confidence / 2.0)
    half = 0.5 * z * math.sqrt(n_eff)
    s = np.sort(kept)
    lo_i = int(np.clip(math.floor(kept.size * 0.5 - half * kept.size / n_eff), 0, kept.size - 1))
    hi_i = int(np.clip(math.ceil(kept.size * 0.5 + half * kept.size / n_eff), 0, kept.size - 1))
    lo, hi = float(s[lo_i]) / mtm, float(s[hi_i]) / mtm
    raw_db = 10 * math.log10(float(np.median(p)))
    return {'mean_linear': mean_hat, 'mean_db': 10 * math.log10(mean_hat),
            'ci_db': [10 * math.log10(lo), 10 * math.log10(hi)], 'confidence': confidence,
            'dof': dof, 'bins': int(n0), 'bins_used': int(kept.size), 'spur_bins_excluded': int(n0 - kept.size),
            'median_db_uncorrected': raw_db, 'bias_db': 10 * math.log10(mtm),
            'effective_samples': float(n_eff),
            'scope': 'relative level of the mean noise power; bias corrected for the bin dof'}


def floor_estimate_db(levels_db, **options):
    """floor_estimate for bins given in dB (as in the JSONL records)."""
    return floor_estimate(10.0 ** (np.asarray(levels_db, dtype=float) / 10.0), **options)


# ------------------------------------------------------------------ peak detection

def _normal_quantile(p):
    # Acklam-free: bisection on erf is plenty for a handful of calls
    lo, hi = -10.0, 10.0
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if 0.5 * (1 + math.erf(mid / math.sqrt(2))) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def detect_peaks(power_linear, floor_mean, dof, alpha=0.01, method='bh', fixed_db=6.0):
    """Indices of bins that exceed the floor, with the false-alarm control `method`.

    bh: Benjamini-Hochberg false-discovery rate alpha over all bins (default);
    bonferroni: family-wise error alpha (look-elsewhere correction);
    fixed: legacy rule, level > floor + fixed_db, no error control.
    Returns {'index', 'p_value', 'q_value'(bh only), 'tested'}.
    """
    p = np.asarray(power_linear, dtype=float)
    ok = np.isfinite(p)
    n = int(ok.sum())
    idx_all = np.flatnonzero(ok)
    if method == 'fixed':
        sel = idx_all[p[ok] > floor_mean * 10 ** (fixed_db / 10)]
        return {'index': sel, 'p_value': survival(p[sel], floor_mean, dof), 'tested': n, 'method': method}
    pv = survival(p[ok], floor_mean, dof)
    if method == 'bonferroni':
        keep = pv <= alpha / max(n, 1)
        return {'index': idx_all[keep], 'p_value': pv[keep], 'tested': n, 'method': method}
    if method != 'bh':
        raise ValueError('method must be bh, bonferroni or fixed')
    order = np.argsort(pv)
    ranked = pv[order] * n / np.arange(1, n + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    keep_sorted = q <= alpha
    chosen = order[keep_sorted]
    sel = np.sort(chosen)
    qv = np.empty(n)
    qv[order] = q
    return {'index': idx_all[sel], 'p_value': pv[sel], 'q_value': qv[sel], 'tested': n, 'method': method}


def group_peaks(index, gap=1):
    """Merge adjacent detected bins into peaks; returns a list of index arrays."""
    index = np.sort(np.asarray(index, dtype=int))
    if index.size == 0:
        return []
    splits = np.flatnonzero(np.diff(index) > gap) + 1
    return np.split(index, splits)


# ----------------------------------------------------- LO diversity / drive scaling

def classify_lo_slope(lo_hz, if_hz, orders=(1, 3, 5, 7), resolution_hz=None):
    """Classify one tone from where its IF lands at several LO settings.

    A source at f appears at IF = f - n*LO (RF-fixed signal n=1, slope -1), at
    IF = -(f - LO) as its IQ image (slope +1) and at IF = f - n*LO for odd n > 1 (LO harmonics),
    or with the opposite sign when the harmonic folds. The best law is the one whose line
    through the (LO, IF) points leaves the smallest residual; its slope must be consistent with
    one candidate within the tolerance, otherwise the call is 'ambiguous'. Fewer than 3 LO settings
    or an LO span below 4 bins makes the slopes indistinguishable: status 'unidentifiable'.
    """
    lo = np.asarray(lo_hz, dtype=float)
    f = np.asarray(if_hz, dtype=float)
    if lo.size < 3 or np.ptp(lo) <= 0:
        return {'status': 'unidentifiable', 'reason': 'needs >= 3 distinct LO settings'}
    res = float(resolution_hz) if resolution_hz else 0.0
    span = float(np.ptp(lo))
    if res and span < 4 * res:
        return {'status': 'unidentifiable', 'reason': 'LO span below four bins'}
    laws = [(1, -1)] + [(n, s) for n in orders for s in (-1, 1) if (n, s) != (1, -1)]
    sigma = max(res, 1e-9) / math.sqrt(12.0) if res else 1e-9
    best = None
    scores = []
    for n, s in laws:
        slope = -s * n  # law (n, s): IF = s*(f - n*LO), so d(IF)/d(LO) = -s*n
        offset = np.mean(f - slope * lo)
        resid = f - (offset + slope * lo)
        rms = float(np.sqrt(np.mean(resid ** 2)))
        scores.append((rms, n, s))
    scores.sort(key=lambda r: (round(r[0] / max(sigma * 4, 1.0), 3), 0 if r[1] == 1 and r[2] == 1 else 1, r[1]))
    rms, n, s = scores[0]
    # the runner-up must be clearly worse, otherwise the labelling is ambiguous
    second = scores[1][0]
    tolerance = max(res, 1.0) * 1.5
    status = 'classified' if rms <= tolerance else 'no_law_fits'
    if status == 'classified' and second <= tolerance:
        status = 'ambiguous'
    kind = 'signal' if (n, s) == (1, 1) else ('image' if n == 1 else 'lo_harmonic')
    return {'status': status, 'order': n, 'sign': s, 'kind': kind, 'rms_residual_hz': rms,
            'runner_up': {'order': scores[1][1], 'sign': scores[1][2], 'rms_residual_hz': second},
            'source_hz': float(np.mean(s * f + n * lo)),
            'slope_hz_per_hz': -s * n, 'lo_points': int(lo.size)}


def drive_order(drive_db, level_db):
    """Nonlinearity order of a feature from its level change against the input drive (dB per dB).

    A linear response has slope 1, an n-th order intermodulation product slope n. Returns the
    least-squares slope, its standard error and the nearest integer order when the slope is
    within 0.35 of it.
    """
    x = np.asarray(drive_db, dtype=float)
    y = np.asarray(level_db, dtype=float)
    if x.size < 2 or np.ptp(x) < 1.0:
        return {'status': 'unidentifiable', 'reason': 'needs >= 2 drive levels spanning >= 1 dB'}
    xm, ym = x.mean(), y.mean()
    sxx = float(((x - xm) ** 2).sum())
    slope = float(((x - xm) * (y - ym)).sum() / sxx)
    se = None
    if x.size > 2:
        resid = y - ym - slope * (x - xm)
        se = float(math.sqrt((resid ** 2).sum() / (x.size - 2) / sxx))
    nearest = int(round(slope))
    return {'status': 'measured', 'slope_db_per_db': slope, 'slope_se': se,
            'order': nearest if nearest >= 1 and abs(slope - nearest) <= 0.35 else None}


# ----------------------------------------------------- floor vs temperature (Kalman)

def _kalman_run(r, x, dt, sigma, q, prior_sd):
    """Filter + RTS smoother for r_k = a_k + b x_k + e. Returns a(t), b, sd(b), log-likelihood, mean NIS."""
    n = r.size
    m = np.array([r[0], 0.0])
    P = np.diag([max(sigma ** 2, 1e-6) * 10, prior_sd ** 2])
    ms = np.empty((n, 2)); Ps = np.empty((n, 2, 2)); mp = np.empty((n, 2)); Pp = np.empty((n, 2, 2))
    ll = 0.0
    nis = 0.0
    for k in range(n):
        if k:
            P = P + np.diag([q * q * dt[k], 0.0])
        mp[k], Pp[k] = m, P
        H = np.array([1.0, x[k]])
        PH = P @ H
        S = float(H @ PH) + sigma ** 2
        K = PH / S
        innov = r[k] - float(H @ m)
        ll -= 0.5 * (math.log(2 * math.pi * S) + innov * innov / S)
        nis += innov * innov / S
        m = m + K * innov
        P = P - np.outer(K, PH)
        ms[k], Ps[k] = m, P
    sm = ms.copy(); sP = Ps.copy()
    for k in range(n - 2, -1, -1):
        C = Ps[k] @ np.linalg.inv(Pp[k + 1] + 1e-18 * np.eye(2))
        sm[k] = ms[k] + C @ (sm[k + 1] - mp[k + 1])
        sP[k] = Ps[k] + C @ (sP[k + 1] - Pp[k + 1]) @ C.T
    return sm[:, 0], float(sm[-1, 1]), float(math.sqrt(max(sP[-1, 1, 1], 0.0))), ll, nis / n


def floor_temperature_kalman(times_s, temps_c, floor_db, groups, sigma_db=None, slope_prior_sd=0.5,
                             drift_grid=None, iterations=3):
    """Floor-versus-temperature slope with a slowly drifting offset (Kalman filter + RTS smoother).

    Model: y_k = c[g_k] + a(t_k) + b (T_k - T_ref) + e_k, e ~ N(0, sigma^2).
    c[g]: per-group (hop centre) offsets, re-estimated alternately so RF content cancels;
    a(t): random walk, process sd q per sqrt(second): slow drift not caused by temperature;
    b: constant slope with a weak Gaussian prior. q is chosen on a grid by marginal likelihood;
    q = 0 is the within-group OLS slope.

    A temperature that only rises (or only falls) over the run cannot be told apart from a slow drift,
    whatever the estimator: `confounded` is True when |corr(T, time)| > 0.9, and then neither the slope
    nor slope_sd_db is trustworthy (they assume no unmodelled drift correlated with the trend).
    `slope_range_db` spans the slopes of every drift setting within 2 log-likelihood units of the best.
    """
    t = np.asarray(times_s, dtype=float)
    temp = np.asarray(temps_c, dtype=float)
    y = np.asarray(floor_db, dtype=float)
    g = np.asarray(groups)
    ok = np.isfinite(t) & np.isfinite(temp) & np.isfinite(y)
    order = np.argsort(t[ok], kind='stable')
    t, temp, y, g = t[ok][order], temp[ok][order], y[ok][order], g[ok][order]
    if t.size < 8 or np.ptp(temp) <= 0:
        return {'status': 'insufficient_temperature_variation', 'records': int(t.size)}
    tref = float(temp.mean())
    x = temp - tref
    labels, gi = np.unique(g, return_inverse=True)
    dt = np.diff(t, prepend=t[0])
    span = max(float(np.ptp(t)), 1.0)
    if drift_grid is None:
        drift_grid = [0.0] + list(np.geomspace(1e-4, 1.0, 9) / math.sqrt(span) * 3)
    results = []
    for q in drift_grid:
        c = np.zeros(labels.size)
        sigma = float(sigma_db) if sigma_db else 0.1
        b = 0.0
        for _ in range(iterations):
            r = y - c[gi]
            a, b, sd, ll, nis = _kalman_run(r, x, dt, sigma, q, slope_prior_sd)
            resid = y - a - b * x
            c = np.array([resid[gi == j].mean() for j in range(labels.size)])
            c -= c.mean()
            if not sigma_db:
                sigma = float(max(np.std(y - c[gi] - a - b * x), 1e-4))
        results.append({'q': float(q), 'slope': b, 'sd': sd, 'll': ll, 'nis': nis, 'a': a, 'sigma': sigma})
    best = max(results, key=lambda r: r['ll'])
    near = [r['slope'] for r in results if best['ll'] - r['ll'] <= 2.0]
    a = best['a']
    corr = float(np.corrcoef(a, x)[0, 1]) if np.ptp(a) > 0 and np.ptp(x) > 0 else 0.0
    tcorr = float(np.corrcoef(t, x)[0, 1]) if np.ptp(t) > 0 else 0.0
    return {'status': 'fit', 'slope_db_per_c': best['slope'], 'slope_sd_db': best['sd'],
            'slope_range_db': [float(min(near)), float(max(near))], 'drift_db_per_sqrt_s': best['q'],
            'drift_range_db': float(np.ptp(a)), 'confounded': bool(abs(tcorr) > 0.9),
            'temperature_time_correlation': tcorr, 'drift_temperature_correlation': corr, 'sigma_db': best['sigma'], 'mean_nis': best['nis'],
            'reference_temperature_c': tref, 'records': int(t.size), 'groups': int(labels.size),
            'scope': 'relative level, not calibrated; slope valid for this receiver state and sensor'}
