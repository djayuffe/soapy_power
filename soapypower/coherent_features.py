"""Train/held-out local carrier-phase features; never blind RF suppression."""
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def coherent_features(samples, sample_rate, fft_size=8192, peak_excess_db=8.,
                      phase_lock_gate=.7, min_validation_excess_db=8., max_candidates=32,
                      origin_exclusion_hz=250000., usable_halfwidth_hz=14000000.):
    """Select/freeze candidates on first half; validate on disjoint second half.

    Short Hann frames overlap 50% inside each partition only. Derotate the known
    FFT-bin phase advance, fit remaining phase slope on training frames, and
    predict held-out phasors without any phase/frequency refit. PLV is a
    descriptive gate, not a significance test. Long-Hann validation spectra use
    a different ENBW; increased tone PSD height is not increased signal power.
    """
    try:
        from scipy.ndimage import median_filter
        from scipy.signal import find_peaks
    except ImportError as error:
        raise ValueError('Coherent phase features require optional SciPy; install soapy_power[fft]') from error
    raw=np.asarray(samples)
    if raw.ndim!=1 or raw.dtype.kind!='c' or not np.isfinite(raw).all():
        raise ValueError('Expected finite one-dimensional complex IQ')
    if (not np.isfinite(sample_rate) or sample_rate<=0 or type(fft_size) is not int or
        fft_size<32 or fft_size%2 or type(max_candidates) is not int or max_candidates<1 or
        not np.isfinite(peak_excess_db) or peak_excess_db<=0 or
        not np.isfinite(min_validation_excess_db) or min_validation_excess_db<=0 or
        not np.isfinite(phase_lock_gate) or not 0<phase_lock_gate<=1 or
        not np.isfinite(origin_exclusion_hz) or not np.isfinite(usable_halfwidth_hz) or
        not 0<=origin_exclusion_hz<usable_halfwidth_hz<=sample_rate/2):
        raise ValueError('Invalid coherent-feature settings')
    half=len(raw)//2
    if half<fft_size*8:raise ValueError('Need at least eight FFT lengths in each disjoint partition')
    # Equal, disjoint partitions. The odd trailing sample, if any, is unused.
    train=np.array(raw[:half],dtype=np.complex128);test=np.array(raw[half:2*half],dtype=np.complex128)
    dc_train=train.mean();dc_test=test.mean()
    train-=dc_train;test-=dc_train  # fitted centering frozen before validation
    step=fft_size//2;win=.5-.5*np.cos(2*np.pi*np.arange(fft_size)/fft_size)
    trframes=sliding_window_view(train,fft_size)[::step];teframes=sliding_window_view(test,fft_size)[::step]
    ft=np.fft.fftshift(np.fft.fft(trframes*win,axis=1),axes=1)
    fv=np.fft.fftshift(np.fft.fft(teframes*win,axis=1),axes=1)
    f=np.fft.fftshift(np.fft.fftfreq(fft_size,1/sample_rate));scale=sample_rate*np.sum(win**2)
    ptr=np.mean(abs(ft)**2,axis=0)/scale;pval=np.mean(abs(fv)**2,axis=0)/scale
    floor=median_filter(ptr,size=129,mode='nearest');tiny=np.finfo(float).tiny
    excess=10*np.log10(np.maximum(ptr,tiny)/np.maximum(floor,tiny));eligible=(abs(f)>=origin_exclusion_hz)&(abs(f)<usable_halfwidth_hz)
    indices,_=find_peaks(excess,height=peak_excess_db,distance=5)
    indices=indices[eligible[indices]];indices=indices[np.argsort(excess[indices])[::-1]][:max_candidates]
    ttrain=np.arange(len(ft))*step/sample_rate;ttest=(half+np.arange(len(fv))*step)/sample_rate
    longwin=.5-.5*np.cos(2*np.pi*np.arange(half)/half)
    fine=np.fft.fftshift(np.fft.fft(test*longwin));fine_f=np.fft.fftshift(np.fft.fftfreq(half,1/sample_rate));fine_psd=abs(fine)**2/(sample_rate*np.sum(longwin**2))
    results=[];traces=[]
    for k in indices:
        train_phasor=ft[:,k]*np.exp(-2j*np.pi*f[k]*ttrain)
        phase=np.unwrap(np.angle(train_phasor));weights=abs(train_phasor)**2
        design=np.column_stack([ttrain,np.ones(len(ttrain))]);coef=np.linalg.lstsq(design*np.sqrt(weights[:,None]),phase*np.sqrt(weights),rcond=None)[0]
        delta=coef[0]/(2*np.pi);estimate=f[k]+delta
        predicted_train=(2*np.pi*f[k]+coef[0])*ttrain+coef[1]
        predicted_test=(2*np.pi*f[k]+coef[0])*ttest+coef[1]
        residual_train=ft[:,k]*np.exp(-1j*predicted_train)
        residual_test=fv[:,k]*np.exp(-1j*predicted_test)
        def plv(z):
            magnitude=abs(z);valid=magnitude>0
            return float(abs(np.mean(z[valid]/magnitude[valid]))) if valid.any() else 0.
        train_plv=plv(residual_train);test_plv=plv(residual_test)
        test_fraction=float(abs(residual_test.mean())**2/np.mean(abs(residual_test)**2))
        # Matched-frequency complex integration on held-out raw samples. Window
        # midpoint leakage phase is absorbed in the fitted frame intercept;
        # absolute intercept is not used to alter power or retune hardware.
        n=np.arange(half)+half
        matched=np.sum(test*longwin*np.exp(-2j*np.pi*estimate*n/sample_rate))
        coherent_density=float(abs(matched)**2/(sample_rate*np.sum(longwin**2)))
        j=int(np.argmin(abs(fine_f-estimate)));neighbors=np.r_[np.arange(j-24,j-7),np.arange(j+8,j+25)];neighbors=neighbors[(neighbors>=0)&(neighbors<half)]
        background=float(np.median(fine_psd[neighbors]))
        validation_excess=float(10*np.log10(max(coherent_density,tiny)/max(background,tiny)))
        accepted=bool(abs(delta)<=sample_rate/fft_size/2 and train_plv>=phase_lock_gate and test_plv>=phase_lock_gate and validation_excess>=min_validation_excess_db)
        results.append(dict(fft_bin=int(k),baseband_hz=float(estimate),bin_center_hz=float(f[k]),sub_bin_offset_hz=float(delta),training_excess_db=float(excess[k]),training_phase_lock=train_plv,validation_phase_lock=test_plv,validation_coherent_fraction=test_fraction,validation_fine_excess_db=validation_excess,coherent_density=coherent_density,accepted=accepted))
        traces.append(dict(train_time_seconds=ttrain.copy(),validation_time_seconds=ttest.copy(),training_phase_residual_deg=np.degrees(np.angle(residual_train)),validation_phase_residual_deg=np.degrees(np.angle(residual_test))))
    dc_before=float(abs(dc_test)**2);dc_after=float(abs(dc_test-dc_train)**2)
    return dict(candidates=results,traces=traces,frequency_hz=f,training_psd=ptr,validation_psd=pval,fine_frequency_hz=fine_f,fine_validation_psd=fine_psd,
                sample_rate_hz=float(sample_rate),fft_size=fft_size,train_samples=half,validation_samples=half,unused_samples=len(raw)-2*half,
                train_frame_count=len(ft),validation_frame_count=len(fv),training_sample_stop_exclusive=half,validation_sample_start=half,
                short_bin_spacing_hz=sample_rate/fft_size,fine_bin_spacing_hz=sample_rate/half,
                short_enbw_hz=float(sample_rate*np.sum(win**2)/np.sum(win)**2),fine_enbw_hz=float(sample_rate*np.sum(longwin**2)/np.sum(longwin)**2),
                dc_train_mean=dc_train,dc_validation_mean=dc_test,dc_trial_mean_power_before=dc_before,dc_trial_mean_power_after=dc_after,
                correction_applied_to_source=False,phase_lock_gate=phase_lock_gate,peak_excess_gate_db=peak_excess_db)
