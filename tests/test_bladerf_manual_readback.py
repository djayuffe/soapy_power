import unittest
from types import SimpleNamespace
from soapypower.frontend import Frontend

class BladeRFManualReadbackTests(unittest.TestCase):
    def frontend(self, value, driver='bladeRF', hardware='bladerf1', tolerance=None):
        raw=SimpleNamespace(getDriverKey=lambda:driver,getHardwareKey=lambda:hardware,
                            getDCOffset=lambda *_:value,getIQBalance=lambda *_:value)
        return Frontend(SimpleNamespace(device=raw),1,0,manual_tolerance=tolerance)

    def test_bladerf1_dc_accepts_actual_lms_step_and_records_readback(self):
        f=self.frontend(complex(0,-1/64))
        r=f._manual_readback('DCOffset',(.01,-.01))
        self.assertEqual(r['actual'],[0,-1/64])

    def test_bladerf1_wrong_dc_setting_is_still_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'did not apply'):
            self.frontend(.1+0j)._manual_readback('DCOffset',(0,0))

    def test_bladerf2_does_not_inherit_lms_dc_tolerance(self):
        with self.assertRaisesRegex(RuntimeError,'did not apply'):
            self.frontend(0j,hardware='bladerf2')._manual_readback('DCOffset',(.01,0))

    def test_fpga_iq_quantum_is_not_relaxed_to_lms_dc_step(self):
        with self.assertRaisesRegex(RuntimeError,'did not apply'):
            self.frontend(0j)._manual_readback('IQBalance',(.01,0))

    def test_explicit_strict_tolerance_takes_precedence(self):
        with self.assertRaisesRegex(RuntimeError,'did not apply'):
            self.frontend(0j,tolerance=1e-5)._manual_readback('DCOffset',(.01,0))

if __name__=='__main__':unittest.main()
