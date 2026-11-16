import os,json,csv,sys
from pathlib import Path
import numpy as np
import argparse,tempfile
parser=argparse.ArgumentParser(description='Expand the terminated B200 full-band bundle with local IQ phase diagnostics; no absolute phase calibration.')
parser.add_argument('directory',type=Path,help='Directory with the full-band matrices and the 27 archived IQ captures')
parser.add_argument('-o','--output',type=Path,help='Output directory (default: DIRECTORY/phase-analysis)')
args=parser.parse_args();source=args.directory.resolve();out=(args.output or source/'phase-analysis').resolve();out.mkdir(parents=True,exist_ok=True)
os.environ.setdefault('MPLCONFIGDIR',str(Path(tempfile.gettempdir())/'soapypower-mpl-cache'))
from soapypower.phase_diagnostics import analyze_phase
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse
plt.rcParams.update({'font.size':10,'figure.facecolor':'#0c1422','axes.facecolor':'#111e31','text.color':'#edf2fa','axes.labelcolor':'#d6dfed','xtick.color':'#b9c9df','ytick.color':'#b9c9df','axes.edgecolor':'#53647b','legend.facecolor':'#16253a','legend.labelcolor':'#edf2fa'})
settings=[(20,'off','#ffbd70'),(20,'on','#73c8ff'),(60,'on','#f18bb1')];centers=[70,100,400,915,1800,2440,3500,5800,6000];results={};rows=[];clouds={};arrays={}
for center in centers:
 for gain,dc,color in settings:
  name=f'iq-{center}M-g{gain}-dc{dc}';record=json.loads((source/(name+'.json')).read_text());integrity=record['integrity'];context=record['context'];assert integrity['continuity']=='verified' and integrity['measurement_status']=='complete' and integrity['timestamp_coverage']==1 and context['antenna']=='RX2' and context['gains']['PGA']==gain and context['dc_correction']==(dc=='on');iq=np.load(source/(name+'.npz'))['iq'];r=analyze_phase(iq,56e6)
  assert r['analyzed_samples']==len(iq)==1048576 and r['segment_count']==255
  assert abs(r['noncircularity_abs']-record['noncircularity_abs'])<1e-12
  band=(r['baseband_hz']>=.25e6)&(r['baseband_hz']<14e6);pairband=(r['pair_baseband_hz']>=.25e6)&(r['pair_baseband_hz']<14e6)
  row=dict(name=name,rx_center_hz=center*1e6,gain_db=gain,dc_correction=dc,circular_r1=r['circular_resultant_r1'],circular_r2=r['circular_resultant_r2'],noncircularity_abs=r['noncircularity_abs'],ellipse_axis_deg=r['ellipse_axis_deg'],iq_coherent_bin_fraction=float(np.mean(r['iq_coherence_squared'][band]>=.2)),mirror_coherent_bin_fraction=float(np.mean(r['complementary_coherence_squared'][pairband]>=.2)),delivered_unique_i_levels=int(np.unique(iq.real).size),delivered_unique_q_levels=int(np.unique(iq.imag).size),angular_samples=r['angular_samples'],angular_excluded_samples=r['angular_excluded_samples'],phase_gate_coherence_squared=.2,ellipse_gate_noncircularity=.05,selected_duration_seconds=r['selected_duration_seconds'])
  rows.append(row);results[(center,gain,dc)]=(r,row)
  for k,v in r.items():
   if isinstance(v,np.ndarray):arrays[name+'__'+k]=v
  if center in (100,915,2440,5800):
   z=iq.astype(complex);z-=z.mean();rms=np.sqrt(np.mean(abs(z)**2));hist,xe,ye=np.histogram2d(z.real/rms,z.imag/rms,bins=100,range=[[-3,3],[-3,3]]);clouds[(center,gain,dc)]=(hist,rms,r['covariance'],float(1-hist.sum()/len(z)),row['ellipse_axis_deg'])
  print('PHASE_DONE',name,'R2',round(row['circular_r2'],4),'axis',row['ellipse_axis_deg'],flush=True)
with (out/'phase-statistics.csv').open('w') as f:
 w=csv.DictWriter(f,fieldnames=rows[0].keys());w.writeheader();w.writerows(rows)
np.savez_compressed(out/'phase-spectra-and-statistics.npz',**arrays)

def save(fig,name,dpi=220):
 fig.savefig(out/(name+'.png'),dpi=dpi);fig.savefig(out/(name+'-preview.png'),dpi=100);plt.close(fig)

def grouped(ax,key,percent=False,scatter=False):
 for gain,dc,color in settings:
  group=[row for row in rows if row['gain_db']==gain and row['dc_correction']==dc];xs=[row['rx_center_hz']/1e9 for row in group];ys=[np.nan if row[key] is None else row[key]*(100 if percent else 1) for row in group]
  if scatter:ax.scatter(xs,ys,s=27,color=color,label=f'{gain} dB · DC {dc}')
  else:ax.plot(xs,ys,'o-',ms=4,lw=.8,color=color,label=f'{gain} dB · DC {dc}')
 ax.set_xlabel('Selected RX center (GHz)');ax.grid(alpha=.15);ax.legend(fontsize=8)

fig,axs=plt.subplots(3,2,figsize=(17,13));a=axs[0,0];grouped(a,'circular_r2');a.set_title('Angular two-lobedness R₂ · zero for uniform phase',loc='left');a.set_ylabel('|mean exp(2jθ)|')
a=axs[0,1];grouped(a,'ellipse_axis_deg',scatter=True);a.set_ylim(-95,95);a.set_title('Noise-ellipse principal axis · modulo 180°',loc='left');a.set_ylabel('Angle relative to local I axis (°)');a.axhline(0,color='#9aadc5',lw=.7)
a=axs[1,0];grouped(a,'iq_coherent_bin_fraction',percent=True);a.set_title('I/Q cross-phase support · 0.25–14 MHz IF',loc='left');a.set_ylabel('Bins with squared coherence ≥0.2 (%)')
a=axs[1,1];grouped(a,'mirror_coherent_bin_fraction',percent=True);a.set_title('Mirror-pair phase support · 0.25–14 MHz IF',loc='left');a.set_ylabel('Bins with squared coherence ≥0.2 (%)')
a=axs[2,0]
for gain,dc,color in settings:
 r,_=results[(2440,gain,dc)];e=r['phase_hist_edges_radians'];a.plot(np.degrees((e[:-1]+e[1:])/2),r['phase_hist_density_per_radian']*2*np.pi,color=color,label=f'{gain} dB · DC {dc}')
a.axhline(1,color='#9aadc5',ls='--',label='Uniform phase');a.set_xlabel('Mean-removed sample angle (°)');a.set_ylabel('Density / uniform density');a.set_title('2440 MHz phase distribution · radius >0.05 RMS',loc='left');a.legend(fontsize=8);a.grid(alpha=.15)
a=axs[2,1]
for gain,dc,color in settings:
 r,_=results[(2440,gain,dc)];a.scatter(r['frame_midpoint_seconds']*1000,r['frame_ellipse_axis_deg'],s=5,color=color,label=f'{gain} dB · DC {dc}')
a.set_ylim(-95,95);a.set_xlabel('Time inside each separate capture (ms)');a.set_ylabel('Ellipse-axis orientation (°)');a.set_title('2440 MHz within-capture ellipse orientation',loc='left');a.legend(fontsize=8);a.grid(alpha=.15)
fig.suptitle('Terminated RX2 · phase and second-order IQ structure',x=.08,ha='left',fontsize=21,y=.975)
fig.text(.08,.918,'27 raw-IQ captures at nine RF centers · 18.72 ms per capture · 255 overlapping Hann frames · no new acquisition\nCoherence ≥0.2 and ellipse strength ≥0.05 are descriptive display gates, not significance or calibration criteria.',color='#aec1da')
fig.text(.08,.025,'These are local I/Q-coordinate phase statistics. Captures after retuning have no common absolute phase reference.\nNo phase unwrapping across RF centers, absolute RF phase, group delay, oscillator phase noise or image-rejection calibration is inferred.',fontsize=10,color='#aec1da')
fig.subplots_adjust(left=.08,right=.96,top=.85,bottom=.115,hspace=.55,wspace=.3);save(fig,'phase-overview')

fig,axs=plt.subplots(3,4,figsize=(22,14));phasecm=plt.get_cmap('twilight_shifted').copy();phasecm.set_bad('#526477');cohcm=plt.get_cmap('viridis').copy();cohcm.set_bad('#526477')
for rr,(gain,dc,color) in enumerate(settings):
 first=results[(centers[0],gain,dc)][0]
 for col,(k,phase,grid) in enumerate([('iq_coherence_squared',False,'baseband_hz'),('iq_cross_phase_deg',True,'baseband_hz'),('complementary_coherence_squared',False,'pair_baseband_hz'),('complementary_phase_deg',True,'pair_baseband_hz')]):
  x=first[grid];use=(x>=.25e6)&(x<14e6);matrix=np.array([results[(center,gain,dc)][0][k][use] for center in centers]);a=axs[rr,col];im=a.imshow(matrix,aspect='auto',interpolation='nearest',extent=[x[use][0]/1e6,x[use][-1]/1e6,9,0],cmap=phasecm if phase else cohcm,vmin=-180 if phase else 0,vmax=180 if phase else 1)
  a.set_yticks(np.arange(9)+.5,labels=[str(c) for c in centers]);a.set_ylabel(f'{gain} dB · DC {dc}\nRX center (MHz)');a.set_xlabel('Positive IF offset (MHz)');a.set_title(['I/Q squared coherence','I/Q cross-phase · conj(I) × Q','Mirror-pair squared coherence','Mirror-pair phase · Z(+f) × Z(−f)'][col],fontsize=11)
  if rr==2:
   cax=fig.add_axes([.065+col*.235,.07,.17,.012]);cb=fig.colorbar(im,cax=cax,orientation='horizontal');cb.set_label('Phase (°)' if phase else 'Squared coherence',fontsize=10)
fig.suptitle('Spectral phase support · measured at nine discrete RX centers',x=.065,ha='left',y=.978,fontsize=22)
fig.text(.065,.927,'Gray phase cells: squared coherence <0.2 or zero spectral support. RF rows are discrete captures, not interpolated phase response.\nPhase is relative to local I/Q axes. Mirror correlation can indicate improper noise or coupled sidebands; it does not identify a physical image or a valid correction.',color='#aec1da')
fig.subplots_adjust(left=.065,right=.97,top=.86,bottom=.16,hspace=.4,wspace=.38);save(fig,'spectral-phase-support')

fig,axs=plt.subplots(3,4,figsize=(18,14))
for rr,(gain,dc,color) in enumerate(settings):
 for cc,center in enumerate((100,915,2440,5800)):
  hist,rms,cov,outside,axis=clouds[(center,gain,dc)];a=axs[rr,cc];a.imshow(np.log10(hist.T+1),origin='lower',extent=[-3,3,-3,3],cmap='magma',vmin=0,vmax=4,aspect='equal',interpolation='nearest');values,vectors=np.linalg.eigh(cov/rms**2);ix=np.argsort(values)[::-1];values=values[ix];vector=vectors[:,ix[0]];angle=np.degrees(np.arctan2(vector[1],vector[0]));a.add_patch(Ellipse((0,0),2*np.sqrt(values[0]),2*np.sqrt(values[1]),angle=angle,fill=False,edgecolor='#73c8ff',lw=1.3))
  if axis is not None:a.plot([-2*np.cos(np.deg2rad(axis)),2*np.cos(np.deg2rad(axis))],[-2*np.sin(np.deg2rad(axis)),2*np.sin(np.deg2rad(axis))],ls='--',lw=.8,color='#73c8ff')
  a.set_title(f'{center} MHz · {gain} dB · DC {dc}\naxis '+('undefined' if axis is None else f'{axis:.1f}°')+f' · outside view {outside*100:.3f}%',fontsize=10);a.set_xlabel('I / complex RMS');a.set_ylabel('Q / complex RMS')
fig.suptitle('IQ noise clouds · covariance ellipses and phase orientation',x=.08,ha='left',y=.98,fontsize=21)
fig.text(.08,.935,'All selected samples contribute to 100×100-bin density maps; color = log₁₀(count + 1), common scale 0–4.\nGlobal sample mean removed; each cloud normalized by its own complex RMS. Blue ellipse = one covariance standard-deviation contour.',color='#aec1da')
fig.text(.08,.025,'Dashed orientation shown only for noncircularity ≥0.05. Angle is relative to I/Q axes and defined modulo 180°.\nRMS normalization hides absolute level differences; the previous amplitude diagnostic and raw IQ retain those levels.',color='#aec1da')
fig.subplots_adjust(left=.08,right=.95,top=.86,bottom=.1,hspace=.45,wspace=.35);save(fig,'iq-noise-clouds')

# Extend the full-band scalar measurement with stability and phase context.
d=np.load(source/'terminated-baseline-data.npz');meta=json.loads((source/'sweep-provenance.json').read_text());edges=d['frequency_edges_hz'];f=(edges[:-1]+edges[1:])/2;baseline=d['baseline_mean_db'];hold=d['holdout_db'];difference=d['holdout_difference_db'];inside=(f>=70e6)&(f<=6000e6);unstable=inside&(abs(difference)>3)
starts=np.flatnonzero(unstable&~np.r_[False,unstable[:-1]]);stops=np.flatnonzero(unstable&~np.r_[unstable[1:],False])+1;regions=[]
for a,b in zip(starts,stops):
 k=a+np.argmax(abs(difference[a:b]));regions.append(dict(start_hz=edges[a],stop_hz=edges[b],cells=int(b-a),largest_change_frequency_hz=f[k],signed_change_db=difference[k],baseline_db=baseline[k],held_out_db=hold[k]))
regions.sort(key=lambda r:abs(r['signed_change_db']),reverse=True)
with (out/'unstable-frequency-regions.csv').open('w') as file:
 w=csv.DictWriter(file,fieldnames=regions[0].keys());w.writeheader();w.writerows(regions)
fig=plt.figure(figsize=(19,20));gs=fig.add_gridspec(6,3,height_ratios=[1.2,1,.9,1,1,.9],hspace=.65,wspace=.42)
a=fig.add_subplot(gs[0,:]);a.fill_between(f/1e9,d['baseline_repeat_p10_db'],d['baseline_repeat_p90_db'],color='#73c8ff',alpha=.3,label='3-repeat observed P10–P90');a.plot(f/1e9,baseline,color='#73c8ff',lw=.65,label='Gain 20 · DC on baseline');a.plot(f/1e9,hold,color='#7be0b8',lw=.6,label='Independent final check');a.set_ylabel('dB(input power/Hz)');a.set_title('Full-band measured baseline · retained central IF, origin excluded',loc='left');a.legend(fontsize=9)
a=fig.add_subplot(gs[1,:]);a.fill_between(f/1e9,-3,3,color='#73c8ff',alpha=.07);a.plot(f/1e9,difference,lw=.6,color='#7be0b8');a.scatter(f[unstable]/1e9,difference[unstable],s=3,color='#f18bb1',label='|difference| >3 dB');a.axhline(0,color='#9aadc5',lw=.7);a.set_ylabel('Held-out − baseline (dB)');a.set_title(f'Stability mask · {int(unstable.sum())} in-spec cells in {len(regions)} contiguous display regions exceed 3 dB',loc='left');a.legend(fontsize=9)
for col,(low,high) in enumerate(((70e6,1e9),(1e9,3e9),(3e9,6e9))):
 a=fig.add_subplot(gs[2,col]);mask=(f>=low)&(f<=high);a.plot(f[mask]/1e9,baseline[mask],lw=.7,color='#73c8ff');a.plot(f[mask]/1e9,hold[mask],lw=.7,color='#7be0b8');a.set_xlim(low/1e9,high/1e9);a.set_ylabel('dB(input power/Hz)');a.set_title(f'Detail · {low/1e9:.2f}–{high/1e9:g} GHz',loc='left')
a=fig.add_subplot(gs[3,:]);colors={0:'#9aadc5',20:'#73c8ff',40:'#ffbd70',60:'#f18bb1'}
for gain in (0,20,40,60):
 ii=[i for i,m in enumerate(meta) if m['gain_db']==gain and m['dc_correction']=='on'];p=10*np.log10(np.mean(10**(d['mean_density_db'][ii]/10),axis=0));a.plot(f/1e9,p,lw=.65,color=colors[gain],label=f'{gain} dB gain')
a.set_ylabel('dB(input power/Hz)');a.set_title('Output noise versus gain · no nominal-gain-to-dBm conversion',loc='left');a.legend(fontsize=9,ncol=4)
a=fig.add_subplot(gs[4,:]);image=a.imshow(d['mean_density_db'],aspect='auto',extent=[edges[0]/1e9,edges[-1]/1e9,8,0],interpolation='nearest',cmap='magma',vmin=-142,vmax=-118);a.set_yticks(np.arange(8)+.5,labels=[f"{m['gain_db']} dB · DC {m['dc_correction']} · "+('check' if 'holdout' in m['phase'] else f"repeat {m['sweep']}") for m in meta],fontsize=9);a.set_title('Eight accepted full-band sweeps · common scale −142 to −118 dB(input power/Hz)',loc='left');fig.colorbar(image,ax=a,pad=.01,fraction=.015,label='dB(input power/Hz)')
for col,(key,title,ylabel) in enumerate([('circular_r2','Phase distribution at nine RX centers','Angular R₂'),('ellipse_axis_deg','Ellipse-axis phase · only noncircularity ≥0.05','Orientation (°)'),('iq_coherent_bin_fraction','I/Q spectral phase support · 0.25–14 MHz','Coherent IF bins (%)')]):
 a=fig.add_subplot(gs[5,col]);grouped(a,key,percent=key.endswith('fraction'),scatter=key=='ellipse_axis_deg');a.set_title(title,loc='left',fontsize=10);a.set_ylabel(ylabel)
for a in list(fig.axes)[:7]:
 a.grid(alpha=.12)
 if a.get_title(loc='left').startswith('Detail'):a.set_xlabel('RF frequency (GHz)');continue
 if a.get_title(loc='left').startswith(('Full-band','Stability','Output noise','Eight accepted')):a.set_xlim(.042,6.008);a.set_xlabel('RF frequency (GHz)');a.axvspan(.042,.07,color='#f18bb1',alpha=.15);a.axvspan(6,6.008,color='#f18bb1',alpha=.15)
fig.suptitle('RX2 · expanded full-band reference, stability and phase analysis',x=.11,ha='left',y=.982,fontsize=22)
fig.text(.11,.94,'Nominal 50 Ω load · ambient ≈23 °C · RF match unknown · 56 MS/s · raw FFT spacing 6.836 kHz · display cells 364.136 kHz\nPSD spans 42–6008 MHz. Phase statistics use raw IQ at nine discrete centers only; no RF-phase interpolation across retunes.',color='#aec1da')
fig.text(.11,.02,'Stability threshold is descriptive and not a detector. Variable peaks remain with the load; RF ingress, mismatch and internal sources are unresolved.\nP10–P90 spans three repeats and is not an uncertainty interval. Phase is local to I/Q axes, not a calibrated RF transfer function.\nNo absolute gain/NF, phase noise in dBc/Hz, group delay, image rejection or inverse correction is derived.',color='#aec1da')
fig.subplots_adjust(left=.11,right=.95,top=.885,bottom=.09);save(fig,'expanded-full-band')
summary=dict(captures=27,rf_centers_mhz=centers,raw_iq_samples=27*1048576,selected_duration_per_capture_seconds=1048576/56e6,phase_coherence_gate_squared=.2,ellipse_orientation_gate_noncircularity=.05,angular_radius_gate_fraction_of_rms=.05,cross_spectrum_convention='E[conj(FFT(I))*FFT(Q)], scipy.signal.csd convention',complementary_spectrum_convention='E[FFT(z)(+f)*FFT(z)(-f)]',absolute_phase_calibrated=False,rf_phase_coherent_across_captures=False,group_delay_measured=False,oscillator_phase_noise_measured=False,complex_correction_applied=False,unstable_fullband_cells=int(unstable.sum()),unstable_contiguous_regions=len(regions),maximum_noncircularity=max(r['noncircularity_abs'] for r in rows),maximum_angular_r2=max(r['circular_r2'] for r in rows),captures_with_defined_ellipse_axis=sum(r['ellipse_axis_deg'] is not None for r in rows),unit_test_count=10)
(out/'phase-analysis-summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary,indent=2),flush=True)
