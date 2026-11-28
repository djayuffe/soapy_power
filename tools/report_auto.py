#!/usr/bin/env python3
"""Autogenerate the full report for a fullrange session directory (features-on.jsonl / features-off.jsonl).
Writes SESSION/report/: compare.json (if missing), waterfall.json (if missing), 10 true-3D waterfalls (PNG+PDF),
intermittency page, summary.json, one merged PDF (if pypdf is installed) and README.txt.
Failed generations return nonzero and leave the published report intact; diagnostics are in report-failure.txt.
usage: report_auto.py SESSION [--gif] [--suite dash_suite.json]"""
import argparse, json, subprocess, sys, traceback, hashlib, shutil
from report_bundle import bundle
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent))


def sh(args, out=None, timeout=1800):
    r = subprocess.run([sys.executable] + [str(a) for a in args], capture_output=True, text=True, timeout=timeout, env={**__import__('os').environ, 'PYTHONPATH': str(HERE.parent)})
    if out is not None and r.returncode == 0:
        Path(out).write_text(r.stdout)
    return r


def intermittency(f, d, out):
    import numpy as np, matplotlib
    matplotlib.use('Agg'); import matplotlib.pyplot as plt
    BG, FG = '#070b14', '#c9d4e6'
    plt.rcParams.update({'figure.facecolor': BG, 'axes.facecolor': '#0b1222', 'text.color': FG, 'axes.labelcolor': FG, 'xtick.color': FG, 'ytick.color': FG, 'axes.edgecolor': '#2b3a55', 'axes.grid': True, 'grid.color': '#24324f', 'grid.alpha': .5, 'axes.spines.top': False, 'axes.spines.right': False, 'axes.titleweight': 'bold', 'axes.titlelocation': 'left'})
    ref = d.get('features-on') or d.get('features-off'); g, t = ref
    edges=np.arange(np.floor(f.min()/10)*10,np.ceil(f.max()/10)*10,10)
    meds,sds=[],[]
    for lo in edges:
        mask=(f>=lo)&(f<lo+10)
        with __import__('warnings').catch_warnings():
            __import__('warnings').simplefilter('ignore',RuntimeWarning)
            values=np.nanmedian(g[:,mask],axis=1) if mask.any() else np.full(len(g),np.nan)
            meds.append(np.nanmedian(values));sds.append(np.nanstd(values))
    meds,sds=np.asarray(meds),np.asarray(sds)
    eligible=np.flatnonzero(np.isfinite(meds)&np.isfinite(sds));chosen=[]
    for i in eligible[np.argsort(-sds[eligible])]:
        if all(abs(edges[i]-edges[j])>=40 for j,_ in chosen):chosen.append((i,'Variable window'))
        if len(chosen)>=3:break
    control=next((i for i in eligible[np.argsort(sds[eligible])] if i not in [j for j,_ in chosen]),None)
    if control is not None:chosen.append((control,'Stable comparison window'))
    fig,axs=plt.subplots(2,2,figsize=(18,10.5),dpi=100)
    fig.suptitle('Pass-to-pass variation in measured 10 MHz windows',fontsize=18,fontweight='bold')
    for ax,(i,role) in zip(axs.flat,chosen):
        lo=edges[i];mask=(f>=lo)&(f<lo+10)
        for key,col,label in [('features-on','#00d1ff','ON'),('features-off','#ffb000','OFF')]:
            if key not in d:continue
            gg,tt=d[key]
            with __import__('warnings').catch_warnings():
                __import__('warnings').simplefilter('ignore',RuntimeWarning)
                values=np.nanmedian(gg[:,mask],axis=1)
            ax.plot(tt,values,'o-',color=col,label=label)
        ax.set(title=f'{lo:g}-{lo+10:g} MHz · {role}',xlabel='Seconds from variant acquisition origin',ylabel='Recorded level (dB)')
        ax.legend(frameon=False)
    for ax in list(axs.flat)[len(chosen):]:
        ax.set_axis_off()
        if not chosen:ax.text(.1,.5,'No finite measured pass variation',transform=ax.transAxes)
    fig.tight_layout(rect=(0, 0, 1, .95)); fig.savefig(out / 'intermittent.png', bbox_inches='tight'); fig.savefig(out / 'intermittent.pdf', bbox_inches='tight'); plt.close(fig)


def _generate(out, options):
    sess=options.session;out=Path(out);out.mkdir(exist_ok=True);status=[]
    suite=options.suite
    source_files=[p for p in (sess/'features-on.jsonl',sess/'features-off.jsonl') if p.exists()]
    def identity(paths):
        result={}
        for path in paths:
            h=hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
            result[str(path.resolve())]=h.hexdigest()
        return result
    provenance_files=source_files+[HERE/x for x in ('report_auto.py','fullrange_compare.py','fullrange_waterfall.py','report_waterfalls3d.py','report_orbits.py')]
    if suite:provenance_files.append(suite)
    if not source_files:provenance_files += [p for p in (sess/'waterfall.json',sess/'compare.json') if p.exists()]
    inputs=identity(provenance_files)
    manifest=out/'manifest.json'
    stale=bool(source_files)
    try:
        import numpy, matplotlib  # noqa
    except Exception as e:
        manifest.write_text(json.dumps(dict(version=1,status='skipped',inputs=inputs,error=str(e)),indent=2)+'\n')
        (out/'status.txt').write_text('skipped: '+str(e)+'\n');print('report skipped:',e);return 1
    if not source_files:
        for name in ('compare.json','waterfall.json'):
            if (sess/name).exists(): shutil.copyfile(sess/name,out/name)
    try:
        if stale or not (out / 'compare.json').exists() or (out / 'compare.json').stat().st_size < 10:
            r = sh([HERE / 'fullrange_compare.py', sess], out / 'compare.json'); status.append(f'compare exit {r.returncode}')
            if r.returncode:raise RuntimeError('compare failed: '+r.stderr[-200:])
        if stale or not (out / 'waterfall.json').exists() or (out / 'waterfall.json').stat().st_size < 10:
            r = sh([HERE / 'fullrange_waterfall.py', sess], out / 'waterfall.json'); status.append(f'waterfall exit {r.returncode}{" " + r.stderr[-200:] if r.returncode else ""}')
            if r.returncode:raise RuntimeError('waterfall failed: '+r.stderr[-200:])
        wf = out / 'waterfall.json'
        if not wf.exists() or wf.stat().st_size < 10:
            raise RuntimeError('no waterfall.json (no features-on/off jsonl?)')
        from report_orbits import generate as orbital_report
        orbital=orbital_report(sess,json.loads(wf.read_text()),out,options.reference)
        status.append('orbital context '+orbital['status'])
        args=[HERE/'report_waterfalls3d.py',wf,out]+([suite] if suite else [])+(['--gif'] if options.gif else [])+['--orbits',out/'orbital-context.json']
        r = sh(args); status.append(f'3d exit {r.returncode}{" " + r.stderr[-300:] if r.returncode else ""}')
        if r.returncode:raise RuntimeError('3D renderer failed; refusing to merge stale pages')
        sys.path.insert(0, str(HERE)); import report_waterfalls3d as w3
        f, d = w3.load(wf); intermittency(f, d, out); status.append('intermittency ok')
        from soapypower.report_telemetry import generate as telemetry_report
        if source_files:
            telemetry_report(source_files,out/'feature-telemetry');status.append('all-record telemetry ok')
            from soapypower.scan_report import generate as capture_preview
            capture_preview(source_files,out/'capture-previews',include_telemetry=False);status.append('measured-dwell previews ok')
            spur=sh(['-m','soapypower.scan_spurs',*source_files,'--out',out/'spur-evidence'],timeout=120)
            if spur.returncode:raise RuntimeError('Measured spur diagnostics failed: '+spur.stderr[-400:])
            status.append('measured spur evidence ok')
        cmp_ = out / 'compare.json'
        if cmp_.exists() and cmp_.stat().st_size > 10:
            c = json.loads(cmp_.read_text()); (out / 'summary.json').write_text(json.dumps({'agreement': c.get('agreement'), **{k: {x: v.get(x) for x in ('records', 'hops', 'passes', 'seconds', 'records_per_s', 'temperature_records')} for k, v in c.get('variants', {}).items()}}, indent=1))
        try:
            if options.no_pdf:raise ImportError('PDF disabled explicitly')
            from pypdf import PdfWriter, PdfReader
            wr = PdfWriter()
            for nm in ('on', 'off'):
                for bn in ('fullrange', '70-300MHz', '300-1000MHz', '1-3GHz', '3-6GHz'):
                    p = out / f'wf3d_{nm}_{bn}.pdf'
                    if p.exists():
                        i = len(wr.pages); [wr.add_page(pg) for pg in PdfReader(str(p)).pages]; wr.add_outline_item(title=f'{nm.upper()} {bn}', page_number=i)
            if (out / 'intermittent.pdf').exists():
                i = len(wr.pages); [wr.add_page(pg) for pg in PdfReader(str(out / 'intermittent.pdf')).pages]; wr.add_outline_item(title='Intermittens', page_number=i)
            extra=list(sorted(out.glob('orbital-az-el-*.pdf')))+list(sorted((out/'feature-telemetry').glob('telemetry-*.pdf')))
            for page in extra:
                i=len(wr.pages);[wr.add_page(pg) for pg in PdfReader(str(page)).pages];wr.add_outline_item(title=page.stem,page_number=i)
            if len(wr.pages):
                wr.write(str(out / 'report.pdf')); status.append(f'pdf {len(wr.pages)} pages')
        except ImportError:
            if not options.no_pdf:raise RuntimeError('PDF generation requires pypdf; install it or use --no-pdf explicitly')
            status.append('PDF disabled explicitly')
        if not (out/'summary.json').exists():(out/'summary.json').write_text(json.dumps(dict(status='no comparison data',orbital_status=orbital['status'])))
        summary=json.loads((out/'summary.json').read_text())
        summary['report_features']=dict(orbital_status=orbital['status'],orbital_reference=orbital['reference'],raw_telemetry=bool(source_files),measured_dwell=bool(source_files),measured_spur_evidence=bool(source_files),merged_pdf=(out/'report.pdf').exists(),animation_requested=options.gif)
        (out/'summary.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n')
        (out/'index.html').write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Automatic scan report</title><style>body{max-width:1400px;margin:24px auto;padding:16px;font:15px system-ui}img{max-width:100%}</style><h1>Automatic receive report</h1><p>Relative recorded levels. Sequential passes are sampled mosaics, not simultaneous RF coverage.</p><p>'+('<a href="report.pdf">Combined PDF</a> · ' if (out/'report.pdf').exists() else '')+'<a href="summary.json">Summary</a> · <a href="orbital-context.json">Az/el reference and status</a> · <a href="orbital-az-el.csv">Az/el CSV</a>'+(' · <a href="feature-telemetry/index.html">All telemetry and feature evidence</a> · <a href="capture-previews/index.html">Measured-dwell waterfalls</a> · <a href="spur-evidence/index.html">Measured spur diagnostics</a>' if source_files else ' · No raw telemetry available in cached waterfall input')+'</p><p>Orbital context: '+__import__('html').escape(orbital.get('reason') or orbital['scope'])+'</p>'+''.join('<p><a href="'+p.name+'">'+p.name+'</a></p><img loading="lazy" src="'+p.name+'">' for p in sorted(out.glob('*.png'))))
        (out / 'README.txt').write_text('Autogenerated by tools/report_auto.py. Relative, uncalibrated levels; antenna input mixes RF-environment change into ON/OFF differences.\nwf3d_*.png/pdf: true-3D waterfalls (linear x MHz, y s, z dB; identical z, colour and time scale).\nintermittent.png: per-pass level in the most variable 10 MHz windows.\nreport.pdf: all pages merged.\n')
    except Exception:
        status.append('error: ' + traceback.format_exc()[-600:])
    current=identity(provenance_files)
    if current!=inputs:status.append('error: source evidence changed during rendering')
    manifest.write_text(json.dumps(dict(version=1,status='failed' if any(x.startswith('error:') for x in status) else 'completed',inputs=inputs,output_reuses_source_cache=not stale,stages=status),indent=2)+'\n')
    (out / 'status.txt').write_text('\n'.join(status) + '\n'); print('\n'.join(status)); return 1 if any(x.startswith('error:') for x in status) else 0


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('session',type=Path)
    p.add_argument('--suite',type=Path)
    p.add_argument('--reference',type=Path,help='Verified archived reference bundle; a session reference always takes precedence')
    p.add_argument('--gif',action='store_true')
    p.add_argument('--no-pdf',action='store_true',help='Explicitly disable merged PDF generation')
    return p


def main(argv=None):
    options=parser().parse_args(argv)
    sess=options.session;out=sess/'report'
    if not sess.is_dir():parser().error('Session directory does not exist')
    if options.suite and not options.suite.is_file():parser().error('Suite JSON does not exist')
    inputs=[p for p in (sess/'features-on.jsonl',sess/'features-off.jsonl') if p.exists()]
    if not inputs:inputs=[p for p in (sess/'compare.json',sess/'waterfall.json') if p.exists()]
    inputs+=list(HERE.glob('*.py'))
    inputs += [HERE.parent/'soapypower'/name for name in ('report_telemetry.py','report_spectrum.py','scan_report.py','satellites.py','scan_spurs.py','spur_pipeline.py')]
    if options.suite:inputs.append(options.suite)
    reference=sess/'reference'
    if reference.exists():inputs.append(reference)
    elif options.reference:inputs.append(options.reference)
    try:
        with bundle(out,inputs) as stage:
            if _generate(stage,options):raise RuntimeError('report stages failed; publication refused')
        (sess/'report-failure.txt').unlink(missing_ok=True)
        return 0
    except Exception:
        (sess/'report-failure.txt').write_text(traceback.format_exc())
        return 1


if __name__ == '__main__':
    sys.exit(main())
