"""Explicit common-duration/bandwidth IQ comparisons without RF phase alignment."""
from fractions import Fraction
import numpy as np
from soapypower.numerics import finite, integer
from soapypower.receiver_quality import headroom_metrics


def common_band_iq(samples, sample_rate, target_rate=2560000, duration=.016,
                   start_seconds=.0005, cutoff_hz=900000, taps=129, max_samples=16000000):
    """Return equal-bandwidth IQ, compensated for the shared FIR group delay.

    Raw code/headroom statistics are evaluated on native unfiltered samples.
    This FIR is a common observation filter, not inverse receiver calibration.
    Resampling uses a bounded rational ratio and Kaiser 8.6 polyphase filter.
    No absolute inter-receiver RF phase or simultaneity is implied.
    """
    from scipy.signal import resample_poly, firwin, lfilter
    fs = finite(sample_rate,'sample rate',positive=True)
    target = finite(target_rate,'target rate',positive=True)
    duration = finite(duration,'duration',positive=True)
    start = finite(start_seconds,'start',nonnegative=True)
    cutoff = finite(cutoff_hz,'cutoff',positive=True)
    taps = integer(taps,'FIR taps',3)
    limit = integer(max_samples,'maximum samples',1)
    if taps%2 != 1 or cutoff >= min(fs,target)/2:
        raise ValueError('common FIR requires odd taps and cutoff below both Nyquist limits')
    n,start_n = round(duration*target),round(start*target)
    if n < 1 or not np.isclose(n,duration*target,rtol=0,atol=1e-7) or not np.isclose(start_n,start*target,rtol=0,atol=1e-7):
        raise ValueError('duration and start must be integral target samples')
    delay = (taps-1)//2
    if start_n < delay:
        raise ValueError('start must exclude the FIR startup transient')
    ratio = (Fraction(str(target))/Fraction(str(fs))).limit_denominator(100000)
    if ratio.numerator > 100000 or not np.isclose(float(ratio),target/fs,rtol=1e-12,atol=0):
        raise ValueError('sample rate ratio cannot be represented within the resampler budget')
    required = int(np.ceil((start_n+n+delay+64)*fs/target))
    if max(required,start_n+n+delay+64,taps) > limit:
        raise ValueError('common-IQ workspace sample budget exceeded')
    z = np.asarray(samples)
    if z.ndim != 1 or z.dtype.kind != 'c' or len(z)<required or not np.isfinite(z[:required]).all():
        raise ValueError('insufficient finite native complex IQ including filter guards')
    native = z[:required]
    y = resample_poly(native.astype(np.complex128),ratio.numerator,ratio.denominator,window=('kaiser',8.6))
    filt = firwin(taps,cutoff,fs=target,window=('kaiser',8.6))
    y = lfilter(filt,[1.],y)[start_n+delay:start_n+delay+n].copy()
    if len(y)!=n:
        raise ValueError('insufficient resampled guard samples')
    return y,dict(sample_rate_hz=target,samples=n,duration_s=duration,source_start_s=start,
                  resampling=[ratio.numerator,ratio.denominator],fir_taps=taps,
                  fir_cutoff_hz=cutoff,group_delay_compensated_samples=delay,
                  native_headroom=headroom_metrics(native),source_modified=False,
                  absolute_phase_aligned=False,receiver_response_equalized=False)


def equal_duration_phase(samples, sample_rate, duration=.016, bin_hz=10000, max_samples=16000000, **options):
    """Run the existing held-out phase diagnostics with identical FFT spacing."""
    from soapypower.coherent_features import coherent_features
    fs = finite(sample_rate,'sample rate',positive=True)
    duration = finite(duration,'duration',positive=True)
    df = finite(bin_hz,'bin spacing',positive=True)
    n,bins = round(fs*duration),round(fs/df)
    if max(n,bins) > integer(max_samples,'phase maximum samples',1):
        raise ValueError('phase sample workspace budget exceeded')
    if bins<8 or bins%2 or not np.isclose(fs/df,bins,rtol=0,atol=1e-7) or not np.isclose(fs*duration,n,rtol=0,atol=1e-7):
        raise ValueError('common phase analysis requires integral duration and even FFT size')
    z = np.asarray(samples)
    if z.ndim != 1 or len(z)<n:
        raise ValueError('insufficient IQ for common duration')
    options.setdefault('usable_halfwidth_hz',min(900000.,fs/2))
    options.setdefault('origin_exclusion_hz',min(250000.,options['usable_halfwidth_hz']/4))
    return coherent_features(z[:n],fs,fft_size=bins,**options)
