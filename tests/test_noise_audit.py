"""Noise diagnostics are deliberately separate from absolute power profiles."""
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import numpy as np
from soapypower.noise_audit import analyze_iq, analyze_file, main
from soapypower.calibration import PowerCalibration


class NoiseAuditTests(unittest.TestCase):
    def noise(self, size, seed=2028):
        rng = np.random.default_rng(seed)
        return (rng.normal(size=size) + 1j*rng.normal(size=size)) * math.sqrt(0.25/2)

    def test_white_complex_noise_normalization_and_linear_mean(self):
        samples = self.noise(512*160)
        report = analyze_iq(samples, 2.4e6, fft_size=512, segments=128)
        integrated = sum(report['noise_density']) * report['bin_width_hz']
        self.assertAlmostEqual(integrated, report['mean_segment_window_weighted_power'], places=13)
        self.assertLess(abs(integrated / 0.25 - 1), 0.03)
        off = np.array(report['noise_density'])[np.abs(report['baseband_hz']) > 2*report['bin_width_hz']]
        self.assertLess(abs(float(np.mean(off)) / (0.25/2.4e6) - 1), 0.03)
        self.assertEqual(report['window'], 'periodic_hann')
        self.assertFalse(report['absolute_calibration'])
        self.assertFalse(report['automatic_correction_applied'])
        self.assertIsNone(report['noise_figure_db'])
        self.assertNotIn('dc_region_excess', report['warnings'])
        self.assertIsNone(report['component_fraction_above_threshold'])
        with self.assertRaises(ValueError):
            PowerCalibration(report)

    def test_dc_detection_and_diagnostic_curve_exclusion(self):
        report = analyze_iq(self.noise(128*100)/10 + 0.5, 1024, fft_size=128, segments=100)
        self.assertIn('dc_region_excess', report['warnings'])
        self.assertGreater(report['dc_region']['excess_above_off_dc_median_db'], 20)
        for index in range(62, 67):
            self.assertIsNone(report['diagnostic_correction_db'][index])
        self.assertGreater(report['noise_density'][64], max(report['noise_density'][10:30]))

    def test_unique_amplitude_counts_exclude_overlap_and_unused_tail(self):
        samples = np.full(256, 1+1j, dtype=np.complex64)
        report = analyze_iq(samples, 1024, fft_size=64, segments=4, amplitude_threshold=0.9)
        self.assertEqual(report['analyzed_unique_samples'], 160)
        self.assertEqual(report['unexamined_samples'], 96)
        self.assertEqual(report['component_fraction_above_threshold'], 1)
        self.assertEqual(report['radial_fraction_above_threshold'], 1)
        self.assertAlmostEqual(report['max_digital_amplitude'], math.sqrt(2))
        self.assertEqual(report['max_component_amplitude'], 1)

    def test_segment_power_variation_and_spectral_shape_are_diagnostic(self):
        samples = self.noise(256*64)
        samples[len(samples)//2:] *= 10
        report = analyze_iq(samples, 1024, fft_size=256, segments=64, overlap=0)
        self.assertIn('nonstationary_segment_power', report['warnings'])
        self.assertGreater(report['segment_power_span_db'], 15)
        relative, curve = report['relative_noise_curve_db'], report['diagnostic_correction_db']
        for index, correction in enumerate(curve):
            if correction is not None:
                self.assertEqual(correction, -relative[index])

    def test_invalid_and_nonfinite_selected_inputs_are_rejected(self):
        for samples in (np.ones(128), np.ones(128, dtype=np.int16), np.ones((2,128), complex), np.ones(10,complex)):
            with self.assertRaises(ValueError):
                analyze_iq(samples, 1024, fft_size=64)
        for options in ({'sample_rate': 0}, {'overlap': 1}, {'segments': 0}, {'amplitude_threshold': float('nan')}, {'dc_exclude_bins': 32}):
            kwargs = dict(sample_rate=1024, fft_size=64); kwargs.update(options)
            with self.assertRaises(ValueError):
                analyze_iq(self.noise(1024), **kwargs)
        samples = self.noise(128); samples[5] = np.nan
        with self.assertRaises(ValueError):
            analyze_iq(samples, 1024, fft_size=64)
        # Unexamined samples are explicitly outside the analysis scope.
        samples[5] = 0; samples[-1] = np.nan
        report = analyze_iq(samples, 1024, fft_size=64, segments=1)
        self.assertEqual(report['unexamined_samples'], 64)

    def test_zero_input_has_no_fabricated_noise_floor(self):
        report = analyze_iq(np.zeros(256, complex), 1024, fft_size=64)
        self.assertEqual(report['noise_density'], [0.0]*64)
        self.assertEqual(report['log_noise_density_db'], [None]*64)
        self.assertIsNone(report['reference_density'])
        self.assertIn('zero_power_segments', report['warnings'])
        json.dumps(report, allow_nan=False)

    def test_npy_cli_no_overwrite_and_no_pickle_or_npz(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'iq.npy'; output=root/'diagnostic.json'
            np.save(source, self.noise(64*32).astype(np.complex64))
            result=subprocess.run([sys.executable, '-m', 'soapypower.noise_audit', str(source),
                                   '--sample-rate','1024','--fft-size','64','--segments','16','--output',str(output)],
                                  capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads(output.read_text()); self.assertEqual(len(report['input_sha256']),64)
            original=output.read_bytes()
            with self.assertRaises(SystemExit):
                main([str(source),'--sample-rate','1024','--fft-size','64','--output',str(output)])
            self.assertEqual(output.read_bytes(),original)
            archive=root/'archive.npz';np.savez(archive,iq=self.noise(128))
            with self.assertRaises(ValueError):
                analyze_file(archive,sample_rate=1024,fft_size=64)
            np.save(source,np.array([{'unsafe':'object'}],dtype=object))
            with self.assertRaises(ValueError):
                analyze_file(source,sample_rate=1024,fft_size=64)
