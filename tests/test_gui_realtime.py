"""Browser logic tested with deterministic clocks and deferred HTTP; no RF."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_js(tmp_path, script, *assets):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node needed for browser state tests')
    path = tmp_path / 'check.cjs'
    path.write_text(script)
    subprocess.run([node, str(path), *[str(ROOT / 'soapypower/web' / asset) for asset in assets]],
                   check=True, timeout=10)


def test_global_clock_server_sync_staleness_and_monotonic_time(tmp_path):
    run_js(tmp_path, r'''
const vm=require('vm'),fs=require('fs'),assert=require('assert');
const nodes=new Map();function field(id){if(!nodes.has(id))nodes.set(id,{});return nodes.get(id);}
let monotonic=1000;
const context={document:{hidden:false,getElementById:field,addEventListener(){}},performance:{now:()=>monotonic},
 setTimeout(){},clearTimeout(){},requestAnimationFrame(){return 1;},AbortController};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
context.fetch=async()=>{monotonic=1200;return{ok:true,json:async()=>({unix_s:10000})};};
(async()=>{
 await vm.runInContext('syncGlobalClock()',context);
 assert.equal(vm.runInContext('globalClock.uncertainty',context),.1);
 assert.equal(vm.runInContext('globalClock.liveNow()',context),10000.1);
 monotonic=6200;assert.equal(vm.runInContext('globalClock.liveNow()',context),10005.1);
 assert.match(field('global-clock-status').textContent,/server sync/);
 monotonic=93200;vm.runInContext('renderGlobalClock()',context);
 assert.match(field('global-clock-status').textContent,/unsynchronized/);
 context.fetch=async()=>{throw Error('offline');};await vm.runInContext('syncGlobalClock()',context);
 assert.equal(vm.runInContext('globalClock.liveNow()',context),10092.1);
})().catch(e=>{console.error(e);process.exitCode=1;});
''', 'clock.js')


def test_workbench_serial_poll_stale_selection_and_failure_controls(tmp_path):
    run_js(tmp_path, r'''
const vm=require('vm'),fs=require('fs'),assert=require('assert');
const nodes=new Map();function field(id){if(!nodes.has(id))nodes.set(id,{open:true,disabled:true,value:'',textContent:'',events:{},
 addEventListener(name,fn){this.events[name]=fn;},replaceChildren(){},append(){}});return nodes.get(id);}
const pending=[];
const context={document:{hidden:false,getElementById:field,addEventListener(){}},setTimeout(){},clearTimeout(){},
 request:()=>new Promise((resolve,reject)=>pending.push({resolve,reject})),format:String};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
(async()=>{
 vm.runInContext("workbench.job='old'",context);
 const old=vm.runInContext('pollExpert()',context);await vm.runInContext('pollExpert()',context);assert.equal(pending.length,1);
 vm.runInContext("workbench.job='new'",context);pending.shift().resolve({id:'old',state:'running',command:'acquisition'});await old;
 assert.equal(field('expert-job-state').textContent,'');
 const next=vm.runInContext('pollExpert()',context);pending.shift().resolve({id:'new',state:'running',command:'acquisition'});await next;
 assert.equal(field('expert-pause').disabled,false);
 const failed=vm.runInContext('pollExpert()',context);pending.shift().reject(Error('disconnected'));await failed;
 for(const action of ['pause','resume','retry','abort'])assert.equal(field('expert-'+action).disabled,true);
 assert.equal(vm.runInContext('workbench.lastJob',context),null);
})().catch(e=>{console.error(e);process.exitCode=1;});
''', 'workbench.js')
