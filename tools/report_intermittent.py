import json, numpy as np, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
W=json.load(open('/mnt/user-data/uploads/soapy_power/sessions/fullrange-combined/waterfall.json'))
f=70+2*(np.arange(2965)+.5)
BG='#070b14';FG='#c9d4e6'
plt.rcParams.update({'figure.facecolor':BG,'axes.facecolor':'#0b1222','text.color':FG,'axes.labelcolor':FG,'xtick.color':FG,'ytick.color':FG,'axes.edgecolor':'#2b3a55','axes.grid':True,'grid.color':'#24324f','grid.alpha':.5,'axes.spines.top':False,'axes.spines.right':False,'axes.titleweight':'bold','axes.titlelocation':'left'})
def g(k):
    a=np.array([[np.nan if x is None else x for x in r] for r in W[k]['grid']],float); t=np.array([np.nan if x is None else x for x in W[k]['t0_s']],float); ok=np.isfinite(a).mean(1)>.5; return a[ok],t[ok]
on,ton=g('features-on'); off,toff=g('features-off')
fig,axs=plt.subplots(2,2,figsize=(18,10.5),dpi=100); fig.suptitle('Intermittente signaler: nivå per pass i utvalgte bånd (median av 2 MHz-celler)',x=.05,ha='left',fontsize=18,fontweight='bold')
for ax,(lo,hi,nm) in zip(axs.flat,((150,160,'150–160 MHz'),(245,275,'245–275 MHz'),(215,230,'215–230 MHz (DAB-III, stabil kontroll)'),(2400,2420,'2400–2420 MHz'))):
    m=(f>=lo)&(f<=hi)
    ax.plot(ton,np.nanmedian(on[:,m],1),'o-',color='#00d1ff',label='ON (pass à 12,5 s)'); ax.plot(toff,np.nanmedian(off[:,m],1),'s-',color='#ffb000',label='OFF (pass à 36 s)')
    ax.set_title(nm); ax.set_xlabel('s fra start'); ax.set_ylabel('dB relativt'); ax.legend(frameon=False)
    so=np.nanstd(np.nanmedian(on[:,m],1)); sf=np.nanstd(np.nanmedian(off[:,m],1)); ax.text(.02,.05,f'σ pass-til-pass: ON {so:.1f} dB · OFF {sf:.1f} dB',transform=ax.transAxes,color='#ffd166')
fig.tight_layout(rect=(0,0,1,.95)); fig.savefig('intermittent.png',bbox_inches='tight'); fig.savefig('pdf/q05.pdf',bbox_inches='tight'); print('ok')
