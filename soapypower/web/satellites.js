'use strict';
const satelliteUI = {revision:0,data:null,timer:null,polling:false,observerDirty:false,range:{low:100,high:6000,seconds:7200}};
const satEl = (tag,text) => {const node=document.createElement(tag);if(text!==undefined)node.textContent=text;return node;};
const satTime = seconds => finite(seconds) ? new Date(seconds*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : '—';

function satelliteAt(curve, seconds) {
  const times=curve.times_s;
  if(!times?.length || seconds<times[0] || seconds>times.at(-1))return null;
  let i=0;while(i+1<times.length-1 && times[i+1]<seconds)i++;
  const part=(seconds-times[i])/(times[i+1]-times[i]);
  const mix=values=>values[i]+part*(values[i+1]-values[i]);
  const azDelta=((curve.az_deg[i+1]-curve.az_deg[i]+540)%360)-180;
  return {rr:mix(curve.range_rate_m_s),el:mix(curve.el_deg),az:(curve.az_deg[i]+part*azDelta+360)%360};
}

function satelliteMarkers(live) {
  const data=satelliteUI.data;
  if(!$('satellite-overlay').checked || !data || data.frame_session!==model.selected || !live)return [];
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
  button.disabled=true;
  try {
    const payload=await request('/api/satellites/prepare',{norad,transmitter_id:id,seconds:300});
    await openWorkbench();
    $('expert-operation').value='acquisition';renderExpert();
    $('expert-runtime').value=payload.runtime;$('expert-seconds').value=String(payload.seconds);
    $('expert-antispur').checked=payload.anti_spur;
    for(const input of $('expert-fields').querySelectorAll('[data-dest]')) {
      if(Object.hasOwn(payload.values,input.dataset.dest))input.value=String(payload.values[input.dataset.dest]);
    }
    workbench.satellite=payload.satellite;invalidateExpert();
    $('expert-description').textContent=`SatNOGS target NORAD ${norad}. Review receiver, gain and frequency geometry. Reference will be frozen before RX starts.`;
  } catch(error){notify(error.message);}
  finally{button.disabled=false;}
}

function satelliteButton(tx,norad) {
  const button=satEl('button','Prepare 5 min');
  button.addEventListener('click',()=>prepareSatellite(norad,tx.id,button));return button;
}

function renderSatelliteCatalog() {
  const target=$('satellite-catalog');target.replaceChildren();
  const search=$('satellite-search').value.toLowerCase();
  const rows=(satelliteUI.data?.catalog||[]).filter(tx=>`${tx.name} ${tx.norad} ${tx.f_hz/1e6} ${tx.mode}`.toLowerCase().includes(search));
  for(const tx of rows.slice(0,120)) {
    const card=satEl('div');card.className='satellite-item';
    card.append(satEl('strong',`${tx.name} · ${tx.norad}`),satEl('span',`${format(tx.f_hz/1e6,6)}–${format(tx.f_high_hz/1e6,6)} MHz · ${tx.mode||'mode unknown'}`),
      satEl('small',`${tx.description}${tx.unconfirmed?' · database frequency unconfirmed':''}`),satelliteButton(tx,tx.norad));target.append(card);
  }
  if(rows.length>120)target.append(satEl('p','Showing 120 transmitters. Narrow the search to inspect the remainder.'));
}

function renderSatellites() {
  const data=satelliteUI.data;if(!data)return;
  $('satellite-source').textContent=`SatNOGS DB · CC BY-SA · fetched ${stamp(data.fetched_utc)} · ${data.active_satellites??'—'} active satellites in range`;
  $('satellite-state').textContent=data.status==='loading'?'Calculating local orbital predictions…':data.error||`${data.eligible_tles??0} eligible TLEs · ${data.stale_or_invalid_tles??0} stale/invalid · sampled pass timing ≈30 s${data.updating?' · updating predictions':''}`;
  if(data.refresh?.state) $('satellite-state').textContent+=` · data refresh: ${data.refresh.state}${data.refresh.error?' · '+data.refresh.error:''}`;
  if(data.prediction_truncated||data.catalog_truncated||data.passes_truncated||data.curves_truncated)$('satellite-state').textContent+=' · result budget reached; narrow RF range';
  const list=$('satellite-passes');list.replaceChildren();
  for(const pass of data.passes||[]) {
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
  if(data.observer&&!satelliteUI.observerDirty)for(const [key,value] of Object.entries(data.observer))$('observer-form').elements.namedItem(key).value=String(value);
  renderSatelliteCatalog();renderSatelliteFrame();scheduleDraw();
  if(typeof syncSkyView==='function')syncSkyView(data);
}

async function pollSatellites() {
  clearTimeout(satelliteUI.timer);
  if(satelliteUI.polling)return;
  if(document.hidden){satelliteUI.timer=setTimeout(pollSatellites,10000);return;}
  satelliteUI.polling=true;
  const revision=satelliteUI.revision,selected=model.selected;
  try {
    const range=satelliteUI.range;
    const query=new URLSearchParams({lo_mhz:range.low,hi_mhz:range.high,seconds:range.seconds});
    if(model.selected)query.set('session',model.selected);
    const result=await request('/api/satellites?'+query);
    if(revision===satelliteUI.revision&&selected===model.selected){satelliteUI.data=result;renderSatellites();}
  } catch(error){$('satellite-state').textContent=error.message;}
  finally {satelliteUI.polling=false;satelliteUI.timer=setTimeout(pollSatellites,revision!==satelliteUI.revision||selected!==model.selected?100:satelliteUI.data?.status==='loading'?1500:10000);}
}

$('satellite-nav').addEventListener('click',()=>{$('satellite-panel').scrollIntoView({behavior:'smooth'});pollSatellites();});
$('satellite-apply').addEventListener('click',()=>{
  satelliteUI.revision++;
  $('satellite-passes').replaceChildren();$('satellite-catalog').replaceChildren();$('satellite-state').textContent='Updating selected RF range…';
  satelliteUI.range={low:Number($('satellite-low').value),high:Number($('satellite-high').value),seconds:Number($('satellite-horizon').value)};
  satelliteUI.data=null;scheduleDraw();pollSatellites();
});
$('satellite-search').addEventListener('input',renderSatelliteCatalog);
$('satellite-overlay').addEventListener('change',()=>{renderSatelliteFrame();scheduleDraw();});
$('satellite-refresh').addEventListener('click',async event=>{
  event.target.disabled=true;
  try{const state=await request('/api/satellites/refresh',{});$('satellite-state').textContent=`SatNOGS update ${state.state}; RX continues independently.`;}
  catch(error){notify(error.message);}finally{event.target.disabled=false;pollSatellites();}
});
$('observer-form').addEventListener('input',()=>{satelliteUI.observerDirty=true;});
$('observer-form').addEventListener('submit',async event=>{
  event.preventDefault();const data={};for(const [key,value] of new FormData(event.target))data[key]=Number(value);
  try{await request('/api/satellites/observer',data);satelliteUI.observerDirty=false;satelliteUI.revision++;satelliteUI.data=null;notify('Local observer saved. Existing frozen references remain unchanged.');pollSatellites();}
  catch(error){notify(error.message);}
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
  satelliteUI.revision++;satelliteUI.data=null;
  clearTimeout(satelliteUI.timer);satelliteUI.timer=setTimeout(pollSatellites,100);
}
