'use strict';

const skyTimeUTC = seconds => finite(seconds)?new Date(seconds*1000).toISOString().slice(11,19):'—';

const skyView = {passes:[],selected:-1,mode:'live',playing:false,time:null,speed:30,
  source:null,raf:null,visible:true,hits:[],latest:null,snapshot:null,focusOrbit:null,transmitterId:null};
for(const key of ['mode','playing','time','speed','lastFrame'])Object.defineProperty(skyView,key,{get:()=>globalClock[key],set:value=>{globalClock[key]=value;}});

// Azimuth clockwise from North; elevation 0° at the horizon, 90° at zenith.
function skyProjection(azimuth,elevation,cx,cy,radius) {
  if(!finite(azimuth)||!finite(elevation))return null;
  const angle=azimuth*Math.PI/180,distance=(90-elevation)/90*radius;
  return {x:cx+Math.sin(angle)*distance,y:cy-Math.cos(angle)*distance};
}

function skyState(track,time) {
  if(!track||track.times_s?.length<2||!finite(time)||!track.range_m)return null;
  const state=satelliteAt(track,time);if(!state)return null;
  const times=track.times_s;let i=0;while(i+1<times.length-1&&times[i+1]<time)i++;
  const part=(time-times[i])/(times[i+1]-times[i]);
  state.range=track.range_m[i]+part*(track.range_m[i+1]-track.range_m[i]);
  return finite(state.range)&&state.range>0?state:null;
}

function skySource(data) {
  return JSON.stringify([data.cache_identity,data.observer,data.lo_hz,data.hi_hz]);
}

function syncSkyView(data,force=false) {
  if(!data||data.status==='loading'){if(!data)clearSkyView();return;}
  skyView.latest=data;
  const source=skySource(data);
  if(!force&&skyView.mode==='playback'&&skyView.passes.length&&source===skyView.source)return;
  if(!force&&document.activeElement===$('sky-pass'))return;
  const previous=skyView.passes[skyView.selected];
  const passes=(data.passes||[]).filter(pass=>pass.sky_track?.times_s?.length>=2);
  let index=previous?passes.findIndex(pass=>pass.norad===previous.norad&&pass.rise_utc<=previous.set_utc&&pass.set_utc>=previous.rise_utc):-1;
  if(index<0)index=passes.length?0:-1;
  if(source!==skyView.source){skyView.mode='live';skyView.playing=false;skyView.lastFrame=null;skyView.focusOrbit=null;}
  skyView.source=source;skyView.passes=passes;skyView.selected=index;
  if(skyView.focusOrbit)skyView.focusOrbit=(data.world||[]).find(s=>s.norad===skyView.focusOrbit.norad)||null;
  const select=$('sky-pass');select.replaceChildren();
  for(let i=0;i<passes.length;i++) {
    const pass=passes[i],option=satEl('option',`${pass.name} · ${pass.norad} · ${skyTimeUTC(pass.rise_utc)}–${skyTimeUTC(pass.set_utc)}`);
    option.value=String(i);select.append(option);
  }
  if(!passes.length)select.append(satEl('option','No eligible pass geometry'));
  else select.value=String(index);
  if(skyView.mode==='playback'&&passes[index])skyView.time=Math.min(passes[index].set_utc,Math.max(passes[index].rise_utc,skyView.time??passes[index].rise_utc));
  const objects=$('sky-object'),world=data.world||[],signature=world.map(o=>`${o.norad}:${o.name}`).join('|');
  if(objects.dataset?.signature!==signature){
    objects.replaceChildren();
    for(const orbit of world){const option=satEl('option',`${orbit.name} · ${orbit.norad}`);option.value=String(orbit.norad);objects.append(option);}
    if(!world.length)objects.append(satEl('option','No orbital tracks loaded'));
    if(objects.dataset)objects.dataset.signature=signature;
  }
  if(document.activeElement!==objects)objects.value=String(skyView.focusOrbit?.norad??passes[index]?.norad??'');
  scheduleSkyDraw();
}

function clearSkyView() {
  skyView.latest=null;skyView.snapshot=null;skyView.passes=[];skyView.selected=-1;skyView.focusOrbit=null;skyView.source=null;
  skyView.mode='live';skyView.playing=false;skyView.lastFrame=null;
  $('sky-pass').replaceChildren(satEl('option','Updating orbital geometry…'));
  $('sky-object').replaceChildren(satEl('option','Updating orbital geometry…'));if($('sky-object').dataset)delete $('sky-object').dataset.signature;scheduleSkyDraw();
}

function selectWorldSatellite(orbit) {
  satelliteUI.focus=orbit.norad;
  if(skyView.mode!=='live'){skyView.mode='live';skyView.playing=false;skyView.lastFrame=null;syncSkyView(skyView.latest,true);}
  const index=skyView.passes.findIndex(p=>p.norad===orbit.norad);
  skyView.focusOrbit=(skyView.latest?.world||[]).find(o=>o.norad===orbit.norad)||orbit;skyView.selected=index;$('sky-object').value=String(orbit.norad);
  if(index>=0)$('sky-pass').value=String(index);
  scheduleSkyDraw();
}

function skySelected() {
  const orbit=skyView.focusOrbit;
  if(orbit&&skyView.mode==='live'){
    const pass=skyView.passes[skyView.selected];
    if(pass)return pass;
    return {...orbit,map_only:true,sky_track:orbit.track,ground_track:orbit.track};
  }
  return skyView.passes[skyView.selected];
}

function selectSkyPass(pass) {
  syncSkyView(satelliteUI.data,true);
  const index=skyView.passes.findIndex(p=>p.norad===pass.norad&&p.rise_utc<=pass.set_utc&&p.set_utc>=pass.rise_utc);
  if(index<0){notify('Pass window changed. Select a current pass from the list.');return;}
  pass=skyView.passes[index];satelliteUI.focus=pass.norad;
  skyView.selected=index;skyView.focusOrbit=null;skyView.snapshot=skyView.latest;skyView.mode='playback';skyView.playing=false;skyView.time=pass.rise_utc;globalClock.end=pass.set_utc;skyView.lastFrame=null;
  $('sky-pass').value=String(index);$('sky-title').scrollIntoView({behavior:'smooth',block:'center'});scheduleSkyDraw();
}

function skyCanvas(id) {
  const canvas=$(id),bounds=canvas.getBoundingClientRect();
  if(!bounds.width||!bounds.height)return null;
  const ratio=Math.min(3,window.devicePixelRatio||1);
  const width=Math.round(bounds.width*ratio),height=Math.round(bounds.height*ratio);
  if(canvas.width!==width||canvas.height!==height){canvas.width=width;canvas.height=height;}
  const context=canvas.getContext('2d');context.setTransform(ratio,0,0,ratio,0,0);
  return {context,width:bounds.width,height:bounds.height};
}

function skyClockText(time) {
  return finite(time)?new Date(time*1000).toISOString().replace('T',' ').slice(0,19)+' UTC':'—';
}

function skyPut(id,value) {
  const text=String(value??'—'),node=$(id);
  if(node.textContent!==text)node.textContent=text;
}

function selectedTransmitters(pass) {
  const data=skyView.mode==='playback'?skyView.snapshot:skyView.latest;
  const items=(data?.catalog||[]).filter(tx=>tx.norad===pass?.norad);
  return items.length?items:pass?.transmitters||[];
}

function syncSkyTransmitters(pass) {
  const items=selectedTransmitters(pass),select=$('sky-transmitter');
  const signature=JSON.stringify([pass?.norad,items.map(t=>[t.id,t.f_hz,t.mode])]);
  if(select.dataset?.signature!==signature){
    select.replaceChildren();
    for(const tx of items){const option=satEl('option',`${format(tx.f_hz/1e6,6)} MHz · ${tx.mode||'mode unknown'}`);option.value=tx.id;select.append(option);}
    if(!items.length)select.append(satEl('option','No downlink available'));
    if(select.dataset)select.dataset.signature=signature;
  }
  if(!items.some(t=>t.id===skyView.transmitterId))skyView.transmitterId=items[0]?.id??null;
  select.value=skyView.transmitterId??'';select.disabled=!items.length;
  return items.find(t=>t.id===skyView.transmitterId)||items[0];
}

function renderSkyReadout(pass,state,time) {
  skyPut('sky-mode',skyView.mode==='live'?'LIVE UTC':skyView.playing?`PLAYBACK ${skyView.speed}×`:'PLAYBACK PAUSED');
  renderGlobalClock();
  skyPut('sky-name',pass?`${pass.name} · ${pass.norad}`:'No pass selected');skyPut('sky-clock',skyClockText(time));
  const mask=(skyView.mode==='playback'?skyView.snapshot:skyView.latest)?.observer?.min_elevation_deg??0;
  skyPut('sky-status',!pass?'No eligible pass geometry. Check RF range, observer and TLE freshness.'
    :!state&&pass.map_only?'Set an observer for local angles, or wait for fresh orbital geometry'
    :!state?(time<pass.rise_utc?'Pass has not entered this prediction segment':'Outside this pass segment')
    :state.el>mask?'Predicted above elevation mask':state.el>=0?'Predicted below elevation mask':'Predicted below horizon');
  skyPut('sky-az',state?`${format(state.az,1)}°`:'—');skyPut('sky-el',state?`${format(state.el,1)}°`:'—');
  skyPut('sky-range',state?`${format(state.range/1000,1)} km`:'—');skyPut('sky-rate',state?`${format(state.rr/1000,3)} km/s`:'—');
  const tx=syncSkyTransmitters(pass),frequency=tx?.f_hz;
  $('sky-prepare').disabled=!tx?.id||tx.tle_eligible===false||!skyView.latest?.observer||satelliteUI.prepareBusy;
  skyPut('sky-doppler',state&&finite(frequency)?`${format(-frequency*state.rr/299792458,0)} Hz @ ${format(frequency/1e6,3)} MHz`:'—');
  skyPut('sky-tle',tx?.tle_epoch_utc?skyClockText(Date.parse(tx.tle_epoch_utc)/1000):'Epoch unavailable in this geometry');
  skyPut('sky-receive',state&&finite(frequency)?`${format(frequency*(1-state.rr/299792458)/1e6,6)} MHz`:'—');
  const event=pass&&!pass.map_only?(time<pass.rise_utc?`AOS in ${duration(pass.rise_utc-time)}`:time<=pass.set_utc?`LOS in ${duration(pass.set_utc-time)}`:'Segment ended'):'No local pass in displayed window';
  skyPut('sky-event',event);
  skyPut('sky-aos',pass&&!pass.map_only?`${skyTimeUTC(pass.rise_utc)}${pass.clipped_start?' · window start':''}`:'—');
  skyPut('sky-los',pass&&!pass.map_only?`${skyTimeUTC(pass.set_utc)}${pass.clipped_end?' · window end':''}`:'—');
  skyPut('sky-peak',pass&&!pass.map_only?`${format(pass.sky_peak_el_deg??pass.max_el_deg,1)}° @ ${skyTimeUTC(pass.sky_peak_utc??pass.max_el_utc)}`:'—');
  const data=skyView.mode==='playback'?skyView.snapshot:skyView.latest;
  const stale=skyView.mode==='live'&&data?.world?.length&&!data.world.some(s=>time>=s.track.times_s[0]&&time<=s.track.times_s.at(-1));
  skyPut('sky-reference',`${skyView.mode==='live'?'Current observer/catalog':'Fixed playback snapshot'} · ${skyView.passes.length} sampled pass segments${stale?' · geometry expired; waiting for update':''}. Receiver and spectrum timestamps are unchanged.`);
  $('sky-play').disabled=!pass||pass.map_only;$('sky-restart').disabled=!pass||pass.map_only;$('sky-seek').disabled=!pass||pass.map_only;
  skyPut('sky-play',skyView.playing?'Pause playback':'Play pass');
  if(pass&&!pass.map_only){
    const fraction=(time-pass.rise_utc)/(pass.set_utc-pass.rise_utc);
    $('sky-seek').value=String(Math.round(Math.max(0,Math.min(1,fraction))*1000));
    $('sky-seek').setAttribute('aria-valuetext',skyClockText(time));
  }
}

function drawPolar(pass,state,time) {
  const plot=skyCanvas('sky-polar');if(!plot)return;
  const {context:c,width:w,height:h}=plot,cx=w/2,cy=h/2,r=Math.max(1,Math.min(w,h)/2-40);
  c.fillStyle='#121a23';c.fillRect(0,0,w,h);c.font='11px ui-monospace,monospace';
  c.lineWidth=1;c.strokeStyle='#344251';c.textAlign='center';c.textBaseline='middle';
  for(const elevation of [0,30,60]) {
    c.beginPath();c.arc(cx,cy,r*(90-elevation)/90,0,Math.PI*2);c.stroke();
    c.fillStyle='#8b9bad';c.fillText(`${elevation}°`,cx+15,cy-r*(90-elevation)/90+12);
  }
  for(let az=0;az<360;az+=45) {
    const p=skyProjection(az,0,cx,cy,r);c.beginPath();c.moveTo(cx,cy);c.lineTo(p.x,p.y);c.stroke();
  }
  c.fillStyle='#dbe5ee';[['N',0],['E',90],['S',180],['W',270]].forEach(([name,az])=>{
    const p=skyProjection(az,0,cx,cy,r+21);c.fillText(name,p.x,p.y);
  });
  c.fillStyle='#8b9bad';c.fillText('90°',cx,cy+12);
  const mask=(skyView.mode==='playback'?skyView.snapshot:skyView.latest)?.observer?.min_elevation_deg??0;
  c.save();c.setLineDash([5,4]);c.strokeStyle='#ba9355';c.beginPath();c.arc(cx,cy,r*(90-mask)/90,0,Math.PI*2);c.stroke();c.restore();
  c.save();c.beginPath();c.arc(cx,cy,r,0,Math.PI*2);c.clip();
  if(pass?.sky_track?.az_deg) {
    const track=pass.sky_track;c.strokeStyle='#76cadb';c.lineWidth=1.7;c.beginPath();let pen=false;
    track.times_s.forEach((t,i)=>{
      const p=skyProjection(track.az_deg[i],track.el_deg[i],cx,cy,r);
      if(!p||track.el_deg[i]<0){pen=false;return;}
      if(pen)c.lineTo(p.x,p.y);else c.moveTo(p.x,p.y);pen=true;
    });c.stroke();
    const first=skyProjection(track.az_deg[0],track.el_deg[0],cx,cy,r),last=skyProjection(track.az_deg.at(-1),track.el_deg.at(-1),cx,cy,r);
    if(first){c.beginPath();c.arc(first.x,first.y,5,0,Math.PI*2);c.stroke();}
    if(last)c.strokeRect(last.x-4,last.y-4,8,8);
  }
  skyView.hits=[];
  if($('sky-others').checked) {
    const seen=new Set();let labels=0;
    const data=skyView.mode==='playback'?skyView.snapshot:skyView.latest;
    const otherPasses=skyView.mode==='live'?(data?.world||[]).map(o=>({...o,sky_track:o.track,map_only:true})):skyView.passes;
    for(const p of otherPasses) {
      if(p.norad===pass?.norad||seen.has(p.norad))continue;
      const other=skyState(p.sky_track,time);if(!other||other.el<=mask)continue;seen.add(p.norad);
      const point=skyProjection(other.az,other.el,cx,cy,r);if(!point)continue;
      c.fillStyle='#7896af';c.beginPath();c.arc(point.x,point.y,3,0,Math.PI*2);c.fill();
      skyView.hits.push({...point,pass:p});
      if(labels++<5){c.fillStyle='#8fa3b7';c.textAlign='left';c.fillText(p.name.slice(0,18),point.x+6,point.y-6);}
    }
  }
  if(state&&state.el>=0) {
    const point=skyProjection(state.az,state.el,cx,cy,r);
    c.fillStyle=state.el>mask?'#a1e4ee':'#d4ad6c';c.beginPath();c.arc(point.x,point.y,6,0,Math.PI*2);c.fill();
    c.strokeStyle='#fff';c.lineWidth=1;c.stroke();skyView.hits.push({...point,pass});
    c.textAlign='left';c.fillText(pass.name.slice(0,24),point.x+9,point.y-9);
  }
  c.restore();
}

function drawElevation(pass,time) {
  const plot=skyCanvas('sky-timeline');if(!plot)return;
  const {context:c,width:w,height:h}=plot,left=38,right=w-16,top=14,bottom=h-29;
  c.fillStyle='#121a23';c.fillRect(0,0,w,h);c.font='10px ui-monospace,monospace';c.lineWidth=.7;
  c.strokeStyle='#344251';c.fillStyle='#96a9bc';c.textAlign='right';
  for(const el of [0,30,60,90]){const y=bottom-el/90*(bottom-top);c.beginPath();c.moveTo(left,y);c.lineTo(right,y);c.stroke();c.fillText(`${el}°`,left-6,y+3);}
  if(!pass||pass.map_only)return;
  const track=pass.sky_track,span=pass.set_utc-pass.rise_utc;
  const x=t=>left+(t-pass.rise_utc)/span*(right-left),y=el=>bottom-Math.max(0,Math.min(90,el))/90*(bottom-top);
  c.strokeStyle='#76cadb';c.lineWidth=1.5;c.beginPath();
  track.times_s.forEach((t,i)=>{if(i)c.lineTo(x(t),y(track.el_deg[i]));else c.moveTo(x(t),y(track.el_deg[i]));});c.stroke();
  c.save();c.setLineDash([4,4]);c.strokeStyle='#ba9355';c.beginPath();c.moveTo(left,y((skyView.mode==='playback'?skyView.snapshot:skyView.latest)?.observer?.min_elevation_deg??0));c.lineTo(right,y((skyView.mode==='playback'?skyView.snapshot:skyView.latest)?.observer?.min_elevation_deg??0));c.stroke();c.restore();
  if(time>=pass.rise_utc&&time<=pass.set_utc){c.strokeStyle='#fff';c.beginPath();c.moveTo(x(time),top);c.lineTo(x(time),bottom);c.stroke();}
  for(let i=0;i<=4;i++){c.textAlign=i===0?'left':i===4?'right':'center';c.fillStyle='#96a9bc';c.fillText(skyTimeUTC(pass.rise_utc+i*span/4),left+i*(right-left)/4,h-9);}
}

function drawSkyView(timestamp) {
  skyView.raf=null;
  if(document.hidden||!skyView.visible){skyView.lastFrame=null;return;}
  const pass=skySelected();
  globalClock.advance(timestamp);
  if(skyView.mode==='playback'&&skyView.playing&&pass&&skyView.time>=pass.set_utc){skyView.time=pass.set_utc;skyView.playing=false;}
  skyView.lastFrame=timestamp;
  let state=pass?skyState(pass.sky_track,skyView.time):null;
  if(skyView.mode==='live'&&pass){
    const orbit=(skyView.latest?.world||[]).find(o=>o.norad===pass.norad);
    state=(orbit?skyState(orbit.track,skyView.time):null)||state;
  }
  drawPolar(pass,state,skyView.time);drawElevation(pass,skyView.time);renderSkyReadout(pass,state,skyView.time);
  if(typeof drawWorldMap==='function')drawWorldMap(pass,skyView.time);
  if(skyView.playing||skyView.mode==='live')skyView.raf=requestAnimationFrame(skyTick);
}

let skyLastDraw=0;
function skyTick(timestamp) {
  if(timestamp-skyLastDraw<80){skyView.raf=requestAnimationFrame(skyTick);return;}
  skyLastDraw=timestamp;drawSkyView(timestamp);
}
function scheduleSkyDraw() {
  if(typeof worldMap!=='undefined')worldMap.lastPaint=-Infinity;
  if(skyView.raf===null&&!document.hidden&&skyView.visible)skyView.raf=requestAnimationFrame(drawSkyView);
}
function skySeek(fraction) {
  const pass=skyView.passes[skyView.selected];if(!pass)return;
  skyView.focusOrbit=null;if(skyView.mode==='live')skyView.snapshot={...skyView.latest,passes:skyView.passes};skyView.mode='playback';skyView.playing=false;skyView.time=pass.rise_utc+Math.max(0,Math.min(1,fraction))*(pass.set_utc-pass.rise_utc);globalClock.end=pass.set_utc;skyView.lastFrame=null;scheduleSkyDraw();
}

$('sky-object').addEventListener('change',()=>{
  const orbit=(skyView.latest?.world||[]).find(o=>o.norad===Number($('sky-object').value));
  if(!orbit)return;skyView.mode='live';skyView.playing=false;skyView.lastFrame=null;selectWorldSatellite(orbit);
});
$('sky-pass').addEventListener('blur',()=>{if(skyView.mode==='live')syncSkyView(skyView.latest,true);});
$('sky-pass').addEventListener('change',()=>{skyView.selected=Number($('sky-pass').value);satelliteUI.focus=skyView.passes[skyView.selected]?.norad??null;skySeek(0);});
$('sky-prepare').addEventListener('click',event=>{
  const pass=skySelected(),tx=selectedTransmitters(pass).find(t=>t.id===skyView.transmitterId);
  if(pass&&tx)prepareSatellite(pass.norad,tx.id,event.target);
});
$('sky-transmitter').addEventListener('change',()=>{skyView.transmitterId=$('sky-transmitter').value;scheduleSkyDraw();});
$('sky-seek').addEventListener('input',()=>skySeek(Number($('sky-seek').value)/1000));
$('sky-restart').addEventListener('click',()=>skySeek(0));
$('sky-live').addEventListener('click',()=>{skyView.mode='live';skyView.playing=false;skyView.focusOrbit=null;skyView.lastFrame=null;syncSkyView(satelliteUI.data,true);});
$('sky-play').addEventListener('click',()=>{
  const pass=skyView.passes[skyView.selected];if(!pass)return;
  if(skyView.mode==='live'||!finite(skyView.time)||skyView.time>=pass.set_utc)skyView.time=pass.rise_utc;
  globalClock.end=pass.set_utc;
  if(skyView.mode==='live')skyView.snapshot={...skyView.latest,passes:skyView.passes};
  skyView.focusOrbit=null;skyView.mode='playback';skyView.playing=!skyView.playing;skyView.lastFrame=null;scheduleSkyDraw();
});
$('sky-speed').addEventListener('change',()=>{skyView.speed=Number($('sky-speed').value);skyView.lastFrame=null;scheduleSkyDraw();});
$('sky-others').addEventListener('change',scheduleSkyDraw);
$('sky-polar').addEventListener('click',event=>{
  const rect=$('sky-polar').getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;
  const hit=skyView.hits.map(p=>({...p,d:Math.hypot(x-p.x,y-p.y)})).sort((a,b)=>a.d-b.d)[0];
  if(hit?.d<16){if(hit.pass.map_only)selectWorldSatellite({...hit.pass,track:hit.pass.sky_track});else selectSkyPass(hit.pass);}
});
const skyIntersection=new IntersectionObserver(entries=>{
  skyView.visible=entries[0].isIntersecting;
  if(!skyView.visible){if(skyView.raf!==null)cancelAnimationFrame(skyView.raf);skyView.raf=null;skyView.lastFrame=null;}
  else scheduleSkyDraw();
});skyIntersection.observe($('sky-title').closest('.sky-view'));
new ResizeObserver(scheduleSkyDraw).observe($('sky-polar'));
document.addEventListener('visibilitychange',()=>{skyView.lastFrame=null;scheduleSkyDraw();});

globalClock.listeners.add(scheduleSkyDraw);
if(satelliteUI.data)syncSkyView(satelliteUI.data,true);
