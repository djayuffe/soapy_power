"""One-off figure script for the 2026-11-17/18 full-range features-on/off reports (docs/results/fullrange-*).

Inputs: SESSIONS/fullrange-combined/{compare,waterfall}.json and SESSIONS/dash_suite.json plus the floor/spur benchmark JSON;
SESSIONS = $SPR_SESSIONS (default ./sessions); benchmark = $SPR_BENCH_JSON (default /tmp/bench_full.json).
Writes PNG/PDF files into the current directory.
"""
import os
import json, numpy as np, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
U=os.environ.get('SPR_SESSIONS','sessions').rstrip('/')+'/'
c=json.load(open(U+'fullrange-combined/compare.json')); S=json.load(open(U+'dash_suite.json')); b=json.load(open(os.environ.get('SPR_BENCH_JSON','/tmp/bench_full.json')))
on,off=c['variants']['features-on'],c['variants']['features-off']; ag=c['agreement']
A,B=S['telemetry_on'],S['telemetry_off']
BL,OR,GR,RD,GY,PU='#0072B2','#E69F00','#009E73','#D55E00','#6b7280','#CC79A7'
plt.rcParams.update({'font.size':9,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.alpha':.25,'axes.titlesize':11,'axes.titleweight':'bold','axes.titlelocation':'left'})
F=lambda v:np.array([np.nan if x is None else x for x in v],float)
TOTAL=8
def page(n,title,sub,rows,cols,ratios=None):
    fig=plt.figure(figsize=(18,12),dpi=110)
    fig.suptitle(title,x=.05,ha='left',fontsize=17,fontweight='bold',y=.985)
    fig.text(.05,.945,sub,fontsize=10,color=GY)
    fig.text(.95,.012,f'soapy_power · B200mini 30B2A32 · 2026-11-17 · page {n}/{TOTAL} · relative, uncalibrated levels',ha='right',fontsize=8,color=GY)
    gs=GridSpec(rows,cols,figure=fig,top=.915,bottom=.05,left=.06,right=.97,hspace=.5,wspace=.4,height_ratios=ratios)
    return fig,gs
def save(fig,n,name): fig.savefig(f'pages/{n:02d}_{name}.png',bbox_inches='tight'); plt.close(fig)
def table(ax,rows,header,colw=None,fs=9,title=None):
    ax.axis('off')
    if title: ax.set_title(title)
    t=ax.table(cellText=rows,colLabels=header,loc='upper left',cellLoc='left',colWidths=colw)
    t.auto_set_font_size(False); t.set_fontsize(fs); t.scale(1,1.45)
    for (r,cc),cell in t.get_celld().items():
        cell.set_edgecolor('#d1d5db')
        if r==0: cell.set_facecolor('#e5e7eb'); cell.set_text_props(fontweight='bold')
def tile(ax,a,v,d,col=None):
    ax.axis('off'); ax.add_patch(plt.Rectangle((0,0),1,1,transform=ax.transAxes,fc='#f3f4f6',ec='#d1d5db'))
    ax.text(.05,.78,a,fontsize=9,color=GY,transform=ax.transAxes); ax.text(.05,.4,v,fontsize=15,fontweight='bold',transform=ax.transAxes,color=col or 'black'); ax.text(.05,.1,d,fontsize=8,color=GY,transform=ax.transAxes)

# ---------- 1 overview
fig,gs=page(1,'Overview: before → after','Everything measured or tested on 2026-11-17: B200 full range, 100–300 MHz suite, estimator benchmark, real-session telemetry',4,4,[.6,1.5,1.2,1.2])
tiles=[('Full range','70–6000 MHz','2 variants × 300 s, antenna'),('Features on',f"{on['records']} rec",f"{on['passes']:.0f} passes · {on['records_per_s']:.1f} rec/s"),('Features off',f"{off['records']} rec",f"{off['passes']:.1f} passes · {off['records_per_s']:.1f} rec/s"),('Python tests','485 passed','was 473 · +12 new')]
for i,t in enumerate(tiles): tile(fig.add_subplot(gs[0,i]),*t)
rows=[['Median floor bias, dof 2','−1.59 dB','+0.002 dB'],['Median floor bias, dof 10','−0.29 dB','+0.002 dB'],['Median floor bias, dof 64','−0.045 dB','0.000 dB'],
 ['2-sweep min-hold bias, dof 2','−3.01 dB','+0.002 dB'],['2-sweep min-hold bias, dof 10','−1.22 dB','+0.003 dB'],
 ['False alarms per noise bin (dof 8)','2.6e-4 (fixed 6 dB)','1.0e-5 (BH), ~0 (Bonferroni)'],['Detect +8 dB line','80 %','60 % (BH)'],['Detect +12 dB line','97 %','96 % (BH)'],
 ['T1–T5 mislabelled peaks (of 360)','120','0'],['T1–T5 false / missed','0 / 0','0 / 0'],['Floor-vs-T error, oscillating T','0.019–0.022 dB/°C','0.010–0.015 dB/°C'],
 ['Floor-vs-T, monotone T','0.076–0.089 dB/°C, no warning','0.061–0.068, flagged confounded 100 %'],
 ['bladeRF DC residual median','0.0280','0.0026'],['bladeRF DC residual p90','0.108','0.0085']]
table(fig.add_subplot(gs[1:3,:3]),rows,['Measure','Before','After'],[.42,.28,.30],fs=10,title='Before / after (synthetic truth; bladeRF = hardware)')
ax=fig.add_subplot(gs[1:3,3]); ax.axis('off'); ax.set_title('Verdict')
for i,(t,col) in enumerate([('Bias removed: floor now unbiased',GR),('Spurs/images now labelled correctly',GR),('FDR costs sensitivity at +8 dB',OR),('Floor-vs-T slope still unresolved on real data',RD),('Features-on is 27 % slower',OR),('bladeRF not connected today',RD)]):
    ax.text(0,.92-i*.15,'● '+t,color=col,fontsize=10.5,transform=ax.transAxes,fontweight='bold')
ax=fig.add_subplot(gs[3,:]); ax.axis('off'); ax.set_title('Sources')
ax.text(0,.7,'fullrange-20261117-214517 (features off) · fullrange-20261117-215049 (features on) · fullrange-combined/compare.json\nokt-20261117-193257 (telemetry session) · okt-20261117-214125 (telemetry on) · okt-20261117-214317 (telemetry off)\ntools/benchmark_floor_spur.py · soapypower/floor_estimation.py · spur_twin.py · EXPERIMENTS.md · master 555c31a',fontsize=10,va='top',transform=ax.transAxes)
save(fig,1,'overview')

# ---------- 2 full-range spectrum bands
fig,gs=page(2,'Full range spectrum, features on vs off','Median level per 1 MHz cell (dB, relative). Lower panel in each band: on − off (median offset removed). Antenna input.',8,2,[1.6,.7]*4)
bands=[(70,300),(300,1000),(1000,3000),(3000,6000)]
mo,lo_=np.array(on['cell_mhz']),np.array(on['cell_level_db']); mf,lf=np.array(off['cell_mhz']),np.array(off['cell_level_db'])
don=dict(zip(mo,lo_)); doff=dict(zip(mf,lf)); cm=np.array(sorted(set(don)&set(doff))); dd=np.array([don[m]-doff[m] for m in cm]); dd-=np.median(dd)
for i,(a,z) in enumerate(bands):
    ax=fig.add_subplot(gs[2*i,:]); s1=(mo>=a)&(mo<z); s2=(mf>=a)&(mf<z)
    ax.plot(mo[s1],lo_[s1],lw=.7,color=BL,label='features on'); ax.plot(mf[s2],lf[s2],lw=.7,color=OR,alpha=.85,label='features off'); ax.set_title(f'{a}–{z} MHz'); ax.set_ylabel('dB'); ax.legend(loc='upper right',frameon=False,ncol=2)
    ax2=fig.add_subplot(gs[2*i+1,:],sharex=ax); s=(cm>=a)&(cm<z); ax2.bar(cm[s],np.clip(dd[s],-6,6),width=1,color=np.where(np.abs(dd[s])>3,RD,GY)); ax2.set_ylabel('Δ dB'); ax2.set_ylim(-6,6)
    if i==3: ax2.set_xlabel('RF MHz')
save(fig,2,'fullrange_spectrum')

# ---------- 3 full-range operations
fig,gs=page(3,'Full range: acquisition, coverage and agreement','Throughput, hop planning, observation depth, temperature and where the variants disagree',3,3)
ax=fig.add_subplot(gs[0,0]); 
for nm,x,col in (('on',on,BL),('off',off,OR)):
    h=np.array(x['hop_series']); ax.plot(h[:,0],h[:,1],'.',ms=2,color=col,label=nm)
ax.set_title('Hop centre vs time (sampled)'); ax.set_xlabel('s'); ax.set_ylabel('MHz'); ax.legend(frameon=False,markerscale=5)
ax=fig.add_subplot(gs[0,1]); ob=np.array(on['cell_observations']); of=np.array(off['cell_observations'])
ax.hist(ob,bins=np.arange(0,60,2),color=BL,alpha=.8,label=f'on (median {np.median(ob):.0f})'); ax.hist(of,bins=np.arange(0,60,2),color=OR,alpha=.6,label=f'off (median {np.median(of):.0f})'); ax.set_title('Observations per 1 MHz cell'); ax.set_xlabel('observations'); ax.legend(frameon=False)
ax=fig.add_subplot(gs[0,2]); t=np.array(on['temperature_series']); ax.plot(t[:,0],t[:,1],color=RD,lw=.8); ax.set_title('RFIC temperature (features on)'); ax.set_xlabel('s'); ax.set_ylabel('°C')
ax.text(.02,.93,f"{on['temperature_records']} fresh readings · span {t[:,1].min():.1f}–{t[:,1].max():.1f} °C",transform=ax.transAxes,fontsize=8,color=GY)
ax=fig.add_subplot(gs[1,0]); ax.hist(np.clip(dd,-4,4),bins=100,color=GR); ax.set_yscale('log'); ax.set_title('On − off per cell (offset removed)'); ax.set_xlabel('dB (clipped ±4)'); ax.set_ylabel('cells')
ax=fig.add_subplot(gs[1,1]); mid=(mo[1:]+mo[:-1])/2
ax.plot(cm,np.abs(dd),'.',ms=1.5,color=GY); ax.axhline(3,color=RD,lw=1); ax.set_yscale('log'); ax.set_ylim(1e-3,1e2); ax.set_title('|Δ| per cell vs RF'); ax.set_xlabel('MHz'); ax.set_ylabel('dB')
ax=fig.add_subplot(gs[1,2]); lab=['records','records/s','passes','obs/cell (median)']; vals=[(on['records'],off['records']),(on['records_per_s'],off['records_per_s']),(on['passes'],off['passes']),(np.median(ob),np.median(of))]
for i,(a_,o_) in enumerate(vals):
    m_=max(a_,o_); ax.bar(i-.2,a_/m_,.4,color=BL); ax.bar(i+.2,o_/m_,.4,color=OR); ax.text(i-.2,a_/m_+.02,f'{a_:.4g}',ha='center',fontsize=8); ax.text(i+.2,o_/m_+.02,f'{o_:.4g}',ha='center',fontsize=8)
ax.set_xticks(range(4)); ax.set_xticklabels(lab,fontsize=8); ax.set_ylim(0,1.2); ax.set_yticks([]); ax.set_title('Throughput relative to larger')
big=sorted([(abs(dd[i]),cm[i],dd[i]) for i in range(len(cm)) if abs(dd[i])>3],reverse=True)[:12]
table(fig.add_subplot(gs[2,0]),[[f'{int(m)}',f'{d:+.1f}'] for _,m,d in big],['MHz','on−off dB'],[.4,.4],title='12 largest differences')
rows=[['Records',on['records'],off['records']],['Distinct hops (rounded)',on['hops'],off['hops']],['Passes',f"{on['passes']:.1f}",f"{off['passes']:.1f}"],['Seconds',f"{on['seconds']:.1f}",f"{off['seconds']:.1f}"],['Records/s',f"{on['records_per_s']:.2f}",f"{off['records_per_s']:.2f}"],['Cells (1 MHz)',on['cells_1MHz'],off['cells_1MHz']],['Median level dB',f"{on['cell_level_median_db']:.2f}",f"{off['cell_level_median_db']:.2f}"],['Temperature records',on['temperature_records'],off['temperature_records']]]
table(fig.add_subplot(gs[2,1]),rows,['Metric','features on','features off'],[.45,.28,.28],title='Session summary')
rows=[['Common cells',ag['common_cells']],['Median diff dB',f"{ag['median_diff_db']:.3f}"],['Robust sd dB',f"{ag['robust_sd_db']:.3f}"],['p95 |Δ| dB',f"{ag['p95_abs_diff_db']:.3f}"],['Cells > 3 dB',ag['cells_over_3db']],['Rejected IQ records','0']]
table(fig.add_subplot(gs[2,2]),rows,['Agreement','value'],[.6,.35],title='On vs off agreement')
save(fig,3,'fullrange_operations')

# ---------- 4 suite 100-300
fig,gs=page(4,'100–300 MHz suite: telemetry on vs off','Same procedure, 15 passes, IF-corrected cell levels (100 kHz cells), fitted IF response, checks',4,3,[1.3,1.1,1,1])
ax=fig.add_subplot(gs[0,:]); ax.plot(A['rf_mhz'],F(A['median_db']),lw=.7,color=BL,label='telemetry on'); ax.plot(B['rf_mhz'],F(B['median_db']),lw=.7,color=OR,alpha=.85,label='telemetry off')
ax.fill_between(A['rf_mhz'],F(A['p10']),F(A['p90']),color=BL,alpha=.12,label='on p10–p90'); ax.set_title('Cell level'); ax.set_ylabel('dB'); ax.legend(frameon=False,ncol=3,loc='upper right')
ax=fig.add_subplot(gs[1,0]); ax.plot(A['rf_mhz'],F(A['median_db'])-F(B['median_db']),lw=.6,color=GR); ax.axhline(0,color=GY,lw=.8); ax.set_title('on − off, per cell'); ax.set_xlabel('RF MHz'); ax.set_ylabel('dB'); ax.set_ylim(-3,3)
ax=fig.add_subplot(gs[1,1]); ax.plot(A['if_mhz'],F(A['if_db']),color=BL,label='on'); ax.plot(B['if_mhz'],F(B['if_db']),color=OR,label='off'); ax.fill_between(A['if_mhz'],F(A['if_db'])-F(A['if_se']),F(A['if_db'])+F(A['if_se']),color=BL,alpha=.2); ax.set_title('IF response (fitted)'); ax.set_xlabel('IF MHz'); ax.legend(frameon=False)
ax=fig.add_subplot(gs[1,2]); dif=F(A['if_db'])-F(B['if_db']); ax.plot(A['if_mhz'],dif,color=PU); ax.axhline(0,color=GY,lw=.8); ax.set_title('IF response on − off'); ax.set_xlabel('IF MHz'); ax.set_ylabel('dB')
ax.text(.02,.05,f'RMS {np.sqrt(np.nanmean(dif**2)):.3f} dB (mean {np.nanmean(dif):+.3f} = gauge)',transform=ax.transAxes,fontsize=8,color=GY)
ck=lambda sec,k:(A['checks'][sec].get(k),B['checks'][sec].get(k))
rows=[]
for l,sec,k in [('IF fit residual MAD dB','if_fit','residual_mad_db'),('IF fit identified slots','if_fit','identified_slots'),('IF fit redundancy (median)','if_fit','median_redundancy'),('Holdout improvement dB','if_holdout','improvement_db'),('Holdout improvement sd dB','if_holdout','improvement_sd_db'),('Holdout spread after dB','if_holdout','spread_after_db'),('Repeatability σ dB','repeatability','sigma_db'),('Repeatability p95 dB','repeatability','p95_abs_db'),('Repeatability cells','repeatability','cells'),('Mixing masked observations','spurious_responses','masked_observations'),('Mixing masked hops','spurious_responses','masked_hops')]:
    a_,b_=ck(sec,k); rows.append([l]+[('—' if v is None else f'{v:.4g}') for v in (a_,b_)])
t=A['checks']['telemetry']; rows+= [['Telemetry fresh temperature records',str(t['records_with_fresh_temperature']),'0 (off)'],['Temperature span °C',f"{t['temperature_span_c']:.3f}",'—'],['Max |trial slope| °C/min',f"{t['max_abs_trial_slope_c_per_min']:.2f}",'—'],['Chain stable / reads while streaming',f"{t['chain_stable']} / {t['temperature_reads_while_streaming']}",'—']]
table(fig.add_subplot(gs[2:,:2]),rows,['Check','telemetry on','telemetry off'],[.5,.25,.25],fs=9.5,title='Analysis checks (all 3 required: passed in both)')
ax=fig.add_subplot(gs[2:,2]); ax.axis('off'); ax.set_title('Reading')
for i,tx in enumerate(['IF fit and holdout agree within 0.01 dB','Repeatability differs (0.13 vs 0.18 dB σ):','  environment varies between passes, not a feature effect','Telemetry adds 1905 fresh temperatures, 0 reads while streaming','Mixing responses: 3 laws identified, ~16–17 k obs masked']):
    ax.text(0,.92-i*.13,tx,fontsize=9.5,transform=ax.transAxes)
save(fig,4,'suite_100_300')

# ---------- 5 floor estimator
fig,gs=page(5,'Estimators: floor bias, detection and false alarms','Synthetic truth (Gamma-distributed bins). Legacy = dB-median, min-hold, fixed 6 dB threshold. New = dof-corrected mean, BH/Bonferroni',3,3)
ax=fig.add_subplot(gs[0,0]); dofs=[x['dof'] for x in b['floor_bias']]; xs=np.arange(3); w=.2
for j,(k,col,lab_) in enumerate((('median_bias_db_legacy',OR,'median legacy'),('median_bias_db_new',BL,'median new'),('minhold2_bias_db_legacy',RD,'min-hold legacy'),('minhold2_bias_db_new',GR,'min-hold new'))):
    vals=[x[k] for x in b['floor_bias']]; ax.bar(xs+(j-1.5)*w,vals,w,color=col,label=lab_)
    for x_,v_ in zip(xs,vals): ax.text(x_+(j-1.5)*w,min(v_,0)-.08 if v_<-.2 else .05,f'{v_:+.2f}',ha='center',fontsize=6.5,rotation=90,va='top' if v_<-.2 else 'bottom')
ax.set_xticks(xs); ax.set_xticklabels([f'dof {d}' for d in dofs]); ax.set_title('Floor bias (dB)'); ax.legend(frameon=False,fontsize=7,loc='lower right')
ax=fig.add_subplot(gs[0,1]); ax.errorbar(range(3),[x['median_bias_db_new'] for x in b['floor_bias']],yerr=[x['new_sd_db'] for x in b['floor_bias']],fmt='o',color=BL,capsize=5); ax.axhline(0,color=GY); ax.set_xticks(range(3)); ax.set_xticklabels([f'dof {d}' for d in dofs]); ax.set_title('New floor estimate: mean ± sd across trials (dB)')
ax=fig.add_subplot(gs[0,2]); det=b['detection']; names=['fixed_6db','bh_0.01','bonferroni_0.01']; cols=[OR,BL,GR]
for n_,col in zip(names,cols):
    r=det[n_]['detect_rate_by_db']; ax.plot([float(k) for k in r],list(r.values()),'o-',color=col,label=n_)
ax.set_title('Detection rate vs line strength'); ax.set_xlabel('line above floor, dB'); ax.set_ylim(0,1.05); ax.legend(frameon=False)
ax=fig.add_subplot(gs[1,0]); ax.bar(names,[det[n]['false_positive_rate'] for n in names],color=cols); ax.set_yscale('log'); ax.set_title('False-alarm rate per noise bin'); ax.tick_params(axis='x',labelsize=8)
for i,n in enumerate(names): ax.text(i,det[n]['false_positive_rate']*1.3 if det[n]['false_positive_rate']>0 else 1e-6,f"{det[n]['false_positive_rate']:.1e}",ha='center',fontsize=8)
ax=fig.add_subplot(gs[1,1]); ax.bar(names,[det[n]['false_positives'] for n in names],color=cols); ax.set_title(f"False alarms in {det['fixed_6db']['noise_bins']:,} noise bins"); ax.tick_params(axis='x',labelsize=8)
for i,n in enumerate(names): ax.text(i,det[n]['false_positives']+3,str(det[n]['false_positives']),ha='center')
rows=[[n_,f"{det[n_]['false_positive_rate']:.2e}"]+[f"{v:.0%}" for v in det[n_]['detect_rate_by_db'].values()] for n_ in names]
table(fig.add_subplot(gs[1,2]),rows,['method','FP rate','+4 dB','+8 dB','+12 dB'],[.34,.2,.14,.14,.14],title='Detection table')
ax=fig.add_subplot(gs[2,:]); ax.axis('off'); ax.set_title('Interpretation')
ax.text(0,.8,'• A bin averaged over K segments is Gamma(K): its dB-median lies below the mean by 10·log10(median/mean) = −1.59 / −0.29 / −0.05 dB at dof 2 / 10 / 64; min-hold of 2 sweeps by −3.0 / −1.2 / −0.45 dB.\n• floor_estimate corrects this from the dof (estimated from the data when not given), excludes spur bins, and returns a distribution-free CI.\n• Benjamini–Hochberg controls the false-discovery rate; the price is lower power on weak lines (+8 dB: 80 % → 60 %). Choose bonferroni for look-elsewhere-safe claims, fixed only for quick looks.',fontsize=10,va='top',transform=ax.transAxes)
save(fig,5,'estimators_floor_detection')

# ---------- 6 classification
fig,gs=page(6,'Spur classification and the T1–T5 matrix','Digital twin of a direct-conversion receiver: DC leak, IQ image (−35 dB), square-wave LO harmonics 3/5/7. LO diversity: 260, 262, 258 MHz.',3,3,[1.1,1.1,1])
per=b['classification']['per_scenario']; names=list(per)
ax=fig.add_subplot(gs[0,:2]); xs=np.arange(len(names))
ax.bar(xs-.2,[per[n]['legacy']['kind_errors'] for n in names],.4,color=OR,label='legacy: mislabelled'); ax.bar(xs+.2,[per[n]['new']['kind_errors'] for n in names],.4,color=BL,label='new: mislabelled')
ax.set_xticks(xs); ax.set_xticklabels([n.split(' ',1)[0]+'\n'+n.split(' ',1)[1] for n in names],fontsize=8); ax.set_title('Mislabelled peaks over 60 trials per scenario'); ax.legend(frameon=False)
ax=fig.add_subplot(gs[0,2]); tot=b['classification']['total']; rows=[[k,tot['legacy'][k],tot['new'][k]] for k in ('tp','fp','missed','kind_errors')]
table(ax,rows,['total','legacy','new'],[.4,.25,.25],title='All scenarios (360 true peaks)')
ax=fig.add_subplot(gs[1,0]); import sys; sys.path.insert(0,os.path.join(os.path.dirname(os.path.abspath(__file__)),'..'))
from soapypower import spur_twin as st
sc=st.scenarios()['T3 800 MHz out-of-band tone']; los=sc['los']
tw=st.Twin(seed=3); caps=[tw.capture(l,tones=sc['tones']) for l in los]
for l,cp,col in zip(los,caps,(BL,OR,GR)): ax.plot(tw.if_hz/1e6,10*np.log10(cp),lw=.5,color=col,label=f'LO {l/1e6:.0f} MHz',alpha=.8)
ax.set_xlim(-28,28); ax.set_title('T3: 800 MHz tone, 3 LO settings'); ax.set_xlabel('IF MHz'); ax.set_ylabel('dB'); ax.legend(frameon=False,fontsize=8)
ax=fig.add_subplot(gs[1,1]); lo=np.array([258,260,262])
for lab_,col,y in (('signal 265 MHz, slope −1',BL,265-lo),('IQ image, slope +1',GR,-(265-lo)),('3rd harmonic of 800 MHz, slope −3',RD,800-3*lo)):
    ax.plot(lo,y,'o-',color=col,label=lab_)
ax.set_title('IF vs LO: slope gives the order'); ax.set_xlabel('LO MHz'); ax.set_ylabel('IF MHz'); ax.legend(frameon=False,fontsize=8)
ax=fig.add_subplot(gs[1,2]); drive=[-10,-5,0,5]; ax2=ax
for o_,col in ((1,BL),(2,OR),(3,RD)): ax.plot(drive,[o_*d+7 for d in drive],'o-',color=col,label=f'order {o_} (slope {o_})')
ax.set_title('Level vs drive: IMD order'); ax.set_xlabel('drive dB'); ax.set_ylabel('level dB'); ax.legend(frameon=False,fontsize=8)
rows=[]
for n in names:
    res=st.analyze(st.Twin(seed=1),st.scenarios()[n]['los'],tones=st.scenarios()[n]['tones']); exp=st.scenarios()[n]['expected']
    rows.append([n,', '.join(f"{e[0]} {e[1]/1e6:+.0f}" for e in exp if len(e)==2) or '—',', '.join(f"{p['kind']} {p['if_hz']/1e6:+.1f}" for p in res['peaks']) or '—',per[n]['legacy']['kind_errors'],per[n]['new']['kind_errors']])
table(fig.add_subplot(gs[2,:]),rows,['Scenario','Expected (kind, IF MHz)','New pipeline found','legacy mislabelled /60','new mislabelled /60'],[.22,.3,.3,.09,.09],fs=8.5,title='T1–T5 matrix (one seeded run)')
save(fig,6,'classification_T1_T5')

# ---------- 7 floor vs temperature
fig,gs=page(7,'Floor vs temperature','OLS within centre vs Kalman filter with drifting offset. A temperature that only rises cannot be separated from drift (flag: confounded).',2,3,[1.2,1])
bt=b['floor_vs_temperature']
for j,prof in enumerate(('monotone','oscillating')):
    ax=fig.add_subplot(gs[0,j]); r=[x for x in bt if x['temperature_profile']==prof]; xs=np.arange(len(r))
    ax.bar(xs-.2,[x['ols_rms_error'] for x in r],.4,color=OR,label='OLS rms error'); ax.bar(xs+.2,[x['kalman_rms_error'] for x in r],.4,color=BL,label='Kalman rms error')
    ax.plot(xs+.2,[x['kalman_reported_sd'] for x in r],'k_',ms=22,label='Kalman reported sd'); ax.set_xticks(xs); ax.set_xticklabels([f"true {x['true_slope_db_per_c']:+.3f}" for x in r]); ax.set_ylabel('dB/°C'); ax.set_title(f'{prof} temperature: slope error'); ax.legend(frameon=False,fontsize=8)
    ax.text(.02,.9,f"flagged confounded: {r[0]['flagged_confounded_share']:.0%}",transform=ax.transAxes,fontsize=9,color=RD if r[0]['flagged_confounded_share'] else GR)
ax=fig.add_subplot(gs[0,2]); ax.errorbar([0.0243],[1],xerr=[0.031],fmt='o',color=OR,capsize=5,label='OLS +0.024 ± 0.031'); ax.errorbar([-0.0184],[0],xerr=[0.0565],fmt='o',color=BL,capsize=5,label='Kalman −0.018 ± 0.057')
ax.axvline(0,color=GY); ax.set_yticks([]); ax.set_ylim(-.7,1.7); ax.set_xlabel('dB/°C'); ax.set_title('Real session okt-20261117-193257'); ax.legend(frameon=False,loc='upper left',fontsize=8)
ax=fig.add_subplot(gs[1,0]); t=np.array(on['temperature_series']); ax.plot(t[:,0],t[:,1],color=RD,lw=.8); ax.set_title('Full-range run: RFIC temperature'); ax.set_xlabel('s'); ax.set_ylabel('°C')
rows=[[x['temperature_profile'],f"{x['true_slope_db_per_c']:+.3f}",f"{x['ols_rms_error']:.4f}",f"{x['kalman_rms_error']:.4f}",f"{x['kalman_reported_sd']:.4f}",f"{x['flagged_confounded_share']:.0%}"] for x in bt]
table(fig.add_subplot(gs[1,1:]),rows,['profile','true dB/°C','OLS rms err','Kalman rms err','Kalman sd','flagged'],[.18,.15,.17,.17,.15,.12],fs=9,title='Benchmark table (30 trials each)')
save(fig,7,'floor_vs_temperature')

# ---------- 8 history, limits, next
fig,gs=page(8,'Hardware history, limits and next steps','What was verified on hardware, what is still open',2,2,[1.2,1])
ax=fig.add_subplot(gs[0,0]); ax.bar(np.arange(2)-.2,[0.0280,0.108],.4,color=OR,label='before'); ax.bar(np.arange(2)+.2,[0.0026,0.0085],.4,color=BL,label='after'); ax.set_xticks(range(2)); ax.set_xticklabels(['median','p90']); ax.set_yscale('log'); ax.set_title('bladeRF x40 + XB-200: DC residual (dense per-centre calibration)'); ax.legend(frameon=False)
rows=[['B200 telemetry: fresh temperatures','5741 at 46.3–47.2 °C'],['Floor vs T (OLS, antenna)','+0.024 ± 0.031 dB/°C (now: unresolved)'],['IF response vs UHD 3.15 run','within 0.126 dB RMS'],['LO placement','on origin'],['bladeRF floor change','−0.05 dB'],['bladeRF throughput','12.3–12.5 of 12.8 hops/s'],['Master','555c31a']]
table(fig.add_subplot(gs[0,1]),rows,['Item','Result'],[.45,.55],fs=9.5,title='Hardware results')
ax=fig.add_subplot(gs[1,0]); ax.axis('off'); ax.set_title('Limits')
for i,t in enumerate(['All levels relative: no dBm, no noise figure','Antenna input: on/off differences include RF environment','Before-values in the estimator benchmark are simulated, not hardware','FDR trades sensitivity for false-alarm control','Floor-vs-T needs a swing in temperature; monotone warm-up is confounded','bladeRF not found by the probe today (unplugged?)','Cells outside 70–6000 MHz in these two files are edge artefacts']):
    ax.text(0,.92-i*.125,'• '+t,fontsize=10,transform=ax.transAxes)
ax=fig.add_subplot(gs[1,1]); ax.axis('off'); ax.set_title('Next')
for i,t in enumerate(['Full range with 50 Ω: isolates feature effects from environment','Terminated run with temperature swing: resolves floor vs T','Signal generator (model, max safe dBm, cable loss): settling and image rejection','Reconnect bladeRF: survey with dense DC','Run T3-style hardware test with a real out-of-band tone']):
    ax.text(0,.92-i*.16,f'{i+1}. '+t,fontsize=10,transform=ax.transAxes)
save(fig,8,'history_limits_next')
