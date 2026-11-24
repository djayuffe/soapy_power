"""Regression checks for measured support and defensible satellite/spur claims."""
import copy,datetime,json,sys,tempfile,unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import satdb as sd
try:
    import analyze_superscan as an        # needs matplotlib + scipy
    HAVE_ANALYSIS = sys.platform != 'win32'   # reports are published through POSIX locks + symlinks
except ImportError:
    an = None; HAVE_ANALYSIS = False
from waterfall_geometry import dwell_mesh
from soapypower import floor_estimation as fe,spur_inference as si,spur_pipeline as sp

@unittest.skipUnless(HAVE_ANALYSIS, 'needs matplotlib and scipy')
class AuditTests(unittest.TestCase):
 def test_dwell_geometry_masks_the_revisit_gap_and_keeps_actual_duration(self):
  edges,data=dwell_mesh([[1,2],[3,4]],[0,10],[.5,10.5])
  np.testing.assert_array_equal(edges,[0,.5,10,10.5]);self.assertTrue(np.isnan(data[1]).all());self.assertAlmostEqual(np.diff(edges)[np.isfinite(data[:,0])].sum(),1)
 def test_dwell_geometry_missing_and_overlapping_intervals(self):
  edges,data=dwell_mesh([[1],[2]],[np.nan,5],[np.nan,6]);np.testing.assert_array_equal(edges,[5,6])
  with self.assertRaises(ValueError):dwell_mesh([[1],[2]],[0,.4],[.5,1])
 def test_superscan_keeps_uncovered_frequency_columns_masked(self):
  R=np.zeros((2,1,16));S=an.stitch(R,np.array([[0],[10.]]),np.array([[1],[11.]]),np.array([1000.]),100,dict(lo_hz=700,hi_hz=1300),150,500)
  np.testing.assert_array_equal(np.diff(S['f']),np.full(6,100));self.assertTrue(np.isnan(S['B'][:,2:5]).all());self.assertTrue(np.all(S['hop'][2:5]==-1))
  self.assertTrue(np.isnan(an.smooth_f(S['B']))[:,2:5].all())
 def test_loader_rejects_incomplete_unverified_changed_gain_and_wrong_grid(self):
  t=datetime.datetime(2026,11,22,tzinfo=datetime.timezone.utc)
  r=dict(power=[-100]*16,first_bin_center_hz=200,step_hz=100,unit='dB(input power/Hz)',time_start=t.isoformat(),time_stop=(t+datetime.timedelta(seconds=1)).isoformat(),metadata=dict(tuned_center_hz=1000,measurement_status='complete',continuity='verified',sample_rate_hz=1600,context={'gains':{'PGA':20}}))
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'x.jsonl'
   for change in ['partial','continuity','gain','grid','timezone']:
    q=copy.deepcopy(r)
    if change=='partial':q['metadata']['measurement_status']='partial'
    if change=='continuity':q['metadata']['continuity']='unknown'
    if change=='gain':q['metadata']['context']['gains']['PGA']=30
    if change=='grid':q['first_bin_center_hz']=201
    if change=='timezone':q['time_start']='2026-11-22T00:00:00'
    p.write_text(json.dumps(r)+'\n'+json.dumps(q)+'\n')
    with self.subTest(change=change),self.assertRaises(ValueError):an.load(p)
 def test_loader_excludes_estimated_and_masked_bins(self):
  from test_analyze_superscan import synth
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'r.jsonl';synth(p,1,[]);rows=[json.loads(x) for x in p.read_text().splitlines()];rows[0]['estimated_bins']=[10];rows[0]['masked_bins']=[11];p.write_text('\n'.join(json.dumps(x) for x in rows))
   P=an.load(p)[0];self.assertTrue(np.isnan(P[0,0,10:12]).all())
 def test_repeated_captures_at_two_settings_do_not_identify_mixing_order(self):
  lo=np.array([100,100,110,110])*1e6;f=200e6-lo
  self.assertEqual(fe.classify_lo_slope(lo,f,resolution_hz=100)['status'],'unidentifiable')
  self.assertEqual(si.bayes_classify_track(lo,f,resolution_hz=100)['status'],'unidentifiable')
  self.assertEqual(fe.distinct_lo_count([100e6,100e6+.001,110e6],100),2)
 def test_pipeline_does_not_pool_training_and_validation_or_different_grids(self):
  a=sp.make_capture(100e6,np.ones(128),rate_hz=1e6,dof=20);b=sp.make_capture(101e6,np.ones(128),rate_hz=1e6,dof=20,partition='validation')
  with self.assertRaisesRegex(ValueError,'validation'):sp.analyze_sweeps([a,b])
  b['partition']='train';b['rate_hz']=2e6
  with self.assertRaisesRegex(ValueError,'matching'):sp.analyze_sweeps([a,b])
 def test_satellite_track_matching_excludes_inactive_and_dead(self):
  t=np.arange(0,61,10.);rr=np.linspace(-2000,2000,len(t));tx=dict(f_hz=137e6,alive=True,status='active')
  p=dict(norad=1,name='synthetic',sat_status='alive',t=t,rr=rr,el=np.full(len(t),30.),max_el=30,transmitters=[tx]);f=sd.doppler(p,137e6,t)
  self.assertTrue(sd.match_track(t,f,[p]));tx['alive']=False;self.assertFalse(sd.match_track(t,f,[p]));tx['alive']=True;tx['status']='inactive';self.assertFalse(sd.match_track(t,f,[p]));tx['status']='active';p['sat_status']='dead';self.assertFalse(sd.match_track(t,f,[p]))
 def test_png_mesh_contains_masked_rows_for_off_air_time(self):
  import matplotlib.pyplot as plt
  S=dict(f=np.array([1e6,1e6+100]),hop=np.array([0,0]),hops=[0],T0=np.array([[0.],[10.]]),T1=np.array([[.5],[10.5]]),step_hz=100)
  fig,ax=plt.subplots();mesh=an.waterfall(ax,np.ones((2,2)),S,0,-1,1);self.assertEqual(np.ma.count_masked(mesh.get_array()),2);plt.close(fig)
 def test_origin_labels_are_hypotheses_and_never_exclusions(self):
  R=np.zeros((3,2,32));c=np.array([1000.,1300.]);R[:,0,22]=10
  v=an.line_origin([dict(f_mhz=.0016,excess_db=10)],R,c,100,100,1000)[0]
  self.assertEqual(v['origin_confidence'],'diagnostic_only');self.assertFalse(v['automatically_excluded'])

class GainEvidenceTests(unittest.TestCase):
 def test_gain_selection_refuses_unknown_or_partial_headroom_evidence(self):
  import gaincal as gc
  from test_gaincal import write_cal
  for fault in ('missing_percentile','partial','unverified','duplicate'):
   with self.subTest(fault=fault),tempfile.TemporaryDirectory() as tmp:
    d=Path(tmp);write_cal(d,[20],strong=.0005);p=d/'gain_20.jsonl';rows=[json.loads(x) for x in p.read_text().splitlines()]
    if fault=='missing_percentile':rows[0]['metadata']['headroom'].pop('p999_component')
    if fault=='partial':rows[0]['metadata']['measurement_status']='partial'
    if fault=='unverified':rows[0]['metadata']['continuity']='unknown'
    if fault=='duplicate':rows[1]=rows[0]
    p.write_text('\n'.join(json.dumps(x) for x in rows)+'\n')
    r=gc.choose(d,fallback=10);self.assertEqual(r['chosen_gain_db'],10);self.assertFalse(r['rows'][0]['safe'])


@unittest.skipUnless(HAVE_ANALYSIS, 'needs matplotlib and scipy')
class ReportingContracts(unittest.TestCase):
 def test_runner_retains_guards_after_gain_selection(self):
  s=(Path(__file__).resolve().parents[1]/'tools/macos_runner/run_session.sh').read_text();body=s[s.index('  ss_args()'):s.index('  ss_plan ||')]
  for arg in ['--rf-state','--require-timestamps','--lock-sensor lo_locked','--reopen-stream','--telemetry-policy']:self.assertIn(arg,body)
  main=s[s.index('  T0=$(date +%s); ss_args'):s.index('  # Each gain context')]
  for arg in ['superscan_run.py','--headroom','--headroom-max 0.5','--headroom-p999 0.2','--gain-step 6']:self.assertIn(arg,main)
 def test_3d_axis_contains_long_observations_and_filters_unknown_times(self):
  import report_waterfalls3d as w3
  self.assertGreaterEqual(w3.time_extent({'x':(None,np.array([0,900,1790]))}),1790)
 def test_3d_surface_does_not_turn_missing_cells_into_noise_floor(self):
  import report_waterfalls3d as w3
  from unittest.mock import patch
  import matplotlib.pyplot as plt
  from mpl_toolkits.mplot3d.axes3d import Axes3D
  seen=[];original=Axes3D.plot_surface
  def spy(self,x,y,z,*args,**kwargs):seen.append(z);return original(self,x,y,z,*args,**kwargs)
  f=np.linspace(70,300,40);g=np.full((4,40),-130.);g[:,10:12]=np.nan
  with patch.object(Axes3D,'plot_surface',spy):fig,_=w3.build(f,g,np.arange(4)*10,70,300,'Synthetic missing-data contract','test');plt.close(fig)
  self.assertTrue(np.ma.getmaskarray(seen[0])[:,10:12].all())

class ContinuousPlanTests(unittest.TestCase):
 def test_repaired_dither_guarantees_two_views_even_inside_shifted_dc_holes(self):
  import satplan
  for rate in (2,4,8):
   p=satplan.plan(satplan.PRESETS['sat-full'],usable=.425*rate*1e6)
   p['centers_hz']=sorted(set(p['centers_hz']+[c+300000 for c in p['centers_hz']]))
   p=satplan.ensure_lo_diversity(p)
   self.assertEqual(p['minimum_lo_views'],2)
   for b in p['bands']:
    for f in np.arange(b['lo_hz'],b['hi_hz']+1,1000):
     self.assertGreaterEqual(sum(p['dc_exclude_hz']<=abs(f-c)<=p['usable_halfwidth_hz'] for c in p['centers_hz']),2)

class PredictionWindowTests(unittest.TestCase):
 def test_pass_visible_only_in_propagation_padding_is_not_reported_in_scan(self):
  from unittest.mock import patch
  t0,t1=1000.,1100.;s=dict(name='synthetic',sat_status='alive',l1='',l2='',transmitters=[dict(f_hz=137e6)])
  def topo(a,b,t,q):
   el=np.where(t<t0,45.,-10.);return el,np.zeros_like(t),np.ones_like(t),np.zeros_like(t)
  with patch.object(sd,'topo',topo):self.assertEqual(sd.predict({1:s},{},t0,t1,[dict(lo_hz=136e6,hi_hz=138e6)]),[])

class PlanSerializationTests(unittest.TestCase):
 def test_cli_centers_match_plan_at_s_band_without_kilohertz_rounding(self):
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   a=Path(tmp)/'custom.json';b=Path(tmp)/'plan.json';a.write_text(json.dumps({'bands':[dict(name='custom',lo_hz=2200012000,hi_hz=2200112000)]}))
   r=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'tools/satplan.py'),str(a),str(b),'--dither','300'],capture_output=True,text=True,check=True)
   actual=[float(x[:-1])*1e6 for x in r.stdout.strip().split(',')]
   np.testing.assert_allclose(actual,json.loads(b.read_text())['centers_hz'],rtol=0,atol=.001)

if __name__=='__main__':unittest.main()
