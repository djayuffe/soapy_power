import unittest
from unittest import mock
import test_audit as audit
import test_architecture as architecture

class StreamArgumentTests(unittest.TestCase):
    setUp = audit.AcquisitionTests.setUp
    capable_receiver = architecture.HardwareIntegrationTests.capable_receiver

    def test_native_map_preserves_values_and_is_recorded_in_context(self):
        class NativeMap(dict): pass
        native=NativeMap()
        with mock.patch.object(self.power.simplesoapy.SoapySDR,'KwargsFromString',return_value=native,create=True) as factory:
            sdr,_=self.capable_receiver(stream_args={'buffers':'128','buflen':'65536','transfers':'16'})
        factory.assert_called_once_with('')
        self.assertIs(sdr.device.stream_args,native)
        self.assertEqual(native,{'buffers':'128','buflen':'65536','transfers':'16'})
        sdr.setup(64,4,max_threads=1)
        self.assertEqual(sdr.measurement_context()['stream_args'],native)
        sdr.stop()

    def test_empty_default_does_not_change_legacy_context(self):
        sdr,_=self.capable_receiver()
        sdr.setup(64,4,max_threads=1)
        self.assertNotIn('stream_args',sdr.measurement_context())
        sdr.stop()

    def test_invalid_stream_argument_values_fail_before_device_open(self):
        for value in ({'buffers':128},{'':'1'},{1:'2'}):
            with mock.patch.object(self.power.simplesoapy,'SoapyDevice') as device:
                with self.assertRaisesRegex(ValueError,'stream arguments'):
                    self.power.SoapyPower(stream_args=value)
                device.assert_not_called()

if __name__=='__main__': unittest.main()
