'use strict';

// One prediction clock for every orbital view; RF records keep their own timestamps.
const globalClock = {
  mode:'live', playing:false, time:null, speed:30, lastFrame:null,
  utc:Date.now()/1000, monotonic:performance.now(), synced:null, uncertainty:null,
  syncing:false, timer:null, end:null, listeners:new Set(), raf:null,
  liveNow() { return this.utc+(performance.now()-this.monotonic)/1000; },
  advance(timestamp) {
    if(this.mode==='live')this.time=this.liveNow();
    else if(this.playing&&this.lastFrame!==null&&!document.hidden)
      this.time+=Math.max(0,timestamp-this.lastFrame)/1000*this.speed;
    if(this.mode==='playback'&&this.playing&&Number.isFinite(this.end)&&this.time>=this.end){this.time=this.end;this.playing=false;}
    this.lastFrame=timestamp;
    return this.time;
  },
  changed() { this.lastFrame=null;for(const listener of this.listeners)listener();renderGlobalClock(); }
};

function renderGlobalClock() {
  const node=document.getElementById('global-clock');if(!node)return;
  const now=globalClock.liveNow(),age=globalClock.synced===null?Infinity:(performance.now()-globalClock.synced)/1000;
  const text=new Date((globalClock.mode==='live'?now:globalClock.time??now)*1000).toISOString().replace('T',' ').slice(0,19)+' UTC';
  if(node.textContent!==text)node.textContent=text;
  const status=document.getElementById('global-clock-status');
  if(status)status.textContent=(globalClock.mode==='live'?'LIVE':globalClock.playing?`SIM ${globalClock.speed}×`:'SIM PAUSED')+
    (age>90?' · clock unsynchronized':` · server sync ±${Math.ceil(globalClock.uncertainty*1000)} ms`);
  node.title='Shared orbital prediction clock. Recorded spectra retain acquisition timestamps. Server UTC accuracy depends on the host system clock.';
}

async function syncGlobalClock() {
  clearTimeout(globalClock.timer);if(globalClock.syncing)return;
  globalClock.syncing=true;
  const controller=new AbortController(),deadline=setTimeout(()=>controller.abort(),4000),start=performance.now();
  try {
    const response=await fetch('/api/health',{cache:'no-store',signal:controller.signal});
    if(!response.ok)throw new Error('Clock unavailable');
    const result=await response.json(),end=performance.now(),rtt=(end-start)/1000;
    if(!Number.isFinite(result.unix_s)||rtt>4)throw new Error('Invalid clock sample');
    globalClock.utc=result.unix_s+rtt/2;globalClock.monotonic=end;
    globalClock.synced=end;globalClock.uncertainty=rtt/2;
  } catch { /* Keep advancing from the last anchor; visibly mark old synchronization. */ }
  finally {clearTimeout(deadline);globalClock.syncing=false;renderGlobalClock();globalClock.timer=setTimeout(syncGlobalClock,30000);}
}

let clockLastPaint=0;
function globalClockTick(timestamp) {
  globalClock.raf=null;
  if(document.hidden){globalClock.lastFrame=null;return;}
  globalClock.advance(timestamp);
  if(timestamp-clockLastPaint>=250){renderGlobalClock();clockLastPaint=timestamp;}
  globalClock.raf=requestAnimationFrame(globalClockTick);
}
document.addEventListener('visibilitychange',()=>{
  globalClock.lastFrame=null;
  if(!document.hidden){if(globalClock.raf===null)globalClock.raf=requestAnimationFrame(globalClockTick);syncGlobalClock();}
});
if(typeof fetch==='function'){syncGlobalClock();globalClock.raf=requestAnimationFrame(globalClockTick);}
