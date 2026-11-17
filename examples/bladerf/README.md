# bladeRF x40 / XB-200 receive scans

Use the device's named stages rather than assuming a single gain number has the same meaning across radios. On the tested bladeRF1, the driver's nominal RX gain is:

```text
lna + rxvga1 + rxvga2 - 6 dB
```

The tested stage ranges are LNA 0/3/6 dB, RXVGA1 5-30 dB in 1 dB steps, and RXVGA2 0-30 dB in 3 dB steps. This is a nominal driver convention, not a measured absolute conversion gain. Prefer early gain before late VGA gain when headroom permits, and check strong signals across the whole tuned bandwidth.

## Reproducible single-center starting point

```sh
soapy_power -d 'driver=bladerf,xb200=custom' -A RX \
  -f 3500M -r 28M -w 28M --force-rate \
  -G lna=6,rxvga1=30,rxvga2=24 \
  --stream-args buffers=128,buflen=65536,transfers=16 \
  -b 4096 -t .005 --tune-delay .02 --settle-samples 32768 \
  --reopen-stream --require-timestamps --partial-policy reject \
  -s 65536 -S 262144 --no-pyfftw -u 1 -F json -O capture.jsonl
```

This is a high-frequency starting point from one antenna experiment, not a safe full-band gain setting. Reduce gain for strong signals. The XB-200 `custom` path requires the RXFILT/RXFILT-ANT jumper or a suitable external filter. The driver handles its mixing-path I/Q swap; do not add another conjugation or 1248 MHz external LO offset.

The stream arguments use a native Soapy kwargs object, avoiding Python-dictionary conversion problems observed in the tested binding. Requested transport values are retained in JSON measurement context. Driver stream-argument meanings vary; the tested SoapyBladeRF implementation uses `buflen` as samples internally despite its older help labeling it bytes.

## Manual correction and validation

For bladeRF1 RX, normalized manual DC readback is quantized in steps of **1/64**, not 1/2048. `--manual-dc=I,Q` uses normalized hardware controls, not an IQ sample mean. Fit register values for the actual device, frequency, gain stages, bandwidth and temperature; then validate them on independent captures before use. JSON records retain requested and actual correction values.

The x40 driver does not expose an automatic DC/IQ correction mode or an LO-lock sensor. Leave automatic mode at `default` and use settling plus timestamp checks. Do not request a nonexistent lock sensor or treat `--dc-correction on` as a portable correction switch.

Do not estimate physical I/Q image correction by forcing antenna data to be circular. Correlated or asymmetric I/Q may be caused by real received signals. A known reference is needed to separate receiver image imbalance from signal structure.

## Measured band starting profile

| Band MHz | LNA | RXVGA1 | RXVGA2 | Nominal gain |
|---|---:|---:|---:|---:|
| 42-237.5 | 6 | 30 | 0 | 30 |
| 237.5-600 | 6 | 20 | 0 | 20 |
| 600-1200 | 6 | 30 | 0 | 30 |
| 1200-2000 | 6 | 30 | 12 | 42 |
| 2000-2800 | 6 | 30 | 18 | 48 |
| 2800-3800 | 6 | 30 | 24 | 54 |

These values were selected from 798 captures with one particular antenna and RF environment. A subsequent full-grid scan found strong signals between anchors and required additional reductions. The generated `validated-full-grid-profile.json` and a live sample-headroom guard supersede this coarse table for that experiment. Do not apply this table as a calibration certificate or a universal optimum.

When changing gain across a scan, preserve raw PSD and stage readback. Combining spectra after subtracting nominal gain is only approximate gain referencing; it is not calibrated input power or noise figure. Display the actual gain map alongside the spectrum.

At 28 MS/s, FFT 4096 gives 6835.9375 Hz spacing and a periodic-Hann ENBW of 10253.90625 Hz. The requested 87.524414% hop overlap is quantized to **87.5%**, producing 3.5 MHz center spacing. Use the actual frequency plan in analysis.

Primary references: [Nuand device configuration](https://www.nuand.com/libbladeRF-doc/v2.5.0/boilerplate.html), [bladeRF1 controls](https://github.com/Nuand/bladeRF/blob/master/host/libraries/libbladeRF/include/bladeRF1.h), [LMS DC scaling](https://github.com/Nuand/bladeRF/blob/master/fpga_common/src/lms.c), [XB-200 setup](https://github.com/Nuand/bladeRF/wiki/Getting-Started%3A-XB200-Transverter-Board).

## State-bound refinement and safety gates

The v2 experiment validated all 19 manual DC anchors using independent captures. A later per-center measurement showed that an anchor is right only near its own frequency: reusing anchors left a median residual |mean| of 0.028 against 0.0026 with a value measured at every center (see [tools/bladerf](../../tools/bladerf/README.md), which also runs these surveys from the repository). Corrections require exact stage-gain agreement, the same XB-200 mix/bypass domain, an anchor within 150 MHz, and the same ten LMS calibration register values observed at device open. Otherwise use zero manual DC or reject a mismatched receiver state. The custom Soapy driver records `lms_dc_state_at_open`; changing internal calibration while a device handle is open invalidates that snapshot and requires reopening.

The generated `receiver-profile-final.json` belongs to this specific receiver state and antenna experiment. It does not replace a known RF reference. The workspace runner uses the dense gain profile, caches live gain reductions, and retains raw, uncentered component max <0.85, P99.9 <0.60 and no components at the 0.95 diagnostic gate. Centering IQ before checking these gates can hide clipping.

The tested final setup retains 20 ms settling, the full 1074-center plan and 87.5% overlap: faster settling and reduced schedules failed preservation criteria. No phase-based image cancellation or automatic notches were justified by the retuned captures. Requested stream arguments are stored under `hardware_info.soapypower_stream_args`, preserving the strict power-calibration context schema.

From the original experiment workspace, `work/venv/bin/python work/blade_lms_state.py apply-saved` explicitly restores the saved volatile LMS values with a current-state backup and readback verification. This is not a new calibration and does not establish validity after temperature, antenna or hardware changes. Then `work/venv/bin/python work/scan_bladerf_v2.py --duration 300` creates a fresh timestamped acquisition. The experiment scripts and isolated driver build are required; the report ZIP is not an installable SDR runtime.
