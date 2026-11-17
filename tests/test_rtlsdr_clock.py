"""A driver sample counter must not masquerade as an RTL hardware clock."""
import json,unittest
import test_audit as audit
import test_architecture as architecture
class RTLClockTests(unittest.TestCase):
 setUp=audit.AcquisitionTests.setUp
 capable_receiver=architecture.HardwareIntegrationTests.capable_receiver
 def receiver(self,driver='RTLSDR',timestamped=True):
  sdr,out=self.capable_receiver();sdr.device.device.getDriverKey=lambda:driver
  original=sdr.device.read_stream
  def packet():
   r=original()
   if timestamped:r.flags=4;r.timeNs=round((sdr.device.reads-1)*r.ret/1024*1e9)
   return r
  sdr.device.read_stream=packet;return sdr,out
 def test_rtl_counter_metadata_preserves_driver_times_without_claiming_hardware(self):
  sdr,out=self.receiver();sdr.sweep(10000,10000,64,4,runs=1,max_threads=1)
  m=json.loads(out.getvalue())['metadata'];self.assertEqual(m['driver_reported_continuity'],'verified');self.assertEqual(m['continuity'],'unverifiable_software_clock');self.assertEqual(m['timestamp_source'],'driver_sample_counter');self.assertIsNone(m['hardware_first_sample_ns']);self.assertIsNone(m['hardware_last_sample_ns']);self.assertIsNotNone(m['driver_first_sample_ns']);self.assertFalse(m['time_basis']['hardware_sample_time']);self.assertTrue(m['time_basis']['driver_sample_counter_time'])
 def test_strict_hardware_timestamp_request_rejects_case_insensitive_rtl(self):
  sdr,out=self.receiver(driver='rTlSdR')
  with self.assertRaisesRegex(RuntimeError,'hardware timestamp continuity'):
   sdr.sweep(10000,10000,64,4,runs=1,max_threads=1,require_timestamps=True)
  self.assertEqual(out.getvalue(),b'');self.assertFalse(sdr.device.is_streaming)
 def test_rtl_without_flags_is_still_unverified(self):
  sdr,out=self.receiver(timestamped=False);sdr.sweep(10000,10000,64,4,runs=1,max_threads=1);m=json.loads(out.getvalue())['metadata'];self.assertNotEqual(m['continuity'],'verified');self.assertIsNone(m['driver_first_sample_ns']);self.assertFalse(m['time_basis']['driver_sample_counter_time'])
 def test_other_timestamped_receivers_keep_existing_verified_behavior(self):
  sdr,out=self.receiver(driver='test');sdr.sweep(10000,10000,64,4,runs=1,max_threads=1,require_timestamps=True);m=json.loads(out.getvalue())['metadata'];self.assertEqual(m['continuity'],'verified');self.assertIsNotNone(m['hardware_first_sample_ns']);self.assertTrue(m['time_basis']['hardware_sample_time']);self.assertFalse(m['time_basis']['driver_sample_counter_time'])
if __name__=='__main__':unittest.main()
