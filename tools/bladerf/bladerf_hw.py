"""Receive-only bladeRF1 capture helper on soapy_power with stage, DC and LMS-state readback.

Run with SOAPY_SDR_PLUGIN_PATH pointing at a SoapyBladeRF module built with
soapybladerf-0.4.1-lms-state.patch (it reports lms_dc_state_at_open), and the libbladeRF it
was linked against first on the library path. Nothing here transmits or writes flash.
"""
import io
import logging
import time

import numpy as np

from soapypower import power
from soapypower.power import SoapyPower
from soapypower.receiver_quality import (headroom_metrics, headroom_passes, lower_bladerf1_gain,
                                         require_lms_calibration_state)

from bladerf_profile import DC_QUANTUM, STAGES

power.psd.simplespectral.use_pyfftw = False
logging.getLogger('simplesoapy').setLevel(logging.ERROR)

STREAM_ARGS = {'buffers': '128', 'buflen': '65536', 'transfers': '16'}
SAMPLE_RATE = 28e6
TRANSIENT = ('SDR overflow:', 'SDR read failed or timed out', 'SDR stream ended abruptly')
HOP_SAMPLES = 35 * 4096          # 5.12 ms at 28 MS/s, the survey's acquisition per hop
SETUP = dict(base_buffer_size=65536, max_buffer_size=262144, fft_overlap=.5, tune_delay=.02,
             settle_samples=32768, require_timestamps=True, reopen_stream=True, partial_policy='reject',
             precision='single', max_threads=4, max_queue_size=4)


class Receiver(SoapyPower):
    def __init__(self, device, lms_state, stream_args=None):
        super().__init__(soapy_args=device, sample_rate=SAMPLE_RATE, bandwidth=SAMPLE_RATE,
                         gain={'lna': 3, 'rxvga1': 23, 'rxvga2': 0}, antenna='RX', force_sample_rate=True,
                         output=io.StringIO(), output_format='json', stream_args=stream_args or STREAM_ARGS)
        self.saved = []
        self.collecting = False
        self.settling = False
        self.retries = []
        self.hardware_info = dict(self.device.device.getHardwareInfo())
        self.lms_state = require_lms_calibration_state(lms_state, self.hardware_info)
        self.setup(4096, HOP_SAMPLES // 4096, **SETUP)

    def _settle(self, tuned_at):
        self.collecting = False
        self.settling = True
        n = super()._settle(tuned_at)
        self.settling = False
        self.saved = []
        self.collecting = True
        return n

    def _read_chunk(self, output):
        n = super()._read_chunk(output)
        if n and self.collecting:
            self.saved.append(output[:n].copy())
        return n

    def hop(self, center_hz, gains, dc):
        """One verified hop: returns (freq, psd_db, iq, start, stop). Retries settle-time overflows."""
        gains = {k: float(gains[k]) for k in STAGES}
        self._requested_gain = gains
        self._frontend.manual_dc = (float(dc[0]), float(dc[1]))
        for attempt in range(4):
            self.settling = False
            try:
                future, start, stop = self.psd(center_hz)
                freq, psd_db = future.result()
                break
            except RuntimeError as error:
                # Transient stream faults (overflow, a failed USB transfer, an abrupt end) discard the
                # whole hop; the next psd() reopens the stream and acquires it again from scratch.
                if not any(text in str(error) for text in TRANSIENT) or attempt == 3:
                    raise
                self.retries.append(dict(center_hz=float(center_hz), error=str(error), during_settling=self.settling,
                                         measurement_emitted=False))
                time.sleep(.05)
        iq = np.concatenate(self.saved)
        integrity, context = self._last_integrity, self._last_context
        if not (integrity['continuity'] == 'verified' and integrity['measurement_status'] == 'complete'
                and integrity['timestamp_coverage'] == 1 and iq.size == HOP_SAMPLES):
            raise RuntimeError('hop integrity check failed at %.0f Hz' % center_hz)
        if context['gains'] != gains:
            raise RuntimeError('stage gain readback differs: %r != %r' % (context['gains'], gains))
        actual = context['hardware_info'].get('soapypower_manual_rx_corrections', {}).get('DCOffset', {}).get('actual')
        if actual is None or max(abs(a - b) for a, b in zip(actual, dc)) > DC_QUANTUM + 1e-12:
            raise RuntimeError('manual DC readback differs: %r != %r' % (actual, dc))
        require_lms_calibration_state(self.lms_state, context['hardware_info'])
        return freq, psd_db, iq, start, stop

    def guarded_hop(self, center_hz, gains, dc, dc_for_gains=None, max_reductions=14):
        """hop() under the live headroom guard: lowers late VGA, early VGA, then LNA until it passes.

        After a reduction the DC is looked up again with dc_for_gains(gains) (None -> zero).
        Returns (freq, psd_db, iq, start, stop, gains, dc, quality, reductions).
        """
        reductions = []
        for _ in range(max_reductions):
            freq, psd_db, iq, start, stop = self.hop(center_hz, gains, dc)
            quality = headroom_metrics(iq)
            if headroom_passes(quality):
                return freq, psd_db, iq, start, stop, gains, dc, quality, reductions
            reductions.append(dict(center_hz=float(center_hz), gains=dict(gains), dc=list(dc), quality=quality,
                                   measurement_emitted=False))
            lower = lower_bladerf1_gain(gains)
            if lower is None:
                break
            gains = {k: float(v) for k, v in lower.items()}
            dc = (dc_for_gains(gains) if dc_for_gains else None) or [0.0, 0.0]
        raise RuntimeError('headroom policy could not be met at %.0f Hz' % center_hz)

    def close(self):
        try:
            self.stop()
        finally:
            self._frontend.manual_dc = (0.0, 0.0)
            self._requested_gain = {'lna': 3.0, 'rxvga1': 23.0, 'rxvga2': 0.0}
            try:
                self._apply_gain()
                self._frontend.apply_corrections()
            except Exception:  # restoring defaults must not hide the original error
                pass

