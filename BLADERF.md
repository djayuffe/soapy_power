# bladeRF receiver calibration

See the [complete calibration procedure](CALIBRATION_STEPS.md) and
[hardware-family characteristics](SDR_DEVICE_GUIDE.md) for preparation, validation
and conditions requiring new reference measurements.

Both bladeRF x40/x115 and bladeRF 2.0 micro can use the measured tone, hot/cold,
known-NF and terminated-baseline workflows in [CALIBRATION.md](CALIBRATION.md).
They operate on Soapy CF32 samples and actual receiver context; they do not infer
dBm from nominal gain, ADC bit count or the bladeRF model name.

## Select and characterize the actual RX chain

```sh
soapy_power --detect
soapy_power -d driver=bladerf --info
```

Select a serial with `-d driver=bladerf,serial=YOUR_SERIAL`. `-C 0` is RX channel
zero (typically labelled RX1 on the board); a supported `-C 1` is a separate
calibration configuration, not an interchangeable second copy. Query available
antenna, rate, bandwidth, gain elements and settings before choosing values.
Do not assume all bladeRF boards have the same controls.

Use fixed overall gain (`-g`) or explicit supported stages (`-G`). Nuand lists
`lna`, `rxvga1`, `rxvga2` on original bladeRF boards and `full` on bladeRF 2.0;
use the names/ranges actually advertised by the installed driver. AGC must be
off for absolute calibration. Keep the same firmware/FPGA, expansion board,
clock source, RF bandwidth, sample rate, physical path and external references
for reference captures and measurements. Disable any supported RX bias tee
before connecting a passive terminator.

An example capture, after confirming the requested settings are supported:

```sh
soapy_power -d driver=bladerf,serial=YOUR_SERIAL -C 0 -A RX \
  -f 915M -r 2M -w 1.5M -g 20 -b 4096 -n 256 -u 4 \
  --settle-samples 32768 --partial-policy reject -l -F json -O terminated.jsonl
soapy_power_calibration baseline terminated.jsonl \
  --temperature-k 293.15 --source-ohms 50 --receiver-ohms 50 \
  --start-hz 915100000 --stop-hz 915200000
```

Use actual termination temperature and impedances. A single terminator measures
a baseline, not both receiver gain and noise. For absolute calibration, capture
known tone on/off pairs over a rectangular RF/baseband grid using
[bladerf-tone.json](examples/calibration/bladerf-tone.json). Fill its null source
levels with actual measured values. Set each tune to `RF - baseband` for an
ordinary noninverted path. External converters/conjugation require the mapping
specified in CALIBRATION.md. Then:

```sh
soapy_power_calibration from-captures bladerf-tone.json -O bladerf-rx0-power.json
soapy_power_calibration validate bladerf-rx0-power.json
```

Use `--calibration bladerf-rx0-power.json` with the same device/channel/settings
and only within the measured RF/baseband domain. Repeat independently for every
RX channel and gain configuration. Verify independent frequencies, offsets and
input levels for interpolation, compression and drift.

## RX DC and IQ correction controls

SoapyBladeRF exposes normalized manual correction controls through the standard
Soapy RX APIs. This application now accepts:

```text
--manual-dc I,Q
--manual-iq GAIN,PHASE
```

Each component must be finite and in `[-1,1]`. These are **driver-normalized
hardware controls**, not IQ sample subtraction, dB gain, phase in radians, or an
automatic optimization algorithm. Supply coefficients established by a suitable
measured procedure; no universal default coefficients are provided. An example
of the syntax for already measured coefficients is
`--manual-dc=0.01,-0.02 --manual-iq=0.003,-0.004`; those example numbers are not
calibration values for your unit.

Requests are capability checked, applied before calibration preflight and after
retunes, and checked against readback. For the `bladerf` driver, DC and IQ
readback comparisons allow its respective normalized 1/2048 and 1/4096
quantization. Driver errors still fail acquisition: advertised support alone
does not guarantee every board/firmware combination accepts a control. Other
drivers use a strict readback tolerance.

If a driver advertises automatic correction for the selected control, explicitly
choose `--dc-correction off` or `--iq-correction off` when using its manual
counterpart; competing automatic/manual corrections are rejected. Leave default
policy unchanged if you are relying on driver calibration rather than supplying
manual coefficients. Requested and actual manual values are recorded under
`context.hardware_info.soapypower_manual_rx_corrections`, included in profile
identity/context matching and checked again after acquisition. A profile made
with manual corrections cannot silently be reused without them.

Some driver capability probes temporarily switch AGC while testing support.
Capabilities are now cached before acquisition instead of repeatedly probing
while collecting measurement context. Actual gain-mode and correction readbacks
remain fresh.

## Native Nuand calibrations and limits

Original bladeRF LMS/DC calibration tables, IQ correction controls, oscillator
trim, bladeRF 2.0 RFIC calibration and newer Nuand gain-calibration tables are
different procedures. Use the applicable Nuand/libbladeRF tools for your board,
with soapy_power disconnected. This program does not flash firmware, alter stored
oscillator calibration, create Nuand `.tbl` files or start loopback/TX calibration.

Nuand's newer gain tables adjust requested hardware gain as tuning changes.
They are not directly interchangeable with soapy_power's measured RF/baseband
PSD-offset profiles. If native gain compensation is enabled, establish and
archive that state first, measure the effective chain, then build the application
profile. If native compensation changes actual gain readback with frequency,
the strict grid builder rejects the mixed contexts. Prefer fixed hardware gain
with native compensation disabled, or separately characterized constant-context
profiles; do not bypass the matching checks. Do not add table corrections a second time or assume that a nominal
zero gain means calibrated dBm. Native table state is not universally exposed
by Soapy; retain table hashes and libbladeRF version in the profile reference
and revalidate after changing it.

Manual coefficient optimization, native table generation/import and physical
RF accuracy are not claimed. Software tests simulate the SoapyBladeRF interface;
no physical bladeRF has been tested here.

Primary references:

- [SoapyBladeRF settings/correction implementation](https://github.com/pothosware/SoapyBladeRF/blob/master/bladeRF_Settings.cpp)
- [Nuand gain stages and device checks](https://github.com/Nuand/bladeRF/wiki/Getting-Started%3A-Verifying-Basic-Device-Operation)
- [Nuand calibration commands](https://github.com/Nuand/bladeRF/blob/master/host/utilities/bladeRF-cli/src/cmd/doc/interactive-help.md)
- [libbladeRF gain calibration API](https://www.nuand.com/libbladeRF-doc/v2.6.0/group___f_n___c_a_l.html)
