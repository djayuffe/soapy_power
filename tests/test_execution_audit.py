"""Regression cases found by the end-to-end execution audit."""
import copy
import datetime
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import numpy as np
from soapypower.psd import PSD
from soapypower.calibration import PowerCalibration,_compare
from soapypower.calibration_cli import main
from soapypower.calibration_workflows import average_capture
from soapypower.files import EvidenceSnapshot,write_json
from soapypower.passive_response import _solve
import test_architecture as architecture
import test_calibration_workflows as workflows
import test_characterization as characterization


class DSPInputOwnershipTests(unittest.TestCase):
    def test_queued_inputs_are_snapshots_in_both_modes_and_precisions(self):
        for precision in ('single','double'):
            for contiguous in (False,True):
                psd=PSD(64,1024,fft_window='boxcar',fft_overlap=0,log_scale=False,
                        precision=precision,max_threads=1,max_queue_size=2)
                entered=threading.Event();release=threading.Event()
                def blocker():entered.set();release.wait(3)
                psd._executor.submit(blocker)
                self.assertTrue(entered.wait(2))
                data=np.ones(128,dtype='complex64' if precision=='single' else 'complex128')
                state=psd.set_center_freq(0)
                try:
                    submit=psd.update_contiguous_async if contiguous else psd.update_async
                    submit(state,data);data[:]=0;release.set()
                    _,power=psd.wait_for_result(state)
                    self.assertAlmostEqual(float(power.sum())*16,1,places=6)
                finally:release.set();psd.close()


class ProfileAuditTests(unittest.TestCase):
    def test_processing_contract_is_validated_at_load_not_only_during_acquisition(self):
        for processing in ({'detrend':'linear'},[],{'detrend':None,'unknown':1}):
            profile=architecture.profile();profile['processing']=processing
            with self.assertRaisesRegex(ValueError,'processing'):PowerCalibration(profile)
        profile=architecture.profile();profile['processing']={'detrend':'constant'}
        PowerCalibration(profile).validate_processing('constant',False)
    def test_malformed_array_fields_produce_validation_errors(self):
        for field in ('baseband_hz','baseband_correction_db'):
            profile=architecture.profile();profile[field]={'bad':'not numeric'}
            with self.assertRaises(ValueError):PowerCalibration(profile)
        from soapypower.response_smoothing import smooth_row
        with self.assertRaises(ValueError):smooth_row([0,1,2],[0,0,0],{'bad':1},1,1)

    def test_nested_context_sequences_compare_types_and_numeric_tolerance(self):
        _compare({'coeff':[0.1,0.2]},{'coeff':[0.1+1e-12,0.2]})
        for actual in ({'coeff':[True,0.2]},{'coeff':(1.,0.2)},{'coeff':[1.,0.2,0.3]}):
            with self.assertRaises(ValueError):_compare({'coeff':[1.,0.2]},actual)
    def test_overlap_large_known_standard_errors_remain_finite(self):
        ids=[];positions=[];values=[]
        for sid,level in (('a',10),('b',20)):
            for bb,response in ((-1,-3),(0,0),(1,-2)):
                ids.append(sid);positions.append(bb);values.append(level+response)
        result=_solve(ids,positions,values,[1e200]*len(values),0)
        np.testing.assert_allclose(result['response_db'],[-3,0,-2],atol=1e-12)
        self.assertTrue(all(np.isfinite(result['conditional_standard_db'])))
        self.assertGreater(result['conditional_standard_db'][0],1e199)
        self.assertAlmostEqual(result['conditional_standard_db'][0]/1e200,1,places=12)


class EvidenceAuditTests(unittest.TestCase):
    setUp=workflows.WorkflowTests.setUp
    capture=workflows.WorkflowTests.capture
    manifest=workflows.WorkflowTests.manifest
    def assert_cli_rejects(self,args):
        with mock.patch('sys.stderr'):
            with self.assertRaises(SystemExit):main(args)
    def test_context_and_build_cannot_overwrite_input_even_with_force(self):
        self.capture('raw.jsonl');path=self.root/'raw.jsonl';before=path.read_bytes()
        self.assert_cli_rejects(['context',str(path),'-O',str(path),'--force'])
        self.assertEqual(path.read_bytes(),before)
        context=self.root/'context.json';context.write_text(json.dumps(architecture.context()))
        csv=self.root/'tones.csv';csv.write_text('rf_hz,baseband_hz,input_dbm,raw_power\n10000,-128,-30,1\n10000,128,-30,1\n20000,-128,-30,1\n20000,128,-30,1\n')
        before=csv.read_bytes()
        self.assert_cli_rejects(['build','--context',str(context),'--measurements',str(csv),
            '--reference','synthetic','--reference-plane','test','--reference-baseband-hz','128',
            '-O',str(csv),'--force'])
        self.assertEqual(csv.read_bytes(),before)
    def test_capture_builder_protects_manifest_and_capture_evidence(self):
        manifest=self.manifest();path=self.root/'manifest.json';path.write_text(json.dumps(manifest))
        capture=self.root/manifest['cells'][0]['on'];before=capture.read_bytes()
        for output in (path,capture):
            self.assert_cli_rejects(['from-captures',str(path),'-O',str(output),'--force'])
        self.assertEqual(capture.read_bytes(),before)
    def test_characterization_protects_deeply_nested_cw_and_noise_captures(self):
        helper=characterization.CaptureCharacterizationTests();helper.setUp()
        try:
            manifest,path=helper.make_manifest()
            for name in (manifest['cells'][0]['receiver'][0]['capture'],manifest['cells'][0]['cascade_cw'][0]['off']):
                source=helper.root/name;before=source.read_bytes()
                self.assert_cli_rejects(['characterize',str(path),'-O',str(source),'--force'])
                self.assertEqual(source.read_bytes(),before)
        finally:helper.doCleanups()
    def test_capture_modified_during_read_is_rejected(self):
        from soapypower import calibration_workflows as module
        self.capture('raw.jsonl');path=self.root/'raw.jsonl'
        original=module.read_records
        def changing(name):
            for record in original(name):yield record
            with Path(name).open('a') as stream:stream.write('\n')
        with mock.patch.object(module,'read_records',changing):
            with self.assertRaisesRegex(ValueError,'changed'):average_capture(path)
    def test_mutation_between_read_and_publication_preserves_destination(self):
        from soapypower import calibration_cli as module
        self.capture('raw.jsonl');path=self.root/'raw.jsonl';output=self.root/'context.json';output.write_text('prior')
        original=module.extract_context
        def changing(name):
            context=original(name)
            with Path(name).open('a') as stream:stream.write('\n')
            return context
        with mock.patch.object(module,'extract_context',changing):
            self.assert_cli_rejects(['context',str(path),'-O',str(output),'--force'])
        self.assertEqual(output.read_text(),'prior')
    def test_mutation_during_output_flush_is_detected_before_publication(self):
        from soapypower import files
        self.capture('raw.jsonl');path=self.root/'raw.jsonl';output=self.root/'context.json';output.write_text('prior')
        original=files.os.fsync
        def changing(fd):
            original(fd)
            with path.open('a') as stream:stream.write('\n')
        with mock.patch.object(files.os,'fsync',changing):
            self.assert_cli_rejects(['context',str(path),'-O',str(output),'--force'])
        self.assertEqual(output.read_text(),'prior')
        self.assertFalse(any(p.name.startswith('.') for p in self.root.iterdir()))

    def test_snapshot_detects_replacement_and_symlink_retarget(self):
        a=self.root/'a';b=self.root/'b';a.write_text('one');b.write_text('two')
        link=self.root/'link'
        try:link.symlink_to(a)
        except (OSError,NotImplementedError):self.skipTest('symlinks unavailable')
        snapshot=EvidenceSnapshot([link]);link.unlink();link.symlink_to(b)
        with self.assertRaisesRegex(ValueError,'changed'):snapshot.verify()
        snapshot=EvidenceSnapshot([a]);temp=self.root/'new';temp.write_text('three');temp.replace(a)
        with self.assertRaisesRegex(ValueError,'changed'):snapshot.verify()


class WriterAuditTests(unittest.TestCase):
    def test_async_direct_spectrum_and_metadata_are_snapshots(self):
        from soapypower.writer import JsonWriter
        sink=io.BytesIO();writer=JsonWriter(sink,log_scale=False)
        entered=threading.Event();release=threading.Event()
        def blocker():entered.set();release.wait(3)
        writer._executor.submit(blocker);self.assertTrue(entered.wait(2))
        freqs=np.array([100.,102.]);power=np.array([1.,2.]);metadata={'nested':{'gain':20}}
        now=datetime.datetime.now(datetime.timezone.utc)
        try:
            future=writer.write_async((freqs,power),now,now,64,metadata)
            power[:]=9;freqs[:]=0;metadata['nested']['gain']=99
            release.set();future.result(timeout=3)
        finally:release.set();writer.close()
        record=json.loads(sink.getvalue())
        self.assertEqual(record['power'],[1,2]);self.assertEqual(record['start_hz'],100)
        self.assertEqual(record['metadata']['nested']['gain'],20)

    def test_v3_payload_must_match_values_and_zero_vs_missing_markers(self):
        from soapypower.writer import JsonWriter,SoapyPowerBinV3Format
        formatter=SoapyPowerBinV3Format();sink=io.BytesIO();writer=JsonWriter(sink,log_scale=True)
        now=datetime.datetime.now(datetime.timezone.utc)
        record=writer.make_record((np.array([100.,102.]),np.array([-np.inf,0.])),now,now,64)
        writer.close()
        target=io.BytesIO()
        with self.assertRaises(ValueError):formatter.write(target,record,np.array([np.nan,0.]))
        self.assertEqual(target.getvalue(),b'')
        formatter.write(target,record,np.array([-np.inf,0.]))
        self.assertEqual(formatter.read(io.BytesIO(target.getvalue()))['zero_power_bins'],[0])
        # Recompute a valid checksum for a semantically invalid NaN-as-zero payload.
        header=dict(record);del header['power'];header.update(binary_version=3,power_dtype='float64-le')
        metadata=json.dumps(header,allow_nan=False,sort_keys=True,separators=(',',':')).encode()
        payload=np.array([np.nan,0.],dtype='<f8').tobytes()
        lengths=formatter.lengths.pack(len(metadata),len(payload))
        raw=formatter.magic+lengths+hashlib.sha256(lengths+metadata+payload).digest()+metadata+payload
        with self.assertRaises(ValueError):formatter.read(io.BytesIO(raw))


class RawAcquisitionAuditTests(unittest.TestCase):
    import test_audit as _audit
    setUp=_audit.AcquisitionTests.setUp
    capable_receiver=architecture.HardwareIntegrationTests.capable_receiver
    def test_uncalibrated_fixed_state_changes_do_not_publish_false_reference(self):
        for field,value in (('gain',99),('sample_rate',2048),('freq',10100)):
            sdr,output=self.capable_receiver()
            original=sdr.device.read_stream
            def changing(original=original,sdr=sdr,field=field,value=value):
                result=original();setattr(sdr.device,field,value);return result
            sdr.device.read_stream=changing
            with self.assertRaises((ValueError,RuntimeError)):
                sdr.sweep(10000,10000,64,2,runs=1)
            self.assertEqual(output.getvalue(),b'')
    def test_requested_agc_remains_a_valid_uncalibrated_display_mode(self):
        sdr,output=self.capable_receiver(auto_gain=True)
        original=sdr.device.read_stream
        def changing():
            result=original();sdr.device.gain+=1;return result
        sdr.device.read_stream=changing
        sdr.sweep(10000,10000,64,2,runs=1)
        self.assertTrue(json.loads(output.getvalue())['metadata']['context']['agc'])


class KnownReferenceContextTests(unittest.TestCase):
    setUp=workflows.WorkflowTests.setUp
    capture=workflows.WorkflowTests.capture
    def test_complete_reference_capture_requires_known_gain_readback(self):
        record=self.capture('unknown.jsonl');record['metadata']['context']['gains']=None
        path=self.root/'unknown.jsonl';path.write_text(json.dumps(record)+'\n')
        with self.assertRaisesRegex(ValueError,'known gains'):average_capture(path,require_complete=True)
        # The baseline diagnostic may still report raw observed density without
        # asserting an absolute/calibrated reference state.
        self.assertEqual(average_capture(path)['metadata']['context']['gains'],None)
