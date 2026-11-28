"""SatNOGS polling and recovery with deferred requests and a minimal DOM."""
from test_gui_realtime import run_js


def test_satellite_poll_cancellation_recovery_and_pass_pagination(tmp_path):
    run_js(tmp_path, r'''
const vm=require('vm'),fs=require('fs'),assert=require('assert');
const fields=new Map(),events={};
function node(){return{dataset:{},value:'',checked:true,textContent:'',children:[],events:{},
 addEventListener(name,fn){const old=this.events[name];this.events[name]=old?(...args)=>{old(...args);fn(...args);}:fn;},append(...items){this.children.push(...items);},
 replaceChildren(...items){this.children=items;},scrollIntoView(){}};}
function field(id){if(!fields.has(id))fields.set(id,node());return fields.get(id);}
const pending=[],timers=[];
const context={finite:Number.isFinite,format:String,stamp:String,$:field,URLSearchParams,AbortController,
 FormData:class{*[Symbol.iterator](){yield ['lat_deg','1'];yield ['lon_deg','2'];yield ['alt_m','0'];yield ['min_elevation_deg','0'];}},performance:{now:()=>1000},setInterval(){},setTimeout(fn,ms){timers.push(ms);},clearTimeout(){},
 document:{hidden:false,addEventListener(name,fn){events[name]=fn;},createElement:node},
 model:{selected:null},scheduleDraw(){},notify(){},
 request:(...args)=>new Promise((resolve,reject)=>pending.push({resolve,reject,args}))};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
(async()=>{
 const old=vm.runInContext('pollSatellites()',context);await vm.runInContext('pollSatellites()',context);assert.equal(pending.length,1);
 field('satellite-low').value='140';field('satellite-high').value='150';field('satellite-horizon').value='1800';
 field('satellite-apply').events.click();assert(pending[0].args[3].aborted);
 const oldMessage=field('satellite-state').textContent;
 pending.shift().reject(Error('stale error'));await old;assert.equal(field('satellite-state').textContent,oldMessage);
 const next=vm.runInContext('pollSatellites()',context);assert.match(pending[0].args[0],/lo_mhz=140/);
 pending.shift().resolve({status:'ready',catalog:[],passes:[]});await next;
 assert.match(field('satellite-health').textContent,/Connected/);
 const fail=vm.runInContext('pollSatellites()',context);pending.shift().reject(Error('offline'));await fail;
 assert.equal(field('satellite-health').dataset.state,'offline');assert(timers.at(-1)>=1500);
 vm.runInContext(`satelliteUI.data={status:'ready',catalog:[],passes:Array.from({length:60},(_,i)=>({
 norad:i+1,name:'Test '+i,rise_utc:1000+i,set_utc:1200+i,max_el_deg:30,transmitters:[{id:'x',f_hz:145e6}],sky_track:null}))};renderSatellites();`,context);
 assert.equal(field('satellite-passes').children.length,24);assert.match(field('satellite-page').textContent,/page 1 \/ 3/);
 field('satellite-next').events.click();assert.match(field('satellite-page').textContent,/page 2 \/ 3/);
 field('satellite-pass-search').value='Test 59';field('satellite-pass-search').events.input();
 assert.equal(field('satellite-passes').children.length,1);assert.equal(field('satellite-next').disabled,true);
 const submit=field('observer-form').events.submit;
 const button={disabled:false};const form={querySelector:()=>button};
 const saving=submit({preventDefault(){},target:form});assert.equal(button.disabled,true);
 field('observer-form').events.input();pending.shift().resolve({state:'saved'});await saving;
 assert.equal(vm.runInContext('satelliteUI.observerDirty',context),true);assert.equal(button.disabled,false);
})().catch(e=>{console.error(e);process.exitCode=1;});
''', 'satellites.js')
