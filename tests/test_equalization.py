"""Measured relative PSD shaping: inverse math, orientation and atomic failures."""
import copy
import datetime
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np
from soapypower.equalization import relative_inverse, equalize_record, equalize_file
from soapypower.calibration import PowerCalibration
from soapypower.calibration_cli import main, extract_context
from soapypower.calibration_workflows import average_capture
from soapypower.files import write_json_lines
from soapypower.records import read_records, integrate_record
from soapypower.psd import PSD
from soapypower.writer import JsonWriter
import test_architecture as architecture
import test_calibration_workflows as workflows
import io


class EqualizerMathTests(unittest.TestCase):
    def test_direct_inverse_flattens_power_and_uses_half_db_amplitude(self):
        offsets=np.array([-40.,-30.,-20.,-30.])
        result=relative_inverse(offsets,-40,0,30)
        np.testing.assert_allclose(result['power_weight_db'],offsets+40)
        chain=10**(np.array(result['relative_chain_power_gain_db'])/10)
        amp=10**(np.array(result['amplitude_weight_db'])/20)
        np.testing.assert_allclose(chain*amp**2,1,rtol=1e-14)
        np.testing.assert_allclose(result['predicted_residual_power_response_db'],0,atol=1e-13)

    def test_regularization_has_unity_anchor_bounded_deep_nulls_and_known_bias(self):
        result=relative_inverse([0,20,1000],0,0.01,6)
        self.assertAlmostEqual(result['power_weight_db'][0],0,places=14)
        self.assertLess(result['power_weight_db'][-1],-900)
        self.assertTrue(all(v<=6 for v in result['power_weight_db']))
        expected=(1.01)*0.1/(0.01+0.01)
        self.assertAlmostEqual(result['uncapped_weight_db'][1],20*math.log10(expected))
        self.assertIn(1,result['boost_limited_bins'])
        self.assertFalse(result['noise_subtracted']);self.assertFalse(result['snr_improvement_claimed'])
        # Regularization intentionally leaves a response error rather than claiming flatness.
        self.assertLess(result['predicted_residual_power_response_db'][1],-10)

    def test_shaped_iq_recovers_reference_psd_and_parseval_without_noise_subtraction(self):
        rng=np.random.default_rng(19);frames=rng.normal(size=(128,64))+1j*rng.normal(size=(128,64))
        offsets=np.fft.fftfreq(64)
        h=1-0.85*np.exp(-((offsets-0.125)/0.07)**2)
        colored=np.fft.ifft(np.fft.fft(frames,axis=1)*h,axis=1)
        outputs=[]
        for data in (frames,colored):
            psd=PSD(64,1024,fft_window='boxcar',fft_overlap=0,log_scale=False,max_threads=1)
            try:
                state=psd.set_center_freq(10000);psd.update(state,data.reshape(-1))
                outputs.append(psd.result(state)[1])
            finally:psd.close()
        design=relative_inverse(-20*np.log10(np.fft.fftshift(h)),0,0,30)
        recovered=outputs[1]*10**(np.array(design['power_weight_db'])/10)
        np.testing.assert_allclose(recovered,outputs[0],rtol=2e-14,atol=1e-16)
        self.assertAlmostEqual(float(recovered.sum())*16,float(np.mean(abs(frames)**2)),places=13)

    def test_capped_direct_inverse_and_invalid_inputs(self):
        result=relative_inverse([0,1000],0,0,12)
        self.assertEqual(result['power_weight_db'],[0,12])
        for args in (([],0),([0,float('nan')],0),([0],0,-1),([0],0,0,-1)):
            with self.assertRaises(ValueError):relative_inverse(*args)

    def test_inverse_changes_noise_and_signal_equally_not_snr(self):
        result=relative_inverse([0,10],0,0,20)
        weights=10**(np.array(result['power_weight_db'])/10)
        signal=np.array([2.,0.2]);noise=np.array([0.1,0.3])
        np.testing.assert_allclose(signal/noise,(signal*weights)/(noise*weights))


class EqualizationRecordTests(unittest.TestCase):
    setUp=workflows.WorkflowTests.setUp
    capture=workflows.WorkflowTests.capture

    def data(self,log=False):
        record=self.capture('raw.jsonl',rf=10000,bb=128,density=1)
        if log:
            record['unit']='dB(input power/Hz)';record['power']=[0.]*64
        return record

    def calibration(self,ctx=None):
        profile=architecture.profile(ctx=ctx)
        profile['reference_baseband_hz']=-512
        profile['baseband_correction_db']=[[0,20],[0,20]]
        return PowerCalibration(profile)

    def test_linear_and_log_equivalent_metadata_units_and_readonly_input(self):
        original=self.data();cal=self.calibration();saved=copy.deepcopy(original)
        linear=equalize_record(original,cal,10000,-512,0,30)
        log=equalize_record(self.data(True),cal,10000,-512,0,30)
        np.testing.assert_allclose(linear['power'],10**(np.array(log['power'])/10),rtol=1e-14)
        self.assertEqual(original,saved)
        self.assertEqual(linear['unit'],'input power/Hz')
        self.assertIsNone(linear['metadata']['calibration'])
        self.assertEqual(linear['metadata']['equalization']['profile']['profile_sha256'],cal.profile_id)
        self.assertGreater(integrate_record(linear)['power'],integrate_record(original)['power'])

    def test_mask_and_zero_preserved_and_estimated_double_correction_rejected(self):
        record=self.data();record['power'][0]=None;record['masked_bins']=[0]
        record['power'][1]=0;record['zero_power_bins']=[1]
        cal=self.calibration();result=equalize_record(record,cal,10000,0)
        self.assertIsNone(result['power'][0]);self.assertEqual(result['power'][1],0)
        with self.assertRaises(ValueError):equalize_record(result,cal,10000,0)
        record=self.data();record['estimated_bins']=[2]
        with self.assertRaises(ValueError):equalize_record(record,cal,10000,0)
        record=self.data();record['metadata']['calibration']={'profile':'already'}
        with self.assertRaises(ValueError):equalize_record(record,cal,10000,0)

    def test_context_coverage_and_processing_fail_closed(self):
        cal=self.calibration()
        for field,value in (('measurement_status','partial'),('tuned_center_hz',None),('detrend','constant')):
            record=self.data();record['metadata'][field]=value
            with self.assertRaises(ValueError):equalize_record(record,cal,10000,0)
        record=self.data();record['metadata']['context']['gains']={'overall':99}
        with self.assertRaises(ValueError):equalize_record(record,cal,10000,0)
        with self.assertRaises(ValueError):equalize_record(self.data(),cal,99999,0)
        with self.assertRaises(ValueError):equalize_record(self.data(),cal,10000,999)

    def test_all_conjugation_and_lnb_sign_coordinates_match_native_psd_calibration(self):
        for conjugate in (False,True):
            for sign in (-1,1):
                context=architecture.context();context.update(lnb_lo=20000 if sign<0 else 0,
                    lnb_high_side=sign<0,iq_conjugate=conjugate)
                cal=self.calibration(context)
                psd=PSD(64,1024,fft_window='boxcar',log_scale=False,iq_conjugate=conjugate,
                        lnb_lo=context['lnb_lo'],frequency_sign=sign,max_threads=1)
                try:
                    state=psd.set_center_freq(10000)
                    rng=np.random.default_rng(42);psd.update(state,rng.normal(size=256)+1j*rng.normal(size=256))
                    freq,power=psd.result(state)
                    metadata=dict(psd.measurement_metadata(state),context=context,measurement_status='complete')
                    sink=io.BytesIO();writer=JsonWriter(sink,log_scale=False)
                    now=datetime.datetime.now(datetime.timezone.utc)
                    writer.write((freq,power),now,now,256,metadata=metadata)
                    record=json.loads(sink.getvalue())
                    result=equalize_record(record,cal,10000,-512,0,30)
                    raw=sign*(freq-context['lnb_lo'])-10000
                    if conjugate:raw=(-raw+512)%1024-512
                    anchor=float(cal.offsets_db([10000],[-512])[0])
                    expected=power*10**((cal.offsets_db(freq,raw)-anchor)/10)
                    np.testing.assert_allclose(result['power'],expected,rtol=2e-14)
                finally:psd.close()

    def test_exact_grid_reconstructs_small_baseband_at_large_rf_and_rejects_tampering(self):
        context=architecture.context();context['lnb_lo']=3e9
        profile=architecture.profile(ctx=context,points=[[3e9, -30],[3e9+20000,-30]])
        cal=PowerCalibration(profile)
        psd=PSD(63,1024,fft_window='boxcar',fft_overlap=0,log_scale=False,lnb_lo=3e9,max_threads=1)
        try:
            state=psd.set_center_freq(10000);psd.update(state,np.ones(126,dtype=complex))
            freq,power=psd.result(state)
            metadata=dict(psd.measurement_metadata(state),context=context,measurement_status='complete')
            sink=io.BytesIO();writer=JsonWriter(sink,log_scale=False)
            now=datetime.datetime.now(datetime.timezone.utc)
            writer.write((freq,power),now,now,126,metadata=metadata)
            record=json.loads(sink.getvalue())
            result=equalize_record(record,cal,3e9+10000,0)
            expected=np.fft.fftshift(np.fft.fftfreq(63,1/1024))
            np.testing.assert_allclose(result['metadata']['equalization']['raw_baseband_hz'],expected,rtol=1e-15,atol=1e-13)
            record['metadata']['frequency_grid']['first_bin_index']+=1
            with self.assertRaises(ValueError):equalize_record(record,cal,3e9+10000,0)
        finally:psd.close()

    def test_atomic_cli_output_provenance_and_reference_rejection(self):
        record=self.data();source=self.root/'input.jsonl';source.write_text(json.dumps(record)+'\n')
        profile=self.root/'profile.json';profile.write_text(json.dumps(self.calibration().profile))
        output=self.root/'output.jsonl'
        args=['equalize',str(source),'--profile',str(profile),'--reference-rf-hz','10000',
              '--reference-baseband-hz','0','-O',str(output)]
        self.assertEqual(main(args),0)
        result=list(read_records(output))[0]
        self.assertEqual(len(result['metadata']['equalization']['source_records_sha256']),64)
        with self.assertRaises(ValueError):average_capture(output)
        with self.assertRaises(ValueError):extract_context(output)
        with mock.patch('sys.stderr'):
            with self.assertRaises(SystemExit):main(args)
        with self.assertRaises(ValueError):equalize_file(source,profile,source,10000,0,overwrite=True)
        with self.assertRaises(ValueError):equalize_file(source,profile,profile,10000,0,overwrite=True)

    def test_late_failure_never_publishes_or_replaces_output(self):
        record=self.data();source=self.root/'input.jsonl'
        source.write_text(json.dumps(record)+'\n'+json.dumps(dict(record,unit='mW/Hz'))+'\n')
        profile=self.root/'profile.json';profile.write_text(json.dumps(self.calibration().profile))
        output=self.root/'late.jsonl'
        with self.assertRaises(ValueError):equalize_file(source,profile,output,10000,0)
        self.assertFalse(output.exists())
        output.write_text('original')
        with self.assertRaises(ValueError):equalize_file(source,profile,output,10000,0,overwrite=True)
        self.assertEqual(output.read_text(),'original')
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith('.')),[])
        with self.assertRaises(ValueError):write_json_lines(output,[],overwrite=True)
        self.assertEqual(output.read_text(),'original')
