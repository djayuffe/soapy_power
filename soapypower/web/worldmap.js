'use strict';

const worldMap={land:null,error:null,hits:[],lastPaint:-Infinity};
function worldProjection(lon,lat,width,height) {
  if(!finite(lon)||!finite(lat)||Math.abs(lat)>90)return null;
  return {x:(((lon+180)%360+360)%360)/360*width,y:(90-lat)/180*height};
}
function worldAt(track,time) {
  const t=track?.times_s;
  if(!t||t.length<2||time<t[0]||time>t.at(-1)||!finite(time))return null;
  let i=0;while(i<t.length-2&&t[i+1]<time)i++;
  const part=(time-t[i])/(t[i+1]-t[i]);
  if(!finite(part)||!['lon_deg','lat_deg','alt_m'].every(k=>track[k]?.length===t.length))return null;
  const mix=key=>track[key][i]+part*(track[key][i+1]-track[key][i]);
  const delta=((track.lon_deg[i+1]-track.lon_deg[i]+540)%360)-180;
  const result={lat:mix('lat_deg'),lon:((track.lon_deg[i]+part*delta+540)%360)-180,alt:mix('alt_m')};
  return Object.values(result).every(finite)&&Math.abs(result.lat)<=90?result:null;
}
// Split at the dateline instead of connecting opposite edges across the map.
function worldLine(c,points,w,h,close=false) {
  let previous=null;
  c.beginPath();
  for(const pair of points) {
    const point=worldProjection(pair[0],pair[1],w,h);
    if(!point){previous=null;continue;}
    if(!previous||Math.abs(point.x-previous.x)>w/2)c.moveTo(point.x,point.y);
    else c.lineTo(point.x,point.y);
    previous=point;
  }
  if(close)c.closePath();c.stroke();
}
function footprint(position,mask=0) {
  // Spherical Earth approximation for the configured elevation mask, visual only.
  const radius=6371008.8,el=mask*Math.PI/180;
  const beta=Math.max(0,Math.acos(Math.min(1,radius/(radius+Math.max(0,position.alt))*Math.cos(el)))-el);
  const lat=position.lat*Math.PI/180,lon=position.lon*Math.PI/180,points=[];
  for(let i=0;i<=180;i++) {
    const bearing=i/180*2*Math.PI;
    const phi=Math.asin(Math.sin(lat)*Math.cos(beta)+Math.cos(lat)*Math.sin(beta)*Math.cos(bearing));
    const lambda=lon+Math.atan2(Math.sin(bearing)*Math.sin(beta)*Math.cos(lat),Math.cos(beta)-Math.sin(lat)*Math.sin(phi));
    points.push([lambda*180/Math.PI,phi*180/Math.PI]);
  }return points;
}
function worldSelected(pass,time) {
  const source=skyView.mode==='playback'?skyView.snapshot:skyView.latest;
  const orbit=(source?.world||[]).find(s=>s.norad===pass?.norad);
  const rolling=orbit?worldAt(orbit.track,time):null;
  return {source,orbit,position:rolling||(pass?.ground_track?worldAt(pass.ground_track,time):null)};
}
function drawWorldMap(pass,time,force=false) {
  const plot=skyCanvas('world-map');if(!plot)return;
  if(!force&&finite(time)&&Math.abs(time-worldMap.lastPaint)<.2)return;
  worldMap.lastPaint=time;
  const {context:c,width:w,height:h}=plot;
  c.fillStyle='#111b25';c.fillRect(0,0,w,h);
  c.fillStyle='#263b48';c.strokeStyle='#426075';c.lineWidth=.6;
  for(const polygon of worldMap.land?.polygons||[]) {
    c.beginPath();for(const ring of polygon)ring.forEach(([lon,lat],i)=>{
      // Preserve +180 at the right edge of static polygons.
      const x=(lon+180)/360*w,y=(90-lat)/180*h;if(i)c.lineTo(x,y);else c.moveTo(x,y);
    });c.closePath();c.fill('evenodd');c.stroke();
  }
  c.strokeStyle='#304353';c.lineWidth=.5;
  for(let lon=-150;lon<180;lon+=30){const x=(lon+180)/360*w;c.beginPath();c.moveTo(x,0);c.lineTo(x,h);c.stroke();}
  for(let lat=-60;lat<=60;lat+=30){const y=(90-lat)/180*h;c.beginPath();c.moveTo(0,y);c.lineTo(w,y);c.stroke();}
  const {source,orbit,position}=worldSelected(pass,time),qth=source?.observer;
  worldMap.hits=[];c.font='11px ui-monospace,monospace';c.textAlign='left';
  if($('sky-others').checked)for(const sat of source?.world||[]) {
    if(sat.norad===pass?.norad)continue;
    const p=worldAt(sat.track,time);if(!p)continue;
    const point=worldProjection(p.lon,p.lat,w,h);
    c.fillStyle='#7e9eb4';c.beginPath();c.arc(point.x,point.y,2.4,0,2*Math.PI);c.fill();
    worldMap.hits.push({...point,satellite:sat});
  }
  const orbitPath=source?.ground_orbits?.[String(pass?.norad)];
  if(orbitPath){c.strokeStyle='#477887';c.lineWidth=1;worldLine(c,orbitPath.lon_deg.map((lon,i)=>[lon,orbitPath.lat_deg[i]]),w,h);}
  if(pass?.ground_track) {
    const track=pass.ground_track;c.strokeStyle='#76cadb';c.lineWidth=1.6;
    worldLine(c,track.lon_deg.map((lon,i)=>[lon,track.lat_deg[i]]),w,h);
  } else if(orbit) {
    const track=orbit.track;c.strokeStyle='#76cadb';c.lineWidth=1.6;
    worldLine(c,track.lon_deg.map((lon,i)=>[lon,track.lat_deg[i]]),w,h);
  }
  if(position) {
    c.strokeStyle='#b8975f';c.setLineDash([4,4]);worldLine(c,footprint(position,qth?.min_elevation_deg??0),w,h);c.setLineDash([]);
    const p=worldProjection(position.lon,position.lat,w,h);
    c.fillStyle='#a1e4ee';c.beginPath();c.arc(p.x,p.y,5,0,2*Math.PI);c.fill();
    c.fillText((pass?.name||orbit?.name||'').slice(0,24),Math.min(w-160,Math.max(4,p.x+8)),Math.max(14,p.y-8));
  }
  if(qth&&finite(qth.lon_deg)&&finite(qth.lat_deg)) {
    const p=worldProjection(qth.lon_deg,qth.lat_deg,w,h);c.strokeStyle='#f2ca83';c.lineWidth=1.5;
    c.strokeRect(p.x-4,p.y-4,8,8);c.fillStyle='#f2ca83';c.fillText('Observer',Math.min(w-70,p.x+8),Math.max(14,p.y-8));
  }
  skyPut('world-position',position?`Sub-satellite ${format(position.lat,3)}°, ${format(position.lon,3)}° · altitude ${format(position.alt/1000,1)} km`:'No geometry at the shared clock time');
  skyPut('world-status',worldMap.error||`${source?.world?.length||0} orbital tracks${source?.world_truncated?' · catalog limit: 512; narrow RF range':''} · same UTC clock as sky plot · amber approximate visibility footprint`);
}
$('world-map').addEventListener('click',event=>{
  const rect=$('world-map').getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;
  const hit=worldMap.hits.map(p=>({...p,d:Math.hypot(x-p.x,y-p.y)})).sort((a,b)=>a.d-b.d)[0];
  if(hit?.d<12)selectWorldSatellite(hit.satellite);
});
new ResizeObserver(()=>{worldMap.lastPaint=-Infinity;scheduleSkyDraw();}).observe($('world-map'));
if(typeof fetch==='function')fetch('/world-land.json',{cache:'force-cache'}).then(r=>{if(!r.ok)throw new Error('Basemap unavailable');return r.json();})
  .then(data=>{worldMap.land=data;worldMap.lastPaint=-Infinity;scheduleSkyDraw();})
  .catch(()=>{worldMap.error='Basemap unavailable; orbital positions and grid remain usable.';scheduleSkyDraw();});
