"""Regression evidence for promoted live acquisition and comparison algorithms."""
import copy
import hashlib
import json
import tempfile
import unittest
import types
from pathlib import Path
from unittest import mock
import numpy as np
import test_audit as audit
import test_architecture as architecture
from soapypower.acquisition_quality import QualityPolicy, RejectionEvidence, HeadroomRejected
from soapypower.scan_profile import ScanProfile, import_campaign_profile
from soapypower.coverage import masked_frequency_plan, coverage_gaps, support_intervals
from soapypower.spectral_comparison import rebin_density, fuse_density, fuse_observations
from soapypower.relative_noise import RelativeNoiseReference
from soapypower.common_iq import common_band_iq, equal_duration_phase
from soapypower.analysis_cli import main as analyze
from soapypower.spur_atlas import analyze_track


def profile():
    return dict(version=1,type='receiver_scan_profile',reference='synthetic regression fixture',
        identity=dict(driver='test',hardware='SimulatedReceiver',serial='TEST-001'),
        sample_rate_hz=1024,bandwidth_hz=1024,antenna='RX',
        bands=[dict(start_hz=0,stop_hz=20000,gains={'overall':20.7})],
        gain_strategy='ladder',gain_ladder=[{'overall':20.7},{'overall':10.7}])


def correction_context():
    c=architecture.context()
    c.update(dc_correction=False,iq_correction=False)
    return {k:v for k,v in c.items() if k not in ('driver','hardware','serial','hardware_info','gains')}


class RuntimeTests(unittest.TestCase):
    setUp = audit.AcquisitionTests.setUp
    capable_receiver = architecture.HardwareIntegrationTests.capable_receiver

    def policy(self, **changes):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        result = dict(evidence_dir=str(Path(directory.name)/'evidence'))
        result.update(changes)
        return result

    def signal(self, sdr, amplitude):
        original = sdr.device.read_stream
        def read():
            packet = original()
            value = amplitude() if callable(amplitude) else amplitude
            sdr.device.buffer[:] = value
            return packet
        sdr.device.read_stream = read

    def scan(self,sdr,**kwargs):
        sdr.sweep(10000,10000,64,4,runs=1,max_threads=1,**kwargs)

    def test_safe_raw_check_records_metadata(self):
        sdr,out = self.capable_receiver()
        self.signal(sdr,.1+.2j)
        policy = self.policy()
        self.scan(sdr,quality_policy=policy)
        m = json.loads(out.getvalue())['metadata']
        self.assertTrue(m['headroom']['accepted'])
        self.assertEqual(m['headroom']['evaluated_samples'],256)
        self.assertAlmostEqual(m['headroom']['max_component'],.2,places=6)
        self.assertEqual(json.loads((Path(policy['evidence_dir'])/'index.json').read_text())['records'],[])

    def test_dc_cannot_hide_unsafe_hop_and_raw_is_retained(self):
        sdr,out = self.capable_receiver()
        self.signal(sdr,.9+.1j)
        policy = self.policy()
        with self.assertRaises(HeadroomRejected):self.scan(sdr,quality_policy=policy,detrend='constant')
        self.assertEqual(out.getvalue(),b'')
        root = Path(policy['evidence_dir'])
        z = np.load(root/'rejected-000000.npz')['iq']
        np.testing.assert_array_equal(z,np.full(256,.9+.1j,dtype=np.complex64))
        meta = json.loads((root/'rejected-000000.json').read_text())
        self.assertEqual(meta['iq_sha256'],hashlib.sha256((root/meta['iq_file']).read_bytes()).hexdigest())
        self.assertFalse(sdr.device.is_streaming)

    def test_single_rare_peak_is_not_hidden_by_percentile(self):
        sdr,out = self.capable_receiver()
        original = sdr.device.read_stream
        def read():
            p = original();sdr.device.buffer[:]=.1;sdr.device.buffer[0]=.99;return p
        sdr.device.read_stream = read
        with self.assertRaises(HeadroomRejected):self.scan(sdr,quality_policy=self.policy())
        self.assertEqual(out.getvalue(),b'')

    def test_backoff_is_bounded_and_successful_gain_is_cached(self):
        sdr,out = self.capable_receiver()
        self.signal(sdr,lambda:.9 if sdr.device.gain>15 else .1)
        policy = self.policy(max_retries=2)
        self.scan(sdr,quality_policy=policy,receiver_profile=profile())
        meta = json.loads(out.getvalue())['metadata']
        self.assertEqual(meta['headroom']['attempt'],1)
        self.assertEqual(meta['context']['gains'],{'overall':10.7})
        self.assertEqual(sdr._gain_cache[10000],{'overall':10.7})
        self.assertEqual(len(json.loads((Path(policy['evidence_dir'])/'index.json').read_text())['records']),1)

    def test_reject_only_policy_never_invents_gain_changes(self):
        sdr,out = self.capable_receiver();self.signal(sdr,.9)
        policy = self.policy(max_retries=3)
        with self.assertRaises(HeadroomRejected):self.scan(sdr,quality_policy=policy)
        self.assertEqual(sdr.device.gain,20.7)
        self.assertEqual(len(json.loads((Path(policy['evidence_dir'])/'index.json').read_text())['records']),1)

    def test_transport_failure_keeps_valid_prefix(self):
        sdr,out = self.capable_receiver();self.signal(sdr,.1)
        original = sdr.device.read_stream
        def read():
            if sdr.device.reads>=1:raise RuntimeError('transport failed')
            return original()
        sdr.device.read_stream = read
        policy = self.policy()
        with self.assertRaisesRegex(RuntimeError,'transport failed'):self.scan(sdr,quality_policy=policy)
        z = np.load(Path(policy['evidence_dir'])/'rejected-000000.npz')['iq']
        self.assertEqual(len(z),64)
        np.testing.assert_array_equal(z,np.full(64,.1,dtype=np.complex64))
        self.assertEqual(out.getvalue(),b'')

    def test_failure_inside_a_multi_packet_chunk_retains_its_prefix(self):
        sdr,out=self.capable_receiver();self.signal(sdr,.1)
        original=sdr.device.read_stream
        def read():
            if sdr.device.reads>=2:raise RuntimeError('mid-chunk transport failure')
            return original()
        sdr.device.read_stream=read;policy=self.policy()
        with self.assertRaisesRegex(RuntimeError,'mid-chunk'):
            self.scan(sdr,quality_policy=policy,max_buffer_size=256)
        z=np.load(Path(policy['evidence_dir'])/'rejected-000000.npz')['iq']
        np.testing.assert_array_equal(z,np.full(128,.1,dtype=np.complex64))
        self.assertEqual(out.getvalue(),b'')

    def test_gain_readback_corruption_rejects_before_capture(self):
        sdr,out = self.capable_receiver()
        sdr.device.device.getGain=lambda *a:19.
        policy = self.policy(max_retries=2)
        with self.assertRaisesRegex(ValueError,'gain readback'):self.scan(sdr,quality_policy=policy,receiver_profile=profile())
        self.assertEqual(len(np.load(Path(policy['evidence_dir'])/'rejected-000000.npz')['iq']),0)
        self.assertEqual(out.getvalue(),b'')

    def test_wrong_identity_fails_before_profile_controls(self):
        sdr,out = self.capable_receiver();p=profile();p['identity']['serial']='OTHER'
        with self.assertRaisesRegex(ValueError,'identity'):self.scan(sdr,quality_policy=self.policy(),receiver_profile=p)
        self.assertEqual(sdr.device.gain,20.7)

    def test_evidence_failure_halts_without_retry(self):
        sdr,out = self.capable_receiver();self.signal(sdr,.9)
        with mock.patch.object(RejectionEvidence,'save',side_effect=OSError('disk failed')):
            with self.assertRaisesRegex(OSError,'disk failed'):
                self.scan(sdr,quality_policy=self.policy(max_retries=2),receiver_profile=profile())
        self.assertEqual(sdr.device.gain,20.7)
        self.assertEqual(out.getvalue(),b'')

    def test_workspace_failure_stops_stream_without_creating_evidence(self):
        sdr,out = self.capable_receiver();policy=self.policy(workspace_bytes=100)
        with self.assertRaisesRegex(ValueError,'workspace'):self.scan(sdr,quality_policy=policy)
        self.assertFalse(Path(policy['evidence_dir']).exists())
        self.assertFalse(sdr.device.is_streaming)

    def test_nonfinite_iq_is_retained_before_error(self):
        sdr,out = self.capable_receiver();self.signal(sdr,complex(np.nan,0));policy=self.policy()
        with self.assertRaises(ValueError):self.scan(sdr,quality_policy=policy)
        self.assertTrue(np.isnan(np.load(Path(policy['evidence_dir'])/'rejected-000000.npz')['iq']).all())
        self.assertEqual(out.getvalue(),b'')

    def test_main_mask_aware_plan_is_used_and_audited(self):
        sdr,out = self.capable_receiver();self.signal(sdr,.1)
        sdr.sweep(10000,12000,64,4,runs=1,max_threads=1,ensure_coverage=True,dc_mask_width=32)
        rows = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        self.assertGreater(len(rows),2)
        self.assertTrue(all(r['metadata']['coverage_plan']['mask_aware'] for r in rows))

    def manual_profile_receiver(self):
        sdr,out=self.capable_receiver(dc_correction='off',iq_correction='off')
        raw=sdr.device.device
        raw.offset=0j
        raw.hasDCOffset=lambda *a:True
        raw.getDCOffset=lambda *a:raw.offset
        raw.setDCOffset=lambda *a:setattr(raw,'offset',a[-1])
        p=profile()
        p['control_context']=correction_context()
        p['dc_anchors']=[dict(center_hz=10000,gains={'overall':20.7},normalized_dc=[.125,.25],validated=True)]
        return sdr,out,p

    def test_gain_backoff_resets_manual_dc_and_profile_can_be_reused(self):
        sdr,out,p=self.manual_profile_receiver()
        self.signal(sdr,lambda:.9 if sdr.device.gain>15 else .1)
        self.scan(sdr,quality_policy=self.policy(max_retries=2),receiver_profile=p)
        self.assertEqual(sdr.device.device.offset,0j)
        m=json.loads(out.getvalue())['metadata']
        self.assertIsNone(m['receiver_profile']['dc_anchor_hz'])
        out.seek(0);out.truncate()
        self.scan(sdr,quality_policy=self.policy(max_retries=2),receiver_profile=p)
        self.assertEqual(sdr.device.device.offset,0j)

    def test_near_gain_readback_cannot_reuse_an_exact_dc_anchor(self):
        sdr,out,p=self.manual_profile_receiver()
        sdr.device.device.getGain=lambda *a:sdr.device.gain+.005
        self.signal(sdr,.1)
        self.scan(sdr,quality_policy=self.policy(),receiver_profile=p)
        self.assertEqual(sdr.device.device.offset,0j)
        self.assertIsNone(json.loads(out.getvalue())['metadata']['receiver_profile']['dc_anchor_hz'])

    def test_existing_evidence_directory_never_gets_replaced(self):
        sdr,out=self.capable_receiver();policy=self.policy()
        Path(policy['evidence_dir']).mkdir()
        marker=Path(policy['evidence_dir'])/'original';marker.write_text('preserve')
        with self.assertRaises(FileExistsError):self.scan(sdr,quality_policy=policy)
        self.assertEqual(marker.read_text(),'preserve')
        self.assertFalse(sdr.device.is_streaming)

    def test_post_acquisition_lock_loss_rejects_and_retains_iq(self):
        sdr,out=self.capable_receiver();self.signal(sdr,.1)
        sdr.device.device.readSensor=lambda *a:'true' if sdr.device.reads==0 else 'false'
        policy=self.policy()
        with self.assertRaisesRegex(RuntimeError,'not locked after'):
            self.scan(sdr,quality_policy=policy,lock_sensor='lo_locked')
        self.assertEqual(len(np.load(Path(policy['evidence_dir'])/'rejected-000000.npz')['iq']),256)
        self.assertEqual(out.getvalue(),b'')

    def test_rf_component_change_is_separate_from_sample_completion(self):
        sdr,out=self.capable_receiver();self.signal(sdr,.1)
        raw=sdr.device.device;raw.listFrequencies=lambda *a:['RF','BB']
        raw.getFrequency=lambda *a:10000+(1 if sdr.device.reads else 0) if a[-1]=='RF' else 0
        policy=self.policy()
        with self.assertRaisesRegex(RuntimeError,'RF tuning component changed'):
            self.scan(sdr,quality_policy=policy)
        m=json.loads((Path(policy['evidence_dir'])/'rejected-000000.json').read_text())
        self.assertEqual(m['integrity']['measurement_status'],'complete')
        self.assertFalse(m['integrity']['rf_state']['after']['calibrated_rf_state'])

    def rtl_identity_receiver(self,candidates,index='0'):
        sdr,out=self.capable_receiver()
        sdr.device.device.getDriverKey=lambda:'rtlsdr'
        sdr.device.device.getHardwareInfo=lambda:{'index':index}
        self.power.simplesoapy.SoapySDR.Device=types.SimpleNamespace(enumerate=lambda *a:candidates)
        self.power.simplesoapy.SoapySDR.KwargsFromString=lambda *a:{}
        self.signal(sdr,.1)
        p=profile();p['identity']['driver']='rtlsdr'
        return sdr,out,p

    def test_rtl_profile_uses_observed_enumeration_not_requested_identity(self):
        sdr,out,p=self.rtl_identity_receiver([dict(driver='rtlsdr',serial='TEST-001',rtl='0')])
        self.scan(sdr,quality_policy=self.policy(),receiver_profile=p)
        context=json.loads(out.getvalue())['metadata']['context']
        self.assertEqual(context['serial'],'TEST-001')
        self.assertEqual(context['hardware_info']['soapypower_identity_enumeration']['rtl'],'0')

    def test_rtl_profile_refuses_ambiguous_or_wrong_index(self):
        for candidates in ([dict(driver='rtlsdr',serial='TEST-001',rtl='1')],
                           [dict(driver='rtlsdr',serial='TEST-001',rtl='0')]*2):
            sdr,out,p=self.rtl_identity_receiver(candidates)
            with self.subTest(candidates=candidates),self.assertRaises(ValueError):
                self.scan(sdr,quality_policy=self.policy(),receiver_profile=p)
            self.assertEqual(out.getvalue(),b'')

    def test_profile_refuses_digital_agc_readback(self):
        sdr,out=self.capable_receiver()
        sdr.device.device.getSettingInfo=lambda:[types.SimpleNamespace(key='digital_agc')]
        sdr.device.device.readSetting=lambda *a:'true'
        with self.assertRaisesRegex(ValueError,'digital_agc'):
            self.scan(sdr,quality_policy=self.policy(),receiver_profile=profile())


class ProfileTests(unittest.TestCase):
    def test_dc_anchor_requires_exact_stage_and_rf_domain(self):
        p=profile();p['control_context']=correction_context();p.update(dc_max_distance_hz=200e6,dc_domain_edges_hz=[237.5e6],dc_anchors=[dict(center_hz=200e6,gains={'overall':20.7},normalized_dc=[.1,.2],validated=True)])
        model=ScanProfile(p)
        self.assertEqual(model.correction(201e6,{'overall':20.7}),([.1,.2],200e6))
        self.assertEqual(model.correction(201e6,{'overall':10.7}),([0.,0.],None))
        self.assertEqual(model.correction(240e6,{'overall':20.7}),([0.,0.],None))

    def test_unvalidated_anchor_cannot_apply(self):
        p=profile();p['control_context']=correction_context();p['dc_anchors']=[dict(center_hz=10000,gains={'overall':20.7},normalized_dc=[.1,.2],validated=False)]
        self.assertEqual(ScanProfile(p).correction(10000,{'overall':20.7}),([0.,0.],None))

    def test_bad_schema_and_backoff_ladder_fail(self):
        for change in ({'version':True},{'sample_rate_hz':float('nan')},{'bands':None},
                       {'gain_ladder':[{'overall':10.7},{'overall':20.7}]},
                       {'gain_ladder':[{'overall':20.7},{'other':10.7}]},
                       {'antenna':None},{'bandwidth_hz':-1}):
            with self.subTest(change=change),self.assertRaises(ValueError):ScanProfile(dict(profile(),**change))

    def test_gain_points_are_not_silently_interpolated(self):
        p=profile();p.pop('bands');p['points']=[dict(center_hz=10000,gains={'overall':20.7})]
        model=ScanProfile(p)
        with self.assertRaisesRegex(ValueError,'no supported'):model.select_gain(10010)

    def test_import_rtl_points_retains_local_support(self):
        context=dict(driver='rtlsdr',hardware='R820T',serial='one',sample_rate=2560000,
                     bandwidth=2560000,antenna='RX',agc=False,device_settings={})
        source=dict(sample_rate_hz=2560000,points=[dict(center_hz=100e6,gain_db=19.7)])
        imported=import_campaign_profile(source,context,'fixture',[19.7,16.6])
        self.assertEqual(imported.select_gain(100e6)[0],{'TUNER':19.7})
        with self.assertRaises(ValueError):imported.select_gain(101e6)
        with self.assertRaises(ValueError):import_campaign_profile(source,context,'fixture',[16.6,19.7])

    def test_bladerf_import_requires_matching_internal_state(self):
        context=dict(driver='bladerf',hardware='bladerf1',serial='one',sample_rate=28e6,
                     bandwidth=28e6,antenna='RX',agc=False,hardware_info={'lms_dc_state_at_open':'changed'})
        source=dict(sample_rate_hz=28e6,bandwidth_hz=28e6,serial='one',lms_state_at_fit='fit')
        with self.assertRaisesRegex(ValueError,'LMS'):import_campaign_profile(source,context,'fixture')


class AlgorithmTests(unittest.TestCase):
    def test_rtl_exact_origin_hole_is_fixed(self):
        old=[100e6+i*960000 for i in range(5)]
        self.assertTrue(coverage_gaps(old,100e6,103e6,960000,20000,margin=625))
        new=masked_frequency_plan(100e6,103e6,960000,625,20000,tune_range=(24e6,1764e6))
        self.assertEqual(coverage_gaps(new,100e6,103e6,960000,20000,margin=625),[])
        f=np.arange(100e6,103e6,625)
        count=np.zeros(len(f),int)
        for c in new:count += ((abs(f-c)<960000-625)&(abs(f-c)>10000+625))
        self.assertTrue(np.all(count>0))

    def test_unreachable_endpoint_and_impossible_mask_fail(self):
        with self.assertRaises(ValueError):masked_frequency_plan(0,100,10,1,8,tune_range=(0,1))
        with self.assertRaises(ValueError):masked_frequency_plan(0,100,10,1,20)

    def test_rebin_power_conservation_and_missing_support(self):
        result,support=rebin_density([0,1,3],[2,4],[0,.5,1.5,3])
        self.assertAlmostEqual(np.sum(result*np.diff([0,.5,1.5,3])),10)
        np.testing.assert_array_equal(support,[1,1,1])
        result,support=rebin_density([0,1,2,3],[0,np.nan,4],[-1,1,2,4])
        self.assertEqual(result[0],0);self.assertTrue(np.isnan(result[1]));self.assertEqual(result[2],4)
        np.testing.assert_array_equal(support,[.5,0,.5])

    def test_rebin_extreme_density_is_finite_without_area_overflow(self):
        result,support=rebin_density([0,1e6,2e6],[1e307,1e307],[0,2e6])
        self.assertAlmostEqual(result[0]/1e307,1)
        self.assertEqual(support[0],1)

    def test_fusion_zero_weight_is_missing_and_zeros_are_real(self):
        result,count=fuse_density([[0,np.nan,3],[2,9,5]],[[1,2,0],[1,0,0]])
        self.assertEqual(result[0],1);self.assertTrue(np.isnan(result[1:]).all())
        np.testing.assert_array_equal(count,[2,0,0])

    def test_rebin_rejects_malformed_grids(self):
        for source in ([0,0,1],[0,np.nan,2],[2,1,0]):
            with self.assertRaises(ValueError):rebin_density(source,[1,2],[0,2])

    def test_common_band_tone_preserves_amplitude_and_phase_delay(self):
        fs=5120000;n=np.arange(100000);z=.2*np.exp(2j*np.pi*100000*n/fs)
        original=z.copy();y,meta=common_band_iq(z,fs)
        self.assertEqual(len(y),40960)
        self.assertEqual(meta['resampling'],[1,2])
        self.assertAlmostEqual(float(np.sqrt(np.mean(abs(y)**2))),.2,places=4)
        expected=.2*np.exp(2j*np.pi*100000*(.0005+np.arange(len(y))/2560000))
        self.assertLess(float(np.max(abs(y-expected))),.0001)
        np.testing.assert_array_equal(z,original)
        self.assertFalse(meta['absolute_phase_aligned'])

    def test_common_iq_budget_and_duration_are_checked(self):
        z=np.ones(100000,dtype=complex)
        for options in ({'duration':1/3000000},{'max_samples':100},{'start_seconds':0}):
            with self.assertRaises(ValueError):common_band_iq(z,2560000,**options)

    def test_common_phase_has_equal_bins_and_disjoint_partitions(self):
        rng=np.random.RandomState(123);z=(rng.randn(50000)+1j*rng.randn(50000))*.01
        result=equal_duration_phase(z,2560000)
        self.assertEqual(result['short_bin_spacing_hz'],10000)
        self.assertAlmostEqual(result['short_enbw_hz'],15000)
        self.assertEqual(result['training_sample_stop_exclusive'],result['validation_sample_start'])

    def test_fusion_exports_sparse_contributors_times_and_peak(self):
        rows=[dict(id='a',frequency_edges_hz=[0,1,2],density=[0,np.nan],units='input2/Hz',
                   reference_convention='fixed fixture state',start_s=0,stop_s=1),
              dict(id='b',frequency_edges_hz=[0,1,2],density=[2,4],units='input2/Hz',
                   reference_convention='fixed fixture state',start_s=2,stop_s=3)]
        result=fuse_observations(rows,[0,1,2,3])
        np.testing.assert_allclose(result['density'],[1,4,np.nan],equal_nan=True)
        np.testing.assert_array_equal(result['contributor_indptr'],[0,2,3,3])
        np.testing.assert_array_equal(result['contributor_hop_index'],[0,1,1])
        np.testing.assert_allclose(result['first_acquisition_s'],[0,2,np.nan],equal_nan=True)
        np.testing.assert_allclose(result['peak_contributing_mean_density'],[2,4,np.nan],equal_nan=True)
        aged=fuse_observations(rows,[0,1,2],reference_time_s=3,max_age_s=1)
        np.testing.assert_array_equal(aged['support_count'],[1,1])
        rows[1]['units']='mW/Hz'
        with self.assertRaisesRegex(ValueError,'different units'):fuse_observations(rows,[0,1,2])

    def test_spur_track_freezes_training_anchor_without_removal(self):
        rows=[dict(capture_id=str(i),partition='train' if i<3 else 'validation',
                   center_hz=c,baseband_hz=10000,sample_rate_hz=2e6,clock_state='internal')
              for i,c in enumerate([100e6,101e6,102e6,100.2e6,101.2e6])]
        result=analyze_track(rows,100)
        self.assertEqual(result['groups'][0]['diagnostic_class'],'if_fixed')
        self.assertEqual(result['automatic_exclusions'],[])
        rows[-1]['baseband_hz']=12000
        self.assertEqual(analyze_track(rows,100)['groups'][0]['diagnostic_class'],'unresolved')

    def test_spur_rate_domains_and_independent_ids_cannot_be_pooled(self):
        rows=[dict(capture_id=str(i),partition='train' if i<3 else 'validation',
                   center_hz=100e6+i*1e6,baseband_hz=10000,sample_rate_hz=2e6 if i<3 else 3e6,clock_state='internal')
              for i in range(5)]
        self.assertTrue(all(not g['identifiable'] for g in analyze_track(rows,100)['groups']))
        rows[-1]['capture_id']='0'
        with self.assertRaisesRegex(ValueError,'unique'):analyze_track(rows,100)


class NoiseReferenceTests(unittest.TestCase):
    def reference(self):
        context=architecture.context()
        method=dict(fft_bins=64,window='hann',normalization='density_input_squared_per_hz')
        ref=RelativeNoiseReference([0,1,2,3],[2,0,np.nan],context,method,
            dict(termination='nominal 50 ohm, RF rating unknown',reference_plane='load via unknown cable/adapter',temperature_c=23,headroom_accepted=True))
        return ref,context,method

    def test_negative_excess_is_preserved_not_called_snr(self):
        ref,context,method=self.reference();r=ref.compare([0,1,2,3],[1,1,1],context,method)
        self.assertAlmostEqual(r['noise_ratio_db'][0],-3.0102999566)
        self.assertEqual(r['signed_excess_density'][0],-1)
        np.testing.assert_array_equal(r['supported'],[True,False,False])
        self.assertFalse(r['snr']);self.assertFalse(r['absolute_power'])

    def test_state_grid_and_method_mismatch_fail(self):
        ref,context,method=self.reference()
        for key,value in (('serial','other'),('gains',{'overall':11}),('sample_rate',1025)):
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'mismatch'):
                ref.compare(ref.edges,[1,1,1],dict(context,**{key:value}),method)
        with self.assertRaisesRegex(ValueError,'grid'):ref.compare([0,1,2,4],[1,1,1],context,method)
        with self.assertRaisesRegex(ValueError,'method'):ref.compare(ref.edges,[1,1,1],context,dict(method,window='boxcar'))

    def test_reference_roundtrip_is_cached_and_cannot_overwrite(self):
        ref,context,method=self.reference()
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'reference.npz';ref.save(p);loaded=RelativeNoiseReference.load(p)
            p.unlink()  # Comparisons use the verified in-memory snapshot, no repeated decompression.
            self.assertTrue(loaded.compare(ref.edges,[1,1,1],context,method)['supported'][0])
            loaded.save(p)
            with self.assertRaises(FileExistsError):loaded.save(p)
            self.assertFalse(loaded.density.flags.writeable)

    def test_offline_cli_retains_support_and_protects_source(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'source.npz';out=Path(d)/'out.npz';target=Path(d)/'edges.json'
            np.savez(p,frequency_edges_hz=[0,1,2],density=[1,np.nan]);target.write_text('[0,2]')
            analyze(['rebin',str(p),str(out),'--target-edges',str(target)])
            with np.load(out) as a:
                self.assertEqual(a['density'][0],1);self.assertEqual(a['supported_fraction'][0],.5)
            with self.assertRaises(SystemExit):analyze(['rebin',str(p),str(p),'--target-edges',str(target),'--overwrite'])

    def test_source_change_blocks_atomic_publication(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'source.npz';target=Path(d)/'edges.json';out=Path(d)/'result.npz'
            np.savez(source,frequency_edges_hz=[0,1],density=[1]);target.write_text('[0,1]')
            def mutate(*a,**k):
                result=rebin_density(*a,**k)
                source.write_bytes(b'changed input')
                return result
            with mock.patch('soapypower.analysis_cli.rebin_density',side_effect=mutate):
                with self.assertRaises(SystemExit):analyze(['rebin',str(source),str(out),'--target-edges',str(target)])
            self.assertFalse(out.exists())

    def test_phase_cli_exports_arrays_and_preserves_heldout_traces(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'source.npz';out=Path(d)/'phase.npz'
            fs=2560000;t=np.arange(50000)/fs
            rng=np.random.RandomState(0)
            z=.1*np.exp(2j*np.pi*400000*t)+.001*(rng.randn(len(t))+1j*rng.randn(len(t)))
            np.savez(source,iq=z)
            analyze(['phase',str(source),str(out),'--sample-rate',str(fs)])
            with np.load(out,allow_pickle=False) as archive:
                meta=json.loads(str(archive['metadata'].item()))
                self.assertFalse(meta['absolute_phase_aligned'])
                self.assertEqual(meta['short_bin_spacing_hz'],10000)
                self.assertIn('candidate_0_validation_phase_residual_deg',archive)
            np.testing.assert_array_equal(np.load(source)['iq'],z)


if __name__ == '__main__':unittest.main()
