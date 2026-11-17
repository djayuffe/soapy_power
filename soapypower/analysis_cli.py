"""Portable offline analysis of explicit linear-density and complex-IQ NPZ files."""
import argparse
import json
import hashlib
from pathlib import Path
import numpy as np
from soapypower.files import EvidenceSnapshot, _atomic_write, load_json, write_json
from soapypower.spectral_comparison import rebin_density, fuse_observations
from soapypower.scan_profile import import_campaign_profile
from soapypower.spur_atlas import analyze_track
from soapypower.relative_noise import RelativeNoiseReference
from soapypower.common_iq import common_band_iq, equal_duration_phase


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command')
    for name in ('rebin','noise-reference','noise-compare','common-iq','phase','fuse','spur-atlas','import-profile'):
        p = sub.add_parser(name)
        p.add_argument('input',help='Density/IQ NPZ, or JSON manifest for fuse/spur-atlas/import-profile')
        p.add_argument('output')
        p.add_argument('--overwrite',action='store_true')
        if name in ('rebin','fuse'):
            p.add_argument('--target-edges',required=True,help='JSON array of target cell edges (Hz)')
        if name in ('noise-reference','noise-compare','import-profile'):
            p.add_argument('--metadata',required=True,help='JSON with context, method and (for reference) reference')
        if name == 'spur-atlas':
            p.add_argument('--tolerance-hz',type=float,required=True)
        if name == 'noise-compare':
            p.add_argument('--reference',required=True)
        if name in ('common-iq','phase'):
            p.add_argument('--sample-rate',type=float,required=True)
            p.add_argument('--duration',type=float,default=.016)
        if name == 'phase':
            p.add_argument('--bin-hz',type=float,default=10000)
            p.add_argument('--origin-exclusion',type=float,default=250000)
            p.add_argument('--usable-halfwidth',type=float,default=900000)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.error('choose an analysis command')
    paths = [args.input]
    for field in ('metadata','target_edges','reference'):
        if getattr(args,field,None): paths.append(getattr(args,field))
    try:
        snapshot = EvidenceSnapshot(paths)
        snapshot.protect_output(args.output)
        if args.command == 'spur-atlas':
            result = analyze_track(load_json(args.input),args.tolerance_hz)
            write_json(args.output,result,args.overwrite,before_publish=snapshot.verify)
            return
        if args.command == 'import-profile':
            meta = load_json(args.metadata)
            digest = hashlib.sha256(Path(args.input).read_bytes()).hexdigest()
            profile = import_campaign_profile(load_json(args.input),meta['context'],
                meta['reference']+'; source_sha256='+digest,meta.get('gain_ladder'),meta.get('point_tolerance_hz',2))
            write_json(args.output,profile.value,args.overwrite,before_publish=snapshot.verify)
            return
        if args.command == 'fuse':
            manifest = load_json(args.input)
            rows = []
            for item in manifest['observations']:
                path = Path(args.input).parent/item['path']
                snapshot.add(path)
                snapshot.protect_output(args.output)
                with np.load(path,allow_pickle=False) as archive:
                    row = {k:v for k,v in item.items() if k!='path'}
                    row.update(frequency_edges_hz=archive['frequency_edges_hz'],density=archive['density'])
                    if 'supported_fraction' in archive:row['supported_fraction']=archive['supported_fraction']
                    rows.append(row)
            data = fuse_observations(rows,load_json(args.target_edges),
                manifest.get('reference_time_s'),manifest.get('max_age_s'))
            _atomic_write(args.output,lambda stream:np.savez_compressed(stream,**data),
                          args.overwrite,before_publish=snapshot.verify)
            return
        with np.load(args.input,allow_pickle=False) as archive:
            if args.command == 'rebin':
                target = load_json(args.target_edges)
                mean,support = rebin_density(archive['frequency_edges_hz'],archive['density'],target,
                    archive['supported_fraction'] if 'supported_fraction' in archive else None)
                data = dict(frequency_edges_hz=np.asarray(target,float),density=mean,supported_fraction=support)
            elif args.command in ('noise-reference','noise-compare'):
                meta = load_json(args.metadata)
                if args.command == 'noise-reference':
                    ref = RelativeNoiseReference(archive['frequency_edges_hz'],archive['density'],
                        meta['context'],meta['method'],meta['reference'])
                    ref.save(args.output,args.overwrite,before_publish=snapshot.verify)
                    return
                ref = RelativeNoiseReference.load(args.reference)
                result = ref.compare(archive['frequency_edges_hz'],archive['density'],meta['context'],meta['method'])
                data = {k:result[k] for k in ('noise_ratio_db','signed_excess_density','supported')}
                data.update(frequency_edges_hz=ref.edges,reference_sha256=np.array(ref.sha256),
                            absolute_power=np.array(False),snr=np.array(False))
            elif args.command == 'common-iq':
                iq,meta = common_band_iq(archive['iq'],args.sample_rate,duration=args.duration)
                data = dict(iq=iq,metadata=np.array(json.dumps(meta,allow_nan=False)))
            else:
                result = equal_duration_phase(archive['iq'],args.sample_rate,args.duration,args.bin_hz,
                    origin_exclusion_hz=args.origin_exclusion,usable_halfwidth_hz=args.usable_halfwidth)
                data = {k:v for k,v in result.items() if isinstance(v,np.ndarray)}
                meta = {k:v for k,v in result.items() if k not in data and k not in ('traces','dc_train_mean','dc_validation_mean')}
                meta.update(dc_train_mean=[result['dc_train_mean'].real,result['dc_train_mean'].imag],
                    dc_validation_mean=[result['dc_validation_mean'].real,result['dc_validation_mean'].imag],
                    absolute_phase_aligned=False)
                data['metadata'] = np.array(json.dumps(meta,allow_nan=False))
                for i,trace in enumerate(result['traces']):
                    data.update({'candidate_{}_{}'.format(i,k):v for k,v in trace.items()})
        _atomic_write(args.output,lambda stream:np.savez_compressed(stream,**data),
                      args.overwrite,before_publish=snapshot.verify)
    except (ValueError,RuntimeError,OSError,KeyError,TypeError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
