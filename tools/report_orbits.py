"""Acquisition-time orbital context for 3D reports; never substitutes live TLEs."""
import csv
import json
from pathlib import Path

import numpy as np


def generate(session, waterfall, output, reference=None):
    from report_bundle import verify
    import satdb as sd
    from soapypower.satellites import observer, transmitters
    output=Path(output)
    own=Path(session)/'reference'
    chosen=own if own.exists() else Path(reference) if reference else None
    result=dict(status='unavailable',scope='SGP4 predictions, not measured antenna pointing or satellite identity',
                reference='session frozen reference' if own.exists() else 'explicit archived reference' if chosen else None,
                variants={},max_tracks=64,max_transmitters_per_track=4)
    if chosen is not None and (chosen/'unavailable.json').is_file() and not (chosen/'bundle-manifest.json').is_file():
        marker=json.loads((chosen/'unavailable.json').read_text())
        if marker.get('status')!='unavailable':raise ValueError('Invalid reference availability marker')
        chosen=None;result['reason']='The recording explicitly marked its orbital reference unavailable; no substitute was used.'
    if chosen is None:
        result.setdefault('reason','No frozen reference. Supply --reference with a verified archived reference bundle; current cache is never substituted.')
    else:
        # A present but broken frozen reference fails publication, rather than falling back.
        verify(chosen)
        qth=observer(sd,chosen/'qth.json')
        db=transmitters(sd,chosen/'satdb')
        result['reference_bundle']=str(chosen.resolve().name)
        for variant in ('features-on','features-off'):
            data=waterfall.get(variant)
            if not data:continue
            origin=data.get('time_origin_unix');duration=data.get('duration_s')
            if not isinstance(origin,(int,float)) or not isinstance(duration,(int,float)) or not np.isfinite([origin,duration]).all() or duration<=0:
                result['variants'][variant]=dict(status='unavailable',reason='No timezone-qualified acquisition time anchor',tracks=[])
                continue
            candidates=dict(list(db.items())[:4096])
            predictions=sd.predict(candidates,qth,origin,origin+duration,
                [dict(lo_hz=waterfall['start_mhz']*1e6,hi_hz=waterfall['stop_mhz']*1e6)],
                step=max(5,duration/720))
            tracks=[]
            for item in predictions[:64]:
                times=np.asarray(item['t']);el=np.asarray(item['el']);az=np.asarray(item['az']);rr=np.asarray(item['rr'])
                keep=(times>=origin)&(times<=origin+duration)&np.isfinite(el)&np.isfinite(az)&np.isfinite(rr)
                if keep.sum()<2:continue
                tracks.append(dict(norad=item['norad'],name=item['name'],times_s=(times[keep]-origin).round(3).tolist(),
                    az_deg=az[keep].round(4).tolist(),el_deg=el[keep].round(4).tolist(),
                    range_rate_m_s=rr[keep].round(3).tolist(),max_el_deg=item['max_el'],tle_epoch_utc=item.get('tle_epoch_utc'),
                    transmitters=[dict(f_hz=tx['f_hz'],mode=tx.get('mode','')) for tx in item['transmitters'][:4]]))
            result['variants'][variant]=dict(status='ready',time_origin_unix=origin,duration_s=duration,
                elevation_mask_deg=qth.get('min_elevation_deg',0),tracks=tracks,
                truncated=len(db)>4096 or len(predictions)>64,
                scope='All qualified tracks exported; figures label at most eight tracks')
        result['status']='ready' if any(v['status']=='ready' for v in result['variants'].values()) else 'unavailable'
        if result['status']!='ready':result['reason']='No qualified recording-time anchors in the waterfall cache'
    (output/'orbital-context.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    with (output/'orbital-az-el.csv').open('w',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['variant','norad','name','elapsed_s','az_deg','el_deg','range_rate_m_s'])
        for variant,data in result['variants'].items():
            for track in data['tracks']:
                for row in zip(track['times_s'],track['az_deg'],track['el_deg'],track['range_rate_m_s']):
                    writer.writerow([variant,track['norad'],track['name'],*row])
    plot(result,output)
    return result


def plot(context,output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    if not any(v.get('tracks') for v in context['variants'].values()):
        import textwrap
        fig=plt.figure(figsize=(16,9),facecolor='white');fig.text(.08,.8,'Satellite azimuth / elevation context',fontsize=22,color='#24394b')
        note=context.get('reason') or 'No eligible above-mask satellite tracks in the acquisition window and RF range.'
        fig.text(.08,.62,textwrap.fill(note,90),fontsize=14,color='#24394b')
        fig.text(.08,.3,textwrap.fill(context['scope'],90),fontsize=12,color='#24394b')
        fig.savefig(output/'orbital-az-el-status.png',dpi=150);fig.savefig(output/'orbital-az-el-status.pdf');plt.close(fig)
    for variant,data in context['variants'].items():
        tracks=data.get('tracks',[])
        if not tracks:continue
        with plt.rc_context({'figure.facecolor':'white','axes.facecolor':'white','text.color':'#24394b','axes.labelcolor':'#24394b',
                             'xtick.color':'#24394b','ytick.color':'#24394b','font.size':10}):
            fig=plt.figure(figsize=(18,10))
            sky=fig.add_axes([.045,.2,.4,.66],projection='polar');ax=fig.add_axes([.48,.2,.38,.66],projection='3d')
            sky.set_theta_zero_location('N');sky.set_theta_direction(-1);sky.set_ylim(90,0)
            for track in tracks[:8]:
                t=np.asarray(track['times_s']);az=np.asarray(track['az_deg']);el=np.asarray(track['el_deg'])
                visible=el>data.get('elevation_mask_deg',0)
                sky.plot(np.deg2rad(az),np.where(visible,el,np.nan),label=track['name'][:25],lw=1.2)
                # Azimuth is cyclic: split both the horizon and 360/0 crossings.
                z=np.where(visible,el,np.nan);z[1:][np.abs(np.diff(az))>180]=np.nan
                ax.plot(az,t,z,lw=1.2)
            sky.set_title('Predicted sky paths · North up / East right');sky.legend(loc='upper center',bbox_to_anchor=(.5,-.08),ncol=2,fontsize=8)
            ax.set(xlabel='Azimuth (degrees)',ylabel='Seconds from acquisition origin',zlabel='',xlim=(0,360),zlim=(0,90))
            fig.text(.965,.52,'Elevation (degrees)',rotation=90,ha='center',va='center',color='#24394b')
            fig.suptitle(f'{variant}: acquisition-time satellite azimuth / elevation\n'+context['scope'])
            name='orbital-az-el-'+variant
            fig.savefig(output/(name+'.png'),dpi=180,bbox_inches='tight');fig.savefig(output/(name+'.pdf'),bbox_inches='tight');plt.close(fig)
