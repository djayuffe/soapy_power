/* Analysis of observed display cells. No inference of transmitter duty cycle. */
(function(root) {
'use strict';
function analyze({width,height,c0=0,c1=width,r0=0,r1=height,threshold=-115,minimum=5,minimumActivity=10,value}) {
  if(!Number.isInteger(width)||!Number.isInteger(height)||width<1||height<1||
     !Number.isFinite(threshold)||!Number.isInteger(minimum)||minimum<1||
     !Number.isFinite(minimumActivity)||minimumActivity<0||minimumActivity>100||
     ![c0,c1,r0,r1].every(Number.isInteger)||c0<0||c1>width||r0<0||r1>height||c0>=c1||r0>=r1)
    throw new Error('Invalid analysis bounds or threshold settings');
  const columns=[],regions=[];let observed=0,active=0,peak=-Infinity,current=null;
  for(let c=c0;c<c1;c++) {
    let n=0,a=0,p=-Infinity;
    for(let r=r0;r<r1;r++){const v=value(r,c);if(!Number.isFinite(v))continue;n++;p=Math.max(p,v);if(v>threshold)a++;}
    const fraction=n?a/n:NaN,eligible=n>=minimum;
    columns.push({c,observed:n,active:a,fraction,peak:n?p:NaN,eligible});
    observed+=n;active+=a;peak=Math.max(peak,p);
    if(eligible&&a>0&&100*fraction>=minimumActivity){
      if(!current){current={c0:c,c1:c+1,observed:0,active:0,peak:-Infinity};regions.push(current);}
      current.c1=c+1;current.observed+=n;current.active+=a;current.peak=Math.max(current.peak,p);
    }else current=null;
  }
  for(const region of regions)region.fraction=region.active/region.observed;
  regions.sort((a,b)=>b.peak-a.peak||a.c0-b.c0);
  return {columns,regions,observed,active,peak:observed?peak:NaN,total:(c1-c0)*(r1-r0),fraction:observed?active/observed:NaN};
}
const api={analyze};if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.HeatmapAnalysis=api;
})(typeof globalThis!=='undefined'?globalThis:this);
