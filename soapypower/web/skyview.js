'use strict';

const skyTimeUTC = seconds => finite(seconds)?new Date(seconds*1000).toISOString().slice(11,19):'—';

const skyView = {passes:[],selected:-1,mode:'live',playing:false,time:null,speed:30,
  source:null,lastFrame:null,raf:null,visible:true,hits:[],latest:null};

// Azimuth clockwise from North; elevation 0° at the horizon, 90° at zenith.
function skyProjection(azimuth,elevation,cx,cy,radius) {
  if(!finite(azimuth)||!finite(elevation))return null;
  const angle=azimuth*Math.PI/180,distance=(90-elevation)/90*radius;
  return {x:cx+Math.sin(angle)*distance,y:cy-Math.cos(angle)*distance};
}

function skyState(track,time) {
  if(!track||track.times_s?.length<2||!finite(time))return null;
  const state=satelliteAt(track,time);if(!state)return null;
  const times=track.times_s;let i=0;while(i+1<times.length-1&&times[i+1]<time)i++;
  const part=(time-times[i])/(times[i+1]-times[i]);
  state.range=track.range_m[i]+part*(track.range_m[i+1]-track.range_m[i]);
  return state;
}

function skySource(data) {
  return JSON.stringify([data.cache_identity,data.observer,data.lo_hz,data.hi_hz]);
}

function syncSkyView(data,force=false) {
  skyView.latest=data;
  if(!data||data.status==='loading')return;
  const source=skySource(data);
  if(!force&&skyView.mode==='playback'&&skyView.passes.length&&source===skyView.source)return;
  const previous=skyView.passes[skyView.selected];
  const passes=(data.passes||[]).filter(pass=>pass.sky_track?.times_s?.length>=2);
  let index=previous?passes.findIndex(pass=>pass.norad===previous.norad&&pass.rise_utc<=previous.set_utc&&pass.set_utc>=previous.rise_utc):-1;
  if(index<0)index=passes.length?0:-1;
  if(source!==skyView.source){skyView.playing=false;skyView.lastFrame=null;}
  skyView.source=source;skyView.passes=passes;skyView.selected=index;
  const select=$('sky-pass');select.replaceChildren();
  for(let i=0;i<passes.length;i++) {
    const pass=passes[i],option=satEl('option',`${pass.name} · ${pass.norad} · ${skyTimeUTC(pass.rise_utc)}–${skyTimeUTC(pass.set_utc)}`);
    option.value=String(i);select.append(option);
  }
  if(!passes.length)select.append(satEl('option','No eligible pass geometry'));
  else select.value=String(index);
  if(skyView.mode==='playback'&&passes[index])skyView.time=Math.min(passes[index].set_utc,Math.max(passes[index].rise_utc,skyView.time??passes[index].rise_utc));
  scheduleSkyDraw();
}

function selectSkyPass(pass) {
  syncSkyView(satelliteUI.data,true);
  const index=skyView.passes.findIndex(p=>p.norad===pass.norad&&p.rise_utc===pass.rise_utc&&p.set_utc===pass.set_utc);
  if(index<0)return;
  skyView.selected=index;skyView.mode='playback';skyView.playing=false;skyView.time=pass.rise_utc;skyView.lastFrame=null;
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

function renderSkyReadout(pass,state,time) {
  skyPut('sky-mode',skyView.mode==='live'?'LIVE UTC':skyView.playing?`PLAYBACK ${skyView.speed}×`:'PLAYBACK PAUSED');
  skyPut('sky-name',pass?`${pass.name} · ${pass.norad}`:'No pass selected');skyPut('sky-clock',skyClockText(time));
  const mask=skyView.latest?.observer?.min_elevation_deg??0;
  skyPut('sky-status',!pass?'No eligible pass geometry. Check RF range, observer and TLE freshness.'
    :!state?(time<pass.rise_utc?'Pass has not entered this prediction segment':'Outside this pass segment')
    :state.el>mask?'Predicted above elevation mask':state.el>=0?'Predicted below elevation mask':'Predicted below horizon');
  skyPut('sky-az',state?`${format(state.az,1)}°`:'—');skyPut('sky-el',state?`${format(state.el,1)}°`:'—');
  skyPut('sky-range',state?`${format(state.range/1000,1)} km`:'—');skyPut('sky-rate',state?`${format(state.rr/1000,3)} km/s`:'—');
  const frequency=pass?.transmitters?.[0]?.f_hz;
  skyPut('sky-doppler',state&&finite(frequency)?`${format(-frequency*state.rr/299792458,0)} Hz @ ${format(frequency/1e6,3)} MHz`:'—');
  skyPut('sky-aos',pass?`${skyTimeUTC(pass.rise_utc)}${pass.clipped_start?' · window start':''}`:'—');
  skyPut('sky-los',pass?`${skyTimeUTC(pass.set_utc)}${pass.clipped_end?' · window end':''}`:'—');
  skyPut('sky-peak',pass?`${format(pass.sky_peak_el_deg??pass.max_el_deg,1)}° @ ${skyTimeUTC(pass.sky_peak_utc??pass.max_el_utc)}`:'—');
  skyPut('sky-reference',`Current observer/catalog · ${skyView.passes.length} sampled pass segments. Playback uses a fixed prediction snapshot; receiver and spectrum clocks are unchanged.`);
  $('sky-play').disabled=!pass;$('sky-restart').disabled=!pass;$('sky-seek').disabled=!pass;
  skyPut('sky-play',skyView.playing?'Pause playback':'Play pass');
  if(pass){
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
  const mask=skyView.latest?.observer?.min_elevation_deg??0;
  c.save();c.setLineDash([5,4]);c.strokeStyle='#ba9355';c.beginPath();c.arc(cx,cy,r*(90-mask)/90,0,Math.PI*2);c.stroke();c.restore();
  c.save();c.beginPath();c.arc(cx,cy,r,0,Math.PI*2);c.clip();
  if(pass) {
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
    for(const p of skyView.passes) {
      if(p===pass||seen.has(p.norad))continue;
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
  if(!pass)return;
  const track=pass.sky_track,span=pass.set_utc-pass.rise_utc;
  const x=t=>left+(t-pass.rise_utc)/span*(right-left),y=el=>bottom-Math.max(0,Math.min(90,el))/90*(bottom-top);
  c.strokeStyle='#76cadb';c.lineWidth=1.5;c.beginPath();
  track.times_s.forEach((t,i)=>{if(i)c.lineTo(x(t),y(track.el_deg[i]));else c.moveTo(x(t),y(track.el_deg[i]));});c.stroke();
  c.save();c.setLineDash([4,4]);c.strokeStyle='#ba9355';c.beginPath();c.moveTo(left,y(skyView.latest?.observer?.min_elevation_deg??0));c.lineTo(right,y(skyView.latest?.observer?.min_elevation_deg??0));c.stroke();c.restore();
  if(time>=pass.rise_utc&&time<=pass.set_utc){c.strokeStyle='#fff';c.beginPath();c.moveTo(x(time),top);c.lineTo(x(time),bottom);c.stroke();}
  for(let i=0;i<=4;i++){c.textAlign=i===0?'left':i===4?'right':'center';c.fillStyle='#96a9bc';c.fillText(skyTimeUTC(pass.rise_utc+i*span/4),left+i*(right-left)/4,h-9);}
}

function drawSkyView(timestamp) {
  skyView.raf=null;
  if(document.hidden||!skyView.visible){skyView.lastFrame=null;return;}
  const pass=skyView.passes[skyView.selected];
  if(skyView.mode==='live')skyView.time=Date.now()/1000;
  else if(skyView.playing&&pass) {
    if(skyView.lastFrame!==null)skyView.time+=Math.max(0,(timestamp-skyView.lastFrame)/1000)*skyView.speed;
    if(skyView.time>=pass.set_utc){skyView.time=pass.set_utc;skyView.playing=false;}
  }
  skyView.lastFrame=timestamp;
  const state=pass?skyState(pass.sky_track,skyView.time):null;
  drawPolar(pass,state,skyView.time);drawElevation(pass,skyView.time);renderSkyReadout(pass,state,skyView.time);
  if(skyView.playing||skyView.mode==='live')skyView.raf=requestAnimationFrame(skyTick);
}

let skyLastDraw=0;
function skyTick(timestamp) {
  if(timestamp-skyLastDraw<80){skyView.raf=requestAnimationFrame(skyTick);return;}
  skyLastDraw=timestamp;drawSkyView(timestamp);
}
function scheduleSkyDraw() {
  if(skyView.raf===null&&!document.hidden&&skyView.visible)skyView.raf=requestAnimationFrame(drawSkyView);
}
function skySeek(fraction) {
  const pass=skyView.passes[skyView.selected];if(!pass)return;
  skyView.mode='playback';skyView.playing=false;skyView.time=pass.rise_utc+fraction*(pass.set_utc-pass.rise_utc);skyView.lastFrame=null;scheduleSkyDraw();
}

$('sky-pass').addEventListener('change',()=>{skyView.selected=Number($('sky-pass').value);skySeek(0);});
$('sky-seek').addEventListener('input',()=>skySeek(Number($('sky-seek').value)/1000));
$('sky-restart').addEventListener('click',()=>skySeek(0));
$('sky-live').addEventListener('click',()=>{skyView.mode='live';skyView.playing=false;skyView.lastFrame=null;syncSkyView(satelliteUI.data,true);});
$('sky-play').addEventListener('click',()=>{
  const pass=skyView.passes[skyView.selected];if(!pass)return;
  if(skyView.mode==='live'||!finite(skyView.time)||skyView.time>=pass.set_utc)skyView.time=pass.rise_utc;
  skyView.mode='playback';skyView.playing=!skyView.playing;skyView.lastFrame=null;scheduleSkyDraw();
});
$('sky-speed').addEventListener('change',()=>{skyView.speed=Number($('sky-speed').value);skyView.lastFrame=null;scheduleSkyDraw();});
$('sky-others').addEventListener('change',scheduleSkyDraw);
$('sky-polar').addEventListener('click',event=>{
  const rect=$('sky-polar').getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;
  const hit=skyView.hits.map(p=>({...p,d:Math.hypot(x-p.x,y-p.y)})).sort((a,b)=>a.d-b.d)[0];
  if(hit?.d<16)selectSkyPass(hit.pass);
});
const skyIntersection=new IntersectionObserver(entries=>{
  skyView.visible=entries[0].isIntersecting;
  if(!skyView.visible){if(skyView.raf!==null)cancelAnimationFrame(skyView.raf);skyView.raf=null;skyView.lastFrame=null;}
  else scheduleSkyDraw();
});skyIntersection.observe($('sky-polar'));
new ResizeObserver(scheduleSkyDraw).observe($('sky-polar'));
document.addEventListener('visibilitychange',()=>{skyView.lastFrame=null;scheduleSkyDraw();});

if(satelliteUI.data)syncSkyView(satelliteUI.data,true);
