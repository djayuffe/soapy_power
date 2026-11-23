#!/usr/bin/env python3
"""Analyze every record in a set of receive sessions, retaining gain/rate partitions.

usage: analyze_scan_set.py OUTPUT SESSION [SESSION ...]
Native-resolution spectral analyses, per-record telemetry CSV, provenance and a
combined HTML/PDF report. No absolute calibration or decoded identity is implied.
"""
import argparse
import collections
import csv
import datetime
import hashlib
import html
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import traceback

from report_bundle import bundle, verify

HERE=Path(__file__).resolve().parent


def sources(session):
    session=Path(session).resolve()
    if (session/'captures.json').exists():
        manifest=json.loads((session/'captures.json').read_text())
        paths=[]
        for entry in manifest['captures']:
            p=(session/entry['file']).resolve()
            if p.parent!=session or not p.is_file():raise ValueError('invalid capture path')
            paths.append(p)
        if len(set(paths))!=len(paths):raise ValueError('duplicate capture partition')
        return paths
    p=session/'superscan.jsonl'
    return [p] if p.is_file() and p.stat().st_size else []


def telemetry(source, writer):
    counts=collections.Counter();centers=collections.Counter();records=[];h=hashlib.sha256();fixed=None;previous=None;credit=dwell=excluded=0.;spectral_bins=masked=0
    with source.open('rb') as f:
        for i,line in enumerate(f):
            h.update(line);r=json.loads(line);m=r['metadata'];ctx=m.get('context',{});rf=m.get('rf_state',{}).get('after',{});hr=m.get('headroom',{});sensors=rf.get('telemetry',{});validity=rf.get('validity',{});io=m.get('host_io',{});ts=m.get('telemetry_statistics',{})
            identity=(json.dumps(ctx,sort_keys=True),m.get('sample_rate_hz'),len(r['power']),r['step_hz'],r.get('unit'))
            if fixed is None:fixed=identity
            if identity!=fixed:raise ValueError('mixed gain/rate/receiver contexts; split source before analysis')
            if m.get('measurement_status')!='complete' or m.get('continuity')!='verified':raise ValueError('incomplete/unverified record cannot enter full analysis')
            a=datetime.datetime.fromisoformat(r['time_start']);b=datetime.datetime.fromisoformat(r['time_stop'])
            if a.tzinfo is None or b.tzinfo is None or b<=a:raise ValueError('invalid acquisition timestamps')
            a,b=a.timestamp(),b.timestamp()
            if previous is not None and a<previous:raise ValueError('overlapping/non-monotonic record times')
            gap=0. if previous is None else a-previous
            if gap<=5:credit+=gap
            else:excluded+=gap
            credit+=b-a;dwell+=b-a;previous=b
            counts['headroom_pass']+=hr.get('accepted') is True;counts['rf_validity_pass']+=validity.get('passed') is True
            counts['temperature_fresh']+=sensors.get('rx:temp',{}).get('fresh') is True;counts['lo_locked']+=sensors.get('rx:lo_locked',{}).get('value') is True
            counts['native_chain_ok']+=rf.get('chain',{}).get('status')=='ok';counts['reference_lock_required']+=validity.get('reference_lock_required') is True
            center=float(m['tuned_center_hz']);centers[center]+=1;spectral_bins+=len(r['power']);masked+=len(set(r.get('masked_bins',[]))|set(r.get('estimated_bins',[])))
            row=dict(source=source.parent.name+'/'+source.name,record=i,start_unix=a,stop_unix=b,center_mhz=center/1e6,gain_db=sum(ctx.get('gains',{}).values()),sample_rate_hz=m.get('sample_rate_hz'),temperature_c=validity.get('temperature_c'),max_component=hr.get('max_component'),p999_component=hr.get('p999_component'),sensor_latency_p95_ms=ts.get('sensor_latency_p95_ms'),native_chain_status=rf.get('chain',{}).get('status'),lo_locked=sensors.get('rx:lo_locked',{}).get('value'),reference_locked=sensors.get('device:ref_locked',{}).get('value'),reference_lock_required=validity.get('reference_lock_required'),rssi_db=sensors.get('rx:rssi',{}).get('value'),headroom_pass=hr.get('accepted'),rf_validity_pass=validity.get('passed'))
            writer.writerow(row);records.append(row)
    if not records:raise ValueError('empty capture')
    return dict(source=str(source),sha256=h.hexdigest(),records=len(records),spectral_bins=spectral_bins,masked_or_estimated_bins=masked,record_span_s=records[-1]['stop_unix']-records[0]['start_unix'],credited_scan_s=credit,rf_dwell_s=dwell,excluded_gap_s=excluded,first_unix=records[0]['start_unix'],last_unix=records[-1]['stop_unix'],gain=json.loads(fixed[0]).get('gains'),sample_rate_hz=fixed[1],fft_bins=fixed[2],step_hz=fixed[3],counts=dict(counts),centers_mhz={str(k/1e6):v for k,v in centers.items()}),records


FIELDS=('source','record','start_unix','stop_unix','center_mhz','gain_db','sample_rate_hz','temperature_c','max_component','p999_component','sensor_latency_p95_ms','native_chain_status','lo_locked','reference_locked','reference_lock_required','rssi_db','headroom_pass','rf_validity_pass')


def diagnostics(all_rows, parts, output):
    import numpy as np
    import matplotlib;matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter,MaxNLocator
    plt.style.use('dark_background');fig,axs=plt.subplots(3,2,figsize=(18,13),layout='constrained')
    params=[('max_component','Raw I/Q maximum',.5),('p999_component','Raw I/Q p99.9',.2),('temperature_c','Driver temperature (C)',None),('sensor_latency_p95_ms','Per-record sensor p95 latency (ms)',None),('center_mhz','Sequential LO center (MHz)',None),('rssi_db','Driver RSSI (relative dB)',None)]
    for n,rows in enumerate(all_rows):
        t=[x['start_unix'] for x in rows];label=f"Part {n+1}: {parts[n]['sample_rate_hz']/1e6:g} MS/s, {rows[0]['gain_db']:g} dB"
        for ax,(field,title,limit) in zip(axs.flat,params):
            y=[x[field] if isinstance(x[field],(int,float)) else np.nan for x in rows]
            ax.plot(t,y,'.-',markersize=2,lw=.55,label=label);ax.set_title(title);ax.xaxis.set_major_formatter(FuncFormatter(lambda v,p:datetime.datetime.fromtimestamp(v,datetime.timezone.utc).strftime('%H:%M')));ax.xaxis.set_major_locator(MaxNLocator(6));ax.grid(alpha=.2)
    for ax,(_,_,limit) in zip(axs.flat,params):
        if limit:ax.axhline(limit,ls='--',color='#ffbd6b',lw=1)
        ax.set_xlabel('UTC acquisition time; gaps are retained')
    axs[0,0].legend(fontsize=8);fig.suptitle('All-record receiver telemetry and guard evidence',fontsize=20)
    fig.savefig(output/'telemetry.png',dpi=220);plt.close(fig)


def rejected_iq(sessions, output):
    import numpy as np
    import matplotlib.pyplot as plt
    results=[]
    for session in sessions:
        for metadata in sorted(Path(session).glob('rejected-main-*/*.json')):
            if metadata.name=='index.json':continue
            r=json.loads(metadata.read_text())
            if 'iq_file' not in r:continue
            p=(metadata.parent/r['iq_file']).resolve()
            if p.parent!=metadata.parent.resolve():raise ValueError('invalid rejected IQ path')
            if hashlib.sha256(p.read_bytes()).hexdigest()!=r['iq_sha256']:raise ValueError('rejected IQ hash mismatch')
            z=np.load(p,allow_pickle=False)['iq'];component=np.maximum(abs(z.real),abs(z.imag));ix=np.flatnonzero(component>=.5)
            rate=float(r['context']['sample_rate']);block=4096;count=len(z)//block
            maximum=component[:count*block].reshape(count,block).max(1);rms=np.sqrt(np.mean(abs(z[:count*block].reshape(count,block))**2,axis=1));t=(np.arange(count)*block+block/2)/rate
            item=dict(metadata=str(metadata),iq_sha256=r['iq_sha256'],samples=len(z),sample_rate_hz=rate,metrics=r['metrics'],above_05_samples=int(len(ix)),above_095_samples=int((component>=.95).sum()),above_05_sample_indices=ix.tolist(),phase_scope='Unreferenced phase of retained rejected IQ only; no coherent reference, no phase reconstructed from PSD')
            results.append(item);fig,axs=plt.subplots(2,2,figsize=(16,10),layout='constrained')
            axs[0,0].plot(t,maximum,label='Block maximum');axs[0,0].plot(t,rms,label='Block complex RMS');axs[0,0].axhline(.5,ls='--',color='orange');axs[0,0].set(xlabel='Seconds within rejected acquisition',ylabel='Digital amplitude',title='All IQ samples aggregated into 4096-sample blocks');axs[0,0].legend()
            axs[0,1].hist(component,bins=200,log=True);axs[0,1].axvline(.5,color='orange');axs[0,1].set(title='Component magnitude distribution',xlabel='max(|I|, |Q|)',ylabel='Samples (log)')
            sample=z[::max(1,len(z)//30000)];axs[1,0].plot(sample.real,sample.imag,'.',ms=.6,alpha=.4);axs[1,0].set(title='Uniformly subsampled IQ constellation (not demodulated)',xlabel='I',ylabel='Q',aspect='equal')
            hist,edges=np.histogram(np.angle(z),bins=72,range=(-np.pi,np.pi));axs[1,1].stairs(hist/len(z),np.degrees(edges));axs[1,1].set(title='Unreferenced phase histogram: all retained samples',xlabel='Phase (degrees)',ylabel='Fraction per 5-degree bin')
            fig.suptitle('Rejected acquisition: guard evidence and phase diagnostics',fontsize=18)
            name=f'rejected-iq-{len(results)}.png';fig.savefig(output/name,dpi=220);plt.close(fig);item['plot']=name
    return results


def cover_pdf(report, output):
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,Image,PageBreak
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape,A4
    from reportlab.lib.units import inch
    style=getSampleStyleSheet();story=[];width,height=landscape(A4)
    def para(text,kind='BodyText'):story.append(Paragraph(html.escape(text),style[kind]));story.append(Spacer(1,8))
    para('Complete partitioned SDR scan analysis','Title')
    para(f"{report['records']:,} accepted records; {report['spectral_bins']:,} recorded spectral bins processed by the native-resolution analyses. {report['credited_scan_s']/60:.3f} minutes of credited sequential scan time, {report['rf_dwell_s']/60:.3f} minutes summed RF dwell.")
    para('Gain and sample-rate contexts remain separate. This is accumulated coverage with interruptions, not one continuous coherent observation. Levels are relative; no absolute RF calibration or decoded identity is claimed.')
    table=[['Part','Records','Rate MS/s','Gain dB','Scan span s','RF dwell s']]
    for n,p in enumerate(report['parts']):table.append([str(n+1),str(p['records']),f"{p['sample_rate_hz']/1e6:g}",str(p['gain']),f"{p['credited_scan_s']:.2f}",f"{p['rf_dwell_s']:.2f}"])
    tb=Table(table,colWidths=[35,70,80,140,110,110]);tb.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#14243a')),('TEXTCOLOR',(0,0),(-1,0),colors.white),('GRID',(0,0),(-1,-1),.4,colors.lightgrey),('BOTTOMPADDING',(0,0),(-1,-1),9),('TOPPADDING',(0,0),(-1,-1),9)]));story.append(tb);story.append(Spacer(1,16))
    para('Failure analysis and corrected behavior','Heading2')
    for text in report['findings']:para(text)
    story.append(PageBreak());para('All-record telemetry','Heading1');story.append(Image(str(output/'telemetry.png'),width=(height-160)*18/13,height=height-160));story.append(PageBreak())
    para('Signal findings and scientific limits','Heading1')
    for n,p in enumerate(report['parts']):
        s=p['analysis'];para(f"Part {n+1}: {s['doppler_tracks']} Doppler-like tracks, {s['drift_tracks']} drifting tracks, {s['persistent_lines']} persistent lines, {s['events']} events and {s['wideband']} wideband transients. These are per-part detections; do not add them as unique transmitters.")
        ids=s.get('identification',{}).get('ids',[])
        for item in ids:para(f"Candidate: {item.get('name')} at {item.get('f_mhz')} MHz; {item.get('confidence')}; decoded identity = false.")
    for text in report['limits']:para(text)
    for r in report['rejected_iq']:
        story.append(PageBreak());para('Retained rejected IQ and phase','Heading1');para(f"Hash verified. {r['samples']:,} complex samples; {r['above_05_samples']} samples at or above 0.5, {r['above_095_samples']} at or above 0.95. {r['phase_scope']}.");story.append(Image(str(output/r['plot']),width=(height-190)*16/10,height=height-190))
    SimpleDocTemplate(str(output/'overview.pdf'),pagesize=landscape(A4),leftMargin=38,rightMargin=38,topMargin=30,bottomMargin=30).build(story)
    from pypdf import PdfReader,PdfWriter
    w=PdfWriter()
    for label,path in [('Overview',output/'overview.pdf')]+[(f'Part {n+1}',output/p['report_dir']/'superscan_analysis.pdf') for n,p in enumerate(report['parts'])]:
        start=len(w.pages)
        for pg in PdfReader(path).pages:w.add_page(pg)
        w.add_outline_item(title=label,page_number=start)
    w.write(output/'full-analysis.pdf')


def generate(output, sessions, *, render_pdf=True):
    inputs=list(HERE.glob('*.py'));paths=[]
    for session in sessions:
        session=Path(session).resolve();paths.extend(sources(session))
        for name in ('bands.json','gaincal.json','gaincal.png','reference','captures.json','result.txt','scan.log'):
            if (session/name).exists():inputs.append(session/name)
    if not paths:raise ValueError('no accepted captures to analyze')
    if len(set(paths))!=len(paths):raise ValueError('duplicate source capture')
    inputs+=paths
    if any(p.is_relative_to(Path(output).resolve()) for p in paths):raise ValueError('output directory contains acquisition input')
    with bundle(output,inputs) as stage:
        parts=[];all_rows=[]
        with (stage/'telemetry.csv').open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=FIELDS);writer.writeheader()
            for n,source in enumerate(paths):
                part,rows=telemetry(source,writer);all_rows.append(rows);dest=stage/f'part-{n+1:02d}'
                with (stage/f'part-{n+1:02d}.log').open('w') as log:
                    child=subprocess.run([sys.executable,str(HERE/'analyze_superscan.py'),str(source),str(dest),'--tag','superscan'],stdout=log,stderr=subprocess.STDOUT)
                if child.returncode:
                    detail=(stage/f'part-{n+1:02d}.log').read_text(errors='replace')[-6000:]
                    raise RuntimeError(f'Partition {n+1} failed (exit {child.returncode}):\n{detail}')
                verify(dest);part['report_dir']=dest.name;part['analysis']=json.loads((dest/'superscan_summary.json').read_text());parts.append(part)
        diagnostics(all_rows,parts,stage);rejected=rejected_iq(sessions,stage)
        report=dict(version=1,created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),records=sum(p['records'] for p in parts),spectral_bins=sum(p['spectral_bins'] for p in parts),credited_scan_s=sum(p['credited_scan_s'] for p in parts),rf_dwell_s=sum(p['rf_dwell_s'] for p in parts),parts=parts,rejected_iq=rejected,findings=['Headroom rejection: conservative threshold exceeded by rare IQ peaks; retained evidence does not demonstrate digital rail clipping. Pilot-only gain qualification now reserves 12 dB by default; live rejection triggers bounded gain backoff into a new partition.','USB abort: host idle sleep/wake coincided with device loss. The supervisor holds scan-scoped sleep assertions, recognizes specific transport failures, bounds reopen retries and detects stalled output. Unknown crashes remain fatal.','Duration: setup, outage gaps above five seconds, and inter-attempt downtime do not consume the acquisition budget. Every attempt and accepted partition is retained with hashes.'],limits=['Antenna measurements cannot establish absolute power, noise figure, analog compression or calibrated sensitivity.','PSD Doppler matches and off-curve controls do not establish decoded identity or a calibrated global false-discovery probability.','PSD contains no recoverable waveform phase. Phase diagnostics are limited to retained rejected IQ without a coherent RF reference.','Sequential LO comparisons are conditional on the changing RF environment and receiver state. No candidate-origin label automatically removes data.','The configured 12 dB burst reserve and five-second gap policy are engineering choices, not universal statistical guarantees.'])
        def finite_json(value):
            if isinstance(value,float) and not math.isfinite(value):return None
            if isinstance(value,dict):return {k:finite_json(v) for k,v in value.items()}
            if isinstance(value,list):return [finite_json(v) for v in value]
            return value
        report=finite_json(report)
        (stage/'summary.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
        if render_pdf:cover_pdf(report,stage)
        esc=html.escape;sections=''
        for n,p in enumerate(parts):
            s=p['analysis'];folder=p['report_dir'];links=''.join(f'<li><a href="{folder}/{x.name}">{esc(x.name)}</a></li>' for x in sorted((stage/folder).glob('*.html')))
            sections+=f'<section><h2>Part {n+1}: {p["sample_rate_hz"]/1e6:g} MS/s / {esc(str(p["gain"]))}</h2><p>{p["records"]:,} records; {p["credited_scan_s"]:.2f} s credited scan time; {p["rf_dwell_s"]:.2f} s summed dwell. SHA-256 <code>{p["sha256"]}</code></p><p><a href="{folder}/superscan_analysis.pdf">Full partition PDF</a> · <a href="{folder}/superscan_summary.json">Complete signal/pass inventory</a> · <a href="{folder}/superscan_tracks.csv">Tracks CSV</a> · <a href="{folder}/superscan_lines.csv">Lines CSV</a> · <a href="{folder}/superscan_events.csv">Events CSV</a></p><img loading="lazy" src="{folder}/superscan_p1_summary.png"><details><summary>Native-resolution interactive waterfalls</summary><ul>{links}</ul></details></section>'
        (stage/'index.html').write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Full partitioned SDR analysis</title><style>body{max-width:1280px;margin:auto;padding:32px;background:#0b1422;color:#e4eefc;font:16px/1.6 system-ui}h1{font-size:42px}h2{color:#70ddce}a{color:#7bd8ff}section{margin:25px 0;padding:22px;background:#14243a}img{width:100%}code{overflow-wrap:anywhere;font-size:12px}p,li{color:#c0d0e4}summary{cursor:pointer}</style>'+f'<h1>Complete partitioned SDR scan analysis</h1><p>{report["records"]:,} accepted records · {report["spectral_bins"]:,} recorded spectral bins processed · {report["credited_scan_s"]/60:.3f} minutes credited sequential scan time · {report["rf_dwell_s"]/60:.3f} minutes summed RF dwell.</p><p>Gain/rate contexts and interruptions are preserved. Levels remain relative. All accepted records are processed; graphical pixel grids and thumbnails are display representations.</p><p><a href="full-analysis.pdf">Multipage full analysis</a> · <a href="summary.json">Complete analysis JSON</a> · <a href="telemetry.csv">Every-record telemetry CSV</a> · <a href="bundle-manifest.json">Input/output hashes</a></p><img src="telemetry.png"><section><h2>Failure findings and fixes</h2><ul>'+''.join('<li>'+esc(x)+'</li>' for x in report['findings'])+'</ul></section>'+sections+''.join(f'<section><h2>Retained rejected IQ / phase</h2><p>{esc(r["phase_scope"])}</p><img src="{r["plot"]}"></section>' for r in rejected)+'<section><h2>Limits</h2><ul>'+''.join('<li>'+esc(x)+'</li>' for x in report['limits'])+'</ul></section>')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('output');p.add_argument('sessions',nargs='+');a=p.parse_args()
    failure=Path(a.output).absolute().with_name(Path(a.output).name+'-failure.txt')
    try:
        r=generate(a.output,a.sessions)
    except Exception:
        failure.parent.mkdir(parents=True,exist_ok=True);failure.write_text(traceback.format_exc());raise
    failure.unlink(missing_ok=True)
    print(json.dumps({k:r[k] for k in ('records','spectral_bins','credited_scan_s','rf_dwell_s')}));return 0

if __name__=='__main__':sys.exit(main())
