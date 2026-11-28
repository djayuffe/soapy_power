#!/usr/bin/env python3
"""Per-pass waterfall (pass x 2 MHz cell) from fullrange features-on/off sessions. Relative levels, numpy only."""
import json, sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
CELL=2.0; F0=70.0; F1=6000.0; N=int((F1-F0)/CELL)
def run(path, usable=0.35, dc_hz=0.5e6, return_origin=False):
    rows=[]; prev=None; pas=-1; t0=None; last_stop=None
    from datetime import datetime
    recorded_unit=None
    with open(path) as f:
        for line in f:
            if not line.strip() or not line.endswith('\n'):continue
            r=json.loads(line)
            if r.get('power') is None: continue
            unit=r.get('unit','dB(input power/Hz)')
            if recorded_unit is not None and unit!=recorded_unit:raise ValueError('Mixed density units need separate reports')
            recorded_unit=unit
            from soapypower.report_spectrum import density
            freq,p,k,c=density(r,usable,dc_hz,F0*1e6,F1*1e6)
            if not k.any(): continue
            if prev is None or c<prev-1e6: pas+=1
            prev=c; t=datetime.fromisoformat(r['time_start']); t0=t0 or t
            if t.tzinfo is None:raise ValueError('Timezone-aware acquisition timestamps required')
            last_stop=datetime.fromisoformat(r.get('time_stop',r['time_start']))
            if last_stop.tzinfo is None or last_stop<t:raise ValueError('Invalid acquisition dwell')
            idx=np.floor((freq[k]/1e6-F0)/CELL).astype(int); rows.append((pas,(t-t0).total_seconds(),idx,p[k]))
    P=pas+1; S=np.zeros((P,N)); C=np.zeros((P,N)); T=np.full(P,np.nan)
    for pas,t,idx,v in rows:
        np.add.at(S[pas],idx,v); np.add.at(C[pas],idx,1)
        if np.isnan(T[pas]): T[pas]=t
    with np.errstate(invalid='ignore',divide='ignore'): G=np.where((C>0)&(S>0),10*np.log10(S/np.maximum(C,1)),np.nan)
    return (G,T,t0.timestamp() if t0 else None,(last_stop-t0).total_seconds() if last_stop and t0 else None) if return_origin else (G,T)
def main():
    sess=Path(sys.argv[1]); out={'cell_mhz':CELL,'start_mhz':F0,'stop_mhz':F1,'aggregation':'Linear-power mean of measured, unmasked bins; displayed in dB','time_scope':'Per-pass sequential mosaic; pass timestamps do not imply simultaneous RF coverage'}
    for nm in ('features-on','features-off'):
        if not (sess/(nm+'.jsonl')).exists():
            print(nm,'missing',file=sys.stderr); continue
        G,T,origin,duration=run(sess/(nm+'.jsonl'),return_origin=True); out[nm]={'time_origin_unix':origin,'duration_s':duration,'passes':int(G.shape[0]),'t0_s':[None if np.isnan(x) else round(float(x),2) for x in T],
            'grid':[[None if np.isnan(x) else round(float(x),2) for x in row] for row in G]}
        print(nm,G.shape,np.isfinite(G).mean(),file=sys.stderr)
    json.dump(out,sys.stdout,separators=(',',':'))
if __name__=='__main__': main()
