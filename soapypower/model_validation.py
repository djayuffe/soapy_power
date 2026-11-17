"""Hardware-independent validation of the scan measurement model.

Observation model per RF cell c seen through hop h at baseband (IF) offset f:

    D[c, h] = S[c] + G[f(c, h)] + e                (dB, uncalibrated)

S is the unknown received RF level (nuisance), G the relative IF response of the
receiver in one fixed state. Redundant overlapping hops identify G up to one
additive constant per connected component of the (cell, IF-slot) graph. Every
result is *relative*: no absolute gain, dBm, noise figure or traceability is
implied, and validation is always on acquisitions not used for fitting.

Complex-IQ model (needs raw IQ or a tone PSD):  y = a x + b conj(x) + d + n.
"""
import json
from pathlib import Path
import math

import numpy as np

from soapypower.records import read_records

CONTEXT_KEYS = ('driver', 'hardware', 'serial', 'channel', 'antenna', 'gains', 'agc',
                'sample_rate', 'bandwidth', 'clock_source', 'time_source',
                'master_clock_rate', 'dc_correction', 'iq_correction', 'manual_dc',
                'manual_iq', 'lnb_lo', 'lnb_high_side', 'iq_conjugate', 'sample_format',
                'hardware_info', 'device_settings', 'tune_args')


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(name + ' must be a positive finite number')
    return float(value)


def _nan_list(values):
    return [None if not math.isfinite(v) else float(v) for v in values]


def _array(values):
    return np.array([np.nan if v is None else v for v in values], dtype=float)


def baseband_axis(record):
    """Baseband offsets of each bin and the RF origin of the hop.

    Uses the recorded frequency grid (rf_origin_hz, frequency_sign), which already
    includes converter-LO and IQ-conjugation mapping. Records without it fall back
    to the coverage midpoint, which is half a bin away from DC for even FFTs.
    """
    meta = record.get('metadata') if isinstance(record.get('metadata'), dict) else {}
    first, step, size = record['first_bin_center_hz'], record['bin_width_hz'], len(record['power'])
    freqs = first + step * np.arange(size)
    grid = meta.get('frequency_grid')
    if isinstance(grid, dict) and isinstance(grid.get('rf_origin_hz'), (int, float)) and math.isfinite(grid['rf_origin_hz']):
        sign = meta.get('frequency_sign', 1)
        if sign not in (1, -1):
            raise ValueError('invalid frequency_sign in record metadata')
        origin = float(grid['rf_origin_hz'])
        return freqs, sign * (freqs - origin), origin, 'frequency_grid'
    origin = .5 * (record['coverage_start_hz'] + record['coverage_stop_hz'])
    return freqs, freqs - origin, origin, 'coverage_midpoint'


def _levels(record):
    meta = record.get('metadata') if isinstance(record.get('metadata'), dict) else {}
    level = np.array([np.nan if v is None else v for v in record['power']], dtype=float)
    estimated = record.get('estimated_bins', meta.get('estimated_bins', []))
    if estimated:
        level[np.asarray(estimated, dtype=int)] = np.nan  # interpolated, not measured
    return level


# --------------------------------------------------------------------------- loading

class HopStore:
    """Per-hop, per-pass cell levels (dB) on a common RF cell grid."""

    def __init__(self, start_hz, stop_hz, cell_hz, edge_fraction=.12, dc_mask_hz=0.0):
        self.start = float(start_hz)
        self.stop = float(stop_hz)
        self.cell = _positive(cell_hz, 'cell_hz')
        if not (math.isfinite(self.start) and math.isfinite(self.stop)) or not self.stop > self.start:
            raise ValueError('stop must exceed start')
        if not 0 <= edge_fraction < .5 or not 0 <= dc_mask_hz < math.inf:
            raise ValueError('invalid edge fraction / DC mask')
        self.edge, self.dc = float(edge_fraction), float(dc_mask_hz)
        self.cells = int(math.ceil((self.stop - self.start) / self.cell))
        if self.cells < 2 or self.cells > 200000:
            raise ValueError('cell grid must have 2..200000 cells')
        self.hops = {}
        self.signature = None
        self.records = 0
        self.max_if = 0.0
        self.origin_sources = set()
        self.telemetry = []   # per-record telemetry/LO-mapping observations (see hop_telemetry)
        self.source = None    # label of the file being added (from_files)

    def cell_center(self, index):
        return self.start + (np.asarray(index) + .5) * self.cell

    def add(self, record):
        meta = record.get('metadata') if isinstance(record.get('metadata'), dict) else {}
        if record['unit'] != 'dB(input power/Hz)':
            raise ValueError('model validation needs uncalibrated logarithmic PSD')
        if meta.get('measurement_status') != 'complete' or meta.get('continuity') != 'verified':
            raise ValueError('incomplete or unverified acquisition in input')
        ctx = meta.get('context')
        gains = ctx.get('gains') if isinstance(ctx, dict) else None
        if (not isinstance(ctx, dict) or ctx.get('agc') is not False or not isinstance(gains, dict) or not gains
                or any(isinstance(g, bool) or not isinstance(g, (int, float)) or not math.isfinite(g) for g in gains.values())):
            raise ValueError('model validation requires an observable fixed-gain context')
        if self.dc and ctx.get('tune_args'):
            raise ValueError('centre exclusion with tuning arguments needs a measured LO/DSP mapping')
        signature = json.dumps({k: ctx.get(k) for k in CONTEXT_KEYS}, sort_keys=True)
        if self.signature is not None and signature != self.signature:
            raise ValueError('receiver context changed within the validation input')
        self.signature = signature
        freqs, baseband, origin, source = baseband_axis(record)
        self.origin_sources.add(source)
        level = _levels(record)
        half = .5 * (record['coverage_stop_hz'] - record['coverage_start_hz'])
        keep = (np.abs(baseband) <= half * (1 - 2 * self.edge)) & np.isfinite(level)
        if self.dc:
            keep &= np.abs(baseband) >= self.dc / 2
        keep &= (freqs >= self.start) & (freqs < self.stop)
        if keep.any():
            self.max_if = max(self.max_if, float(np.abs(baseband[keep]).max()))
        key = int(round(origin))
        hop = self.hops.setdefault(key, {'center': origin, 'passes': []})
        row = np.full(self.cells, np.nan)
        if keep.any():
            index = np.floor((freqs[keep] - self.start) / self.cell).astype(int)
            index = np.clip(index, 0, self.cells - 1)
            linear = 10 ** (level[keep] / 10)
            sums = np.bincount(index, weights=linear, minlength=self.cells)
            counts = np.bincount(index, minlength=self.cells)
            need = max(1, int(.5 * self.cell / record['bin_width_hz']))
            good = counts >= need
            row[good] = 10 * np.log10(sums[good] / counts[good])
        hop['passes'].append(row)
        self.records += 1
        entry = hop_telemetry(meta)
        if entry:
            finite_row = row[np.isfinite(row)]
            entry.update(key=key, center_hz=float(origin), pass_index=len(hop['passes']) - 1, source=self.source,
                         time_s=_timestamp(record.get('time_start')),
                         floor_db=float(np.median(finite_row)) if finite_row.size else None)
            self.telemetry.append(entry)

    @classmethod
    def from_files(cls, paths, start_hz, stop_hz, cell_hz, **options):
        store = cls(start_hz, stop_hz, cell_hz, **options)
        if isinstance(paths, (str, bytes)) or hasattr(paths, '__fspath__'):
            paths = [paths]
        for path in paths:
            store.source = Path(str(path)).name
            for record in read_records(path):
                store.add(record)
        if not store.hops:
            raise ValueError('no usable records')
        return store

    def subset(self, passes):
        """New store with only the selected pass indices of every hop."""
        other = HopStore(self.start, self.stop, self.cell, self.edge, self.dc)
        other.signature, other.max_if, other.origin_sources = self.signature, self.max_if, set(self.origin_sources)
        for key, hop in self.hops.items():
            rows = [hop['passes'][i] for i in passes if i < len(hop['passes'])]
            if rows:
                other.hops[key] = {'center': hop['center'], 'passes': rows}
                other.records += len(rows)
        return other

    def pass_count(self):
        return min(len(h['passes']) for h in self.hops.values())

    def medians(self, passes=None):
        """Per-hop nan-median over the selected pass indices -> {key: (center, row)}."""
        out = {}
        for key, hop in self.hops.items():
            rows = hop['passes'] if passes is None else [hop['passes'][i] for i in passes if i < len(hop['passes'])]
            if not rows:
                continue
            stack = np.vstack(rows)
            seen = np.isfinite(stack).any(axis=0)
            row = np.full(stack.shape[1], np.nan)
            if seen.any():
                row[seen] = np.nanmedian(stack[:, seen], axis=0)
            out[key] = (hop['center'], row)
        return out


# ------------------------------------------------------------------ relative IF model

def component_labels(matrix):
    """Connected-component label per IF slot (-1 = unobserved); slots are linked by shared cells."""
    seen = np.isfinite(matrix)
    parent = list(range(matrix.shape[1]))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for row in seen:
        idx = np.flatnonzero(row)
        root = find(idx[0]) if idx.size else None
        for j in idx[1:]:
            parent[find(j)] = root
    labels = np.full(matrix.shape[1], -1)
    observed = np.flatnonzero(seen.any(axis=0))
    roots = {}
    for j in observed:
        labels[j] = roots.setdefault(find(j), len(roots))
    return labels


def additive_fit(matrix, iterations=25, tolerance=1e-9, huber_k=1.345, min_scale_db=0.02):
    """Robust two-way fit matrix[i, j] = rows[i] + cols[j] + residual on gapped data.

    Each IRLS step solves the weighted least-squares problem exactly: rows are
    eliminated analytically and the column Schur complement is solved with a
    minimum-norm least-squares solve (one free constant per connected component,
    each component's columns sum to zero). Huber weights use a robust residual
    scale, so tones/bursts seen in only some IF slots are down-weighted.
    """
    m = np.asarray(matrix, dtype=float)
    seen = np.isfinite(m)
    y = np.where(seen, m, 0.0)
    w = seen.astype(float)
    cols = np.zeros(m.shape[1])
    for _ in range(iterations):
        row_sum = w.sum(axis=1)
        inverse = np.where(row_sum > 0, 1 / np.where(row_sum > 0, row_sum, 1), 0.0)
        wy = w * y
        projected = w.T * inverse
        schur = np.diag(w.sum(axis=0)) - projected @ w
        rhs = wy.sum(axis=0) - projected @ wy.sum(axis=1)
        new = np.linalg.lstsq(schur, rhs, rcond=None)[0]
        rows = inverse * (wy.sum(axis=1) - w @ new)
        residual = np.where(seen, y - rows[:, None] - new[None, :], 0.0)
        change = float(np.abs(new - cols).max(initial=0))
        cols = new
        scale = max(min_scale_db, 1.4826 * float(np.median(np.abs(residual[seen])))) if seen.any() else min_scale_db
        w = np.where(seen, 1.0 / np.maximum(1.0, np.abs(residual) / (huber_k * scale)), 0.0)
        if change < tolerance:
            break
    return rows, cols, np.where(seen, residual, np.nan)


def _design(store, medians, if_bin_hz, max_if_hz):
    """Rows = RF cells, columns = IF slots. Several hops landing in one slot are averaged (dB)."""
    half = int(math.ceil(max_if_hz / if_bin_hz))
    ncol = 2 * half + 1
    total = np.zeros((store.cells, ncol))
    count = np.zeros((store.cells, ncol))
    centers = store.cell_center(np.arange(store.cells))
    for _, (origin, row) in medians.items():
        col = np.rint((centers - origin) / if_bin_hz).astype(int) + half
        ok = np.isfinite(row) & (col >= 0) & (col < ncol)
        idx = np.flatnonzero(ok)
        np.add.at(total, (idx, col[idx]), row[idx])
        np.add.at(count, (idx, col[idx]), 1)
    with np.errstate(invalid='ignore', divide='ignore'):
        matrix = np.where(count > 0, total / np.where(count > 0, count, 1), np.nan)
    return matrix, (np.arange(ncol) - half) * if_bin_hz


def overlap_redundancy(matrix):
    """Observed IF slots per RF cell."""
    return np.isfinite(matrix).sum(axis=1)


def fit_if_response(store, passes=None, if_bin_hz=None, max_if_hz=None, min_observations=3):
    """Relative IF response (dB, zero at the anchor slot) from overlap redundancy.

    Only slots in the anchor's connected component are reported; others are None
    because their offset relative to the anchor is not identified by the data.
    """
    if_bin_hz = _positive(if_bin_hz or store.cell, 'IF bin')
    max_if_hz = _positive(max_if_hz or store.max_if or if_bin_hz, 'maximum IF')
    matrix, axis = _design(store, store.medians(passes), if_bin_hz, max_if_hz)
    redundancy = overlap_redundancy(matrix)
    usable = redundancy >= min_observations
    if usable.sum() < 8:
        return {'status': 'insufficient_overlap', 'usable_cells': int(usable.sum()),
                'median_redundancy': float(np.median(redundancy)) if redundancy.size else 0.0}
    sub = matrix[usable]
    labels = component_labels(sub)
    _, cols, residual = additive_fit(sub)
    observed = labels >= 0
    sizes = np.bincount(labels[observed])
    main = int(np.argmax(sizes))
    anchor = int(np.argmin(np.where(labels == main, np.abs(axis), np.inf)))
    response = np.where(labels == main, cols - cols[anchor], np.nan)
    counts = np.isfinite(residual).sum(axis=0)
    se = np.full(len(axis), np.nan)
    for j in np.flatnonzero((labels == main) & (counts > 1)):
        r = residual[:, j]
        r = r[np.isfinite(r)]
        se[j] = 1.4826 * np.median(np.abs(r)) / math.sqrt(r.size)
    return {'status': 'fit', 'if_axis_hz': axis.tolist(), 'response_db': _nan_list(response),
            'response_se_db': _nan_list(se), 'usable_cells': int(usable.sum()),
            'median_redundancy': float(np.median(redundancy[usable])),
            'identifiable_components': int(sizes.size), 'fully_identified': bool(sizes.size == 1),
            'identified_slots': int((labels == main).sum()), 'observed_slots': int(observed.sum()),
            'residual_mad_db': float(1.4826 * np.nanmedian(np.abs(residual))),
            'anchor_if_hz': float(axis[anchor]), 'if_bin_hz': if_bin_hz, 'max_if_hz': max_if_hz,
            'origin_sources': sorted(store.origin_sources)}


def apply_if_response(matrix, response):
    return np.asarray(matrix, dtype=float) - _array(response)[None, :]


def _spread(matrix, keep):
    """Median across kept cells of the std across IF slots: the apparent RF dispersion."""
    if not keep.any():
        return float('nan')
    rows = matrix[keep]
    return float(np.median([np.std(r[np.isfinite(r)]) for r in rows]))


def compare_responses(first, second):
    """Gauge-free disagreement of two fitted responses over their common slots."""
    a, b = _array(first['response_db']), _array(second['response_db'])
    if len(a) != len(b) or first['if_axis_hz'] != second['if_axis_hz']:
        raise ValueError('responses use different IF axes')
    common = np.isfinite(a) & np.isfinite(b)
    if common.sum() < 4:
        return {'common_slots': int(common.sum()), 'rms_db': None, 'max_abs_db': None}
    diff = a[common] - b[common]
    diff -= np.median(diff)
    se = np.hypot(_array(first.get('response_se_db', [None] * len(a)))[common],
                  _array(second.get('response_se_db', [None] * len(b)))[common])
    normalized = diff[np.isfinite(se) & (se > 0)] / se[np.isfinite(se) & (se > 0)]
    return {'common_slots': int(common.sum()), 'rms_db': float(np.sqrt(np.mean(diff ** 2))),
            'max_abs_db': float(np.abs(diff).max()), 'p95_abs_db': float(np.percentile(np.abs(diff), 95)),
            'rms_normalized': float(np.sqrt(np.mean(normalized ** 2))) if normalized.size else None}


def _row_std(matrix, keep):
    """Per-RF-cell std across IF slots (NaN where not kept)."""
    out = np.full(matrix.shape[0], np.nan)
    for i in np.flatnonzero(keep):
        values = matrix[i][np.isfinite(matrix[i])]
        out[i] = np.std(values)
    return out


def _median_sd(values, resamples=200, seed=0):
    """Bootstrap standard deviation of the median (fixed seed: analyses are reproducible)."""
    values = np.asarray(values, float)
    if values.size < 2:
        return float('nan')
    rng = np.random.default_rng(seed)
    return float(np.std([np.median(values[rng.integers(0, values.size, values.size)]) for _ in range(resamples)]))


def holdout_validation(fit_store, test_store, if_bin_hz=None, max_if_hz=None, min_observations=3,
                       max_disagreement_db=0.25, quiet_quantile=0.25, min_quiet_cells=50):
    """Fit on one store, refit independently on held-out acquisitions, compare.

    Pass rules: the fit store identifies one component; the independent refit
    reproduces the response (gauge-free RMS <= max_disagreement_db); applying the
    fit to the holdout does not increase the IF-slot dispersion of quiet RF cells
    beyond the response's own estimation noise plus two bootstrap standard
    deviations of that paired statistic. Quiet cells are the lowest quiet_quantile
    of dispersion in the *fit* store, so their selection is independent of the
    holdout data. With an antenna, time-varying signals dominate the dispersion of
    most cells; there an all-cell comparison is reported (with its bootstrap SD)
    but cannot decide. A flat receiver passes with no significant improvement; a
    changed receiver fails.
    """
    if (fit_store.start, fit_store.stop, fit_store.cell) != (test_store.start, test_store.stop, test_store.cell):
        raise ValueError('fit and test stores must share the same cell grid')
    if_bin_hz = if_bin_hz or fit_store.cell
    max_if_hz = max_if_hz or max(fit_store.max_if, test_store.max_if)
    fit = fit_if_response(fit_store, None, if_bin_hz, max_if_hz, min_observations)
    refit = fit_if_response(test_store, None, if_bin_hz, max_if_hz, min_observations)
    if fit['status'] != 'fit' or refit['status'] != 'fit':
        return {'status': 'not_fitted', 'passed': False, 'fit_status': fit['status'], 'refit_status': refit['status']}
    agreement = compare_responses(fit, refit)
    response = _array(fit['response_db'])
    matrix, _ = _design(test_store, test_store.medians(), if_bin_hz, max_if_hz)
    matrix[:, ~np.isfinite(response)] = np.nan  # compare before/after on identical observations
    keep = np.isfinite(matrix).sum(axis=1) >= min_observations
    before_rows = _row_std(matrix, keep)
    after_rows = _row_std(apply_if_response(matrix, fit['response_db']), keep)
    before, after = float(np.nanmedian(before_rows)), float(np.nanmedian(after_rows))
    drift = level_drift(fit_store, test_store)
    # Subtracting an estimated response adds its own estimation noise; a correct but
    # unnecessary correction (flat receiver) may raise the spread by about that much.
    se = _array(fit['response_se_db'])
    se_typical = float(np.median(se[np.isfinite(se)])) if np.isfinite(se).any() else 0.0
    reference, _ = _design(fit_store, fit_store.medians(), if_bin_hz, max_if_hz)
    reference[:, ~np.isfinite(response)] = np.nan
    reference_rows = _row_std(reference, np.isfinite(reference).sum(axis=1) >= min_observations)
    candidates = keep & np.isfinite(reference_rows)
    quiet = candidates & (reference_rows <= np.quantile(reference_rows[candidates], quiet_quantile)) if candidates.any() else candidates
    selection = 'fit-store quiet quantile'
    if quiet.sum() < min_quiet_cells:
        quiet, selection = keep, 'all cells (too few quiet cells)'
    paired = after_rows[quiet] - before_rows[quiet]
    quiet_before = float(np.median(before_rows[quiet])) if quiet.any() else float('nan')
    paired_median = float(np.median(paired)) if paired.size else float('nan')
    paired_sd = _median_sd(paired)
    allowed_increase = (math.sqrt(quiet_before ** 2 + se_typical ** 2) - quiet_before + 2 * paired_sd
                        if math.isfinite(quiet_before) and math.isfinite(paired_sd) else float('nan'))
    passed = bool(fit['fully_identified'] and agreement['rms_db'] is not None
                  and agreement['rms_db'] <= max_disagreement_db and paired_median <= allowed_increase)
    return {'status': 'validated', 'passed': passed, 'agreement': agreement,
            'max_disagreement_db': max_disagreement_db,
            'quiet_cells': {'selection': selection, 'quantile': quiet_quantile, 'cells': int(quiet.sum()),
                            'spread_before_db': quiet_before,
                            'spread_after_db': float(np.median(after_rows[quiet])) if quiet.any() else float('nan'),
                            'paired_median_increase_db': paired_median, 'paired_sd_db': paired_sd,
                            'allowed_increase_db': allowed_increase},
            'spread_before_db': before, 'spread_after_db': after, 'improvement_db': before - after,
            'improvement_sd_db': math.hypot(_median_sd(before_rows[keep], seed=1), _median_sd(after_rows[keep], seed=2)),
            'fully_identified': fit['fully_identified'], 'refit_fully_identified': refit['fully_identified'],
            'level_drift': drift, 'fit': fit,
            'scope': 'relative IF response, independent holdout acquisitions; not absolute calibration'}


def level_drift(first, second):
    """Median cell-level change between two stores for identical hops (receiver + RF environment)."""
    a, b = first.medians(), second.medians()
    diffs = [b[k][1] - a[k][1] for k in a if k in b]
    d = np.concatenate([x[np.isfinite(x)] for x in diffs]) if diffs else np.array([])
    if d.size < 8:
        return {'status': 'no_common_hops', 'cells': int(d.size)}
    return {'status': 'measured', 'cells': int(d.size), 'median_db': float(np.median(d)),
            'p95_abs_db': float(np.percentile(np.abs(d - np.median(d)), 95)),
            'scope': 'includes RF environment changes; a stable physical input isolates receiver drift'}


def repeatability(store, max_sigma_db=None, min_cells=8):
    """Robust single-measurement sigma from two disjoint halves of the passes (even/odd)."""
    n = store.pass_count()
    if n < 2:
        return {'status': 'need_two_passes', 'passed': False}
    a = store.medians(list(range(0, n, 2)))
    b = store.medians(list(range(1, n, 2)))
    diffs = [a[k][1] - b[k][1] for k in a if k in b]
    d = np.concatenate([x[np.isfinite(x)] for x in diffs]) if diffs else np.array([])
    if d.size < min_cells:
        return {'status': 'insufficient_cells', 'passed': False, 'cells': int(d.size)}
    sigma = float(1.4826 * np.median(np.abs(d - np.median(d))) / math.sqrt(2))
    result = {'status': 'measured', 'cells': int(d.size), 'bias_db': float(np.median(d)),
              'sigma_db': sigma, 'p95_abs_db': float(np.percentile(np.abs(d), 95)), 'passes': n,
              'scope': 'includes RF environment variation between passes'}
    if max_sigma_db is not None:
        result.update(max_sigma_db=max_sigma_db, passed=bool(sigma <= max_sigma_db))
    return result


# ------------------------------------------------------------------- cell levels

def cell_levels(store, response=None, if_bin_hz=None, max_if_hz=None, exclude=None):
    """Per-RF-cell level across every hop/IF position that saw the cell (dB).

    With a fitted relative IF response the observations are corrected first, so the
    median is an estimate of S[c] up to the response's single gauge constant.
    exclude: {hop key: boolean cell mask} of observations to leave out (e.g. harmonic
    mixing responses); the remaining hops still estimate those cells.
    """
    if_bin_hz = if_bin_hz or store.cell
    max_if_hz = max_if_hz or store.max_if or if_bin_hz
    medians = store.medians()
    for key, mask in (exclude or {}).items():
        if key in medians:
            row = medians[key][1].copy()
            row[np.asarray(mask, dtype=bool)] = np.nan
            medians[key] = (medians[key][0], row)
    matrix, _ = _design(store, medians, if_bin_hz, max_if_hz)
    if response is not None:
        matrix = apply_if_response(matrix, response)
    seen = np.isfinite(matrix).any(axis=1)
    out = {k: np.full(store.cells, np.nan) for k in ('median_db', 'p10_db', 'p90_db')}
    if seen.any():
        rows = matrix[seen]
        out['median_db'][seen] = np.nanmedian(rows, axis=1)
        out['p10_db'][seen] = np.nanpercentile(rows, 10, axis=1)
        out['p90_db'][seen] = np.nanpercentile(rows, 90, axis=1)
    result = {'rf_hz': store.cell_center(np.arange(store.cells)).tolist(), 'cell_hz': store.cell,
              'observations': np.isfinite(matrix).sum(axis=1).tolist(), 'if_corrected': response is not None}
    result.update({k: _nan_list(v) for k, v in out.items()})
    return result


def excess_regions(rf_hz, level_db, reference_db, threshold_db=3.0, cell_hz=None):
    """Contiguous cells where level - reference exceeds the threshold, strongest first."""
    rf = np.asarray(rf_hz, dtype=float)
    excess = _array(level_db) - (_array(reference_db) if np.ndim(reference_db) else float(reference_db))
    above = np.isfinite(excess) & (excess > threshold_db)
    cell = cell_hz or (float(np.median(np.diff(rf))) if rf.size > 1 else 0.0)
    edges = np.flatnonzero(np.diff(np.concatenate([[0], above.astype(int), [0]])))
    regions = []
    for start, stop in zip(edges[::2], edges[1::2] - 1):
        peak = start + int(np.nanargmax(excess[start:stop + 1]))
        regions.append({'start_hz': float(rf[start] - cell / 2), 'stop_hz': float(rf[stop] + cell / 2),
                        'peak_hz': float(rf[peak]), 'peak_excess_db': float(excess[peak]),
                        'cells': int(stop - start + 1)})
    return sorted(regions, key=lambda r: -r['peak_excess_db'])


def observation_residuals(store, response, levels):
    """Residual D - S - G for every (hop, pass, cell) observation.

    Returns arrays lo_hz, if_hz, rf_hz, residual_db, hop_key. A deterministic
    LO-dependent artefact shows as large residuals for some hops of a cell and none
    for others; a real RF signal raises every hop that sees the cell.
    """
    S = _array(levels['median_db'])
    G = _array(response['response_db'])
    axis = np.asarray(response['if_axis_hz'], dtype=float)
    step, half = axis[1] - axis[0], (len(axis) - 1) // 2
    centers = store.cell_center(np.arange(store.cells))
    parts = {k: [] for k in ('lo_hz', 'if_hz', 'rf_hz', 'residual_db', 'hop_key', 'cell')}
    for key, hop in store.hops.items():
        slots = np.rint((centers - hop['center']) / step).astype(int) + half
        valid = (slots >= 0) & (slots < len(axis))
        g = np.full(store.cells, np.nan)
        g[valid] = G[slots[valid]]
        for row in hop['passes']:
            r = row - S - g
            ok = np.flatnonzero(np.isfinite(r))
            parts['lo_hz'].append(np.full(ok.size, hop['center']))
            parts['if_hz'].append(centers[ok] - hop['center'])
            parts['rf_hz'].append(centers[ok])
            parts['residual_db'].append(r[ok])
            parts['hop_key'].append(np.full(ok.size, key))
            parts['cell'].append(ok)
    return {k: np.concatenate(v) if v else np.array([]) for k, v in parts.items()}


def mixing_hypotheses(lo_hz, if_hz, orders=(1, 3, 5, 7), bin_hz=0.5e6, min_los=3, min_events=50, allow_image=True):
    """Which mixing law explains a set of excess observations?

    Each law maps an observation to a source frequency f = n*LO - s*IF: (n=1, s=-1)
    is an ordinary RF-fixed signal, (n=1, s=+1) its IQ image, odd n > 1 the LO-harmonic
    responses of a direct-conversion mixer. Under the true law, observations made with
    different LO settings land on the same source frequency. Each observation is
    attributed to the law under which its source bin collects the most distinct LO
    settings (at least `min_los`); ties go to the RF-fixed law, so real signals are
    never explained away. A non-RF law is identified with >= `min_events` attributions.
    """
    lo = np.asarray(lo_hz, dtype=float)
    iff = np.asarray(if_hz, dtype=float)
    if lo.size < min_events:
        return {'status': 'too_few_events', 'events': int(lo.size), 'hypotheses': []}
    lo_id = np.unique(np.rint(lo), return_inverse=True)[1].reshape(-1)
    laws = [(1, -1)] + [(n, sign) for n in orders for sign in (-1, 1)
                        if (n, sign) != (1, -1) and (allow_image or (n, sign) != (1, 1))]
    bins_all, support = [], []
    for n, sign in laws:
        bins = np.rint((n * lo - sign * iff) / bin_hz).astype(np.int64)
        pairs = np.unique(np.stack([bins, lo_id]), axis=1)
        values, los = np.unique(pairs[0], return_counts=True)
        bins_all.append(bins)
        support.append(los[np.searchsorted(values, bins)])
    support = np.vstack(support)
    best = np.argmax(support, axis=0)  # first maximum: the RF-fixed law wins ties
    strong = support[best, np.arange(lo.size)] >= min_los
    result = []
    for index, (n, sign) in enumerate(laws):
        kind = 'rf' if index == 0 else ('image' if n == 1 else 'harmonic')
        mine = strong & (best == index)
        values, counts = np.unique(bins_all[index][mine], return_counts=True)
        bands = []
        for value in values:
            if bands and value - bands[-1][1] <= 1:
                bands[-1][1] = value
            else:
                bands.append([value, value])
        result.append({'order': n, 'sign': sign, 'kind': kind, 'attributed_events': int(mine.sum()),
                       'share': float(mine.mean()), 'max_distinct_los': int(support[index].max()),
                       'source_bands_hz': [[float((x - .5) * bin_hz), float((y + .5) * bin_hz)] for x, y in bands],
                       'top_sources_hz': [float(v * bin_hz) for v in values[np.argsort(-counts)[:6]]],
                       'identified': bool(kind != 'rf' and mine.sum() >= min_events)})
    result.sort(key=lambda h: (-h['identified'], -h['attributed_events']))
    return {'status': 'tested', 'events': int(lo.size), 'bin_hz': bin_hz, 'min_los': min_los, 'min_events': min_events,
            'unexplained_events': int((~strong).sum()), 'hypotheses': result}


def mixing_mask(store, hypotheses, guard_hz=0.25e6):
    """{hop key: cell mask} of observations an identified mixing law maps into a source band."""
    centers = store.cell_center(np.arange(store.cells))
    masks = {}
    for key, hop in store.hops.items():
        mask = np.zeros(store.cells, dtype=bool)
        iff = centers - hop['center']
        for h in hypotheses:
            if not h.get('identified'):
                continue
            f = h['order'] * hop['center'] - h['sign'] * iff
            for lo_band, hi_band in h['source_bands_hz']:
                mask |= (f >= lo_band - guard_hz) & (f <= hi_band + guard_hz)
        if mask.any():
            masks[key] = mask
    return masks


def image_parent_ok(law, levels, min_parent_db=20.0):
    """An IQ image needs a strong in-band parent at its source frequency.

    With image rejection of tens of dB, an image visible above the floor requires the
    parent cell to stand at least min_parent_db above the floor median; otherwise the
    image law is only an alias of another mechanism and is rejected.
    """
    rf = np.asarray(levels['rf_hz'], dtype=float)
    level = _array(levels['median_db'])
    floor = float(np.nanmedian(level))
    inside = np.zeros(rf.size, dtype=bool)
    for lo_band, hi_band in law['source_bands_hz']:
        inside |= (rf >= lo_band) & (rf <= hi_band)
    if not inside.any() or not np.isfinite(level[inside]).any():
        return False
    return bool(np.nanmax(level[inside]) - floor >= min_parent_db)


def spurious_responses(store, response, levels, threshold_db=6.0, iterations=4, min_parent_db=20.0, **options):
    """Identify LO-harmonic/image responses, mask them and re-estimate the cell levels.

    Excess is first measured against levels that the responses themselves inflate, so
    only the strongest are found; the identification is repeated against the masked
    levels until the mask stops growing. Returns (report, masked levels or None).
    """
    mask, current, identified, rounds = {}, levels, {}, []
    for _ in range(iterations):
        obs = observation_residuals(store, response, current)
        hot = obs['residual_db'] > threshold_db
        for key, cells in mask.items():
            hot &= ~((obs['hop_key'] == key) & cells[obs['cell']])
        tested = mixing_hypotheses(obs['lo_hz'][hot], obs['if_hz'][hot], **options)
        image = next((h for h in tested['hypotheses'] if h['kind'] == 'image' and h['identified']), None)
        if image is not None and not image_parent_ok(image, current, min_parent_db):
            tested = mixing_hypotheses(obs['lo_hz'][hot], obs['if_hz'][hot], allow_image=False, **options)
            tested['image_rejected'] = 'no in-band parent >= {:g} dB above the floor'.format(min_parent_db)
        rounds.append({'excess_observations': int(hot.sum()), 'image_rejected': tested.get('image_rejected'),
                       'identified': [(h['order'], h['sign'], h['attributed_events']) for h in tested['hypotheses'] if h['identified']]})
        grew = False
        for h in tested['hypotheses']:
            if not h['identified']:
                continue
            key = (h['order'], h['sign'])
            if key not in identified:
                identified[key] = {k: h[k] for k in ('order', 'sign', 'kind', 'top_sources_hz')}
                identified[key].update(source_bands_hz=[], attributed_events=0, identified=True)
            law = identified[key]
            law['attributed_events'] = max(law['attributed_events'], h['attributed_events'])
            new = [band for band in h['source_bands_hz'] if band not in law['source_bands_hz']]
            if new:
                law['source_bands_hz'] = sorted(law['source_bands_hz'] + new)
                grew = True
        if not grew:
            break
        mask = mixing_mask(store, list(identified.values()))
        current = cell_levels(store, response['response_db'], exclude=mask)
    laws = sorted(identified.values(), key=lambda h: -h['attributed_events'])
    report = {'status': 'tested', 'threshold_db': threshold_db, 'rounds': rounds, 'identified': laws,
              'first_round': tested if len(rounds) == 1 else None,
              'scope': 'mixing-law attribution of excess observations; source frequencies are inferred, not measured'}
    if not laws:
        return report, None
    report['masked_observations'] = int(sum(int(m.sum()) for m in mask.values()))
    report['masked_hops'] = len(mask)
    return report, current


def floor_summary(levels, spur_threshold_db=3.0):
    """Terminated-input view: floor level, flatness across RF and internal features above it."""
    level = _array(levels['median_db'])
    ok = np.isfinite(level)
    if ok.sum() < 8:
        return {'status': 'insufficient_cells'}
    floor = float(np.median(level[ok]))
    result = {'status': 'measured', 'floor_median_db': floor,
              'flatness_p5_p95_db': float(np.percentile(level[ok], 95) - np.percentile(level[ok], 5)),
              'spur_threshold_db': spur_threshold_db,
              'internal_features': excess_regions(levels['rf_hz'], level, floor, spur_threshold_db, levels['cell_hz']),
              'scope': 'receiver floor with the declared termination; relative, uncalibrated'}
    p90 = _array(levels['p90_db']) if levels.get('p90_db') is not None else None
    if p90 is not None and np.isfinite(p90).sum() >= 8:
        # Intermittent content: the median hides bursts that the 90th percentile over
        # hop/IF visits still shows. A steady spur raises both, so the measure is the
        # within-cell spread p90 - median against the typical spread of a quiet cell.
        spread = p90 - level
        reference = float(np.nanmedian(spread))
        regions = excess_regions(levels['rf_hz'], spread, reference, spur_threshold_db, levels['cell_hz'])
        result.update(typical_p90_minus_median_db=reference,
                      intermittent_cells=int(np.sum(np.isfinite(spread) & (spread - reference > spur_threshold_db))),
                      intermittent_features=regions[:20])
    return result


def compare_inputs(terminated, antenna, threshold_db=3.0):
    """Classify features by comparing a terminated-input session with an antenna session.

    Each argument: {'levels': cell_levels(...), 'response': fit_if_response(...)}.
    external: antenna median exceeds the terminated median for the same cells.
    internal: steady features above the terminated floor itself (receiver/host generated);
    antenna_minus_terminated_db near 0 means the antenna feature is that internal one.
    internal_intermittent: bursts present with the termination (p90 - median spread above
    the typical spread); with an unshielded load these may also be local pickup, so they
    are reported separately from steady internal spurs.
    """
    t, a = terminated['levels'], antenna['levels']
    if t['rf_hz'] != a['rf_hz']:
        raise ValueError('sessions use different RF cell grids')
    tl, al = _array(t['median_db']), _array(a['median_db'])
    external = excess_regions(t['rf_hz'], al, tl, threshold_db, t['cell_hz'])
    floor = floor_summary(t, threshold_db)
    internal = []
    for region in floor.get('internal_features', []):
        i = int(np.argmin(np.abs(np.asarray(t['rf_hz']) - region['peak_hz'])))
        internal.append(dict(region, antenna_minus_terminated_db=None if not np.isfinite(al[i] - tl[i]) else float(al[i] - tl[i])))
    intermittent = []
    tp, ap = (_array(t['p90_db']) if t.get('p90_db') is not None else None,
              _array(a['p90_db']) if a.get('p90_db') is not None else None)
    for region in floor.get('intermittent_features', []):
        i = int(np.argmin(np.abs(np.asarray(t['rf_hz']) - region['peak_hz'])))
        delta = None if ap is None or not np.isfinite(ap[i] - tp[i]) else float(ap[i] - tp[i])
        intermittent.append(dict(region, antenna_p90_minus_terminated_p90_db=delta))
    both = np.isfinite(tl) & np.isfinite(al)
    result = {'threshold_db': threshold_db, 'external': external, 'internal': internal,
              'internal_intermittent': intermittent,
              'terminated_floor': {k: v for k, v in floor.items() if k not in ('internal_features', 'intermittent_features')},
              'antenna_minus_terminated_median_db': float(np.median(al[both] - tl[both])) if both.any() else None,
              'cells_compared': int(both.sum()),
              'scope': 'relative levels from one fixed receiver state; uncalibrated'}
    if terminated.get('response') and antenna.get('response'):
        result['if_response_agreement'] = compare_responses(terminated['response'], antenna['response'])
    return result


# ------------------------------------------------------------------------- settling

def fit_settling(delays_s, error_db, noise_floor_db=0.05):
    """|error(t)| = A exp(-t/tau); error is level minus the longest-delay reference."""
    t = np.asarray(delays_s, dtype=float)
    e = np.abs(np.asarray(error_db, dtype=float))
    if t.size < 4 or t.size != e.size or not np.isfinite(t).all() or not np.isfinite(e).all() or np.any(np.diff(t) <= 0):
        raise ValueError('settling fit needs >= 4 increasing finite delays and matching errors')
    use = e > noise_floor_db
    if use.sum() < 2:
        return {'status': 'already_settled', 'tau_s': 0.0, 'amplitude_db': float(e.max())}
    slope, intercept = np.polyfit(t[use], np.log(e[use]), 1)
    if slope >= 0:
        return {'status': 'not_decaying', 'tau_s': None, 'amplitude_db': float(math.exp(intercept))}
    return {'status': 'fit', 'tau_s': float(-1 / slope), 'amplitude_db': float(math.exp(intercept))}


def choose_delay(delays_s, error_db, tolerance_db=0.1, noise_floor_db=0.05, margin=1.25):
    """Shortest delay meeting the tolerance, verified at held-out delays.

    Even-indexed delays fit the model; odd-indexed delays are only used to verify
    it. The model must also predict that the reference (longest delay) itself is
    settled to a tenth of the tolerance, otherwise nothing is chosen.
    """
    t = np.asarray(delays_s, dtype=float)
    e = np.asarray(error_db, dtype=float)
    fit_idx, held = np.arange(0, t.size, 2), np.arange(1, t.size, 2)
    model = fit_settling(t[fit_idx], e[fit_idx], noise_floor_db) if fit_idx.size >= 4 else fit_settling(t, e, noise_floor_db)
    if model['status'] == 'already_settled':
        choice = float(t[0])
    elif model['status'] != 'fit':
        return {'status': model['status'], 'passed': False, 'model': model, 'tolerance_db': tolerance_db}
    else:
        reference_residual = model['amplitude_db'] * math.exp(-t[-1] / model['tau_s'])
        if reference_residual > tolerance_db / 10:
            return {'status': 'reference_not_settled', 'passed': False, 'model': model,
                    'predicted_reference_error_db': reference_residual, 'tolerance_db': tolerance_db}
        needed = model['tau_s'] * math.log(max(model['amplitude_db'] / tolerance_db, 1.0))
        choice = max(float(t[0]), needed * margin)
    later = held[t[held] >= choice]
    verified = bool(later.size and np.all(np.abs(e[later]) <= tolerance_db))
    measured_ok = bool(np.any(t >= choice) and np.all(np.abs(e[t >= choice]) <= tolerance_db))
    return {'status': 'chosen', 'delay_s': choice, 'model': model, 'heldout_points': int(later.size),
            'passed': bool(verified and measured_ok), 'tolerance_db': tolerance_db, 'margin': margin}


def _tone_visits(path, tone_hz, edge_fraction=.25, dc_mask_hz=0.0, window_hz=20e3):
    """(record, freqs, baseband, origin, level, tone_baseband) for hops that see the tone well inside."""
    for record in read_records(path):
        meta = record.get('metadata') if isinstance(record.get('metadata'), dict) else {}
        if meta.get('measurement_status') != 'complete':
            raise ValueError('incomplete acquisition in tone input')
        freqs, baseband, origin, _ = baseband_axis(record)
        sign = meta.get('frequency_sign', 1)
        tone_bb = sign * (tone_hz - origin)
        half = .5 * (record['coverage_stop_hz'] - record['coverage_start_hz'])
        if abs(tone_bb) + window_hz / 2 > half * (1 - 2 * edge_fraction) or abs(tone_bb) - window_hz / 2 < dc_mask_hz / 2:
            continue
        yield record, freqs, baseband, origin, _levels(record), tone_bb


def _band_power(freqs, level, center, width, step):
    sel = (np.abs(freqs - center) <= width / 2) & np.isfinite(level)
    if not sel.any():
        return None
    return float(np.sum(10 ** (level[sel] / 10)) * step)


def tone_level(path, tone_hz, window_hz=20e3, edge_fraction=.25, dc_mask_hz=0.0):
    """Median over hop-visits of the band power (dB) around the tone."""
    values = []
    for record, freqs, _, _, level, _ in _tone_visits(path, tone_hz, edge_fraction, dc_mask_hz, window_hz):
        p = _band_power(freqs, level, tone_hz, window_hz, record['bin_width_hz'])
        if p:
            values.append(10 * math.log10(p))
    if not values:
        raise ValueError('no hop observed the tone inside the allowed IF window')
    return float(np.median(values)), len(values)


def settling_from_scans(files_by_delay, tone_hz, window_hz=20e3, **options):
    """files_by_delay: {tune_delay_s: jsonl}. Reference = the longest delay."""
    delays = sorted(files_by_delay)
    levels = [tone_level(files_by_delay[d], tone_hz, window_hz, **options)[0] for d in delays]
    return delays, levels, [v - levels[-1] for v in levels]


# ------------------------------------------------------------------------ IQ / image

def fit_iq_model(y, tone_norm_freq):
    """Least squares of y = a e^{jwn} + b e^{-jwn} + d for a single complex tone.

    Returns |a|, |b|, d, image rejection (dB) and the residual noise power.
    tone_norm_freq is cycles/sample; unknown tone phase is absorbed in a and b.
    """
    y = np.asarray(y, dtype=complex)
    if y.ndim != 1 or y.size < 64 or not np.isfinite(y).all():
        raise ValueError('IQ model fit needs a finite complex vector of >= 64 samples')
    n = np.arange(y.size)
    phase = np.exp(2j * np.pi * tone_norm_freq * n)
    basis = np.column_stack([phase, np.conj(phase), np.ones(y.size)])
    coef = np.linalg.lstsq(basis, y, rcond=None)[0]
    residual = y - basis @ coef
    a, b, d = coef
    irr = float('inf') if abs(b) == 0 else 20 * math.log10(abs(a) / abs(b))
    return {'a_abs': float(abs(a)), 'b_abs': float(abs(b)), 'dc_real': float(d.real), 'dc_imag': float(d.imag),
            'image_rejection_db': irr, 'residual_power': float(np.mean(np.abs(residual) ** 2))}


def image_rejection_from_scan(path, tone_hz, window_hz=20e3, floor_offset_hz=100e3, floor_width_hz=400e3,
                              edge_fraction=.25, dc_mask_hz=500e3, min_excess_db=3.0):
    """IQ image rejection versus baseband offset from a single CW tone seen by many hops.

    For each hop the image lies at the mirrored baseband offset, i.e. RF 2*origin - tone,
    which moves with the hop; an RF-fixed signal coincides with it in at most a few hops,
    so the median is robust. Floor-limited hops give only a lower bound on rejection.
    """
    measured, bounds = [], []
    for record, freqs, _, origin, level, tone_bb in _tone_visits(path, tone_hz, edge_fraction, dc_mask_hz, window_hz):
        step = record['bin_width_hz']
        image_hz = 2 * origin - tone_hz
        if abs(image_hz - tone_hz) < window_hz + 2 * floor_offset_hz + floor_width_hz:
            continue

        def floor(center):
            sel = (np.abs(freqs - center) >= floor_offset_hz) & (np.abs(freqs - center) <= floor_offset_hz + floor_width_hz / 2)
            sel &= np.isfinite(level)
            return None if sel.sum() < 8 else float(np.median(10 ** (level[sel] / 10)) * window_hz)
        tone_p, image_p = _band_power(freqs, level, tone_hz, window_hz, step), _band_power(freqs, level, image_hz, window_hz, step)
        tone_f, image_f = floor(tone_hz), floor(image_hz)
        if None in (tone_p, image_p, tone_f, image_f) or tone_p <= tone_f * 10 ** (min_excess_db / 10):
            continue
        tone_x = tone_p - tone_f
        if image_p > image_f * 10 ** (min_excess_db / 10):
            measured.append((tone_bb, 10 * math.log10(tone_x / (image_p - image_f))))
        else:
            bounds.append((tone_bb, 10 * math.log10(tone_x / (image_f * (10 ** (min_excess_db / 10) - 1)))))
    result = {'measured_hops': len(measured), 'floor_limited_hops': len(bounds),
              'per_hop': [{'baseband_hz': bb, 'image_rejection_db': v} for bb, v in measured] +
                         [{'baseband_hz': bb, 'image_rejection_lower_bound_db': v} for bb, v in bounds],
              'scope': 'scalar PSD estimate; RF-fixed signals at an image frequency bias single hops'}
    if measured:
        result['median_image_rejection_db'] = float(np.median([v for _, v in measured]))
    if bounds:
        result['min_lower_bound_db'] = float(min(v for _, v in bounds))
    return result


# ------------------------------------------------------------------ synthetic truth

def _timestamp(text):
    import datetime
    try:
        return datetime.datetime.fromisoformat(str(text)).timestamp()
    except (TypeError, ValueError):
        return None


def _real(value):
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) else None)


def hop_telemetry(meta):
    """Temperature, processing-chain and analog-LO observations of one record (empty if none recorded).

    Reads the post-acquisition telemetry snapshot (rf_state.after, soapypower.telemetry) and the
    controller LO mapping. A temperature counts only when the snapshot marked it fresh.
    """
    out = {}
    rf = meta.get('rf_state')
    after = rf.get('after') if isinstance(rf, dict) else None
    if isinstance(after, dict):
        validity = after.get('validity') if isinstance(after.get('validity'), dict) else {}
        out['temperature_c'] = _real(validity.get('temperature_c'))
        chain = after.get('chain') if isinstance(after.get('chain'), dict) else {}
        out['chain_sha256'] = chain.get('sha256') if isinstance(chain.get('sha256'), str) else None
        value = chain.get('value') if isinstance(chain.get('value'), dict) else {}
        tracking = value.get('iq_tracking')
        out['iq_tracking'] = tracking if isinstance(tracking, bool) else None
        key = after.get('temperature_sensor_key')
        reading = (after.get('telemetry') or {}).get(key) if isinstance(after.get('telemetry'), dict) and key else None
        polled = reading.get('polled_while_streaming') if isinstance(reading, dict) else None
        out['temperature_polled_while_streaming'] = polled if isinstance(polled, bool) else None
    mapping = meta.get('lo_mapping')
    if isinstance(mapping, dict) and mapping.get('status') == 'controller_mapping':
        out['analog_lo_baseband_hz'] = _real(mapping.get('analog_lo_baseband_hz'))
    return out


def telemetry_summary(stores, dc_mask_hz=0.0, controlled_input=False, min_groups=10):
    """Receiver telemetry across a session: temperature, drift, floor-vs-temperature, chain, LO placement.

    floor_vs_temperature regresses each hop's median cell level on its RFIC temperature within the
    same center (both demeaned per center, so RF content cancels). With an uncontrolled antenna
    input it is a diagnostic only; never applied as a correction. The RFIC sensor is approximate.
    """
    entries = [e for store in stores for e in store.telemetry]
    report = {'status': 'measured' if entries else 'unavailable', 'records': len(entries),
              'controlled_input': bool(controlled_input), 'applied_as_correction': False,
              'absolute_temperature_calibrated': False}
    if not entries:
        return report
    temps = [e for e in entries if e.get('temperature_c') is not None]
    report['records_with_fresh_temperature'] = len(temps)
    report['temperature_reads_while_streaming'] = sum(1 for e in entries if e.get('temperature_polled_while_streaming') is True)
    chains = sorted({e['chain_sha256'] for e in entries if e.get('chain_sha256')})
    report['chain_fingerprints'] = chains
    report['chain_stable'] = len(chains) <= 1
    report['iq_tracking_states'] = sorted({e['iq_tracking'] for e in entries if e.get('iq_tracking') is not None})
    if temps:
        t = np.array([e['temperature_c'] for e in temps])
        report.update(temperature_min_c=float(t.min()), temperature_max_c=float(t.max()),
                      temperature_median_c=float(np.median(t)), temperature_span_c=float(np.ptp(t)))
        trials = []
        for source in sorted({e['source'] for e in temps}, key=str):
            rows = sorted((e['time_s'], e['temperature_c']) for e in temps if e['source'] == source and e['time_s'] is not None)
            if not rows:
                continue
            x = np.array([r[0] for r in rows]); y = np.array([r[1] for r in rows])
            slope = float(np.polyfit(x - x[0], y, 1)[0] * 60) if np.ptp(x) > 0 and len(rows) >= 3 else None
            edge = max(1, len(rows) // 10)  # median of the first/last tenth: robust to one-off readings at open
            trials.append({'source': source, 'records': len(rows), 'start_c': float(np.median(y[:edge])),
                           'end_c': float(np.median(y[-edge:])), 'first_reading_c': float(y[0]),
                           'median_c': float(np.median(y)), 'seconds': float(np.ptp(x)), 'slope_c_per_min': slope})
        report['per_trial'] = trials
        slopes = [abs(r['slope_c_per_min']) for r in trials if r['slope_c_per_min'] is not None]
        report['max_abs_trial_slope_c_per_min'] = max(slopes) if slopes else None
        groups = {}
        for e in temps:
            if e.get('floor_db') is not None:
                groups.setdefault(e['key'], []).append((e['temperature_c'], e['floor_db']))
        dt, dy = [], []
        for rows in groups.values():
            if len(rows) < 2:
                continue
            a = np.array(rows)
            dt.extend(a[:, 0] - a[:, 0].mean()); dy.extend(a[:, 1] - a[:, 1].mean())
        dt, dy = np.array(dt), np.array(dy)
        used = sum(1 for rows in groups.values() if len(rows) >= 2)
        regression = {'centers': used, 'observations': int(dt.size), 'within_center_temperature_sd_c': float(dt.std()) if dt.size else 0.0}
        if used < min_groups or dt.size < used + 3 or float((dt ** 2).sum()) <= 1e-12:
            regression['status'] = 'insufficient_temperature_variation'
        else:
            slope = float((dt * dy).sum() / (dt ** 2).sum())
            residual = dy - slope * dt
            dof = dt.size - used - 1
            se = float(math.sqrt((residual ** 2).sum() / dof / (dt ** 2).sum())) if dof > 0 else None
            r = float(np.corrcoef(dt, dy)[0, 1]) if dy.std() > 0 else 0.0
            regression.update(status='fit', slope_db_per_c=slope, slope_se_db=se, correlation=r,
                              explained_fraction=r * r,
                              interpretation=('receiver gain/noise drift with temperature' if controlled_input
                                              else 'diagnostic only: antenna input is not a controlled source'))
        report['floor_vs_temperature'] = regression
    lo = np.array([e['analog_lo_baseband_hz'] for e in entries if e.get('analog_lo_baseband_hz') is not None])
    if lo.size:
        report['lo_mapping'] = {'records': int(lo.size), 'max_abs_analog_lo_offset_hz': float(np.abs(lo).max()),
                                'records_lo_outside_dc_mask': int((np.abs(lo) > dc_mask_hz / 2).sum()) if dc_mask_hz else None,
                                'dc_mask_hz': float(dc_mask_hz)}
    return report


def synthetic_scan(path, start_hz, stop_hz, hop_hz, passes=6, rate_hz=56e6, bins=8192,
                   retained_hz=None, if_response=None, floor_db=-150.0, tones=(),
                   lo_spur_db=20.0, sigma_db=0.15, seed=1, tone_scale=1.0, offset_hz=0.0,
                   image_rejection_db=None, frequency_grid=True, context=None, harmonic_sources=(),
                   temperature=None, thermal_db_per_c=0.0, lo_offset_hz=0.0, time_origin_s=0.0):
    """Write a spec-conformant JSONL scan from a known model (test/dry-run truth).

    Bins follow an even FFT (DC bin at index n/2, as soapy_power records). The DC bin
    is marked estimated. if_response(f_if_hz) -> dB is the ground-truth IF response.
    The LO spur sits at DC (IF-fixed). Tones: (rf_hz, level_db above floor); with
    image_rejection_db each tone also produces its IQ image at 2*origin - rf.
    harmonic_sources: (source_hz, order, sign, dB above floor) responses at IF = sign*(order*LO - source).
    temperature(t_s) -> deg C adds telemetry snapshots (rf_state/lo_mapping, as soapypower.telemetry
    records them) and scales every level by thermal_db_per_c relative to temperature(0);
    t_s counts from time_origin_s at 0.05 s per record.
    """
    import datetime
    from pathlib import Path
    rng = np.random.default_rng(seed)
    step = rate_hz / bins
    n = int(round((retained_hz or rate_hz) / step))
    n -= n % 2
    if_response = if_response or (lambda f: 0 * f)
    centers = []
    c = start_hz + hop_hz / 2 + offset_hz
    while c - hop_hz / 2 < stop_hz:
        centers.append(c)
        c += hop_hz
    t0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    context = context or {'driver': 'synthetic', 'serial': 'S', 'channel': 0, 'agc': False, 'gains': {'PGA': 20.0},
                          'sample_rate': rate_hz, 'bandwidth': rate_hz, 'tune_args': ''}
    with Path(path).open('x', encoding='utf-8') as out:
        tick = 0
        for _ in range(passes):
            for center in centers:
                k = np.arange(n) - n // 2
                f = center + k * step
                offset = f - center
                lin = 10 ** ((floor_db + if_response(offset) + rng.normal(0, sigma_db, n)) / 10)
                for rf, level in tones:
                    images = [(rf, level)]
                    if image_rejection_db is not None:
                        images.append((2 * center - rf, level - image_rejection_db))
                    for where, db in images:
                        near = np.abs(f - where) <= step * 1.5
                        lin[near] += 10 ** ((floor_db + db + if_response(f[near] - center)) / 10) * tone_scale
                for source, order, sign, level in harmonic_sources:
                    near = np.abs(offset - sign * (order * center - source)) <= step * 1.5
                    lin[near] += 10 ** ((floor_db + level + if_response(offset[near])) / 10)
                lin[np.abs(offset) < step * 1.5] += 10 ** ((floor_db + lo_spur_db) / 10)
                t_s = time_origin_s + tick * .05
                if temperature is not None:
                    lin *= 10 ** (thermal_db_per_c * (temperature(t_s) - temperature(0.0)) / 10)
                db = 10 * np.log10(lin)
                db[n // 2] = floor_db  # interpolated DC estimate, flagged below
                ts = t0 + datetime.timedelta(seconds=t_s)
                tick += 1
                metadata = {'measurement_status': 'complete', 'continuity': 'verified', 'context': context,
                            'estimated_bins': [n // 2], 'frequency_sign': 1}
                if temperature is not None:
                    metadata['rf_state'] = {'after': {
                        'validity': {'passed': True, 'temperature_c': float(temperature(t_s))},
                        'chain': {'status': 'ok', 'sha256': 'synthetic-chain', 'value': {'iq_tracking': True}},
                        'temperature_sensor_key': 'rx:temp',
                        'telemetry': {'rx:temp': {'status': 'ok', 'fresh': True, 'polled_while_streaming': False}}}}
                    metadata['lo_mapping'] = {'status': 'controller_mapping', 'analog_lo_baseband_hz': float(lo_offset_hz)}
                if frequency_grid:
                    metadata['frequency_grid'] = {'rf_origin_hz': center, 'step_hz': step,
                                                  'first_bin_index': int(k[0]), 'bin_count': n}
                out.write(json.dumps({
                    'version': 2, 'time_start': ts.isoformat(),
                    'time_stop': (ts + datetime.timedelta(seconds=.005)).isoformat(),
                    'start_hz': float(f[0]), 'stop_hz': float(f[-1] + step), 'step_hz': step,
                    'first_bin_center_hz': float(f[0]), 'bin_width_hz': step,
                    'coverage_start_hz': float(f[0] - step / 2), 'coverage_stop_hz': float(f[-1] + step / 2),
                    'estimated_bins': [n // 2], 'samples': 65536, 'unit': 'dB(input power/Hz)',
                    'power': [float(v) for v in db], 'masked_bins': [], 'zero_power_bins': [],
                    'metadata': metadata}) + '\n')
    return centers


# ------------------------------------------------------------------------ summary

def validate_scan(paths, start_hz, stop_hz, cell_hz=100e3, edge_fraction=.12, dc_mask_hz=0.0,
                  max_sigma_db=0.5, max_disagreement_db=0.25, max_if_hz=None):
    """Single-session report: even passes fit, odd passes are the independent holdout."""
    store = HopStore.from_files(paths, start_hz, stop_hz, cell_hz, edge_fraction=edge_fraction, dc_mask_hz=dc_mask_hz)
    n = store.pass_count()
    report = {'kind': 'model_validation', 'version': 1, 'records': store.records, 'hops': len(store.hops),
              'passes': n, 'repeatability': repeatability(store, max_sigma_db),
              'evidence_level': 'relative, uncalibrated', 'calibrated': False}
    if n >= 4:
        report['if_model'] = holdout_validation(store.subset(range(0, n, 2)), store.subset(range(1, n, 2)),
                                                max_if_hz=max_if_hz, max_disagreement_db=max_disagreement_db)
    else:
        report['if_model'] = {'status': 'need_four_passes', 'passed': False}
    report['passed'] = bool(report['repeatability'].get('passed') and report['if_model'].get('passed'))
    return report
