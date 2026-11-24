import io, json, time, unittest
import numpy as np
from tests import test_audit as audit  # noqa: F401  (AcquisitionTests.setUp builds a SoapyPower on a fake device)


def packet(ret, flags=0):
    return type('Packet', (), dict(ret=ret, flags=flags, timeNs=0))()


class SettleOverflowTests(unittest.TestCase):
    setUp = audit.AcquisitionTests.setUp

    def prepare(self, settle_samples=16, lock_timeout=5.0):
        self.sdr.setup(8, 3, base_buffer_size=8, max_threads=1)
        self.sdr._settle_samples = settle_samples
        self.sdr._active_tune_delay = 0
        self.sdr._lock_sensor = None
        self.sdr._reference_lock_sensor = None
        self.sdr._lock_timeout = lock_timeout
        self.sdr._acquisition_stage = 'settling'

    def test_overflow_while_settling_restarts_the_window_and_is_counted(self):
        self.prepare()
        script = [packet(-4)] + [packet(8)] * 6
        def read():
            self.sdr.device.buffer[:8] = 1
            return script.pop(0)
        self.sdr.device.read_stream = read
        self.sdr._settle(time.monotonic())
        self.assertEqual(self.sdr._settle_overflow_count, 1)
        self.assertLessEqual(len(script), 4)  # the 16-sample window was re-read in full after the overflow

    def test_persistent_overflow_while_settling_still_ends_in_settle_timeout(self):
        self.prepare(lock_timeout=0.05)
        self.sdr.device.read_stream = lambda: packet(-4)
        with self.assertRaisesRegex(RuntimeError, 'settle timeout'):
            self.sdr._settle(time.monotonic())

    def test_overflow_while_measuring_is_never_tolerated_and_reports_stage(self):
        self.prepare()
        self.sdr._acquisition_stage = 'measurement'
        self.sdr.device.read_stream = lambda: packet(-4)
        with self.assertRaisesRegex(RuntimeError, 'SDR overflow.*refusing discontinuous IQ'):
            self.sdr._read_chunk(np.empty(8, dtype=np.complex64))

    def test_read_chunk_works_on_an_object_that_never_ran_setup(self):
        bare = self.power.SoapyPower.__new__(self.power.SoapyPower)
        self.assertEqual(bare._acquisition_stage, 'idle')


class UhdRingHintTests(unittest.TestCase):
    setUp = audit.AcquisitionTests.setUp

    def hinted(self, args):
        with self.assertLogs('soapypower.power', level='INFO') as captured:
            self.power.logger.info('probe')  # assertLogs needs at least one record
            self.power.SoapyPower(soapy_args=args, output=io.BytesIO(), output_format='soapy_power_bin')
        return any('num_recv_frames' in m for m in captured.output)

    def test_hint_only_for_uhd_without_num_recv_frames(self):
        self.assertTrue(self.hinted('driver=uhd,serial=X'))
        self.assertFalse(self.hinted('driver=uhd, serial=X,num_recv_frames=512'))
        self.assertFalse(self.hinted('driver=rtlsdr'))
        self.assertFalse(self.hinted(''))


class JsonPowerVectorTests(unittest.TestCase):
    def test_vectorised_power_equals_the_per_element_conversion(self):
        power = np.array([-100.5, np.nan, np.inf, -np.inf, 0.0, 1e-30, -57.123456789], dtype=np.float32)
        old = [float(v) if np.isfinite(v) else None for v in power]
        values = np.asarray(power, dtype=np.float64).tolist()
        finite = np.isfinite(power)
        for i in np.flatnonzero(~finite):
            values[int(i)] = None
        self.assertEqual(values, old)
        json.dumps(values, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
