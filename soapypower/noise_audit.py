"""Offline IQ noise diagnostics; never an absolute calibration or automatic correction."""
import argparse
import hashlib
import math
from pathlib import Path

import numpy as np

from soapypower.files import write_json
from soapypower.numerics import finite, integer
from soapypower.psd import PSD


def _db(value):
    return None if value == 0 else 10 * math.log10(value)


def analyze_iq(samples, sample_rate, fft_size=4096, segments=128, overlap=0.5,
               center_hz=0, amplitude_threshold=None, dc_exclude_bins=2,
               dc_warning_db=10, variation_warning_db=6):
    """Analyze at most `segments` periodic-Hann frames using bounded frame work.

    All amplitude/sample statistics refer to the unique prefix used by those
    frames. Overlap does not duplicate samples in amplitude-threshold counts.
    Segment count is not an effective independent-average count.
    """
    samples = np.asarray(samples)
    if samples.ndim != 1 or samples.dtype.kind != 'c' or samples.dtype.itemsize not in (8, 16):
        raise ValueError('input must be a one-dimensional complex64/complex128 IQ array')
    sample_rate = finite(sample_rate, 'sample rate', positive=True)
    fft_size = integer(fft_size, 'FFT size', 4)
    segments = integer(segments, 'maximum segments', 1)
    overlap = finite(overlap, 'overlap', nonnegative=True)
    if overlap >= 1:
        raise ValueError('overlap must be less than one')
    center_hz = finite(center_hz, 'center frequency')
    dc_exclude_bins = integer(dc_exclude_bins, 'DC exclusion bins')
    if dc_exclude_bins >= fft_size // 2:
        raise ValueError('DC exclusion must leave off-DC bins')
    dc_warning_db = finite(dc_warning_db, 'DC warning dB', positive=True)
    variation_warning_db = finite(variation_warning_db, 'variation warning dB', positive=True)
    if amplitude_threshold is not None:
        amplitude_threshold = finite(amplitude_threshold, 'amplitude threshold', positive=True)
    if samples.size < fft_size:
        raise ValueError('input contains no complete FFT segment')
    step = fft_size - math.floor(fft_size * overlap)
    count = min(segments, 1 + (samples.size - fft_size) // step)
    used = fft_size + (count - 1) * step
    psd = PSD(fft_size, sample_rate, fft_window='hann', fft_overlap=0, log_scale=False,
              max_threads=1, input_format='complex')
    state = psd.set_center_freq(center_hz)
    # Own only frame-sized temporaries; the source may be memory mapped.
    segment_powers, peak, component_peak = [], 0.0, 0.0
    radial_exceed = component_exceed = 0
    sample_count = 0
    mean = 0j
    try:
        for index in range(count):
            frame = np.asarray(samples[index * step:index * step + fft_size], dtype=np.complex128)
            if not np.isfinite(frame).all():
                raise ValueError('selected IQ samples must be finite')
            psd.update(state, frame)
            weighted = frame * psd._window
            with np.errstate(over='ignore', invalid='ignore'):
                power = float(np.sum(weighted.real**2 + weighted.imag**2) / psd._window_energy)
            if not math.isfinite(power):
                raise ValueError('segment integrated power exceeds float64; rescale IQ')
            segment_powers.append(power)
            unique = frame if index == 0 else frame[-step:]
            radial = np.hypot(unique.real, unique.imag)
            components = np.maximum(abs(unique.real), abs(unique.imag))
            if not np.isfinite(radial).all():
                raise ValueError('IQ amplitude exceeds float64; rescale IQ')
            peak = max(peak, float(radial.max()))
            component_peak = max(component_peak, float(components.max()))
            if amplitude_threshold is not None:
                radial_exceed += int(np.count_nonzero(radial > amplitude_threshold))
                component_exceed += int(np.count_nonzero(components > amplitude_threshold))
            # Scale before summation to avoid overflow of a long raw sum.
            chunk_mean = complex(np.sum(unique / len(unique)))
            next_count = sample_count + len(unique)
            mean += (chunk_mean - mean) * (len(unique) / next_count)
            sample_count = next_count
        frequencies, density = psd.result(state)
        enbw = psd.enbw
    finally:
        psd.close()
    offsets = (np.arange(fft_size) - fft_size // 2) * (sample_rate / fft_size)
    off_dc = abs(np.arange(fft_size) - fft_size // 2) > dc_exclude_bins
    positive = density > 0
    reference_bins = off_dc & positive
    # Median is a robust diagnostic anchor, not a gain/noise-temperature estimate.
    if reference_bins.any():
        ordered = np.sort(density[reference_bins])
        middle = len(ordered) // 2
        reference = (float(ordered[middle]) if len(ordered) % 2 else
                     float(ordered[middle-1]) / 2 + float(ordered[middle]) / 2)
    else:
        reference = None
    log_density = [None if x == 0 else _db(float(x)) for x in density]
    relative = [None if x == 0 or reference is None else 10 * (math.log10(float(x)) - math.log10(reference)) for x in density]
    diagnostic = [None if not off_dc[i] or value is None else -value for i, value in enumerate(relative)]
    dc_bins = ~off_dc
    dc_values = density[dc_bins]
    dc_mean = math.fsum(float(x) / len(dc_values) for x in dc_values)
    dc_excess = None if reference is None or dc_mean == 0 else 10 * (math.log10(dc_mean) - math.log10(reference))
    positive_segments = [x for x in segment_powers if x > 0]
    span = (10 * (math.log10(max(positive_segments)) - math.log10(min(positive_segments)))
            if positive_segments and len(positive_segments) == count else None)
    average_power = math.fsum(value / count for value in segment_powers)
    if average_power > 0 and count > 1:
        cv = math.sqrt(math.fsum((value / average_power - 1)**2 for value in segment_powers) / (count - 1))
    else:
        cv = None
    warnings = []
    if not reference_bins.any():
        warnings.append('no_positive_off_dc_reference')
    if dc_excess is not None and dc_excess > dc_warning_db:
        warnings.append('dc_region_excess')
    if span is not None and span > variation_warning_db:
        warnings.append('nonstationary_segment_power')
    if any(value == 0 for value in segment_powers):
        warnings.append('zero_power_segments')
    if component_exceed:
        warnings.append('component_amplitude_threshold_exceeded')
    if radial_exceed:
        warnings.append('radial_amplitude_threshold_exceeded_not_proof_of_clipping')
    if count < 2:
        warnings.append('insufficient_segments_for_variation')
    return {
        'type': 'noise_diagnostic', 'version': 1, 'absolute_calibration': False,
        'automatic_correction_applied': False, 'noise_figure_db': None,
        'sample_rate_hz': sample_rate, 'center_hz': center_hz, 'fft_size': fft_size,
        'window': 'periodic_hann', 'detrend': None, 'bin_width_hz': sample_rate / fft_size,
        'window_enbw_hz': enbw, 'overlap_samples': fft_size - step,
        'requested_segments': segments, 'segment_count': count,
        'available_samples': int(samples.size), 'analyzed_unique_samples': used,
        'unexamined_samples': int(samples.size - used), 'input_dtype': str(samples.dtype),
        'frequency_hz': frequencies.tolist(), 'baseband_hz': offsets.tolist(),
        'density_unit': 'input power/Hz', 'noise_density': density.tolist(),
        'log_density_unit': 'dB(input power/Hz)', 'log_noise_density_db': log_density,
        'zero_power_bins': np.flatnonzero(~positive).tolist(),
        'reference_density': reference, 'relative_noise_curve_db': relative,
        'diagnostic_correction_db': diagnostic,
        'diagnostic_correction_status': 'display-only inverse relative curve; not a gain calibration',
        'dc_region': {'half_width_bins': dc_exclude_bins, 'mean_density': dc_mean,
                      'excess_above_off_dc_median_db': dc_excess},
        'segment_window_weighted_power': segment_powers,
        'mean_segment_window_weighted_power': average_power,
        'segment_power_span_db': span, 'segment_power_coefficient_of_variation': cv,
        'sample_mean_iq': [mean.real, mean.imag], 'max_digital_amplitude': peak,
        'max_component_amplitude': component_peak, 'amplitude_threshold': amplitude_threshold,
        'radial_fraction_above_threshold': None if amplitude_threshold is None else radial_exceed / used,
        'component_fraction_above_threshold': None if amplitude_threshold is None else component_exceed / used,
        'warnings': warnings,
        'limitations': ['Receiver-added noise prevents one terminated curve from determining gain or NF.',
                        'Digital amplitudes cannot establish analog linearity or absence of compression.',
                        'IQ scaling/full-scale and reference plane must be established independently.',
                        'Overlapping segments are correlated; no confidence interval is inferred.']}


def analyze_file(path, **kwargs):
    samples = np.load(path, mmap_mode='r', allow_pickle=False)
    if not isinstance(samples, np.ndarray):
        if hasattr(samples, 'close'):
            samples.close()
        raise ValueError('input must be one .npy IQ array, not an archive')
    report = analyze_iq(samples, **kwargs)
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    report['input_sha256'] = digest.hexdigest()
    report['input_file'] = str(path)
    return report


def parser():
    result = argparse.ArgumentParser(prog='soapy_power_noise_audit', description=__doc__)
    result.add_argument('input', help='one-dimensional complex64/complex128 .npy file')
    result.add_argument('--sample-rate', type=float, required=True)
    result.add_argument('--fft-size', type=int, default=4096)
    result.add_argument('--segments', type=int, default=128)
    result.add_argument('--overlap', type=float, default=0.5)
    result.add_argument('--center-hz', type=float, default=0)
    result.add_argument('--amplitude-threshold', type=float)
    result.add_argument('--dc-exclude-bins', type=int, default=2)
    result.add_argument('--dc-warning-db', type=float, default=10)
    result.add_argument('--variation-warning-db', type=float, default=6)
    result.add_argument('-O', '--output', required=True)
    result.add_argument('--force', action='store_true')
    return result


def main(argv=None):
    command = parser()
    args = command.parse_args(argv)
    try:
        report = analyze_file(args.input, sample_rate=args.sample_rate, fft_size=args.fft_size,
                              segments=args.segments, overlap=args.overlap, center_hz=args.center_hz,
                              amplitude_threshold=args.amplitude_threshold, dc_exclude_bins=args.dc_exclude_bins,
                              dc_warning_db=args.dc_warning_db, variation_warning_db=args.variation_warning_db)
        write_json(args.output, report, overwrite=args.force)
    except (ValueError, OSError, OverflowError) as error:
        command.error(str(error))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
