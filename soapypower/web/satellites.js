'use strict';
const satelliteUI = {revision:0,data:null,timer:null,polling:false,observerDirty:false,observerEditRevision:0,observerSaving:false,range:{low:100,high:6000,seconds:7200},controller:null,failures:0,lastSuccess:null,transport:'connecting',focus:null,prepareBusy:false,page:0};
const satEl = (tag,text) => {const node=document.createElement(tag);if(text!==undefined)node.textContent=text;return node;};
const satTime = seconds => finite(seconds) ? new Date(seconds*1000).toISOString().slice(11,19) : '—';

function satelliteAt(curve, seconds) {
  const times=curve?.times_s;
  if(!times||times.length<2||!finite(seconds)|| seconds<times[0] || seconds>times.at(-1))return null;
  let i=0;while(i+1<times.length-1 && times[i+1]<seconds)i++;
  const part=(seconds-times[i])/(times[i+1]-times[i]);
  if(!finite(part)||!['az_deg','el_deg','range_rate_m_s'].every(k=>curve[k]?.length===times.length))return null;
  const mix=values=>values[i]+part*(values[i+1]-values[i]);
  const azDelta=((curve.az_deg[i+1]-curve.az_deg[i]+540)%360)-180;
  const result={rr:mix(curve.range_rate_m_s),el:mix(curve.el_deg),az:(curve.az_deg[i]+part*azDelta+360)%360};
  return Object.values(result).every(finite)?result:null;
}

function satelliteMarkers(live) {
  const data=satelliteUI.data;
  if(!$('satellite-overlay').checked || !data || data.frame_error || data.frame_session!==model.selected || !live)return [];
  const stop=Date.parse(live.stop)/1000,start=Date.parse(live.start)/1000;
  const time=finite(start)&&start<=stop?(start+stop)/2:stop;
  const first=live.frequencies_hz?.[0],last=live.frequencies_hz?.at(-1);
  if(!finite(time)||!finite(first)||!finite(last))return [];
  const markers=[];
  for(const curve of data.curves||[]) {
    const state=satelliteAt(curve,time);
    if(!state||state.el<=(data.frame_min_el_deg??0))continue;
    for(const tx of curve.transmitters) {
      const low=tx.f_hz*(1-state.rr/299792458),high=(tx.f_high_hz||tx.f_hz)*(1-state.rr/299792458);
      if(high<first||low>last)continue;
      markers.push({name:curve.name,norad:curve.norad,hz:(low+high)/2,low,high,el:state.el,az:state.az,
        doppler_hz:(low+high-tx.f_hz-(tx.f_high_hz||tx.f_hz))/2,mode:tx.mode});
    }
  }
  return markers.sort((a,b)=>b.el-a.el).slice(0,24);
}

function drawSatelliteOverlay(context,geometry,live) {
  const markers=satelliteMarkers(live),first=live?.frequencies_hz?.[0],last=live?.frequencies_hz?.at(-1);
  if(!finite(first)||!finite(last)||last<=first)return;
  const {left,right,top,bottom}=geometry;
  context.save();context.beginPath();context.rect(left,top,right-left,bottom-top);context.clip();
  context.strokeStyle='#76cadb';context.fillStyle='#a1dce7';context.font='10px ui-monospace,monospace';
  context.textAlign='left';context.setLineDash([3,4]);
  markers.slice(0,8).forEach((marker,index)=>{
    const x=left+(marker.hz-first)/(last-first)*(right-left);
    context.beginPath();context.moveTo(x,top);context.lineTo(x,bottom);context.stroke();
    context.fillText(`${marker.name.slice(0,25)} · predicted`,Math.min(right-170,Math.max(left+4,x+4)),top+12+(index%4)*13);
  });
  context.restore();
}

function renderSatelliteFrame() {
  const data=satelliteUI.data,markers=satelliteMarkers(model.detail?.live);
  const live=model.detail?.live;
  if(!$('satellite-overlay').checked) $('satellite-frame').textContent='Predicted overlay disabled. Recorded spectra and raw data are unchanged.';
  else if(!live) $('satellite-frame').textContent='No acquisition frame selected. Passes below use current UTC and the current observer.';
  else if(data?.frame_session!==model.selected) $('satellite-frame').textContent='Updating reference for the selected acquisition frame…';
  else if(data?.frame_error) $('satellite-frame').textContent=`Frame reference unavailable: ${data.frame_error}. No orbital markers applied.`;
  else $('satellite-frame').textContent=`Frame ${stamp(live.stop)} · ${data?.frame_reference||'reference pending'} · ${markers.length} predicted in-band downlink candidates. `+
    (markers.length?markers.slice(0,3).map(m=>`${m.name}: ${format(m.hz/1e6,6)} MHz, Δf ${format(m.doppler_hz,0)} Hz, el ${format(m.el,1)}°, az ${format(m.az,0)}°`).join(' | '):'No eligible above-mask prediction for this frame.')+
    ' · Predictions do not confirm a measured signal or identity.';
}

async function prepareSatellite(norad,id,button) {
  if(satelliteUI.prepareBusy)return;
  satelliteUI.prepareBusy=true;button.disabled=true;
  const revision=satelliteUI.revision,seconds=Number($('satellite-duration').value||300);
  try {
    const payload=await request('/api/satellites/prepare',{norad,transmitter_id:id,seconds});
    if(revision!==satelliteUI.revision)throw new Error('Satellite context changed; prepare the target again.');
    await openWorkbench();
    if(revision!==satelliteUI.revision||!workbench.catalog||workbench.busy)throw new Error('Context changed or Workbench is busy. Prepare the target again.');
    $('expert-operation').value='acquisition';renderExpert();
    $('expert-runtime').value=payload.runtime;$('expert-seconds').value=String(payload.seconds);
    $('expert-antispur').checked=payload.anti_spur;
    for(const input of $('expert-fields').querySelectorAll('[data-dest]')) {
      if(Object.hasOwn(payload.values,input.dataset.dest))input.value=String(payload.values[input.dataset.dest]);
    }
    workbench.satellite=payload.satellite;invalidateExpert();
    $('expert-description').textContent=`SatNOGS target NORAD ${norad}. Review receiver, gain and frequency geometry. Reference will be frozen before RX starts.`;
  } catch(error){notify(error.message);}
  finally{satelliteUI.prepareBusy=false;button.disabled=false;}
}

function satelliteButton(tx,norad) {
  const button=satEl('button','Prepare scan');
  button.disabled=tx.tle_eligible===false||!satelliteUI.data?.observer;
  button.title=tx.tle_eligible===false?'Fresh eligible orbital elements required':'Review receiver and gain in the Workbench before queuing';
  button.addEventListener('click',()=>prepareSatellite(norad,tx.id,button));return button;
}

function renderSatelliteCatalog() {
  const target=$('satellite-catalog');
  const search=$('satellite-search').value.toLowerCase();
  const rows=(satelliteUI.data?.catalog||[]).filter(tx=>`${tx.name} ${tx.norad} ${tx.f_hz/1e6} ${tx.mode}`.toLowerCase().includes(search));
  const signature=JSON.stringify([search,rows]);
  if(target.dataset?.signature===signature)return;
  if(target.dataset)target.dataset.signature=signature;target.replaceChildren();
  for(const tx of rows.slice(0,120)) {
    const card=satEl('div');card.className='satellite-item';
    card.append(satEl('strong',`${tx.name} · ${tx.norad}`),satEl('span',`${format(tx.f_hz/1e6,6)}–${format(tx.f_high_hz/1e6,6)} MHz · ${tx.mode||'mode unknown'}`),
      satEl('small',`${tx.description}${tx.unconfirmed?' · database frequency unconfirmed':''}`),satelliteButton(tx,tx.norad));
    const track=satEl('button','Track live');track.disabled=tx.tle_eligible===false;track.addEventListener('click',()=>trackSatellite(tx.norad));card.append(track);
    if(tx.tle_eligible===false)card.append(satEl('small','Orbital elements stale or invalid'));target.append(card);
  }
  if(rows.length>120)target.append(satEl('p','Showing 120 transmitters. Narrow the search to inspect the remainder.'));
}

function trackSatellite(norad) {
  satelliteUI.focus=norad;if(typeof clearSkyView==='function')clearSkyView();restartSatellitePoll();
  $('sky-title').scrollIntoView({behavior:'smooth',block:'center'});
}

function restartSatellitePoll() {
  satelliteUI.revision++;satelliteUI.controller?.abort();clearTimeout(satelliteUI.timer);pollSatellites();
}

function satelliteHealth() {
  const node=$('satellite-health');if(!node)return;
  const age=satelliteUI.lastSuccess===null?null:Math.max(0,(performance.now()-satelliteUI.lastSuccess)/1000);
  node.textContent=satelliteUI.transport==='offline'?`Connection lost · retrying${age===null?'':` · last response ${Math.floor(age)} s ago`}`:
    satelliteUI.transport==='connecting'?'Connecting to local prediction service…':`Connected · last response ${Math.floor(age??0)} s ago`;
  node.dataset.state=satelliteUI.transport;
}

function renderSatellites() {
  const data=satelliteUI.data;if(!data)return;
  $('satellite-source').textContent=`SatNOGS DB · CC BY-SA · fetched ${stamp(data.fetched_utc)} · ${data.active_satellites??'—'} active satellites in range`;
  $('satellite-state').textContent=data.status==='loading'?'Calculating local orbital predictions…':data.error||`${data.eligible_tles??0} eligible TLEs · ${data.stale_or_invalid_tles??0} stale/invalid · 30 s pass search · bracketed crossings refined to 0.5 s${data.updating?' · updating predictions':''}`;
  if(data.refresh?.state) $('satellite-state').textContent+=` · data refresh: ${data.refresh.state}${data.refresh.error?' · '+data.refresh.error:''}`;
  if(data.prediction_truncated||data.catalog_truncated||data.passes_truncated||data.curves_truncated||data.world_truncated)$('satellite-state').textContent+=' · result budget reached; narrow RF range';
  const query=$('satellite-pass-search').value.trim().toLowerCase();
  const matching=(data.passes||[]).filter(p=>`${p.name} ${p.norad}`.toLowerCase().includes(query));
  const pages=Math.max(1,Math.ceil(matching.length/24));satelliteUI.page=Math.max(0,Math.min(pages-1,satelliteUI.page));
  $('satellite-page').textContent=`${matching.length} pass segments · page ${satelliteUI.page+1} / ${pages}`;
  $('satellite-prev').disabled=satelliteUI.page===0;$('satellite-next').disabled=satelliteUI.page>=pages-1;
  const visible=matching.slice(satelliteUI.page*24,(satelliteUI.page+1)*24);
  const list=$('satellite-passes'),passSignature=JSON.stringify([data.status,visible]);
  if(list.dataset?.signature!==passSignature){
  if(list.dataset)list.dataset.signature=passSignature;list.replaceChildren();
  for(const pass of visible) {
    const tx=pass.transmitters[0],row=satEl('tr');
    const cell=text=>{const c=satEl('td',text);row.append(c);return c;};
    cell(`${pass.name} · ${pass.norad}`);
    cell(`${satTime(pass.rise_utc)} – ${satTime(pass.set_utc)}${pass.clipped_start||pass.clipped_end?' · clipped to window':''}`);
    cell(`${format(pass.sky_peak_el_deg??pass.max_el_deg,1)}°`);
    cell(pass.transmitters.slice(0,3).map(t=>`${format(t.f_hz/1e6,6)} MHz ${t.mode||''}`).join(' · '));
    const actions=cell();
    const view=satEl('button','View pass');view.disabled=!pass.sky_track;view.addEventListener('click',()=>selectSkyPass(pass));
    actions.append(view,satelliteButton(tx,pass.norad));list.append(row);
  }
  if(!list.children.length) {const row=satEl('tr'),cell=satEl('td',data.status==='loading'?'Calculating passes…':'No eligible passes in the selected window/range. Check observer, RF range and TLE freshness.');cell.colSpan=5;row.append(cell);list.append(row);}
  }
  if(data.observer&&!satelliteUI.observerDirty)for(const [key,value] of Object.entries(data.observer))$('observer-form').elements.namedItem(key).value=String(value);
  renderSatelliteCatalog();renderSatelliteFrame();scheduleDraw();
  if(typeof syncSkyView==='function'){syncSkyView(data);
    if(satelliteUI.focus&&data.focus_norad===satelliteUI.focus){const orbit=(data.world||[]).find(o=>o.norad===satelliteUI.focus);
      if(orbit&&skyView.mode==='live'&&skyView.focusOrbit?.norad!==orbit.norad)selectWorldSatellite(orbit);}
  }
  satelliteHealth();
}

async function pollSatellites() {
  clearTimeout(satelliteUI.timer);
  if(satelliteUI.polling)return;
  if(document.hidden){satelliteUI.timer=setTimeout(pollSatellites,10000);return;}
  satelliteUI.polling=true;
  const revision=satelliteUI.revision,selected=model.selected;
  const controller=new AbortController();satelliteUI.controller=controller;
  try {
    const range=satelliteUI.range;
    const query=new URLSearchParams({lo_mhz:range.low,hi_mhz:range.high,seconds:range.seconds});
    if(model.selected)query.set('session',model.selected);
    if(satelliteUI.focus)query.set('norad',String(satelliteUI.focus));
    const result=await request('/api/satellites?'+query,null,true,controller.signal);
    if(revision===satelliteUI.revision&&selected===model.selected){satelliteUI.data=result;satelliteUI.failures=0;satelliteUI.transport='connected';satelliteUI.lastSuccess=performance.now();renderSatellites();}
  } catch(error){if(revision===satelliteUI.revision&&selected===model.selected&&error.name!=='AbortError'){satelliteUI.failures++;satelliteUI.transport='offline';$('satellite-state').textContent=error.message;satelliteHealth();}}
  finally {satelliteUI.polling=false;satelliteUI.controller=null;
    if(!document.hidden)satelliteUI.timer=setTimeout(pollSatellites,revision!==satelliteUI.revision||selected!==model.selected?100:satelliteUI.failures?Math.min(30000,1500*2**Math.min(4,satelliteUI.failures)):satelliteUI.data?.status==='loading'||satelliteUI.data?.updating?1500:10000);}
}

$('satellite-nav').addEventListener('click',()=>{$('satellite-panel').scrollIntoView({behavior:'smooth'});pollSatellites();});
$('satellite-apply').addEventListener('click',()=>{
  const low=Number($('satellite-low').value),high=Number($('satellite-high').value);
  if(!finite(low)||!finite(high)||low<1||high>10000||high<=low){notify('Choose an ascending RF range between 1 and 10000 MHz.');return;}
  satelliteUI.revision++;satelliteUI.controller?.abort();satelliteUI.page=0;satelliteUI.focus=null;
  $('satellite-passes').replaceChildren();$('satellite-catalog').replaceChildren();delete $('satellite-passes').dataset.signature;delete $('satellite-catalog').dataset.signature;$('satellite-state').textContent='Updating selected RF range…';
  satelliteUI.range={low:Number($('satellite-low').value),high:Number($('satellite-high').value),seconds:Number($('satellite-horizon').value)};
  satelliteUI.data=null;if(typeof clearSkyView==='function')clearSkyView();scheduleDraw();pollSatellites();
});
$('satellite-search').addEventListener('input',renderSatelliteCatalog);
$('satellite-overlay').addEventListener('change',()=>{renderSatelliteFrame();scheduleDraw();});
$('satellite-refresh').addEventListener('click',async event=>{
  event.target.disabled=true;
  try{const state=await request('/api/satellites/refresh',{});$('satellite-state').textContent=`SatNOGS update ${state.state}; RX continues independently.`;}
  catch(error){notify(error.message);}finally{event.target.disabled=false;pollSatellites();}
});
$('observer-form').addEventListener('input',()=>{satelliteUI.observerDirty=true;satelliteUI.observerEditRevision++;});
$('observer-form').addEventListener('submit',async event=>{
  event.preventDefault();if(satelliteUI.observerSaving)return;
  const data={};for(const [key,value] of new FormData(event.target))data[key]=Number(value);
  const editRevision=satelliteUI.observerEditRevision,button=event.target.querySelector('button[type="submit"]');
  satelliteUI.observerSaving=true;button.disabled=true;
  try{
    await request('/api/satellites/observer',data);
    satelliteUI.observerDirty=editRevision!==satelliteUI.observerEditRevision;
    satelliteUI.revision++;satelliteUI.controller?.abort();satelliteUI.data=null;
    if(typeof clearSkyView==='function')clearSkyView();
    notify(satelliteUI.observerDirty?'Position saved; newer form edits remain unsaved.':'Local observer saved. Frozen references remain unchanged.');pollSatellites();
  } catch(error){notify(error.message);}
  finally{satelliteUI.observerSaving=false;button.disabled=false;}
});
setTimeout(pollSatellites,1000);

function drawSatelliteWaterfall(context,live,rows,width,height,first,span) {
  if(!live||!finite(live.center_hz))return;
  const firstIF=live.frequencies_hz?.[0]-live.center_hz,lastIF=live.frequencies_hz?.at(-1)-live.center_hz;
  if(!finite(firstIF)||!finite(lastIF)||lastIF<=firstIF)return;
  context.save();context.fillStyle='#91d7e3';
  for(const row of rows) {
    if(!finite(row.center_hz))continue;
    const markers=satelliteMarkers({...row,frequencies_hz:[row.center_hz+firstIF,row.center_hz+lastIF]});
    const start=Date.parse(row.start),stop=Date.parse(row.stop),y=((start+stop)/2-first)/span*height;
    for(const marker of markers) {
      const offset=(marker.hz-row.center_hz-firstIF)/(lastIF-firstIF);
      const index=Math.round(offset*(row.power.length-1));
      if(offset>=0&&offset<=1&&finite(row.power[index]))context.fillRect(offset*width-1,y-1,3,2);
    }
  }
  context.restore();
}
$('scan-satnogs').addEventListener('click',()=>{
  $('scan-dialog').close();$('satellite-panel').scrollIntoView({behavior:'smooth'});pollSatellites();
});

function satelliteSelectionChanged() {
  satelliteUI.revision++;satelliteUI.controller?.abort();satelliteUI.data=null;
  clearTimeout(satelliteUI.timer);satelliteUI.timer=setTimeout(pollSatellites,100);
}

$('satellite-pass-search').addEventListener('input',()=>{satelliteUI.page=0;renderSatellites();});
$('satellite-prev').addEventListener('click',()=>{satelliteUI.page--;renderSatellites();});
$('satellite-next').addEventListener('click',()=>{satelliteUI.page++;renderSatellites();});
$('satellite-retry').addEventListener('click',restartSatellitePoll);
document.addEventListener('visibilitychange',()=>{
  clearTimeout(satelliteUI.timer);
  if(document.hidden)satelliteUI.controller?.abort();else pollSatellites();
});
if(typeof window!=='undefined'&&window.addEventListener)window.addEventListener('online',restartSatellitePoll);
setInterval(satelliteHealth,1000);

function saveSatellitePreferences() {
  try {localStorage.setItem('soapySatelliteView',JSON.stringify({range:satelliteUI.range,
    overlay:$('satellite-overlay').checked,others:$('sky-others').checked,duration:Number($('satellite-duration').value)}));}
  catch { /* Viewer preferences are optional; observer coordinates stay on the server. */ }
}
try {
  const saved=JSON.parse(localStorage.getItem('soapySatelliteView')||'null'),range=saved?.range;
  if(range&&finite(range.low)&&finite(range.high)&&range.low>=1&&range.high<=10000&&range.high>range.low&&[1800,3600,7200].includes(range.seconds)) {
    satelliteUI.range=range;$('satellite-low').value=String(range.low);$('satellite-high').value=String(range.high);$('satellite-horizon').value=String(range.seconds);
  }
  if(typeof saved?.overlay==='boolean')$('satellite-overlay').checked=saved.overlay;
  if(typeof saved?.others==='boolean')$('sky-others').checked=saved.others;
  if([60,300,600,1800].includes(saved?.duration))$('satellite-duration').value=String(saved.duration);
} catch { /* Invalid or unavailable browser storage does not affect reception. */ }
for(const id of ['satellite-apply','satellite-overlay','sky-others','satellite-duration'])
  $(id).addEventListener(id==='satellite-apply'?'click':'change',saveSatellitePreferences);
