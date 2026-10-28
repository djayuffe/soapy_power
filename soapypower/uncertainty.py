"""Explicit standard dB uncertainty components and user-declared correlation."""
import math
import numpy as np
from soapypower.numerics import finite

COMPONENTS = ('reference_source_db', 'repeatability_db', 'interpolation_db', 'drift_db', 'mismatch_db')


def evaluate_budget(budget):
    if not isinstance(budget, dict) or set(budget) - {'components', 'independent', 'correlation', 'coverage_factor'}:
        raise ValueError('invalid uncertainty budget fields')
    components = budget.get('components')
    if not isinstance(components, dict) or set(components) != set(COMPONENTS):
        raise ValueError('uncertainty components must include all five named terms; use null for unknown')
    values, missing = [], []
    for name in COMPONENTS:
        value = components[name]
        if value is None:
            missing.append(name)
            values.append(0)
        else:
            values.append(finite(value, name, nonnegative=True))
    if budget.get('independent') is True:
        if 'correlation' in budget:
            raise ValueError('choose independent components or a correlation matrix')
        correlation = np.eye(len(COMPONENTS))
    else:
        try:
            correlation = np.asarray(budget.get('correlation'), dtype=float)
        except (TypeError, ValueError):
            raise ValueError('invalid uncertainty correlation matrix') from None
        if (correlation.shape != (5, 5) or not np.isfinite(correlation).all()
                or np.any(abs(correlation) > 1) or not np.allclose(correlation, correlation.T, rtol=0, atol=1e-12)
                or not np.allclose(np.diag(correlation), 1, rtol=0, atol=1e-12)
                or np.linalg.eigvalsh(correlation).min() < -1e-12):
            raise ValueError('correlation must be a symmetric positive-semidefinite 5x5 unit-diagonal matrix')
    coverage = finite(budget.get('coverage_factor'), 'coverage factor', positive=True)
    combined = None
    if not missing:
        scale = max(values)
        if scale == 0:
            combined = 0.0
        else:
            scaled = np.asarray(values) / scale
            combined = scale * math.sqrt(max(0.0, float(scaled @ correlation @ scaled)))
        if not math.isfinite(combined) or not math.isfinite(combined * coverage):
            raise ValueError('combined uncertainty is outside float64 range')
    return {'status': 'complete_declared_calibration_budget' if not missing else 'incomplete',
            'combined_standard_db': combined, 'expanded_db': None if combined is None else combined * coverage,
            'coverage_factor': coverage, 'missing_components': missing,
            'components': dict(components), 'correlation': correlation.tolist(),
            'measurement_statistics_included': False,
            'coverage_probability': None,
            'scope': 'user-supplied calibration terms; not automatic evidence of RF accuracy or a confidence interval'}
