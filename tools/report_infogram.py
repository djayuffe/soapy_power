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
c=json.load(open(U+'fullrange-combined/compare.json')); S=json.load(open(U+'dash_suite.json')); Bn=json.load(open(os.environ.get('SPR_BENCH_JSON','/tmp/bench_full.json')))
on,off=c['variants']['features-on'],c['variants']['features-off']; ag=c['agreement']; A,B=S['telemetry_on'],S['telemetry_off']
BL,OR,GR,RD,GY,PU='#0072B2','#E69F00','#009E73','#D55E00','#6b7280','#CC79A7'
plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.alpha':.25,'axes.titlesize':12,'axes.titleweight':'bold','axes.titlelocation':'left'})
def cells(v):
    m=np.array(v['cell_mhz'],float); l=np.array([np.nan if x is None else x for x in v['cell_level_db']],float); o=np.array(v['cell_observations'],float)
    k=(m>=70)&(m<6000); return m[k],l[k],o[k]
mo,lo,oo=cells(on); mf,lf,of=cells(off)
common=np.array(sorted(set(mo)&set(mf))); io={m:i for i,m in enumerate(mo)}; jf={m:i for i,m in enumerate(mf)}
a=np.array([lo[io[m]] for m in common]); b=np.array([lf[jf[m]] for m in common]); d=a-b; d-=np.nanmedian(d); sd=1.4826*np.nanmedian(np.abs(d-np.nanmedian(d)))
def roll(x,w):
    return np.array([np.nanmedian(x[max(0,i-w//2):i+w//2+1]) for i in range(len(x))])
BANDS=[('FM 88–108',87.5,108),('Air 118–137',118,137),('VHF/DAB 174–240',174,240),('TETRA/PMR 380–470',380,470),('LTE800 791–862',791,862),('GSM900 880–960',880,960),('GNSS L1',1560,1590),('GSM1800 1710–1880',1710,1880),('UMTS 1920–2170',1920,2170),('ISM/WiFi 2.4G',2400,2483.5),('LTE2600 2500–2690',2500,2690),('3.5G 3400–3800',3400,3800),('WiFi 5G 5150–5850',5150,5850)]
def bandname(f):
    for n,l,h in BANDS:
        if l<=f<=h: return n
    return '—'
fig=plt.figure(figsize=(24,40),dpi=90)
gs=GridSpec(9,12,figure=fig,top=.955,bottom=.012,left=.045,right=.985,hspace=.5,wspace=.9,height_ratios=[.55,1.5,1.0,1.25,1.5,1.0,1.0,1.15,.75])
fig.text(.045,.985,'soapy_power — FULL RANGE 70 MHz–6 GHz, alt i ett bilde',fontsize=26,fontweight='bold',va='center')
fig.text(.045,.968,'B200mini 30B2A32 · antenne · 2026-11-17 · 2×300 s (features ON / OFF) + 100–300 MHz suite (0,1 MHz) · relative, ukalibrerte nivåer (ingen dBm) · bånd-navn er nominelle tildelinger, ikke verifisert identitet',fontsize=11.5,color=GY,va='center')
# KPI tiles
tiles=[('5 964','felles 1 MHz-celler\n70–6000 MHz',BL),(f'{on["records"]:,}'.replace(',',' '),'records ON\n17,2 rec/s',BL),(f'{off["records"]:,}'.replace(',',' '),'records OFF\n23,5 rec/s',OR),(f'{ag["median_diff_db"]:+.2f} dB','median Δ ON−OFF\nrobust sd 0,33 dB',GY),('75','celler |Δ|>3 dB\n(1,3 % av 5 964)',RD),('24 / 8,3','passer ON / OFF\n215 / 849 hops/pass',GR),('0','avviste IQ-blokker\n(headroom)',GR),('3/3','suite-sjekker OK\nσ rep. 0,129 dB',GR)]
for i,(v,t,col) in enumerate(tiles):
    ax=fig.add_subplot(gs[0,i*12//8:(i+1)*12//8]) if False else fig.add_subplot(gs[0,int(i*1.5):int((i+1)*1.5)+(1 if i%2==0 else 0)]) if False else None
from matplotlib.gridspec import GridSpecFromSubplotSpec as GSS
tg=GSS(1,8,subplot_spec=gs[0,:],wspace=.12)
axs=[]
x0=0
for i,(v,t,col) in enumerate(tiles):
    ax=fig.add_subplot(tg[0,i]); ax.axis('off')
    ax.add_patch(plt.Rectangle((0,0),1,1,transform=ax.transAxes,fc='#f3f4f6',ec='#d1d5db'))
    ax.text(.5,.62,v,ha='center',va='center',fontsize=19 if len(v)<8 else 15,fontweight='bold',color=col,transform=ax.transAxes)
    ax.text(.5,.2,t,ha='center',va='center',fontsize=8.5,color='#374151',transform=ax.transAxes)
# 1 full spectrum
ax=fig.add_subplot(gs[1,:]); 
for n,l,h in BANDS: ax.axvspan(l,h,color='#e5e7eb',alpha=.7,lw=0)
ax.axvspan(100,300,color=GR,alpha=.12,lw=0)
ax.plot(mf,lf,color=OR,lw=.5,alpha=.9,label=f'OFF (n={len(mf)})'); ax.plot(mo,lo,color=BL,lw=.5,alpha=.9,label=f'ON (n={len(mo)})')
ymin,ymax=np.nanpercentile(np.r_[lo,lf],.2)-1,np.nanmax(np.r_[lo,lf])+4
ax.set_ylim(ymin,ymax); ax.set_xlim(70,6000)
for n,l,h in BANDS: ax.text((l+h)/2,ymax-.4,n,rotation=90,ha='center',va='top',fontsize=7.5,color='#4b5563')
ax.text(300,ymin+.8,'◄ 100–300 MHz (zoom i panel 6)',ha='left',fontsize=9,color=GR,fontweight='bold')
ax.set_title('1 · Hele spekteret, 1 MHz-celler (relativt nivå, dB) — ON blå, OFF oransje'); ax.set_xlabel('MHz'); ax.set_ylabel('dB (relativt)'); ax.legend(loc='upper right',frameon=True,ncol=2,bbox_to_anchor=(1,.82))
# 2 obs and delta
ax=fig.add_subplot(gs[2,:7]); eb=np.arange(70,6001,25)
for mm,vv,col,nm in ((mo,oo,BL,'ON'),(mf,of,OR,'OFF')):
    ix=np.digitize(mm,eb); ym=[np.mean(vv[ix==k]) if (ix==k).any() else np.nan for k in range(1,len(eb))]; ax.step(eb[:-1],ym,where='post',color=col,lw=1.2,label=f'{nm} obs/celle (snitt per 25 MHz)')
ax.set_ylim(0,None)
ax.set_title('2 · Observasjoner per 1 MHz-celle (snitt per 25 MHz)'); ax.set_xlabel('MHz'); ax.set_ylabel('obs'); ax.legend(frameon=False); ax.set_xlim(70,6000)
ax=fig.add_subplot(gs[2,7:]); ax.scatter(common,np.clip(d,-6,6),s=1.5,color=GY,alpha=.5); ax.plot(common,roll(d,41),color=BL,lw=1.2)
ax.axhspan(-3*sd,3*sd,color=GR,alpha=.12); out=np.abs(d)>3*sd; ax.scatter(common[out&(np.abs(d)<=6)],d[out&(np.abs(d)<=6)],s=3,color=RD)
ax.set_title(f'3 · Δ = ON − OFF per celle (±3 sd = ±{3*sd:.2f} dB, klippet ±6)'); ax.set_xlabel('MHz'); ax.set_ylabel('dB'); ax.set_xlim(70,6000)
# 3 tables: bands & peaks
def table(ax,rows,header,colw,fs=9,title=None,sc=1.4):
    ax.axis('off'); 
    if title: ax.set_title(title)
    t=ax.table(cellText=rows,colLabels=header,loc='upper left',cellLoc='left',colWidths=colw); t.auto_set_font_size(False); t.set_fontsize(fs); t.scale(1,sc)
    for (r,cc),cell in t.get_celld().items():
        cell.set_edgecolor('#d1d5db')
        if r==0: cell.set_facecolor('#e5e7eb'); cell.set_text_props(fontweight='bold')
rows=[]
allb=BANDS+[('Resten (ikke i bånd-liste)',0,0)]
for n,l,h in BANDS:
    k=(mo>=l)&(mo<=h); kf=(mf>=l)&(mf<=h); kc=(common>=l)&(common<=h)
    if k.sum()==0: continue
    rows.append([n,int(k.sum()),f'{np.nanmedian(lo[k]):.1f}',f'{np.nanpercentile(lo[k],95):.1f}',f'{np.nanmax(lo[k]):.1f}',f'{np.nanmedian(lf[kf]):.1f}' if kf.sum() else '–',f'{np.nanmedian(d[kc]):+.2f}' if kc.sum() else '–',f'{np.nanmedian(oo[k]):.0f}/{np.nanmedian(of[kf]):.0f}' if kf.sum() else '–'])
ax=fig.add_subplot(gs[3,:6]); table(ax,rows,['Bånd (nominelt)','celler','med ON','p95 ON','maks ON','med OFF','med Δ','obs ON/OFF'],[.3,.08,.11,.11,.11,.11,.09,.12],fs=8.6,title='4 · Per bånd',sc=1.28)
# peaks
fl=roll(lo,201); ex=lo-fl; order=np.argsort(-ex); picked=[]
for i in order:
    if np.isnan(ex[i]): continue
    if all(abs(mo[i]-mo[j])>=4 for j in picked): picked.append(i)
    if len(picked)>=16: break
rows2=[]
for i in picked:
    f=mo[i]; j=jf.get(f); rows2.append([f'{f:.0f}',bandname(f),f'{lo[i]:.1f}',f'{ex[i]:+.1f}',f'{lf[j]:.1f}' if j is not None else '–',f'{int(oo[i])}'])
ax=fig.add_subplot(gs[3,6:]); table(ax,rows2,['MHz','bånd (nominelt)','nivå ON','over lokal gulv','nivå OFF','obs ON'],[.1,.34,.14,.2,.14,.1],fs=8.6,title='5 · Sterkeste topper (ON, over 201-cellers median-gulv, ≥4 MHz fra hverandre)',sc=1.28)
# 100-300 zoom
ax=fig.add_subplot(gs[4,:8])
NF=lambda v:np.array([np.nan if x is None else x for x in v],float)
rf=NF(A['rf_mhz']); med=NF(A['median_db']); p10=NF(A['p10']); p90=NF(A['p90'])
ax.axvspan(100,108,color='#e5e7eb',alpha=.9,lw=0); ax.axvspan(174,240,color='#e5e7eb',alpha=.9,lw=0)
ax.fill_between(rf,p10,p90,color=BL,alpha=.2,label='suite p10–p90 (0,1 MHz, 15 passer)'); ax.plot(rf,med,color=BL,lw=.8,label='suite median, telemetri på')
rfb=NF(B['rf_mhz']); ax.plot(rfb,NF(B['median_db']),color=PU,lw=.6,alpha=.8,label='suite median, telemetri av')
kz=(mo>=100)&(mo<=300); kzf=(mf>=100)&(mf<=300)
ax.step(mo[kz],lo[kz],where='mid',color=GR,lw=1,label='full-range ON, 1 MHz'); ax.step(mf[kzf],lf[kzf],where='mid',color=OR,lw=1,label='full-range OFF, 1 MHz')
ax.text(104,ax.get_ylim()[1],'FM',ha='center',va='top',fontsize=9,fontweight='bold'); ax.text(207,ax.get_ylim()[1],'VHF / DAB-III',ha='center',va='top',fontsize=9,fontweight='bold')
ax.set_xlim(100,300); ax.set_title('6 · 100–300 MHz zoom — suite (0,1 MHz) mot full-range (1 MHz)'); ax.set_xlabel('MHz'); ax.set_ylabel('dB (relativt)'); ax.legend(frameon=True,fontsize=8,loc='upper right',ncol=1)
NF=lambda v:np.array([np.nan if x is None else x for x in v],float)
ax=fig.add_subplot(gs[4,8:]); ax.errorbar(NF(A['if_mhz']),NF(A['if_db']),yerr=NF(A['if_se']),fmt='-',color=BL,lw=.8,ecolor='#bfdbfe',elinewidth=.6,label=f'IF-respons ON  (MAD {A["checks"]["if_fit"]["residual_mad_db"]:.3f} dB)')
ax.plot(NF(B['if_mhz']),NF(B['if_db']),color=PU,lw=.8,alpha=.8,label=f'IF-respons telemetri av (MAD {B["checks"]["if_fit"]["residual_mad_db"]:.3f})')
ax.set_title('7 · IF-respons (fitted, relativt) ±SE'); ax.set_xlabel('IF MHz'); ax.set_ylabel('dB'); ax.legend(frameon=False,fontsize=8.5)
# suite details
ax=fig.add_subplot(gs[5,:4]); ax.plot(rf,NF(A['obs']),color=BL,lw=.8,label='obs/bin, telemetri på'); ax.plot(rfb,NF(B['obs']),color=PU,lw=.8,label='telemetri av'); ax.set_title('8 · Suite: obs per 0,1 MHz-bin'); ax.set_xlabel('MHz'); ax.legend(frameon=False); ax.set_xlim(100,300)
ax=fig.add_subplot(gs[5,4:8]); w=NF(A['p90'])-NF(A['p10']); wb=NF(B['p90'])-NF(B['p10']); ax.plot(rf,w,color=BL,lw=.8,label=f'p90−p10 på (median {np.nanmedian(w):.2f} dB)'); ax.plot(rfb,wb,color=PU,lw=.8,label=f'av (median {np.nanmedian(wb):.2f} dB)'); ax.set_title('9 · Suite: spredning p90−p10 over passer'); ax.set_xlabel('MHz'); ax.set_ylabel('dB'); ax.legend(frameon=False); ax.set_xlim(100,300)
ax=fig.add_subplot(gs[5,8:]); 
for nm,v,col in (('ON',lo,BL),('OFF',lf,OR)): ax.hist(v[np.isfinite(v)],bins=120,histtype='step',color=col,lw=1.3,label=nm,density=True)
ax.hist(med[np.isfinite(med)],bins=60,histtype='stepfilled',color=GR,alpha=.25,density=True,label='suite 100–300'); ax.set_title('10 · Nivåfordeling (alle celler)'); ax.set_xlabel('dB'); ax.legend(frameon=False)
# time series
ax=fig.add_subplot(gs[6,:4]); t=np.array(on['temperature_series']); ax.plot(t[:,0],t[:,1],color=RD,lw=1); ax.set_title(f'11 · B200-temperatur under ON-kjøring ({t[:,1].min():.1f}–{t[:,1].max():.1f} °C, {on["temperature_records"]} ferske)'); ax.set_xlabel('s'); ax.set_ylabel('°C')
ax=fig.add_subplot(gs[6,4:8]); 
for nm,x,col in (('ON',on,BL),('OFF',off,OR)):
    h=np.array(x['hop_series']); ax.plot(h[:,0],h[:,1],color=col,lw=.8,label=nm)
ax.set_title('12 · Hop-posisjon (MHz) mot tid — sveipmønster'); ax.set_xlabel('s'); ax.set_ylabel('MHz'); ax.legend(frameon=False)
ax=fig.add_subplot(gs[6,8:]); xx=np.linspace(-4,4,400); dd=np.clip(d[np.isfinite(d)],-4,4)
ax.hist(dd,bins=160,range=(-4,4),density=True,color=BL,alpha=.6,label='Δ ON−OFF'); ax.plot(xx,np.exp(-.5*(xx/sd)**2)/(sd*np.sqrt(2*np.pi)),color=RD,lw=1.5,label=f'Gauss-null sd {sd:.2f} dB'); ax.set_yscale('log'); ax.set_ylim(1e-3,None)
ax.set_title(f'13 · Fordeling av Δ (hale: {np.mean(np.abs(d[np.isfinite(d)])>3*sd)*100:.1f} % > 3 sd mot 0,27 %)'); ax.set_xlabel('dB'); ax.legend(frameon=False,fontsize=8.5)
# bench
ax=fig.add_subplot(gs[7,:3]); fb=Bn['floor_bias']; x=np.arange(len(fb)); w=.2
ax.bar(x-1.5*w,[r['median_bias_db_legacy'] for r in fb],w,color=OR,label='median før'); ax.bar(x-.5*w,[r['median_bias_db_new'] for r in fb],w,color=BL,label='median etter')
ax.bar(x+.5*w,[r['minhold2_bias_db_legacy'] for r in fb],w,color='#f6c177',label='min-hold før'); ax.bar(x+1.5*w,[r['minhold2_bias_db_new'] for r in fb],w,color='#7fb3d5',label='min-hold etter')
ax.set_xticks(x); ax.set_xticklabels([f'dof {r["dof"]}' for r in fb]); ax.set_title('14 · Gulvbias (dB), før → etter'); ax.legend(frameon=False,fontsize=8); ax.axhline(0,color='k',lw=.7)
ax=fig.add_subplot(gs[7,3:6]); dt=Bn['detection']
for k,col,nm in (('fixed_6db',OR,'fast 6 dB'),('bh_0.01',BL,'BH FDR 1 %'),('bonferroni_0.01',GR,'Bonferroni')):
    r=dt[k]['detect_rate_by_db']; xs=sorted(r,key=float); ax.plot([float(q) for q in xs],[r[q]*100 for q in xs],'o-',color=col,label=f'{nm}  FP/bin {dt[k]["false_positive_rate"]:.1e}')
ax.set_title('15 · Deteksjon mot SNR (%)'); ax.set_xlabel('linje over gulv, dB'); ax.legend(frameon=False,fontsize=8)
ax=fig.add_subplot(gs[7,6:9]); cl=Bn['classification']['per_scenario']; names=list(cl); x=np.arange(len(names))
ax.bar(x-.2,[cl[n]['legacy']['kind_errors'] for n in names],.4,color=OR,label='feilmerket før'); ax.bar(x+.2,[cl[n]['new']['kind_errors'] for n in names],.4,color=BL,label='etter')
ax.set_xticks(x); ax.set_xticklabels([n.split(' ')[0] for n in names]); ax.set_title('16 · Feilmerkede topper T1–T5 (360 tp: 120 → 0)'); ax.legend(frameon=False,fontsize=8)
ax=fig.add_subplot(gs[7,9:]); ft=Bn['floor_vs_temperature']; lab=[f'{r["temperature_profile"][:4]} {r["true_slope_db_per_c"]:+.3f}' for r in ft]; x=np.arange(len(ft))
ax.bar(x-.2,[r['ols_rms_error'] for r in ft],.4,color=OR,label='OLS'); ax.bar(x+.2,[r['kalman_rms_error'] for r in ft],.4,color=BL,label='Kalman'); ax.set_xticks(x); ax.set_xticklabels(lab,rotation=40,ha='right',fontsize=8)
ax.set_title('17 · Gulv-mot-T feil (dB/°C); mono flagges 100 % confounded'); ax.legend(frameon=False,fontsize=8)
# text
ax=fig.add_subplot(gs[8,:]); ax.axis('off')
txt=('HVA DATAENE KAN SI\n'
'• Begge kjøringene dekker 70–6000 MHz og er enige: median Δ −0,03 dB, robust sd 0,33 dB, p95 0,81 dB. Funksjonene (rf-state, headroom, ensure-coverage, telemetri) flytter ikke nivåskalaen.\n'
'• ON koster 27 % rate og halverer obs/celle (grovere plan: 215 mot 849 hops/pass), men gir 3× så mange passer. Headroom hadde ingenting å avvise (0 IQ-blokker).\n'
'• Største ON/OFF-avvik: 2404–2419 MHz (opptil +19 dB) — trolig WiFi/Bluetooth som varierer mellom kjøringene, ikke funksjonene. 741 MHz mangler i ON.\n'
'• 100–300 MHz: IF-fit MAD 0,621 (på) mot 0,630 (av), repeterbarhet σ 0,129 mot 0,180 dB, 3/3 sjekker bestått begge. 1905 ferske temperaturer, span 0,88 °C.\n\n'
'HVA DE IKKE KAN SI\n'
'• Alt er relativt (ingen dBm, ingen støytall). Antenne ⇒ miljøendring er blandet inn i alle Δ; 50 Ω-kjøring trengs for ren funksjonseffekt. Kun alt-på mot alt-av er testet, ikke enkeltfunksjoner.\n'
'• Gulv-mot-temperatur er uavklart (OLS +0,024; Kalman −0,018 ± 0,057 dB/°C på 0,9 °C span). Image-rejection og settling krever signalgenerator. bladeRF ikke funnet 17.11.; 700–960 MHz DC-rest uforklart.')
ax.text(0,1,txt,va='top',fontsize=11.2,family='DejaVu Sans',linespacing=1.35,transform=ax.transAxes)
fig.savefig('full_range_infographic.png',bbox_inches='tight'); fig.savefig('pdf/p00.pdf',bbox_inches='tight'); print('ok')
