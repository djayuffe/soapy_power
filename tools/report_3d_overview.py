"""One-off figure script for the 2026-11-17/18 full-range features-on/off reports (docs/results/fullrange-*).

Inputs: SESSIONS/fullrange-combined/{compare,waterfall}.json and SESSIONS/dash_suite.json;
SESSIONS = $SPR_SESSIONS (default ./sessions).
Writes PNG/PDF files into the current directory.
"""
import os
import json, numpy as np, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import cm, colors
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401  registers the 3d projection on older matplotlib
U=os.environ.get('SPR_SESSIONS','sessions').rstrip('/')+'/'
W=json.load(open(U+'fullrange-combined/waterfall.json')); S=json.load(open(U+'dash_suite.json'))['telemetry_on']
f=W['start_mhz']+W['cell_mhz']*(np.arange(2965)+.5)
G={k:np.array([[np.nan if x is None else x for x in r] for r in W[k]['grid']],float) for k in ('features-on','features-off')}
for k in G: G[k]=G[k][np.isfinite(G[k]).mean(1)>.5]
BG='#070b14'; FG='#c9d4e6'
plt.rcParams.update({'figure.facecolor':BG,'axes.facecolor':BG,'text.color':FG,'axes.labelcolor':FG,'xtick.color':FG,'ytick.color':FG,'font.size':11,'axes.edgecolor':'#2b3a55'})
cmap=plt.get_cmap('turbo'); 
def style(ax):
    for a in (ax.xaxis,ax.yaxis,ax.zaxis): a.set_pane_color((0.04,0.06,0.11,1)); a.line.set_color('#2b3a55'); a._axinfo['grid']['color']=(0.3,0.4,0.6,0.25)
def surf(ax,x,y,z,vmin,vmax,alpha=1):
    X,Y=np.meshgrid(x,y); Zm=np.ma.masked_invalid(z)
    nz=(Zm-vmin)/(vmax-vmin); fc=cmap(np.clip(np.nan_to_num(nz.filled(np.nan),nan=0),0,1))
    ax.plot_surface(X,Y,np.where(np.isfinite(z),z,vmin),facecolors=fc,rstride=1,cstride=1,linewidth=0,antialiased=True,shade=False,alpha=alpha)
def glow(ax,x,y,z,col,lw=1.6):
    ax.plot(x,np.full_like(x,y),z,color=col,lw=lw,alpha=.95)
vmin,vmax=-136,-104
XT=[100,300,1000,2000,3000,4500,6000]
sq=np.sqrt
def xfix(ax,lo=70,hi=6000):
    ax.set_xticks([sq(t) for t in XT]); ax.set_xticklabels([f'{t}' for t in XT]); ax.set_xlim(sq(lo),sq(hi))
def big(fig,ax,zoom=1.25):
    ax.set_position([-.08,-.12,1.12,1.18])

def hero():
    fig=plt.figure(figsize=(20,12),dpi=110); ax=fig.add_subplot(111,projection='3d'); style(ax)
    z=G['features-on']; P=z.shape[0]; y=np.arange(P)
    ds=2; surf(ax,sq(f[::ds]),y,np.maximum(z[:,::ds],-136),vmin,vmax)
    mx=np.nanmax(z,0)
    ax.plot(sq(f),np.full_like(f,-1.5),np.maximum(mx,-136),color='#ffd166',lw=1.3,label='maks over passer')
    ax.set_xlabel('RF (MHz, √-skala)',labelpad=14); ax.set_ylabel('pass (tid →)',labelpad=12); ax.set_zlabel('dB relativt',labelpad=10)
    ax.set_zlim(-136,-100); xfix(ax); ax.set_ylim(-2,P); ax.view_init(elev=27,azim=-58); ax.set_box_aspect((2.6,1.6,1.1),zoom=1.35)
    sm=cm.ScalarMappable(norm=colors.Normalize(vmin,vmax),cmap=cmap); cb=fig.colorbar(sm,ax=ax,shrink=.5,pad=.02); cb.set_label('dB relativt'); cb.outline.set_edgecolor('#2b3a55')
    for nm,fx in (('GSM900',930),('LTE800',805),('DAB',225),('GSM1800',1840),('UMTS',2120),('WiFi 2.4',2410)):
        i=np.argmin(abs(f-fx)); ax.text(sq(fx),-5,min(np.nanmax(z[:,i]),-104)+3,nm,color='#ffd166',fontsize=9,ha='center')
    fig.suptitle('FULL RANGE 70 MHz–6 GHz · vannfall i 3D (features ON, 24 passer, 2 MHz celler)',fontsize=20,fontweight='bold',x=.06,ha='left',y=.95)
    fig.text(.06,.905,'B200mini · antenne · 2026-11-17 · relative nivåer · hvert "bånd" i dybden er ett sveip på ~12 s',color='#8fa3c4',fontsize=11)
    fig.savefig('3d_hero_waterfall.png',bbox_inches='tight'); fig.savefig('pdf/q01.pdf',bbox_inches='tight'); plt.close(fig)
def onoff():
    fig=plt.figure(figsize=(20,12),dpi=110); ax=fig.add_subplot(111,projection='3d'); style(ax)
    eb=np.arange(70,6001,10); 
    def prof(z):
        m=np.nanmedian(z,0); ix=np.digitize(f,eb); return np.array([np.nanmax(m[ix==k]) if (ix==k).any() else np.nan for k in range(1,len(eb))])
    on,off=prof(G['features-on']),prof(G['features-off']); x=eb[:-1]+5
    for yy,pr,col in ((0,on,'#00d1ff'),(1,off,'#ffb000')):
        xs=sq(x[np.isfinite(pr)]); zs=pr[np.isfinite(pr)]
        ax.bar(xs,np.full_like(xs,.55),zs+136,zdir='z',color=col,alpha=.0)  # placeholder to set limits
        for a,b,cc in zip(xs,zs,[col]*len(xs)): pass
        ax.bar(xs,zs+136,zs=-136,zdir='y',color=col,alpha=.0)
        ax.plot(xs,np.full_like(xs,yy),zs,color=col,lw=1.4)
        ax.add_collection3d(plt.matplotlib.collections.PolyCollection([list(zip(xs,zs))+[(xs[-1],-136),(xs[0],-136)]],facecolors=col,alpha=.22),zs=yy,zdir='y')
    d=on-off; ok=np.isfinite(d)
    ax.plot(sq(x[ok]),np.full(ok.sum(),2.2),-128+8*np.clip(d[ok],-6,6)/6,color='#ff3d81',lw=1.2)
    ax.add_collection3d(plt.matplotlib.collections.PolyCollection([list(zip(sq(x[ok]),-128+8*np.clip(d[ok],-6,6)/6))+[(sq(x[ok][-1]),-128),(sq(x[ok][0]),-128)]],facecolors='#ff3d81',alpha=.25),zs=2.2,zdir='y')
    ax.set_yticks([0,1,2.2]); ax.set_yticklabels(['ON','OFF','Δ ON−OFF (±6 dB)']); ax.set_xlabel('RF (MHz, √-skala)',labelpad=14); ax.set_zlabel('dB relativt'); ax.set_zlim(-136,-100); ax.set_ylim(-.3,2.6)
    ax.view_init(elev=22,azim=-60); ax.set_box_aspect((2.8,1.2,1.0),zoom=1.3); xfix(ax)
    fig.suptitle('ON mot OFF som tredimensjonale profiler (maks per 10 MHz, median over passer) + Δ-bånd',fontsize=19,fontweight='bold',x=.06,ha='left',y=.93)
    fig.savefig('3d_on_off.png',bbox_inches='tight'); fig.savefig('pdf/q02.pdf',bbox_inches='tight'); plt.close(fig)
def zoom():
    fig=plt.figure(figsize=(20,12),dpi=110); ax=fig.add_subplot(111,projection='3d'); style(ax)
    k=(f>=100)&(f<=300); z=G['features-on'][:,k]; P=z.shape[0]; y=np.arange(P)
    surf(ax,f[k],y,np.maximum(z,-136),-136,-108)
    rf=np.array([np.nan if v is None else v for v in S['rf_mhz']]); p10=np.array([np.nan if v is None else v for v in S['p10']]); md=np.array([np.nan if v is None else v for v in S['median_db']]); p90=np.array([np.nan if v is None else v for v in S['p90']])
    ya=P+3
    ax.plot(rf,np.full_like(rf,ya),p90,color='#ff3d81',lw=.9,label='suite p90 (0,1 MHz)'); ax.plot(rf,np.full_like(rf,ya),md,color='#00d1ff',lw=1.3,label='suite median'); ax.plot(rf,np.full_like(rf,ya),p10,color='#7bffb2',lw=.9,label='suite p10')
    ax.add_collection3d(plt.matplotlib.collections.PolyCollection([list(zip(rf[np.isfinite(p90)],p90[np.isfinite(p90)]))+list(zip(rf[np.isfinite(p10)][::-1],p10[np.isfinite(p10)][::-1]))],facecolors='#00d1ff',alpha=.18),zs=ya,zdir='y')
    ax.set_xlabel('RF (MHz)',labelpad=12); ax.set_ylabel('pass',labelpad=10); ax.set_zlabel('dB relativt'); ax.set_zlim(-136,-104); ax.set_xlim(100,300); ax.set_ylim(0,ya+1)
    ax.view_init(elev=30,azim=-55); ax.set_box_aspect((2.4,1.6,1.0),zoom=1.3); ax.legend(loc='upper left',frameon=False)
    fig.suptitle('100–300 MHz i 3D — full-range-passer (2 MHz) + suite-bånd p10/median/p90 (0,1 MHz) bakerst',fontsize=19,fontweight='bold',x=.06,ha='left',y=.93)
    fig.savefig('3d_100_300.png',bbox_inches='tight'); fig.savefig('pdf/q03.pdf',bbox_inches='tight'); plt.close(fig)
def variability():
    fig=plt.figure(figsize=(20,12),dpi=110); ax=fig.add_subplot(111,projection='3d'); style(ax)
    z=G['features-on']; sd=np.nanstd(z,0); md=np.nanmedian(z,0); ds=2
    xs=sq(f[::ds]); ax.scatter(xs,sd[::ds],np.maximum(md[::ds],-136),c=np.maximum(md[::ds],-136),cmap=cmap,s=6+30*np.clip((md[::ds]+136)/30,0,1),alpha=.8,linewidths=0)
    ax.set_xlabel('RF (MHz, √-skala)',labelpad=14); ax.set_ylabel('σ over 24 passer (dB)',labelpad=10); ax.set_zlabel('median nivå (dB)'); ax.set_ylim(0,min(8,np.nanmax(sd))); xfix(ax)
    ax.view_init(elev=24,azim=-66); ax.set_box_aspect((2.8,1.3,1.0),zoom=1.3)
    fig.suptitle('Tidsvariasjon: frekvens × σ(pass til pass) × nivå — hvilke signaler er stabile, hvilke blinker',fontsize=19,fontweight='bold',x=.06,ha='left',y=.93)
    fig.savefig('3d_variability.png',bbox_inches='tight'); fig.savefig('pdf/q04.pdf',bbox_inches='tight'); plt.close(fig)
for fn in (hero,onoff,zoom,variability): fn(); print(fn.__name__)
