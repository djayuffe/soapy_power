# SDR hardware characteristics and calibration choices

[Assisted and automatic calibration](AUTO_CALIBRATION.md) can discover capabilities,
collect references and validate a profile using guided prompts or an explicit
source-control hook.

This guide covers major SoapySDR receiver families and important architectural
exceptions, reviewed against the linked vendor/driver documentation on
2026-10-29. It is not an inventory of every commercial SDR or a claim that every
model is tested. Product capability, installed Soapy plugin capability and this
application's exposed controls are separate questions. Discover your exact unit
and follow the [step-by-step procedure](CALIBRATION_STEPS.md).

## Architecture determines what must be measured

| Architecture | Calibration consequences |
| --- | --- |
| Tuner/low IF followed by digital conversion | Tuner bands, tracking filters, IF response, image rejection and decimation can change with tune/rate. The final complex output can still have a center artifact. |
| Direct-conversion RFIC | LO leakage, IQ imbalance, DC tracking and analog/baseband filters depend on RF, bandwidth and gain. Native correction is separate from absolute power calibration. |
| Superheterodyne with real IF sampling | Mixer/image response, IF filters and Nyquist mapping matter even when output is complex IQ. Do not assume all unwanted energy entered through the displayed RF band. |
| Direct RF/ADC sampling | External filtering, ADC range, input impedance, aliases and Nyquist zones define the response. Digital tuning does not supply analog image rejection. |
| Sound-card or software receiver | OS mixing, resampling, AGC and scaling may alter samples outside Soapy's control. Fixed-state absolute calibration may be unavailable. |
| Network wrapper | Server state, transport format/scaling, loss and forwarded metadata join the underlying receiver's configuration. |

CF32 is a sample representation, not RF volts or a universal dBFS definition.
ADC bit depth, host container width, nominal gain and factory frequency trim do
not establish input dBm. This program uses complex two-sided PSD normalization;
do not add a real-signal factor of two to already converted complex IQ.

## Family overview

Every row needs per-unit, per-path measurement for absolute power. “Fixed” below
means verified gain behavior and readbacks, not just an AGC checkbox.

| Family / variants | Distinctive state to preserve | First calibration priority |
| --- | --- | --- |
| RTL-SDR tuners; Blog V3/V4 | Tuner type/band, RF and digital AGC, direct-sampling/upconversion mode, bias tee | Terminated baseline, moderate fixed gain, CW level checks |
| Airspy R2 / Mini | Tuner filters, RF/IF gain strategy, hardware/host rate and decimation | Verify fixed gain and usable IF band; separate converter profile |
| Airspy HF+ / Discovery | Preselector, analog attenuation/AGC and embedded digital gain behavior | Prove constant response across all reference levels |
| SDRplay RSP1/1A/1B, RSP2/pro, RSPduo, RSPdx/dxR2 | RF input, gain reduction/LNA state, IF AGC, IF mode, decimation, notch/HDR state | Inspect actual API/plugin controls and port impedance |
| HackRF One / Pro | Board generation, RF amp/LNA/VGA, filters, sampling/firmware mode | Compression and image/alias testing before higher gain |
| bladeRF x40/x115 / 2.0 micro | RFIC generation, channel, expansion path, correction state | Native correction followed by measured power grid |
| LimeSDR LMS7002M boards | RX path/port, LNA/TIA/PGA, analog filters and digital stages | Native calibration at intended state, then independent references |
| PlutoSDR / AD936x-based boards | RFIC/board identity, manual/AGC state, tracking, FIR/rate/bandwidth | Establish reproducible gain and filter state |
| USRP B2xx / E3xx / N3xx RFIC families | Model-specific RFIC, channel, filter/clock/gain state | Use applicable UHD calibration procedure |
| USRP N2xx / X3xx and daughterboards | Actual daughterboard and subdevice, antenna, LO path | Characterize the selected frontend, not just the motherboard |
| USRP X410 / X420 / X440 | Different frontend architectures and native procedures | Use model-specific RF/IF/alias and calibration guidance |
| Red Pitaya / direct-ADC boards | Exact input impedance/range, coupling, FPGA/DDC/filter configuration | Establish connector voltage/power definition and anti-alias filtering |
| Hermes-Lite2 and related receivers | Board filters, gain/attenuator, FPGA/network path | Verify direct-sampling passband and alias response |
| FUNcube / audio-IQ paths | Model, gain/filter state, OS sample path and normalization | Exclude audio processing/resampling and verify fixed scaling |
| Remote / other plugins | Underlying hardware plus all server and wrapper state | Establish provenance, scaling and integrity before calibration |

### RTL-SDR: tuner paths, V3 direct sampling and V4 upconversion

Identify the actual tuner and board; cheap dongles are not interchangeable.
RF AGC and digital AGC can be separate. Use advertised fixed stages/settings and
verify repeatability with known input steps. Tuner band transitions and rate
changes need additional RF/baseband reference points. Low digital amplitude does
not exclude tuner overload from strong out-of-band signals.

Blog V4 uses internal HF upconversion: do not select the V3 direct-sampling
recipe for V4. Install compatible drivers. Bias-tee behavior must be checked at
the actual connector, including persistent configuration; account for any DC
block. The [manufacturer V4 guide](https://www.rtl-sdr.com/v4/) and
[SoapyRTLSDR settings](https://github.com/pothosware/SoapyRTLSDR/blob/master/Settings.cpp)
are the relevant starting points. Settings vary by installed build.

Use the [75 Ω termination discussion](CALIBRATION.md) for a television load.
A 75 Ω terminator is useful for repeatability diagnostics, but an ideal mismatch
calculation does not characterize the tuner's impedance or noise parameters.
Repeat after changing tuner band, mode, gain, rate, correction or external path.

### Airspy R2 and Mini: tuner/IF response and decimation

R2 uses a tuner/low-IF architecture and a 12-bit ADC; tracking filters and digital
processing form part of the measured response. Do not treat a wider host sample
container as more calibrated ADC resolution. Choose the actual board's supported
rates and gain controls rather than copying another Airspy model's settings.
See the [R2 hardware description](https://airspy.com/airspy-r2/).

An external SpyVerter or other converter adds its own gain/noise, LO drift,
sideband mapping and reference plane. Calibrate the combined chain or separately
measure and de-embed it. Factory frequency adjustment concerns the oscillator,
not RF power ([Airspy quick start](https://airspy.com/quickstart/)). Repeat after
rate/decimation, filter, gain strategy or converter changes.

### Airspy HF+ and Discovery: hidden gain behavior matters

HF+ is a different architecture from R2/Mini. Discovery uses IF sampling,
noise-shaping conversion, preselection and digital conversion. Its manufacturer
describes embedded digital IF AGC in addition to analog controls. Disabling an
exposed analog AGC therefore does not establish constant total digital gain.
See the [HF+ Discovery description](https://airspy.com/airspy-hf-discovery/).

Check measured response across all intended tone and hot/cold levels, including
strong blockers outside the analysis band. If internal gain behavior cannot be
held fixed or its scaling established, retain raw diagnostics and conditional
comparisons rather than constructing an asserted absolute fixed-gain profile.
Record preselector/rate state and verify again after tuning across filter bands.

### SDRplay: model, port, API and gain reduction

RSP models differ in ports, filtering, bias power and receiver configuration.
On multi-input units a high-impedance input is not equivalent to a 50 Ω SMA path.
RSPduo channels and operating modes need separate context; RSPdx HDR and notch
settings change the response. Preserve IF mode, decimation, RF port and filters.
See the [hardware comparison](https://www.sdrplay.com/resources/RSPcomparisonChart.pdf)
for the models it covers; use the specific newer model's manual for newer variants.

SDRplay API and plugin versions determine which controls exist. RF gain may be
expressed as gain reduction or an LNA state index rather than a physical positive
dB gain. Do not infer sign, range or stage equivalence from a slider name. Verify
IF AGC separately and distinguish receiver AGC from audio processing. The
[manufacturer gain/AGC guide](https://www.sdrplay.com/wp-content/uploads/2018/06/Gain_and_AGC_in_SDRuno.pdf)
explains those distinctions; SDRuno controls are not automatically Soapy options.

The [hardware API page](https://sdrplay.com/hardware-api/) lists support through
RSP1B and RSPdxR2 for API 3.15 and explicitly distinguishes nRSP-ST's SDRconnect
WebSocket interface. Do not assume nRSP-ST is accessible through the same Soapy
plugin. Check the installed [SoapySDRPlay3 driver](https://github.com/pothosware/SoapySDRPlay3).
Recalibrate for port, LNA/gain reduction, mode, notch, rate and bandwidth changes.

### HackRF One and Pro: different generations

Preserve the distribution of RF amplifier, LNA and VGA settings, not merely their
sum. Check unwanted signals, analog compression and sampling/filter choices.
Half-duplex hardware is not a simultaneously transmitting reference source and
receiver. Do not copy a damage ceiling into a source-level recipe. Consult the
[HackRF documentation](https://hackrf.readthedocs.io/en/latest/) and
[Soapy gain implementation](https://github.com/pothosware/SoapyHackRF/blob/master/HackRF_Settings.cpp).

HackRF Pro adds hardware/firmware features, including precision modes and DC
artifact handling, beyond One. Availability through a particular Soapy version
must be established independently; default compatibility does not expose every
new mode. Do not assume One's ADC/mode or DC recipe covers Pro. See the
[Pro hardware page](https://greatscottgadgets.com/hackrf/pro/). Treat a sampling
mode or firmware change as a new calibration configuration.

### bladeRF: original boards and 2.0 micro

Original x40/x115 and 2.0 micro have different frontend/correction behavior and
gain controls. Calibrate RX channels independently and preserve expansion boards,
ports, firmware/FPGA and clock setup. Factory oscillator trim is not a power
certificate. Nuand's [2.0 micro page](https://www.nuand.com/bladeRF-2.0-micro/)
and [board support material](https://www.nuand.com/support/) identify the hardware.

Follow [BLADERF.md](BLADERF.md) for capability-checked manual DC/IQ coefficients,
quantization and measured tone templates. Historical original-board correction
instructions must not be applied indiscriminately to the newer RFIC. Complete
native correction first, then measure on/off power; repeat if correction state,
channel, gain distribution, rate, bandwidth or expansion path changes.

### LimeSDR: RF path and LMS7002M state

LMS7002M-based boards expose multiple analog and digital stages; connectors and
RX paths are board-specific. LNA/TIA/PGA settings with the same nominal total
gain need not have the same noise figure, headroom or response. Analog filter
calibration, rate changes, digital filtering and native DC/IQ calibration can
change the measurement state. Record every exposed stage and actual readback.

Use the appropriate board and software generation's native procedure, then
verify reference levels again. LimeSuite and other driver generations are not
interchangeable command sets. See the [Mini hardware guide](https://wiki.myriadrf.org/LimeSDR-Mini)
and [LMS7002M documentation](https://github.com/myriadrf/LMS7002M-docs).
A matched load and known CW grid remain necessary for an application power scale.

### PlutoSDR and other AD936x boards

The AD936x chain includes analog gain/filtering and digital rate/filter stages,
with manual/automatic gain and tracking functions. Preserve FIR, rate, bandwidth,
tracking state and manual gain. A sample-rate change may alter more than FFT
bin spacing. See [Pluto's signal-chain description](https://wiki.analog.com/university/tools/pluto/users/understanding)
and [ADI's device-control reference](https://analogdevicesinc.github.io/pyadi-iio/devices/adi.ad936x.html).
Those APIs do not imply identical Soapy setting names.

Identify firmware, libiio transport/context and actual board/RFIC. A custom
AD9361/AD9363 board, AntSDR-style board or modified Pluto can have different
matching, filtering, oscillator and gain behavior despite sharing a chip family.
Do not rely on unofficial tuning extensions as specifications. Validate fixed
scaling with known levels and remeasure after any frontend/filter change.

### Ettus / USRP: use the frontend, not the brand, as the unit of calibration

| Hardware group | Specific consideration |
| --- | --- |
| B2xx; E310/E320; N300/N310 RFIC devices | Use their RFIC-specific native calibration and gain behavior; channels and paths remain separate. |
| N200/N210; X300/X310; other daughterboard systems | Record daughterboard, subdevice and antenna. BasicRX/LFRX-style paths differ fundamentally from tunable mixer frontends. |
| Traditional RFX/WBX/SBX/CBX/UBX and related boards | Use the applicable UHD frontend calibration and actual supported corrections; do not transfer tables between serials. |
| N320/N321 and other newer frontends | Follow exact model/UHD guidance; do not generalize the N300/N310 RFIC procedure. |
| X410 with ZBX | Superheterodyne frontend and real-IF conversion; IF/image behavior is not the same as X440 direct RF sampling. |
| X420 | UHD documents a distinct IQ/DC correction procedure; older daughterboard tools are not universal. |
| X440 | Direct-sampling frontend; external conditioning and Nyquist-zone/alias management are central. |

Sources: [UHD frontend calibration](https://files.ettus.com/manual/page_calibration.html),
[ZBX architecture](https://files.ettus.com/manual/page_zbx.html),
[X440 hardware](https://kb.ettus.com/X440), and
[X4xx configuration](https://files.ettus.com/manual/page_usrp_x4xx.html).

Follow [ETTUS.md](ETTUS.md) for discovery, channel/subdevice identity, clock and
lock readbacks, correction controls and the distinction between native frontend
calibration, UHD power-reference calibration and measured application profiles.
Never apply both absolute conversions. A 10 MHz reference and PPS do not alone
prove coherent channels or synchronized epochs; this application does not perform
multi-board synchronization. Repeat after clock/FPGA/rate/frontend configuration
changes, even when the RF center remains the same.

### Red Pitaya and other direct ADC inputs

Check the **input** specification, not the output impedance. Original STEMlab
125-14 inputs are high impedance and DC coupled, with selectable voltage ranges;
this is not a generic 50 Ω power-meter input. Other variants can differ. See the
[original board specifications](https://redpitaya.com/stemlab-125-14/) and
[hardware manual](https://redpitaya.readthedocs.io/en/latest/developerGuide/hardware/ORIG_GEN/125-14/top.html).

Record jumper/range, coupling, input network, ADC/FPGA image, DDC and decimation.
Define whether the reference is connector voltage or power delivered to a known
termination. Add a properly characterized termination/network if that is the
intended reference plane. Use analog filtering to reject aliases; an FFT cannot
separate frequencies that have already aliased onto each other.

### Hermes-Lite2 and other networked direct-sampling receivers

Hermes-Lite2 uses direct sampling and FPGA/network transport. Preserve board
filters, gain/attenuator state and firmware; an accessory filter bank is part of
the reference chain. Verify image/alias rejection across the intended RF band,
and transport integrity at the selected rate. See the [project hardware](https://hermeslite.com/)
and [hardware/software sources](https://github.com/softerhardware/Hermes-Lite2).
The existence of a compatible transport plugin does not guarantee complete gain,
clock or timestamp readbacks for application calibration.

### FUNcube and audio-derived IQ

Original FUNcube and Pro+ are different receivers; preserve the exact model,
filter and LNA/mixer/IF gain state. The Pro+ documentation describes its own IQ
sample path and filter banks: see the [hardware description](https://www.funcubedongle.com/?page_id=1073)
and [manual](https://www.funcubedongle.com/MyImages/FCD2ManualV4.pdf).

Where samples pass through an OS audio subsystem, disable mixing, level controls,
effects and resampling or explicitly characterize the resulting path. An audio
sample container is not a voltage calibration. If sample scaling or gain cannot
be held fixed and recorded, use diagnostic results rather than an asserted
absolute profile. Check IQ orientation and LO artifacts with a known tone.

### Remote devices, instruments and other plugins

A remote connection adds server-side gain/filter settings, driver versions,
transport format, scaling and packet continuity to the receiver's configuration.
SoapyRemote supports transport format/scale choices; preserve the actual setup
([project](https://github.com/pothosware/SoapyRemote),
[protocol definitions](https://github.com/pothosware/SoapyRemote/blob/master/common/SoapyRemoteDefs.hpp)).
Do not invent application flags for settings controlled by the external server.

For any other receiver or instrument plugin, establish this minimum contract:
complex IQ with known sampling/normalization; correct RF/baseband mapping;
selected physical path; reproducible gains and correction state; adequate
readbacks; complete, stationary data; and independent known references. An
instrument reporting already calibrated power is not automatically a source of
uncalibrated IQ suitable for this pipeline. The [SoapySDR project](https://github.com/pothosware/SoapySDR/wiki)
is the starting point for locating a driver, not a certification of this contract.

## Record once, verify every session

Keep the filled [measurement checklist](CALIBRATION_STEPS.md), device discovery,
actual capture metadata, reference-plane diagram and manufacturer limits with
each profile. Revalidate when a hidden state is suspected, even if metadata did
not change. If an unsupported control or unavailable readback blocks a workflow,
retain the raw diagnostic evidence and document the limitation rather than
claiming a universal calibration from a model name.
