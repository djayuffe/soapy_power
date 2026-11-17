# RTL2832U / R820T antenna scans

Probe the actual card before selecting a range. This experiment's R820T reports approximately 24-1764 MHz, a single `TUNER` gain element, and no manual DC/IQ correction controls. It cannot measure the bladeRF upper bands. Do not transfer bladeRF stage gains or LMS register corrections.

Enumerate native fractional gains, disable tuner and digital AGC, then validate selected gains on independent uncentered IQ. The experiment tested all 29 gains at 13 frequencies in two randomized rounds, added two discrete steps of margin, and learned exact-center reductions from a five-minute baseline before the final scan. The retained raw component limits were maximum <0.85, P99.9 <0.60 and zero hits at >=0.95. Digital headroom does not prove analog linearity.

Native five-second `rtl_test` counter trials passed at 2.048 and 2.56 MS/s, but lost samples at 2.88 and 3.2 MS/s. The final rate is 2.56 MS/s, FFT 4096, periodic Hann: 625 Hz bins and 937.5 Hz ENBW. These are finite session results, not universal card specifications. Each hop retains 65536 complex samples after stream restart and settling discard. The historical scan retained 20 kHz <= |IF| <0.96 MHz with 0.96 MHz center spacing. That spacing left exact grid-center holes where the neighbouring taper reached zero. New scans should use `--ensure-coverage --usable-halfwidth 960k --dc-mask-width 40k`; the planner reduces the step and verifies strictly positive off-mask support. Fuse overlapping PSD in linear power and retain missing cells explicitly.

SoapyRTLSDR stream keys are `bufflen` (bytes), `buffers` and `asyncBuffs`; these differ from the bladeRF keys and meanings. The experiment used 32768, 32 and 8 respectively. A zero-ms settling request still discards at least 16384 complex samples and has stream-start overhead. Validate such settings against bracketed slower captures before promotion.

Check native gain units: librtlsdr uses tenths of a dB. Convert requests with rounding *after* multiplication by ten, and read native state divided by ten. Casting the dB value before multiplication truncates fractional settings. The isolated experiment patch and complete hardware gain validation table accompany the report; native state is still not a measured RF conversion gain.

The driver can set `HAS_TIME` using its software count of host-delivered samples. This is not independent hardware timing. The scanner therefore records `continuity=unverifiable_software_clock`, preserves `driver_first_sample_ns` / `driver_last_sample_ns`, leaves hardware sample times null, and rejects `--require-timestamps`. A successful counter preflight does not certify absence of hidden loss in later antenna captures.

Fit any software mean/phase model on one IQ partition and evaluate it without refitting on a disjoint second partition. Preserve source IQ and raw PSD. A smoother origin or more circular IQ population does not establish calibrated sensitivity, image rejection or phase noise. Absolute power, NF, frequency accuracy, analog compression, antenna response and RF transfer phase require known physical references.

The reusable runtime/analysis commands now live in [receiver quality examples](../receiver_quality/README.md); the campaign settings remain historical evidence, not universal defaults.
