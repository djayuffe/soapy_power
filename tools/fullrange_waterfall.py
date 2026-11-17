#!/usr/bin/env python3
"""Per-pass waterfall (pass x 2 MHz cell) from fullrange features-on/off sessions. Relative levels, numpy only."""
import json, sys
from pathlib import Path
import numpy as np
CELL=2.0; F0=70.0; F1=6000.0; N=int((F1-F0)/CELL)
def run(path, usable=0.35, dc_hz=0.5e6):
    rows=[]; prev=None; pas=-1; t0=None
    from datetime import datetime
    with open(path) as f:
        for line in f:
            r=json.loads(line)
            if r.get('power') is None: continue
            p=np.asarray(r['power'],float); f0,w=float(r['first_bin_center_hz']),float(r['bin_width_hz'])
            freq=f0+w*np.arange(p.size); c=.5*(r['coverage_start_hz']+r['coverage_stop_hz']); h=.5*(r['coverage_stop_hz']-r['coverage_start_hz'])
            k=(np.abs(freq-c)<=usable*2*h)&(np.abs(freq-c)>=dc_hz)&np.isfinite(p)&(freq>=F0*1e6)&(freq<F1*1e6)
            if not k.any(): continue
            if prev is None or c<prev-1e6: pas+=1
            prev=c; t=datetime.fromisoformat(r['time_start']); t0=t0 or t
            idx=np.floor((freq[k]/1e6-F0)/CELL).astype(int); rows.append((pas,(t-t0).total_seconds(),idx,p[k]))
    P=pas+1; S=np.zeros((P,N)); C=np.zeros((P,N)); T=np.full(P,np.nan)
    for pas,t,idx,v in rows:
        np.add.at(S[pas],idx,v); np.add.at(C[pas],idx,1)
        if np.isnan(T[pas]): T[pas]=t
    with np.errstate(invalid='ignore'): G=np.where(C>0,S/np.maximum(C,1),np.nan)
    return G,T
def main():
    sess=Path(sys.argv[1]); out={'cell_mhz':CELL,'start_mhz':F0,'stop_mhz':F1}
    for nm in ('features-on','features-off'):
        if not (sess/(nm+'.jsonl')).exists():
            print(nm,'missing',file=sys.stderr); continue
        G,T=run(sess/(nm+'.jsonl')); out[nm]={'passes':int(G.shape[0]),'t0_s':[None if np.isnan(x) else round(float(x),2) for x in T],
            'grid':[[None if np.isnan(x) else round(float(x),2) for x in row] for row in G]}
        print(nm,G.shape,np.isfinite(G).mean(),file=sys.stderr)
    json.dump(out,sys.stdout,separators=(',',':'))
if __name__=='__main__': main()
