'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {analyze}=require('../examples/ettus/heatmap_analysis.js');
function run(grid,options={}){return analyze({width:grid[0].length,height:grid.length,minimum:1,threshold:-100,minimumActivity:10,value:(r,c)=>grid[r][c],...options});}
test('missing observations do not count as silence; threshold is strict',()=>{
 const a=run([[-90,NaN,-100], [NaN,NaN,-101]]);
 assert.equal(a.observed,3);assert.equal(a.active,1);assert.equal(a.fraction,1/3);
 assert.equal(a.columns[0].fraction,1);assert.ok(Number.isNaN(a.columns[1].fraction));assert.equal(a.columns[2].fraction,0);
 assert.equal(a.regions.length,1);assert.deepEqual([a.regions[0].c0,a.regions[0].c1],[0,1]);
});
test('minimum observations prevents single-observation candidate regions',()=>{
 const a=run([[-80,-90], [NaN,-110]],{minimum:2});
 assert.equal(a.columns[0].eligible,false);assert.equal(a.regions.length,1);assert.equal(a.regions[0].c0,1);assert.equal(a.regions[0].fraction,.5);
});
test('adjacent qualifying cells group and peak sort is deterministic',()=>{
 const a=run([[-90,-80,-120,-70,-130],[-110,-95,-120,-90,-140]]);
 assert.equal(a.regions.length,2);assert.equal(a.regions[0].c0,3);assert.deepEqual([a.regions[1].c0,a.regions[1].c1],[0,2]);
 assert.equal(a.regions[1].observed,4);assert.equal(a.regions[1].active,3);
});
test('viewport excludes observations outside its time and frequency bounds',()=>{
 const a=run([[0,0,0],[-80,-120,-90],[-110,-80,-120]],{r0:1,r1:3,c0:1,c1:3});
 assert.equal(a.total,4);assert.equal(a.observed,4);assert.equal(a.active,2);assert.equal(a.peak,-80);assert.equal(a.columns.length,2);
});
test('all missing is undefined activity and no candidates, even at zero filter',()=>{
 const a=run([[NaN,NaN]],{minimumActivity:0});
 assert.equal(a.regions.length,0);assert.ok(Number.isNaN(a.peak));assert.ok(Number.isNaN(a.fraction));
 assert.equal(run([[-120]],{minimumActivity:0}).regions.length,0);
});
test('rejects invalid filters and bounds',()=>{
 for(const settings of [{minimum:0},{minimum:1.5},{threshold:NaN},{minimumActivity:101},{r0:-1},{c1:4},{r0:1,r1:1}])assert.throws(()=>run([[-90]],settings),/Invalid/);
});
