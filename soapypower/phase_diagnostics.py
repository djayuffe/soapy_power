"""Local IQ phase statistics. No absolute phase, group-delay or image calibration."""
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def analyze_phase(samples, sample_rate, fft_size=8192, overlap=.5,
                  max_segments=255, coherence_gate=.2, angular_amplitude_gate=.05,
                  orientation_gate=.05):
    """Use global-mean-removed IQ and periodic-Hann Welch frames.

    I/Q cross spectrum follows scipy.signal.csd: E[conj(I_fft)*Q_fft].
    Complementary coherence uses E[Z(f)*Z(-f)]. Both return squared
    coherence; phases below the descriptive gate are NaN. The gate is not a
    statistical significance threshold. Ellipse orientation is modulo 180°.
    """
    z = np.asarray(samples)
    if z.ndim != 1 or z.dtype.kind != 'c' or not np.isfinite(z).all():
        raise ValueError('IQ must be a finite one-dimensional complex array')
    if (not np.isfinite(sample_rate) or sample_rate <= 0 or
        type(fft_size) is not int or fft_size < 8 or fft_size % 2 or
        type(max_segments) is not int or max_segments < 2 or
        not np.isfinite(overlap) or not 0 <= overlap < 1 or
        not np.isfinite(coherence_gate) or not 0 < coherence_gate <= 1 or
        not np.isfinite(angular_amplitude_gate) or not 0 <= angular_amplitude_gate < 1 or
        not np.isfinite(orientation_gate) or not 0 < orientation_gate <= 1):
        raise ValueError('Invalid phase analysis settings')
    step = fft_size-int(fft_size*overlap)
    count = min(max_segments, 1+(len(z)-fft_size)//step)
    if count < 2:
        raise ValueError('Phase coherence needs at least two complete frames')
    used = fft_size+(count-1)*step
    z = np.asarray(z[:used], dtype=np.complex128)
    mean = z.mean();z = z-mean
    power = float(np.mean(abs(z)**2))
    if not np.isfinite(power) or power <= 0:
        raise ValueError('Phase undefined for zero variance IQ')
    radius = abs(z);keep = radius > angular_amplitude_gate*np.sqrt(power)
    angles = np.angle(z[keep]);unit = z[keep]/radius[keep]
    r1 = abs(unit.mean());r2 = abs(np.mean(unit**2))
    pseudo = complex(np.mean(z*z));noncirc = abs(pseudo)/power
    axis = ((np.degrees(np.angle(pseudo))/2+90) % 180)-90
    covariance = np.cov(np.stack([z.real,z.imag]), bias=True)
    hist, edges = np.histogram(angles, bins=np.linspace(-np.pi,np.pi,73), density=True)
    frames = sliding_window_view(z,fft_size)[::step][:count]
    win = .5-.5*np.cos(2*np.pi*np.arange(fft_size)/fft_size)
    fi = np.fft.rfft(frames.real*win,axis=1)
    fq = np.fft.rfft(frames.imag*win,axis=1)
    pii = np.mean(abs(fi)**2,axis=0);pqq = np.mean(abs(fq)**2,axis=0)
    cross = np.mean(fi.conj()*fq,axis=0)
    denominator = pii*pqq
    coherence = np.divide(abs(cross)**2,denominator,out=np.full_like(pii,np.nan),where=denominator>0)
    coherence = np.clip(coherence,0,1)
    cross_phase = np.where(coherence>=coherence_gate,np.degrees(np.angle(cross)),np.nan)
    fz = np.fft.fft(frames*win,axis=1)
    k = np.arange(1,fft_size//2)  # DC and self-paired Nyquist excluded
    plus,minus = fz[:,k],fz[:,-k]
    pp = np.mean(abs(plus)**2,axis=0);pm = np.mean(abs(minus)**2,axis=0)
    complementary = np.mean(plus*minus,axis=0)
    pair_denominator = pp*pm
    pair_coherence = np.divide(abs(complementary)**2,pair_denominator,out=np.full_like(pp,np.nan),where=pair_denominator>0)
    pair_coherence = np.clip(pair_coherence,0,1)
    pair_phase = np.where(pair_coherence>=coherence_gate,np.degrees(np.angle(complementary)),np.nan)
    frame_power = np.mean(abs(frames)**2,axis=1)
    frame_pseudo = np.mean(frames*frames,axis=1)
    frame_noncirc = abs(frame_pseudo)/frame_power
    frame_axis = ((np.degrees(np.angle(frame_pseudo))/2+90)%180)-90
    frame_axis = np.where(frame_noncirc>=orientation_gate,frame_axis,np.nan)
    return dict(sample_rate_hz=float(sample_rate),fft_size=fft_size,segment_count=count,
                analyzed_samples=used,unexamined_samples=len(samples)-used,
                selected_duration_seconds=used/sample_rate,
                coherence_gate=coherence_gate,orientation_gate=orientation_gate,
                angular_amplitude_gate_rms=angular_amplitude_gate,
                angular_samples=int(keep.sum()),angular_excluded_samples=int((~keep).sum()),
                circular_resultant_r1=float(r1),circular_resultant_r2=float(r2),
                noncircularity_abs=float(noncirc),ellipse_axis_deg=float(axis) if noncirc>=orientation_gate else None,
                mean_iq=mean,covariance=covariance,
                phase_hist_density_per_radian=hist,phase_hist_edges_radians=edges,
                baseband_hz=np.fft.rfftfreq(fft_size,1/sample_rate),
                iq_coherence_squared=coherence,iq_cross_phase_deg=cross_phase,
                iq_cross_spectrum=cross,
                pair_baseband_hz=k*sample_rate/fft_size,
                complementary_coherence_squared=pair_coherence,
                complementary_phase_deg=pair_phase,
                sideband_power_ratio_db=10*np.log10(np.maximum(pp,np.finfo(float).tiny)/np.maximum(pm,np.finfo(float).tiny)),
                frame_midpoint_seconds=(np.arange(count)*step+fft_size/2)/sample_rate,
                frame_noncircularity_abs=frame_noncirc,frame_ellipse_axis_deg=frame_axis)
