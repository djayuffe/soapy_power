import unittest
import numpy as np
from soapypower.receiver_quality import headroom_metrics,headroom_passes,require_lms_calibration_state,lower_bladerf1_gain,matching_dc_anchor

class ReceiverQualityTests(unittest.TestCase):
    def test_dc_rail_cannot_be_hidden_by_centering(self):
        z=np.full(10000,1+.01j)
        m=headroom_metrics(z)
        self.assertLess(m['ac_rms'],1e-12)
        self.assertFalse(headroom_passes(m))

    def test_single_over_limit_burst_is_rejected_even_with_low_quantile(self):
        z=np.full(10000,.01+.01j);z[555]=.99+0j
        m=headroom_metrics(z)
        self.assertLess(m['p999_component'],.02)
        self.assertFalse(headroom_passes(m))

    def test_safe_complex_capture_passes_without_changing_source(self):
        z=np.array([.03+.04j,-.03-.04j]*1000,dtype=np.complex64);original=z.copy()
        self.assertTrue(headroom_passes(headroom_metrics(z)))
        np.testing.assert_array_equal(z,original)

    def test_nonfinite_or_invalid_iq_is_rejected(self):
        for z in (np.array([],complex),np.array([1],float),np.ones((2,2),complex),np.array([complex(float('nan'),0)])):
            with self.assertRaises(ValueError):headroom_metrics(z)

    def test_invalid_metrics_never_pass_silently(self):
        for m in ({},{'max_component':float('nan'),'p999_component':0,'threshold_095_fraction':0}):
            with self.assertRaises(ValueError):headroom_passes(m)

    def test_changed_or_unobservable_lms_state_rejects_old_profile(self):
        for info in ({},{'lms_dc_state_at_open':'new'}):
            with self.assertRaises(ValueError):require_lms_calibration_state('old',info)
        self.assertEqual(require_lms_calibration_state('old',{'lms_dc_state_at_open':'old'}),'old')

    def test_gain_backoff_preserves_early_stages_until_late_stage_is_zero(self):
        g={'lna':6,'rxvga1':30,'rxvga2':12}
        self.assertEqual(lower_bladerf1_gain(g),dict(lna=6,rxvga1=30,rxvga2=6))
        self.assertEqual(g['rxvga2'],12)

    def test_full_gain_backoff_reaches_minimum_within_fourteen_states(self):
        g=dict(lna=6,rxvga1=30,rxvga2=30);seen=[]
        while g is not None:
            seen.append(sum(g.values()));g=lower_bladerf1_gain(g)
        self.assertLessEqual(len(seen),14)
        self.assertEqual(seen[-1],5)
        self.assertTrue(all(a>b for a,b in zip(seen,seen[1:])))

    def test_gain_backoff_rejects_nonphysical_stage_values(self):
        for g in (dict(lna=2,rxvga1=30,rxvga2=0),dict(lna=6,rxvga1=4,rxvga2=0),dict(lna=6,rxvga1=30,rxvga2=2)):
            with self.assertRaises(ValueError):lower_bladerf1_gain(g)

    def test_dc_anchor_cannot_cross_xb_path_or_gain(self):
        g=dict(lna=6,rxvga1=30,rxvga2=0)
        a=dict(center_hz=220e6,gains=g,validated=True)
        self.assertIsNone(matching_dc_anchor([a],g,250e6))
        self.assertIsNone(matching_dc_anchor([a],dict(lna=3,rxvga1=30,rxvga2=0),220e6))

    def test_dc_anchor_requires_validation_and_local_support(self):
        g=dict(lna=6,rxvga1=30,rxvga2=0);a=dict(center_hz=700e6,gains=g,validated=True)
        self.assertIs(matching_dc_anchor([a],g,710e6),a)
        self.assertIsNone(matching_dc_anchor([a],g,900e6))
        self.assertIsNone(matching_dc_anchor([dict(a,validated=False)],g,700e6))

if __name__=='__main__':unittest.main()
