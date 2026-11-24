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
c=json.load(open(U+'fullrange-combined/compare.json')); s=json.load(open(U+'dash_suite.json')); b=json.load(open(os.environ.get('SPR_BENCH_JSON','/tmp/bench_full.json')))
on,off=c['variants']['features-on'],c['variants']['features-off']
BL,OR,GR,RD,GY='#0072B2','#E69F00','#009E73','#D55E00','#6b7280'
plt.rcParams.update({'font.size':9,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.alpha':.25,'axes.titlesize':10.5,'axes.titleweight':'bold','axes.titlelocation':'left'})
fig=plt.figure(figsize=(18,15),dpi=110); gs=GridSpec(5,6,figure=fig,top=.94,bottom=.05,left=.06,right=.98,hspace=.62,wspace=.55,height_ratios=[.55,1.25,1.1,1.1,1.1])
fig.suptitle('soapy_power · B200mini · 2026-11-17 · antenna input · relative, uncalibrated levels',x=.06,ha='left',fontsize=15,fontweight='bold',y=.99)
# KPI tiles
ag=c['agreement']
k=[('Full range','70–6000 MHz','215-hop plan, 2 variants × 300 s'),
   ('Features on',f"{on['records']} rec · {on['passes']:.0f} passes",f"{on['records_per_s']:.1f} rec/s · {on['temperature_records']} temperatures"),
   ('Features off',f"{off['records']} rec · {off['passes']:.1f} passes",f"{off['records_per_s']:.1f} rec/s · no telemetry"),
   ('On vs off, 1 MHz cells',f"{ag['median_diff_db']:+.2f} dB median",f"robust sd {ag['robust_sd_db']:.2f} dB · {ag['cells_over_3db']} cells >3 dB"),
   ('100–300 suite',"3/3 required pass","telemetry on and off"),
   ('Test suite','485 passed','12 new · master b91ba2a+')]
for i,(a,v,d) in enumerate(k):
    ax=fig.add_subplot(gs[0,i]); ax.axis('off'); ax.add_patch(plt.Rectangle((0,0),1,1,transform=ax.transAxes,fc='#f3f4f6',ec='#d1d5db'))
    ax.text(.05,.78,a,fontsize=9,color=GY,transform=ax.transAxes); ax.text(.05,.42,v,fontsize=14,fontweight='bold',transform=ax.transAxes); ax.text(.05,.1,d,fontsize=8,color=GY,transform=ax.transAxes)
# full-range spectrum
ax=fig.add_subplot(gs[1,:4])
ax.plot(on['cell_mhz'],on['cell_level_db'],lw=.5,color=BL,label='features on'); ax.plot(off['cell_mhz'],off['cell_level_db'],lw=.5,color=OR,label='features off',alpha=.8)
ax.set_title('Full range, median level per 1 MHz cell (dB, relative)'); ax.set_xlabel('RF MHz'); ax.set_ylabel('dB (uncalibrated)'); ax.legend(loc='upper right',frameon=False,ncol=2)
# diff
don=dict(zip(on['cell_mhz'],on['cell_level_db'])); doff=dict(zip(off['cell_mhz'],off['cell_level_db']))
cm=sorted(set(don)&set(doff)); d=np.array([don[m]-doff[m] for m in cm]); d0=d-np.median(d)
ax=fig.add_subplot(gs[1,4:]); ax.hist(np.clip(d0,-4,4),bins=80,color=GR); ax.set_yscale('log')
ax.set_title('On − off per cell (offset removed)'); ax.set_xlabel('dB (clipped ±4)'); ax.set_ylabel('cells'); ax.text(.97,.95,f'p95 |Δ| {ag["p95_abs_diff_db"]:.2f} dB',transform=ax.transAxes,ha='right',va='top')
# temp + hops
ax=fig.add_subplot(gs[2,:2]); t=np.array(on['temperature_series']); ax.plot(t[:,0],t[:,1],color=RD); ax.set_title('RFIC temperature, features on'); ax.set_xlabel('s'); ax.set_ylabel('°C')
ax=fig.add_subplot(gs[2,2:4])
for nm,x,col in (('on',on,BL),('off',off,OR)):
    h=np.array(x['hop_series']); ax.plot(h[:,0],h[:,1],'.',ms=1.5,color=col,label=nm)
ax.set_title('Hop centre vs time (sampled)'); ax.set_xlabel('s'); ax.set_ylabel('centre MHz'); ax.legend(frameon=False,markerscale=6)
ax=fig.add_subplot(gs[2,4:]); lab=['records','records/s','passes']; vals=[(on['records'],off['records']),(on['records_per_s'],off['records_per_s']),(on['passes'],off['passes'])]
for i,(a_,o_) in enumerate(vals):
    ax.bar(i-.2,a_/max(a_,o_),.4,color=BL); ax.bar(i+.2,o_/max(a_,o_),.4,color=OR); ax.text(i-.2,a_/max(a_,o_)+.02,f'{a_:.4g}',ha='center',fontsize=8); ax.text(i+.2,o_/max(a_,o_)+.02,f'{o_:.4g}',ha='center',fontsize=8)
ax.set_xticks(range(3)); ax.set_xticklabels(lab); ax.set_ylim(0,1.2); ax.set_title('Throughput (relative to larger)'); ax.set_yticks([])
# suite
A,B=s['telemetry_on'],s['telemetry_off']
ax=fig.add_subplot(gs[3,:2]); ax.plot(A['rf_mhz'],[np.nan if v is None else v for v in A['median_db']],lw=.6,color=BL,label='telemetry on'); ax.plot(B['rf_mhz'],[np.nan if v is None else v for v in B['median_db']],lw=.6,color=OR,label='telemetry off',alpha=.8)
ax.set_title('100–300 MHz cell levels (IF-corrected)'); ax.set_xlabel('RF MHz'); ax.set_ylabel('dB'); ax.legend(frameon=False)
ax=fig.add_subplot(gs[3,2:4]); F=lambda v:np.array([np.nan if x is None else x for x in v],float); ax.plot(A['if_mhz'],F(A['if_db']),color=BL,label='on'); ax.plot(B['if_mhz'],F(B['if_db']),color=OR,label='off'); ax.fill_between(A['if_mhz'],F(A['if_db'])-F(A['if_se']),F(A['if_db'])+F(A['if_se']),color=BL,alpha=.2)
ax.set_title('IF response, fitted'); ax.set_xlabel('IF MHz'); ax.set_ylabel('dB'); ax.legend(frameon=False)
ax=fig.add_subplot(gs[3,4:]); ax.axis('off'); ax.set_title('Suite checks (on / off)')
rows=[('IF residual MAD dB','if_fit','residual_mad_db'),('Holdout improvement dB','if_holdout','improvement_db'),('Repeatability σ dB','repeatability','sigma_db'),('Repeatability p95 dB','repeatability','p95_abs_db'),('Masked mixing obs','spurious_responses','masked_observations')]
for i,(l,sec,key) in enumerate(rows):
    y=.85-i*.16; ax.text(0,y,l,transform=ax.transAxes); ax.text(.75,y,f"{A['checks'][sec][key]:.4g} / {B['checks'][sec][key]:.4g}",transform=ax.transAxes,ha='right',fontweight='bold')
ax.text(0,.05,f"temperature span {A['checks']['telemetry']['temperature_span_c']:.2f} °C, {A['checks']['telemetry']['records_with_fresh_temperature']} fresh readings",transform=ax.transAxes,color=GY,fontsize=8)
# benchmarks
ax=fig.add_subplot(gs[4,:2]); dofs=[x['dof'] for x in b['floor_bias']]; xs=np.arange(len(dofs)); w=.2
for j,(key,col,lab_) in enumerate((('median_bias_db_legacy',OR,'median, legacy'),('median_bias_db_new',BL,'median, new'),('minhold2_bias_db_legacy',RD,'min-hold, legacy'),('minhold2_bias_db_new',GR,'min-hold, new'))):
    ax.bar(xs+(j-1.5)*w,[x[key] for x in b['floor_bias']],w,color=col,label=lab_)
ax.set_xticks(xs); ax.set_xticklabels([f'dof {d_}' for d_ in dofs]); ax.set_ylabel('floor bias, dB'); ax.set_title('Floor estimate bias: before → after'); ax.legend(frameon=False,fontsize=7,loc='lower right'); [ax.text(x_+(j_-1.5)*.2,.05,f'{v_:+.3f}',ha='center',fontsize=6,rotation=90) for x_,d_ in enumerate(b['floor_bias']) for j_,v_ in ((1,d_['median_bias_db_new']),(3,d_['minhold2_bias_db_new']))]
ax=fig.add_subplot(gs[4,2:4]); per=b['classification']['per_scenario']; names=list(per); xs=np.arange(len(names))
ax.bar(xs-.2,[per[n]['legacy']['kind_errors'] for n in names],.4,color=OR,label='legacy mislabelled'); ax.bar(xs+.2,[per[n]['new']['kind_errors'] for n in names],.4,color=BL,label='new mislabelled')
ax.set_xticks(xs); ax.set_xticklabels([n.split(' ')[0] for n in names]); ax.set_title('T1–T5: mislabelled peaks (60 trials each)'); ax.legend(frameon=False)
tot=b['classification']['total']; ax.text(.02,.55,f"new: {tot['new']['tp']} found, {tot['new']['fp']} false, {tot['new']['missed']} missed",transform=ax.transAxes,fontsize=8)
ax=fig.add_subplot(gs[4,4:]); ax.errorbar([0.0243],[1],xerr=[0.031],fmt='o',color=OR,capsize=4,label='OLS +0.024 ± 0.031'); ax.errorbar([-0.0184],[0],xerr=[0.0565],fmt='o',color=BL,capsize=4,label='Kalman −0.018 ± 0.057')
ax.axvline(0,color=GY,lw=1); ax.set_yticks([]); ax.set_ylim(-.7,1.7); ax.set_xlabel('floor vs temperature, dB/°C'); ax.set_title('Real session: slope unresolved'); ax.legend(frameon=False,loc='upper left',fontsize=8)
fig.text(.06,.012,'Sources: fullrange-combined/compare.json, suite sessions okt-20261117-214125/214317, tools/benchmark_floor_spur.py, EXPERIMENTS.md. Antenna input: on/off cell differences include changing RF environment.',fontsize=8,color=GY)
fig.savefig('soapy_power_dashboard.png',bbox_inches='tight')
