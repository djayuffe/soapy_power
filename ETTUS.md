# Ettus / USRP calibration

Use matching UHD and SoapyUHD installations. Discover actual capabilities before
selecting channels, antennas, clock sources or sensors:

```sh
soapy_power --detect
soapy_power -d 'driver=uhd,type=b200,serial=REPLACE' --info
```

`driver=uhd` selects the Soapy registry; the reported driver key may instead be a
model such as `b200`. `rx_subdev=A:0` is an optional device argument where that
subdevice exists. Select the actual RX channel with `-C` and antenna with `-A`.
Profiles now retain the RX frontend mapping and motherboard serial fallback,
as well as daughterboard details supplied by the driver. Recapture profiles
when their recorded hardware context no longer matches.

## Clock and reference controls

`--clock-source`, `--time-source` and `--master-clock-rate` set supported Soapy
controls and verify readback before acquisition. Changing clock configuration
reapplies the requested sample rate and nonzero bandwidth; processing uses the
actual accepted rate. Unsupported requests fail explicitly.

For example, only on hardware advertising these sources, with an external
reference physically attached, append:

```text
--clock-source external --reference-lock-sensor ref_locked
```

Choose sensor names from `--info`, not this example alone. Reference lock reads
a motherboard sensor; `--lock-sensor` reads the selected RX sensor, commonly
`lo_locked`. Both requested gates must pass before samples are accepted.
`--tune-delay` and discarded samples still apply. A time-source selection does
not set an epoch, prove UTC accuracy, or synchronize multiple USRPs. Current
SoapyUHD clock/time operations use its exposed motherboard scope (typically
motherboard zero); this application does not implement multi-board timing.

## Three different calibrations

1. Native frontend DC/IQ calibration suppresses offsets and images. It does not
   establish input dBm. UHD tools and supported methods differ by device:
   B2xx/E3xx/N3xx RFIC calibration, traditional daughterboard calibration and
   X420 correction procedures are not interchangeable. Follow the applicable
   UHD manual with this application disconnected. No native TX calibration,
   EEPROM changes or calibration-file generation run automatically here.
2. Measured application power calibration establishes a reference plane using
   a known tone, calibrated hot/cold noise source, or independently known NF.
   Follow [CALIBRATION.md](CALIBRATION.md) and start with
   [ettus-tone.json](examples/calibration/ettus-tone.json). Null source powers
   deliberately require real measurements. Keep gain fixed and measure every
   RF/baseband grid cell, including signal-off observations.
3. UHD power-reference calibration is a separate, hardware-dependent API and
   gain policy. USRPs are not universally factory-calibrated power meters.
   Do not add its dBm conversion to an application profile or assume opaque
   native calibration files are importable. Native power holding may change
   gain on retuning and invalidate a fixed-gain application profile.

Explicit `--dc-correction` and `--iq-correction` selections are capability
checked and reapplied after retuning. Automatic DC suppression may attenuate
wanted energy near zero IF. Offset tuning and measured exclusions are often
preferable for those measurements. Manual DC/IQ coefficients require supported
APIs and explicit disabling of conflicting automatic correction. IQ coefficients
are complex correction values, not dB/radians. If documented hardware
quantization requires it, `--manual-correction-tolerance` sets an absolute
component readback tolerance; use the smallest justified value. The tolerance
is recorded in the profile context. Never infer a universal USRP coefficient
resolution or use a loose tolerance to hide an ignored setter.

Repeat calibration for changes in serial/daughterboard/subdevice/channel,
antenna, gain elements, rate, bandwidth, clock configuration, native correction
state or external RF chain. Check actual context after each tune. Temperature,
compression, mismatch, source accuracy and drift still require measurements.

Place a matched termination at the chosen reference plane, including before an
external LNA when characterizing the whole chain. A 75-ohm terminator on a
50-ohm input needs mismatch accounting; it is not an equivalent matched load.
At high gain, test known source-level steps and gain steps: low ADC amplitude
does not rule out analog compression. See the diagnostic workflow below.

[Noise diagnostics and colored workflow](NOISE_CALIBRATION_AUDIT.md) distinguish
noise shape from gain calibration. No physical USRP was available for this
release; software tests exercise simulated device boundaries and real FFTs.

References: [UHD frontend calibration](https://files.ettus.com/manual/page_calibration.html),
[UHD power calibration](https://files.ettus.com/manual/page_power.html),
[SoapyUHD implementation](https://github.com/pothosware/SoapyUHD/blob/master/SoapyUHDDevice.cpp).
