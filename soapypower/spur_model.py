"""Physical and generative model of direct-conversion receiver spurs.

The receivers in this repository (USRP B200/B200mini with AD9361/AD9364 and
bladeRF x40 with LMS6002D) mix with a square-wave LO.  A square wave contains
every odd harmonic, so an input at ``f_rf`` also appears at

    f_if = s * (f_rf - n * f_lo),   n = 1, 3, 5, ...,  s = +/-1

where ``s=+1`` is the normal down-conversion, ``s=-1`` the IQ image and
``n>1`` a harmonic mixing response.  The same model covers DC leakage
(``f_if=0``), IQ gain/phase imbalance and n-th order intermodulation.

This module is deliberately independent of the acquisition code.  It provides
the physical dictionary used by :mod:`soapypower.spur_pipeline`, the digital
twin in :mod:`soapypower.spur_twin`, and the synthetic tests.  All levels are
relative unless a caller supplies an absolute calibration.
"""
from dataclasses import asdict, dataclass
import math

import numpy as np


# ----------------------------------------------------------------- receiver profiles

@dataclass(frozen=True)
class ReceiverProfile:
    """A receiver's RF range, LO harmonic content and nominal imperfections."""

    name: str
    rf_min_hz: float
    rf_max_hz: float
    architecture: str
    max_lo_harmonic: int
    sample_rate_hz: float
    adc_bits: int
    image_rejection_db: float
    dc_offset_db: float
    thermal_db_per_c: float
    notes: str = ''

    def __post_init__(self):
        if not self.name or self.rf_min_hz <= 0 or self.rf_max_hz <= self.rf_min_hz:
            raise ValueError('receiver profile needs a name and a positive RF range')
        if self.max_lo_harmonic < 1 or self.max_lo_harmonic % 2 == 0:
            raise ValueError('max_lo_harmonic must be an odd positive integer')
        if self.sample_rate_hz <= 0:
            raise ValueError('sample_rate_hz must be positive')

    @property
    def harmonic_orders(self):
        return tuple(range(1, self.max_lo_harmonic + 1, 2))

    def to_dict(self):
        out = asdict(self)
        out['harmonic_orders'] = list(self.harmonic_orders)
        out['scope'] = 'profile values are nominal; DC/IQ/thermal coefficients are receiver-state specific'
        return out


B200MINI = ReceiverProfile(
    name='Ettus B200mini (AD9364)',
    rf_min_hz=70e6,
    rf_max_hz=6e9,
    architecture='direct conversion, square-wave LO',
    max_lo_harmonic=7,
    sample_rate_hz=56e6,
    adc_bits=12,
    image_rejection_db=35.0,
    dc_offset_db=15.0,
    thermal_db_per_c=-0.024,
    notes='70 MHz-6 GHz; RX2 is the usual antenna port; no preselector',
)

BLADERF_X40 = ReceiverProfile(
    name='bladeRF x40 (LMS6002D)',
    rf_min_hz=300e6,
    rf_max_hz=3.8e9,
    architecture='direct conversion, square-wave LO',
    max_lo_harmonic=7,
    sample_rate_hz=40e6,
    adc_bits=12,
    image_rejection_db=30.0,
    dc_offset_db=12.0,
    thermal_db_per_c=-0.024,
    notes='300 MHz-3.8 GHz; ~50 MHz only with an external transverter; XB-200 changes the path',
)

GENERIC = ReceiverProfile(
    name='generic direct-conversion SDR',
    rf_min_hz=1e6,
    rf_max_hz=6e9,
    architecture='direct conversion, square-wave LO',
    max_lo_harmonic=7,
    sample_rate_hz=20e6,
    adc_bits=12,
    image_rejection_db=35.0,
    dc_offset_db=15.0,
    thermal_db_per_c=-0.024,
    notes='fallback; use a measured profile when the device is known',
)

PROFILES = {
    'b200': B200MINI,
    'b200mini': B200MINI,
    'bladerf': BLADERF_X40,
    'bladerf-x40': BLADERF_X40,
    'generic': GENERIC,
}


def receiver_profile(name_or_profile):
    """Look up a profile by name, or pass a ReceiverProfile through unchanged."""
    if isinstance(name_or_profile, ReceiverProfile):
        return name_or_profile
    key = str(name_or_profile).strip().lower()
    if key not in PROFILES:
        raise ValueError('unknown receiver profile: {!r}'.format(name_or_profile))
    return PROFILES[key]


# ------------------------------------------------------------------ LO harmonics

def square_wave_amplitudes(orders=(1, 3, 5, 7)):
    """Amplitudes of the odd square-wave LO harmonics, relative to a sine of amplitude 1.

    The Fourier series of a +/-1 square wave has 4/(pi*n) for odd n.  Power is
    proportional to 1/n**2, which is the value used by the twin.
    """
    out = {}
    for n in orders:
        n = int(n)
        if n < 1 or n % 2 == 0:
            raise ValueError('LO harmonic orders must be odd positive integers')
        out[n] = 4.0 / (math.pi * n)
    return out


def harmonic_power_db(orders=(1, 3, 5, 7), normalized=True):
    """Power of each odd LO harmonic in dB, normalized to the fundamental."""
    amplitudes = square_wave_amplitudes(orders)
    ref = amplitudes[min(amplitudes)] if normalized else 1.0
    return {n: 20.0 * math.log10(a / ref) for n, a in amplitudes.items()}


# --------------------------------------------------------------- mixing products

def mixing_products(rf_hz, lo_hz, orders=(1, 3, 5, 7), image_rejection_db=35.0,
                    include_image=True):
    """Every first-order product of one RF tone with the square-wave LO.

    Returns a list of dictionaries with signed IF (Hz), harmonic order, sign,
    kind and relative amplitude.  ``kind`` is ``signal``, ``image`` or
    ``lo_harmonic``.  The relation is ``if_hz = sign * (rf_hz - order*lo_hz)``.
    """
    rf_hz, lo_hz = float(rf_hz), float(lo_hz)
    if not math.isfinite(rf_hz) or not math.isfinite(lo_hz):
        raise ValueError('rf_hz and lo_hz must be finite')
    orders = tuple(int(n) for n in orders)
    if not orders or any(n < 1 or n % 2 == 0 for n in orders):
        raise ValueError('orders must be a nonempty sequence of odd positive integers')
    image_amplitude = 10.0 ** (-float(image_rejection_db) / 20.0)
    if not 0.0 <= image_amplitude <= 1.0:
        raise ValueError('image_rejection_db must be nonnegative')
    out = []
    for n in orders:
        out.append({
            'rf_hz': rf_hz, 'lo_hz': lo_hz, 'if_hz': rf_hz - n * lo_hz,
            'order': n, 'sign': 1, 'kind': 'signal' if n == 1 else 'lo_harmonic',
            'relative_amplitude': 1.0 / n,
            'slope_hz_per_hz': -n,
            'harmonic': n,
        })
        if include_image and n == 1:
            out.append({
                'rf_hz': rf_hz, 'lo_hz': lo_hz, 'if_hz': -(rf_hz - lo_hz),
                'order': 1, 'sign': -1, 'kind': 'image',
                'relative_amplitude': image_amplitude,
                'slope_hz_per_hz': 1,
                'harmonic': 1,
            })
    return out


def spur_atlas(lo_hz, rf_sources, orders=(1, 3, 5, 7), image_rejection_db=35.0,
               rate_hz=None, include_image=True):
    """Mixing products for many RF sources at one LO setting.

    ``rf_sources`` may be a sequence of frequencies or ``(frequency, amplitude)``
    pairs.  If ``rate_hz`` is given, ``in_band`` says whether the signed IF lies
    inside ``[-rate/2, rate/2]``.
    """
    out = []
    for item in rf_sources:
        if isinstance(item, (tuple, list)):
            rf_hz, amplitude = float(item[0]), float(item[1])
        else:
            rf_hz, amplitude = float(item), 1.0
        for product in mixing_products(rf_hz, lo_hz, orders=orders,
                                       image_rejection_db=image_rejection_db,
                                       include_image=include_image):
            product = dict(product)
            product['input_amplitude'] = amplitude
            product['output_amplitude'] = amplitude * product['relative_amplitude']
            if rate_hz is not None:
                product['in_band'] = bool(abs(product['if_hz']) <= float(rate_hz) / 2.0)
            out.append(product)
    return out


def two_tone_intermod(f1_hz, f2_hz, order=3, include_sum=True):
    """RF frequencies of two-tone intermodulation products of a given order.

    For order 3 this gives ``2*f1-f2`` and ``2*f2-f1`` plus, when
    ``include_sum`` is true, the third-order sums ``2*f1+f2`` and ``f1+2*f2``.
    The caller decides which of them are inside the receiver band; no product
    is silently dropped.
    """
    f1_hz, f2_hz = float(f1_hz), float(f2_hz)
    order = int(order)
    if order < 2:
        raise ValueError('intermodulation order must be at least 2')
    out = []
    for m in range(1, order):
        n = order - m
        candidates = [(m * f1_hz - n * f2_hz, m, -n),
                      (m * f2_hz - n * f1_hz, -n, m)]
        if include_sum:
            candidates += [(m * f1_hz + n * f2_hz, m, n),
                           (m * f2_hz + n * f1_hz, n, m)]
        for frequency, a, b in candidates:
            if frequency <= 0:
                continue
            item = {'rf_hz': frequency, 'order': order, 'm': a, 'n': b,
                    'kind': 'intermod', 'label': '{}f1{:+d}f2'.format(a, b)}
            if item not in out:
                out.append(item)
    return out


# --------------------------------------------------------------- IQ imperfections

def iq_coefficients(gain_db=0.0, phase_deg=0.0):
    """Return ``(c, d)`` for the complex IQ model ``y = c*x + d*conj(x)``.

    ``gain_db`` is the I/Q gain ratio (I minus Q, in dB); ``phase_deg`` is the
    quadrature error in degrees.  With no error ``c=1, d=0``.  The image
    rejection ratio is ``|c/d|**2``, so the representation is directly
    testable against a spectrum analyser or the image-rejection routine in
    :mod:`soapypower.model_validation`.
    """
    gain_db, phase_deg = float(gain_db), float(phase_deg)
    if not math.isfinite(gain_db) or not math.isfinite(phase_deg):
        raise ValueError('IQ gain and phase errors must be finite')
    g = 10.0 ** (gain_db / 20.0)
    q = 1.0 / g
    phi = math.radians(phase_deg)
    c = 0.5 * ((g + q * math.cos(phi)) + 1j * (q * math.sin(phi)))
    d = 0.5 * ((g - q * math.cos(phi)) + 1j * (q * math.sin(phi)))
    return complex(c), complex(d)


def image_rejection_db(gain_db=0.0, phase_deg=0.0):
    """Image rejection ratio in dB for an IQ gain/phase error."""
    c, d = iq_coefficients(gain_db, phase_deg)
    if abs(d) == 0.0:
        return float('inf')
    return 20.0 * math.log10(abs(c) / abs(d))


def apply_iq_imbalance(iq, gain_db=0.0, phase_deg=0.0):
    """Apply the complex IQ mismatch model to a complex baseband array."""
    x = np.asarray(iq, dtype=np.complex128)
    if x.ndim != 1 or x.size == 0:
        raise ValueError('iq must be a nonempty one-dimensional complex array')
    c, d = iq_coefficients(gain_db, phase_deg)
    return c * x + d * np.conj(x)


def iq_temperature_error(gain_db=0.0, phase_deg=0.0, temperature_c=25.0,
                         reference_c=25.0, gain_db_per_c=1e-3,
                         phase_deg_per_c=1e-2):
    """First-order temperature drift of the IQ gain/phase error.

    The coefficients are illustrative defaults, not a calibration.  Replace
    them with measurements from the receiver-state telemetry before using the
    result for correction.
    """
    delta = float(temperature_c) - float(reference_c)
    return {'gain_db': float(gain_db) + float(gain_db_per_c) * delta,
            'phase_deg': float(phase_deg) + float(phase_deg_per_c) * delta,
            'delta_c': delta,
            'scope': 'first-order illustrative drift; calibrate per receiver state'}


def quantize_iq(iq, bits=12, full_scale=1.0):
    """Uniform mid-rise ADC model, returning quantized values and the error.

    This is a diagnostic twin, not a replacement for the receiver's actual
    ADC/filter chain.  ``full_scale`` is the clipping magnitude of each
    component; values outside it are clipped and counted.
    """
    x = np.asarray(iq, dtype=np.complex128)
    bits = int(bits)
    full_scale = float(full_scale)
    if x.ndim != 1 or x.size == 0:
        raise ValueError('iq must be a nonempty one-dimensional complex array')
    if bits < 2 or full_scale <= 0:
        raise ValueError('bits must be at least 2 and full_scale positive')
    levels = 2 ** bits
    step = 2.0 * full_scale / (levels - 1)
    i = np.clip(np.round(x.real / step) * step, -full_scale, full_scale)
    q = np.clip(np.round(x.imag / step) * step, -full_scale, full_scale)
    y = i + 1j * q
    clipped = int(np.sum(np.abs(x.real) > full_scale) + np.sum(np.abs(x.imag) > full_scale))
    return y, {'error': y - x, 'step': step, 'clipped_components': clipped,
               'bits': bits, 'full_scale': full_scale}


def iq_from_image_rejection(image_rejection_db_value, phase_deg=0.0):
    """Inverse of :func:`image_rejection_db`: gain error giving a requested IRR.

    The phase error is held fixed and the gain error is solved from the
    magnitude ratio; this is useful for generating a twin with a known IRR.
    """
    target = float(image_rejection_db_value)
    if not math.isfinite(target) or target < 0:
        raise ValueError('image rejection must be finite and nonnegative')
    # Monotonic bisection in gain; the magnitude ratio is well behaved.
    lo, hi = -60.0, 60.0
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if image_rejection_db(mid, phase_deg) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ----------------------------------------------------------------- time domain twin

@dataclass(frozen=True)
class Tone:
    """A real RF tone expressed by its analytic baseband amplitude."""

    rf_hz: float
    amplitude: float = 1.0
    phase_rad: float = 0.0

    def __post_init__(self):
        if not math.isfinite(self.rf_hz) or not math.isfinite(self.amplitude):
            raise ValueError('tone frequency and amplitude must be finite')
        if self.amplitude < 0:
            raise ValueError('tone amplitude must be nonnegative')


def simulate_iq(lo_hz, rate_hz, samples, tones=(), orders=(1, 3, 5, 7),
                dc=(0.0, 0.0), iq_gain_db=0.0, iq_phase_deg=0.0,
                noise_amplitude=0.0, intermod=(), phase_noise_rad=0.0, seed=0,
                bandwidth_hz=None):
    """Generate complex baseband samples for a direct-conversion receiver.

    Parameters
    ----------
    tones : sequence of Tone or ``(rf_hz, amplitude[, phase_rad])``
        Real RF tones.  Each is multiplied by the square-wave harmonic
        amplitude ``1/n`` in the baseband model.
    dc : (i, q)
        Complex DC offset before IQ mismatch.
    intermod : sequence of dicts with ``rf_hz``, ``amplitude`` and optional
        ``order``/``phase_rad``
        Products generated before the mixer.  They are added at the
        fundamental only; their own harmonic responses are not implied.
    phase_noise_rad : float
        RMS LO phase noise (white, per sample), added as a common phase
        perturbation of all down-converted terms.

    Returns
    -------
    (iq, truth) where truth is a list of dictionaries describing every
    contribution before the final complex noise.  The truth includes the
    image of each tone when the IQ error is nonzero.
    """
    rate_hz = float(rate_hz)
    samples = int(samples)
    if not math.isfinite(rate_hz) or rate_hz <= 0 or samples < 8:
        raise ValueError('rate_hz must be positive and samples at least 8')
    if noise_amplitude < 0 or phase_noise_rad < 0:
        raise ValueError('noise amplitude and phase noise must be nonnegative')
    rng = np.random.default_rng(seed)
    t = np.arange(samples, dtype=float) / rate_hz
    x = np.zeros(samples, dtype=np.complex128)
    truth = []
    bandwidth_hz = float(rate_hz) if bandwidth_hz is None else float(bandwidth_hz)
    if bandwidth_hz <= 0 or bandwidth_hz > rate_hz:
        raise ValueError('bandwidth_hz must be positive and no larger than rate_hz')

    def _tone(item):
        if isinstance(item, Tone):
            return item
        if len(item) == 2:
            return Tone(float(item[0]), float(item[1]), 0.0)
        return Tone(float(item[0]), float(item[1]), float(item[2]))

    for item in tones:
        tone = _tone(item)
        for n in orders:
            n = int(n)
            if n < 1 or n % 2 == 0:
                raise ValueError('LO harmonic orders must be odd positive integers')
            amplitude = tone.amplitude / n
            if amplitude == 0:
                continue
            if_hz = tone.rf_hz - n * lo_hz
            if abs(if_hz) > bandwidth_hz / 2.0:
                continue
            x += amplitude * np.exp(1j * (2.0 * math.pi * if_hz * t + tone.phase_rad))
            truth.append({'kind': 'signal' if n == 1 else 'lo_harmonic',
                          'order': n, 'sign': 1, 'if_hz': if_hz,
                          'amplitude': amplitude, 'rf_hz': tone.rf_hz})

    for item in intermod:
        rf_hz = float(item['rf_hz'])
        amplitude = float(item.get('amplitude', 1.0))
        phase = float(item.get('phase_rad', 0.0))
        if_hz = rf_hz - lo_hz
        if abs(if_hz) > bandwidth_hz / 2.0:
            continue
        x += amplitude * np.exp(1j * (2.0 * math.pi * if_hz * t + phase))
        truth.append({'kind': 'intermod', 'order': int(item.get('order', 3)),
                      'sign': 1, 'if_hz': if_hz, 'amplitude': amplitude,
                      'rf_hz': rf_hz})

    dc = complex(dc[0], dc[1]) if not isinstance(dc, complex) else dc
    x += dc
    if dc:
        truth.append({'kind': 'dc', 'order': 0, 'sign': 1, 'if_hz': 0.0,
                      'amplitude': abs(dc), 'rf_hz': lo_hz})

    if phase_noise_rad:
        x *= np.exp(1j * rng.normal(0.0, float(phase_noise_rad), samples))

    c, d = iq_coefficients(iq_gain_db, iq_phase_deg)
    y = c * x + d * np.conj(x)
    if d != 0:
        for item in list(truth):
            truth.append({'kind': 'image', 'order': item.get('order', 1),
                          'sign': -1, 'if_hz': -item['if_hz'],
                          'amplitude': item['amplitude'] * abs(d / c) if c else 0.0,
                          'rf_hz': item.get('rf_hz')})

    if noise_amplitude:
        y = y + (rng.normal(size=samples) + 1j * rng.normal(size=samples)) * (
            float(noise_amplitude) / math.sqrt(2.0))
    return y, truth


def welch_psd(iq, rate_hz, fft_size=1024, segments=None, overlap=0.5, window='hann'):
    """Complex Welch PSD with a two-sided frequency axis.

    The normalization is ``|FFT(x*w)|**2 / (rate*sum(w**2))`` per segment,
    averaged over segments.  For white complex noise of variance ``sigma**2``
    the mean PSD is ``sigma**2/rate`` and ``mean(psd)*rate`` recovers the
    variance.  The axis is fft-shifted and covers ``[-rate/2, rate/2)``.
    """
    x = np.asarray(iq, dtype=np.complex128)
    rate_hz = float(rate_hz)
    fft_size = int(fft_size)
    if x.ndim != 1 or x.size < fft_size or fft_size < 8:
        raise ValueError('iq must be one-dimensional and at least fft_size long')
    if rate_hz <= 0:
        raise ValueError('rate_hz must be positive')
    if segments is None:
        step = max(1, int(round(fft_size * (1.0 - float(overlap)))))
        segments = 1 + (x.size - fft_size) // step
    segments = int(segments)
    if segments < 1:
        raise ValueError('segments must be at least 1')
    step = max(1, int(round(fft_size * (1.0 - float(overlap)))))
    if x.size < fft_size + (segments - 1) * step:
        raise ValueError('not enough samples for the requested number of segments')
    if window == 'hann':
        w = np.hanning(fft_size)
    elif window in (None, 'boxcar', 'rect'):
        w = np.ones(fft_size)
    else:
        raise ValueError('window must be hann or boxcar')
    scale = 1.0 / (rate_hz * float(np.sum(w ** 2)))
    psd = np.zeros(fft_size, dtype=float)
    for k in range(segments):
        chunk = x[k * step:k * step + fft_size] * w
        spectrum = np.fft.fft(chunk)
        psd += np.abs(spectrum) ** 2
    psd *= scale / segments
    freqs = np.fft.fftshift(np.fft.fftfreq(fft_size, d=1.0 / rate_hz))
    return freqs, np.fft.fftshift(psd), 2 * segments


def simulate_psd(lo_hz, rate_hz, tones=(), fft_size=1024, segments=20,
                 orders=(1, 3, 5, 7), dc=(0.0, 0.0), iq_gain_db=0.0,
                 iq_phase_deg=0.0, noise_amplitude=1.0, intermod=(),
                 seed=0, bandwidth_hz=None):
    """Convenience wrapper: time-domain twin -> Welch PSD + truth."""
    samples = fft_size + max(0, segments - 1) * max(1, int(round(fft_size * 0.5)))
    iq, truth = simulate_iq(lo_hz, rate_hz, samples, tones=tones, orders=orders,
                            dc=dc, iq_gain_db=iq_gain_db,
                            iq_phase_deg=iq_phase_deg,
                            noise_amplitude=noise_amplitude, intermod=intermod,
                            seed=seed, bandwidth_hz=bandwidth_hz)
    freqs, psd, dof = welch_psd(iq, rate_hz, fft_size=fft_size,
                                segments=segments, overlap=0.5)
    # The image truth is relative to c, but the PSD is relative; keep the
    # labels and frequencies, and let callers compare amplitudes in dB.
    return {'if_hz': freqs, 'power': psd, 'dof': dof, 'truth': truth,
            'lo_hz': float(lo_hz), 'rate_hz': float(rate_hz),
            'fft_size': int(fft_size), 'segments': int(segments)}


def tone_power_db(tone, reference=1.0):
    """Power of a tone in dB relative to a reference amplitude."""
    if reference <= 0:
        raise ValueError('reference must be positive')
    return 20.0 * math.log10(max(float(tone.amplitude), 1e-300) / reference)
