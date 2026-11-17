import json, numpy as np, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
U='/mnt/user-data/uploads/soapy_power/sessions/'
c=json.load(open(U+'fullrange-combined/compare.json')); S=json.load(open(U+'dash_suite.json'))
on,off=c['variants']['features-on'],c['variants']['features-off']; A,B=S['telemetry_on'],S['telemetry_off']
BL,OR,GR,RD,GY,PU='#0072B2','#E69F00','#009E73','#D55E00','#6b7280','#CC79A7'
plt.rcParams.update({'font.size':9,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.alpha':.25,'axes.titlesize':10.5,'axes.titleweight':'bold','axes.titlelocation':'left'})
F=lambda v:np.array([np.nan if x is None else x for x in v],float)
mo,lo_,oo=np.array(on['cell_mhz']),np.array(on['cell_level_db']),np.array(on['cell_observations'])
mf,lf,of=np.array(off['cell_mhz']),np.array(off['cell_level_db']),np.array(off['cell_observations'])
common=np.array(sorted(set(mo[(mo>=70)&(mo<6000)])&set(mf[(mf>=70)&(mf<6000)])))
io={m:i for i,m in enumerate(mo)}; jf={m:i for i,m in enumerate(mf)}
a=np.array([lo_[io[m]] for m in common]); o=np.array([lf[jf[m]] for m in common]); no=np.array([oo[io[m]] for m in common]); nf=np.array([of[jf[m]] for m in common])
d=a-o; d-=np.median(d); sd=1.4826*np.median(np.abs(d))
roll=lambda x,w=41:np.array([np.median(x[max(0,i-w//2):i+w//2+1]) for i in range(len(x))])
fig=plt.figure(figsize=(22,17),dpi=105); gs=GridSpec(5,6,figure=fig,top=.93,bottom=.04,left=.055,right=.985,hspace=.55,wspace=.5,height_ratios=[.9,1.2,1.2,1.2,1])
fig.suptitle('Effects of features ON vs OFF — B200mini, antenna, 70 MHz–6 GHz, 2×300 s  (+ 100–300 MHz suite, telemetry on/off)',x=.055,ha='left',fontsize=17,fontweight='bold',y=.985)
fig.text(.055,.955,'ON = rf-state + headroom(2 retries) + ensure-coverage + telemetry policy.  OFF = plain scan.  Relative, uncalibrated.  Antenna: differences contain RF-environment change; null = robust sd of Δ.',fontsize=10,color=GY)
# A: effect forest
ax=fig.add_subplot(gs[0,:3])
rows=[('records / s',on['records_per_s']/off['records_per_s']-1,'cost'),('records total',on['records']/off['records']-1,'cost'),('hops per pass (plan size)',215/849-1,'cost'),('passes in 300 s',on['passes']/off['passes']-1,'gain'),
 ('observations / cell',np.median(oo)/np.median(of)-1,'cost'),('cells with <10 obs',((oo<10).sum()-(of<10).sum())/max(1,(of<10).sum()) if (of<10).sum() else 0,'n'),
 ('suite: IF residual MAD',A['checks']['if_fit']['residual_mad_db']/B['checks']['if_fit']['residual_mad_db']-1,'n'),('suite: repeatability σ',A['checks']['repeatability']['sigma_db']/B['checks']['repeatability']['sigma_db']-1,'gain'),
 ('suite: masked mixing obs',A['checks']['spurious_responses']['masked_observations']/B['checks']['spurious_responses']['masked_observations']-1,'n'),('suite: holdout improvement (abs dB)',A['checks']['if_holdout']['improvement_db']-B['checks']['if_holdout']['improvement_db'],'n')]
rows=[r for r in rows if r[0]!='cells with <10 obs']
y=np.arange(len(rows))[::-1]
cols={'cost':OR,'gain':GR,'n':GY}
ax.barh(y,[r[1]*100 for r in rows],color=[cols[r[2]] for r in rows]); ax.set_yticks(y); ax.set_yticklabels([r[0] for r in rows]); ax.axvline(0,color='k',lw=.8)
for yy,r in zip(y,rows): ax.text(r[1]*100+(1.5 if r[1]>=0 else -1.5),yy,f'{r[1]*100:+.1f} %' if 'abs' not in r[0] else f'{r[1]:+.4f} dB',va='center',ha='left' if r[1]>=0 else 'right',fontsize=8.5)
ax.set_xlim(-90,215); ax.set_title('Effect of ON relative to OFF (percent change)'); ax.set_xlabel('% (orange = cost, green = benefit, grey = neutral)')
# B: rate over time
ax=fig.add_subplot(gs[0,3:])
for nm,x,col in (('ON',on,BL),('OFF',off,OR)):
    t=np.array(x['hop_series'])[:,0]; bins=np.arange(0,305,10); h,_=np.histogram(t,bins); ax.step(bins[:-1],h*20/10,where='post',color=col,label=f'{nm}  mean {x["records_per_s"]:.1f}/s')
ax.set_title('Record rate over time (sampled ×20, 10 s bins)'); ax.set_xlabel('s'); ax.set_ylabel('records/s'); ax.legend(frameon=False); ax.set_ylim(0,None)
# C: delta vs RF with rolling median and band
ax=fig.add_subplot(gs[1,:4]); ax.scatter(common,np.clip(d,-8,8),s=2,color=GY,alpha=.6); ax.plot(common,roll(d),color=BL,lw=1.2,label='rolling median (41 cells)')
ax.axhspan(-3*sd,3*sd,color=GR,alpha=.12,label=f'±3 robust sd ({3*sd:.2f} dB)'); out=np.abs(d)>3*sd; ax.scatter(common[out&(np.abs(d)<=8)],d[out&(np.abs(d)<=8)],s=4,color=RD)
ax.set_title('Δ = ON − OFF per 1 MHz cell (median offset removed, clipped ±8)'); ax.set_xlabel('RF MHz'); ax.set_ylabel('dB'); ax.legend(frameon=False,ncol=2,loc='upper right')
for lab,x0,x1 in (('FM',88,108),('VHF',150,175),('GSM/LTE 800–960',790,960),('2.4 GHz',2400,2500)): ax.axvspan(x0,x1,color=OR,alpha=.12); ax.text((x0+x1)/2,7.3,lab,ha='center',fontsize=7)
# D: band table
ax=fig.add_subplot(gs[1,4:]); ax.axis('off'); ax.set_title('Δ by band')
bands=[(70,300),(300,1000),(1000,2000),(2000,3000),(3000,4500),(4500,6000)]
cell=[]
for x0,x1 in bands:
    m=(common>=x0)&(common<x1); dd=d[m]; cell.append([f'{x0}–{x1}',f'{np.median(dd):+.2f}',f'{1.4826*np.median(np.abs(dd-np.median(dd))):.2f}',f'{(np.abs(dd)>3*sd).sum()}/{m.sum()}',f'{np.max(np.abs(dd)):.1f}'])
t=ax.table(cellText=cell,colLabels=['band MHz','median Δ','robust sd','cells>3σ','max |Δ|'],loc='upper left',cellLoc='left'); t.auto_set_font_size(False); t.set_fontsize(9); t.scale(1,1.6)
for (r,cc),ce in t.get_celld().items():
    ce.set_edgecolor('#d1d5db')
    if r==0: ce.set_facecolor('#e5e7eb')
ax.text(0,.02,f'null sd of Δ over all cells: {sd:.2f} dB\n(includes environment change + estimation noise)',transform=ax.transAxes,fontsize=9,color=GY)
# E: delta vs level
ax=fig.add_subplot(gs[2,:2]); h=ax.hexbin(o,np.clip(d,-6,6),gridsize=(70,40),bins='log',cmap='viridis',mincnt=1); ax.axhline(0,color='w',lw=.8)
ax.set_title('Δ vs OFF level (level-dependent effect?)'); ax.set_xlabel('OFF level dB'); ax.set_ylabel('Δ dB'); fig.colorbar(h,ax=ax,label='cells (log)')
wk=o<np.percentile(o,50); st=o>np.percentile(o,99)
ax.text(.02,.95,f'weakest 50 %: median Δ {np.median(d[wk]):+.3f}\nstrongest 1 %: median Δ {np.median(d[st]):+.2f}',transform=ax.transAxes,va='top',color='w',fontsize=8.5)
# F: delta vs obs ratio
ax=fig.add_subplot(gs[2,2:4]); ax.scatter(no/nf,np.clip(d,-6,6),s=3,color=PU,alpha=.5); ax.set_title('Δ vs observation-depth ratio ON/OFF'); ax.set_xlabel('obs ON / obs OFF per cell'); ax.set_ylabel('Δ dB')
# G: CDF of |Δ| vs gaussian null
ax=fig.add_subplot(gs[2,4:]); x=np.sort(np.abs(d)); ax.plot(x,np.arange(1,len(x)+1)/len(x),color=BL,label='observed |Δ|')
from math import erf,sqrt
g=np.linspace(0,6,300); ax.plot(g,[erf(v/(sd*sqrt(2))) for v in g],'--',color=GY,label=f'Gaussian null σ={sd:.2f}'); ax.set_xlim(0,4); ax.set_title('CDF of |Δ|: tails beyond the null = real differences'); ax.set_xlabel('|Δ| dB'); ax.legend(frameon=False,loc='lower right')
ax.text(.4,.55,f'{(np.abs(d)>3*sd).mean()*100:.1f} % of cells beyond 3σ\n(null: 0.27 %)',transform=ax.transAxes,fontsize=9)
# H: suite Δ
ax=fig.add_subplot(gs[3,:3]); ds=F(A['median_db'])-F(B['median_db']); ds-=np.nanmedian(ds); ax.plot(A['rf_mhz'],ds,lw=.6,color=GR); ax.axhline(0,color=GY,lw=.8); ax.set_ylim(-3,3)
ax.set_title(f'100–300 MHz suite: Δ = telemetry on − off per 100 kHz cell (robust sd {1.4826*np.nanmedian(np.abs(ds)):.2f} dB)'); ax.set_xlabel('RF MHz'); ax.set_ylabel('dB')
ax=fig.add_subplot(gs[3,3:]); dif=F(A['if_db'])-F(B['if_db']); se=F(A['if_se']); ax.plot(A['if_mhz'],dif-np.nanmean(dif),color=PU); ax.fill_between(A['if_mhz'],-2*se,2*se,color=GY,alpha=.25,label='±2 se of ON fit'); ax.axhline(0,color='k',lw=.6)
ax.set_title('IF response ON − OFF (gauge removed)'); ax.set_xlabel('IF MHz'); ax.set_ylabel('dB'); ax.legend(frameon=False)
ax.text(.02,.05,f'RMS {np.sqrt(np.nanmean((dif-np.nanmean(dif))**2)):.3f} dB',transform=ax.transAxes,fontsize=9,color=GY)
# I: top differences table + conclusions
ax=fig.add_subplot(gs[4,:2]); ax.axis('off'); ax.set_title('Largest |Δ| cells (full range)')
idx=np.argsort(-np.abs(d))[:10]; t=ax.table(cellText=[[f'{int(common[i])}',f'{d[i]:+.1f}',f'{o[i]:.1f}',f'{a[i]:.1f}'] for i in idx],colLabels=['MHz','Δ dB','OFF dB','ON dB'],loc='upper left',cellLoc='left'); t.auto_set_font_size(False); t.set_fontsize(8.5); t.scale(1,1.3)
for (r,cc),ce in t.get_celld().items():
    ce.set_edgecolor('#d1d5db')
    if r==0: ce.set_facecolor('#e5e7eb')
ax=fig.add_subplot(gs[4,2:]); ax.axis('off'); ax.set_title('What the features do, and what the data can and cannot say')
tx=[f'• Throughput: ON records {on["records_per_s"]/off["records_per_s"]*100-100:+.0f} % per second (rf-state snapshots, headroom checks, telemetry); plan is coarser (215 vs 849 hops/pass) so passes rise 24 vs 8.3.',
    f'• Depth: ON ≈{np.median(oo):.0f} obs per cell, OFF ≈{np.median(of):.0f}. OFF reaches 2× the depth per cell in the same 300 s.',
    f'• Level agreement: median Δ {np.median(a-o):+.2f} dB, robust σ {sd:.2f} dB; {(np.abs(d)>3*sd).mean()*100:.1f} % of cells beyond 3σ, concentrated at 2.4 GHz (ON +19 dB), 70–300 MHz, 800–960 MHz and ~2.1 GHz.',
    '• No rejected IQ with headroom on: no overload on this antenna, so headroom had nothing to do.',
    '• Suite: IF fit/holdout agree within 0.01 dB with and without telemetry; telemetry adds 1905 fresh temperatures and 0 reads while streaming.',
    '• Cannot say: each feature’s own effect (only all-on vs all-off), absolute level (uncalibrated), or temperature slope (span 0.9 °C, unresolved).',
    '• To isolate: 50 Ω input and one feature at a time (headroom, ensure-coverage, rf-state, telemetry policy).']
for i,t_ in enumerate(tx): ax.text(0,.97-i*.14,t_,fontsize=9.3,va='top',transform=ax.transAxes,wrap=True)
fig.text(.985,.008,'soapy_power · fullrange-20261117-214517 (off) / -215049 (on) · okt-20261117-214125/214317 · master c4e8c03',ha='right',fontsize=8,color=GY)
fig.savefig('effects_on_off.png',bbox_inches='tight')
print(sd,(np.abs(d)>3*sd).mean(),np.median(d[wk]),np.median(d[st]))
