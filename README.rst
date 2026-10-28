soapy_power
===========

Obtain power spectrum from SoapySDR devices (RTL-SDR, Airspy, SDRplay, HackRF, bladeRF, USRP, LimeSDR, etc.)

Requirements
------------

- `Python 3 <https://www.python.org>`_
- `NumPy <http://www.numpy.org>`_
- SoapySDR Python bindings and the driver for your SDR (installed separately)
- `SimpleSoapy <https://github.com/xmikos/simplesoapy>`_
- `SimpleSpectral <https://github.com/xmikos/simplespectral>`_
- Optional: `pyFFTW <https://github.com/pyFFTW/pyFFTW>`_ (optional FFTW backend)
- Optional: `SciPy <https://www.scipy.org>`_ (optional SciPy FFT backend)

SciPy and pyFFTW provide optional accelerated FFT backends. Performance and
memory use depend on FFT size, backend, thread count and the host system.

Measurement and maintenance
---------------------------

See `OPERATIONS.md <OPERATIONS.md>`_ for receiver bring-up, architecture/DC
choices, physical calibration and recurring reference checks.
`RADIO_THEORY.md <RADIO_THEORY.md>`_ defines the mathematical contracts;
`DEVELOPMENT.md <DEVELOPMENT.md>`_ describes backend tests and package checks.

See `BLADERF.md <BLADERF.md>`_ for bladeRF RX calibration and manual DC/IQ controls.

See `CALIBRATION.md <CALIBRATION.md>`_ for RTL-SDR/75 ohm termination,
antennas, measured tone and hot/cold calibration workflows.
`REMEDIATION.md <REMEDIATION.md>`_ documents JSON v2, binary v3, exact targets
and partial/timing/queue integrity policies.

The offline ``soapy_power_calibration`` command provides ``context``, ``build``,
``validate``, ``integrate``, ``baseline``, ``compare`` and ``from-captures`` routines without opening a radio. Run
``soapy_power_calibration --help`` for commands. Profiles are constructed from
actual measured tone powers; nominal receiver gain is not a dBm calibration.

Usage
-----
::

    usage: soapy_power [-h] [-f Hz|Hz:Hz] [-O FILE | --output-fd NUM]
                       [-F {json,rtl_power,rtl_power_fftw,soapy_power_bin,soapy_power_bin_v3}]
                       [-q] [--debug] [--detect] [--info] [--version]
                       [-b BINS | -B Hz] [-n REPEATS | -t SECONDS | -T SECONDS]
                       [-c | -u RUNS | -e SECONDS] [-d DEVICE] [-C CHANNEL]
                       [-A ANTENNA] [-r Hz] [-w Hz] [-p PPM]
                       [-g dB | -G STRING | -a] [--lnb-lo Hz] [--lnb-high-side]
                       [--dc-correction {default,on,off}]
                       [--iq-correction {default,on,off}]
                       [--clock-source CLOCK_SOURCE] [--time-source TIME_SOURCE]
                       [--master-clock-rate HZ]
                       [--manual-correction-tolerance MANUAL_CORRECTION_TOLERANCE]
                       [--manual-dc I,Q] [--manual-iq GAIN,PHASE]
                       [--tune-args KEY=VALUE,...] [--device-settings STRING]
                       [--force-rate] [--force-bandwidth] [--tune-delay SECONDS]
                       [--reset-stream] [-o PERCENT | -k PERCENT] [-s BUFFER_SIZE]
                       [-S MAX_BUFFER_SIZE] [--even | --pow2] [--max-threads NUM]
                       [--max-queue-size NUM] [--fft-threads FFT_THREADS]
                       [--no-pyfftw] [--partial-policy {emit,reject,legacy}]
                       [--acquisition-rounding {exact,block}]
                       [--settle-samples SETTLE_SAMPLES]
                       [--lock-sensor LOCK_SENSOR]
                       [--reference-lock-sensor REFERENCE_LOCK_SENSOR]
                       [--lock-timeout LOCK_TIMEOUT]
                       [--timestamp-tolerance-samples TIMESTAMP_TOLERANCE_SAMPLES]
                       [--require-timestamps]
                       [--workspace-budget WORKSPACE_BUDGET]
                       [--allow-estimated-dc] [--calibration JSON]
                       [--dc-mask-width HZ] [--dc-offset HZ]
                       [--precision {single,double}] [--iq-conjugate] [-l] [-R]
                       [-D {none,constant}]
                       [--fft-window {boxcar,hann,hamming,blackman,bartlett,kaiser,tukey}]
                       [--fft-window-param FLOAT] [--fft-overlap PERCENT]

    Obtain a power spectrum from SoapySDR devices

    Main options:
      -h, --help            show this help message and exit
      -f Hz|Hz:Hz, --freq Hz|Hz:Hz
                            center frequency or frequency range to scan, number
                            can be followed by a k, M or G multiplier (default:
                            1420405752)
      -O FILE, --output FILE
                            output to file (incompatible with --output-fd, default
                            is stdout)
      --output-fd NUM       output to existing file descriptor (incompatible with
                            -O)
      -F {json,rtl_power,rtl_power_fftw,soapy_power_bin,soapy_power_bin_v3}, --format {json,rtl_power,rtl_power_fftw,soapy_power_bin,soapy_power_bin_v3}
                            output format (default: rtl_power)
      -q, --quiet           limit verbosity
      --debug               detailed debugging messages
      --detect              detect connected SoapySDR devices and exit
      --info                show info about selected SoapySDR device and exit
      --version             show program's version number and exit

    FFT bins:
      -b BINS, --bins BINS  number of FFT bins (incompatible with -B, default:
                            512)
      -B Hz, --bin-size Hz  bin size in Hz (incompatible with -b)

    Averaging:
      -n REPEATS, --repeats REPEATS
                            integration length in FFT-sized sample blocks
                            (incompatible with -t and -T, default: 1600)
      -t SECONDS, --time SECONDS
                            integration time (incompatible with -T and -n)
      -T SECONDS, --total-time SECONDS
                            total integration time of all hops (incompatible with
                            -t and -n)

    Measurements:
      -c, --continue        repeat the measurement endlessly (incompatible with -u
                            and -e)
      -u RUNS, --runs RUNS  number of measurements (incompatible with -c and -e,
                            default: 1)
      -e SECONDS, --elapsed SECONDS
                            scan session duration (time limit in seconds,
                            incompatible with -c and -u)

    Device settings:
      -d DEVICE, --device DEVICE
                            SoapySDR device to use
      -C CHANNEL, --channel CHANNEL
                            SoapySDR RX channel (default: 0)
      -A ANTENNA, --antenna ANTENNA
                            SoapySDR selected antenna
      -r Hz, --rate Hz      sample rate (default: 2000000.0)
      -w Hz, --bandwidth Hz
                            filter bandwidth (default: 0)
      -p PPM, --ppm PPM     frequency correction in ppm
      -g dB, --gain dB      total gain (incompatible with -G and -a, default:
                            37.2)
      -G STRING, --specific-gains STRING
                            specific gains of individual amplification elements
                            (incompatible with -g and -a, example:
                            LNA=28,VGA=12,AMP=0
      -a, --agc             enable Automatic Gain Control (incompatible with -g
                            and -G)
      --lnb-lo Hz           LNB LO frequency, negative for upconverters (default:
                            0)
      --lnb-high-side       use RF = LNB LO - IF and mirror the IQ frequency axis
      --dc-correction {default,on,off}
                            driver DC correction mode (default leaves device
                            behavior unchanged)
      --iq-correction {default,on,off}
                            driver IQ correction mode (default leaves device
                            behavior unchanged)
      --clock-source CLOCK_SOURCE
                            supported motherboard clock source
      --time-source TIME_SOURCE
                            supported time source; does not reset/synchronize
                            device time
      --master-clock-rate HZ
      --manual-correction-tolerance MANUAL_CORRECTION_TOLERANCE
                            explicit absolute tolerance for normalized manual
                            correction readback
      --manual-dc I,Q       normalized RX hardware DC correction (capability
                            checked)
      --manual-iq GAIN,PHASE
                            normalized driver IQ correction controls, not
                            dB/radians
      --tune-args KEY=VALUE,...
                            SoapySDR frequency arguments, e.g. OFFSET=250000
                            (driver dependent)
      --device-settings STRING
                            SoapySDR device settings (example: biastee=true)
      --force-rate          ignore list of sample rates provided by device and
                            allow any value
      --force-bandwidth     ignore list of filter bandwidths provided by device
                            and allow any value
      --tune-delay SECONDS  time to delay measurement after changing frequency (to
                            avoid artifacts)
      --reset-stream        reset streaming after changing frequency (to avoid
                            artifacts)

    Crop:
      -o PERCENT, --overlap PERCENT
                            percent of overlap when frequency hopping
                            (incompatible with -k)
      -k PERCENT, --crop PERCENT
                            percent of crop when frequency hopping (incompatible
                            with -o)

    Performance options:
      -s BUFFER_SIZE, --buffer-size BUFFER_SIZE
                            base buffer size (number of samples, 0 = auto,
                            default: 0)
      -S MAX_BUFFER_SIZE, --max-buffer-size MAX_BUFFER_SIZE
                            maximum buffer size (number of samples, -1 =
                            unlimited, 0 = auto, default: 0)
      --even                use only even numbers of FFT bins
      --pow2                use only powers of 2 as number of FFT bins
      --max-threads NUM     maximum number of PSD threads (0 = auto, default: 0)
      --max-queue-size NUM  maximum size of PSD work queue (-1 = unlimited, 0 =
                            auto, default: 0)
      --fft-threads FFT_THREADS
                            threads per pyFFTW transform (default: 1; PSD workers
                            run in parallel)
      --no-pyfftw           don't use pyfftw library even if it is available (use
                            scipy.fftpack or numpy.fft)

    Other options:
      --partial-policy {emit,reject,legacy}
                            partial acquisition policy (default: reject
                            calibrated, emit metadata-capable raw)
      --acquisition-rounding {exact,block}
      --settle-samples SETTLE_SAMPLES
      --lock-sensor LOCK_SENSOR
                            driver RX lock sensor name; requires a true readback
      --reference-lock-sensor REFERENCE_LOCK_SENSOR
                            motherboard reference-lock sensor, separate from RX LO
                            lock
      --lock-timeout LOCK_TIMEOUT
      --timestamp-tolerance-samples TIMESTAMP_TOLERANCE_SAMPLES
      --require-timestamps
      --workspace-budget WORKSPACE_BUDGET
                            application FFT workspace bytes per worker; excludes
                            driver/input queues and backend cache
      --allow-estimated-dc
      --calibration JSON    measured, settings-matched power calibration profile;
                            output dBm/Hz or mW/Hz
      --dc-mask-width HZ    exclude this baseband DC/spur bandwidth as NaN
                            (default: disabled)
      --dc-offset HZ        spur center in raw IQ baseband before conjugation
                            (requires DC mask)
      --precision {single,double}
                            FFT computation precision (default: double)
      --iq-conjugate        conjugate IQ to reverse baseband spectral orientation
      -l, --linear          linear power values instead of logarithmic
      -R, --remove-dc       interpolate the central PSD bin (requires boxcar;
                            estimates rather than measures its power)
      -D {none,constant}, --detrend {none,constant}
                            remove mean value from data to cancel DC bias
                            (default: none)
      --fft-window {boxcar,hann,hamming,blackman,bartlett,kaiser,tukey}
                            Welch's method window function (default: hann)
      --fft-window-param FLOAT
                            shape parameter of window function (required for
                            kaiser and tukey windows)
      --fft-overlap PERCENT
                            Welch's method overlap between segments (default: 50)

Numerical accuracy and tuning
-----------------------------

PSD values use a two-sided Welch density for complex IQ samples. Linear values
have units of input power per Hz; logarithmic values are 10*log10(density).
They are not calibrated dBm without device-specific calibration. Integration of
linear density over frequency gives signal power. Zero power is represented as
negative infinity in logarithmic output.

Averages use the number of complete Welch segments and compensated float64
accumulation. FFT computation defaults to double precision; ``--precision single``
is available when throughput matters more than the additional precision. Log conversion happens after averaging.
The radio acquisition path preserves overlap across reads, so the PSD does not
depend on how an uninterrupted record is divided into buffers. Independent
records submitted through the library API remain independent. Acquisition targets exactly ``bins*repeats`` samples by default; use
``--acquisition-rounding block`` for the old block-rounded policy. Metadata
distinguishes actual/requested counts, partial reasons, hardware sample clocks
and host UTC read bounds. Calibrated partial acquisitions are rejected by default.

A Hann window with 50 percent segment overlap is a useful general default.
Boxcar gives narrow bins for coherent tones but has more leakage. Other windows
trade frequency resolution for leakage suppression; there is no universal best
window. Constant detrending removes each segment's mean. DC interpolation only
replaces the central bin and is mainly useful with boxcar.

Install an accelerated backend with ``pip install '.[fft]'`` (SciPy) or
``pip install '.[fftw]'`` (pyFFTW). ``--no-pyfftw`` selects the available fallback.
``--pow2`` rounds FFT lengths upward and therefore changes frequency resolution.
Automatic live settings use up to four PSD workers and 32 pending chunk slots,
and acquisition chunks of about 100 ms (at least one device block, at most
100 MiB unless a device block itself is larger). Choose explicit thread and
queue limits for available RAM: ``--max-buffer-size`` limits
one acquisition buffer, not the whole process, and FFT workspaces and queued
copies also consume memory. ``--max-threads 1 --max-queue-size 1`` minimizes
concurrent PSD jobs. Live acquisition refuses an unbounded queue and fails explicitly on overload.

Binary format version 2 always writes little-endian float32 payloads. Readers
reject truncated records, unsupported versions and invalid payload alignment.
Writers drain on shutdown and report background failures. Supplied output file
descriptors and streams remain owned by the caller.

Run hardware-free regression tests with::

    python -m unittest discover -s tests -v

SciPy enables an additional reference comparison. Real device tuning, gain
calibration, driver behavior and sustained acquisition require hardware testing.

Welch batch sizes respect a conservative application workspace budget per
worker (64 MiB by default), excluding queued input, driver and backend caches. Windows are DFT-periodic for both odd and even FFT
lengths; Tukey alpha=0 is boxcar and alpha=1 is Hann. ``--fft-threads`` controls
threads per pyFFTW transform (default 1) independently of ``--max-threads`` PSD
workers, avoiding nested CPU oversubscription by default.

Radio and IQ conventions
------------------------

Input is complex CF32 IQ from SoapySDR, interpreted as I + jQ. A positive
baseband tone maps above the tuned frequency by default; no one-sided factor
of two is applied. ``--iq-conjugate`` explicitly reverses baseband orientation
when an IQ convention or external signal chain requires it. It does not
estimate or automatically correct IQ imbalance.

Low-side translation uses RF = LO + IF with ``--lnb-lo``. Negative LO offsets
remain useful for upconverters. ``--lnb-high-side`` uses RF = LO - IF, mirrors
the frequency axis and emits ascending RF bins. An even FFT has one Nyquist
endpoint, so its mirrored interval uses the opposite endpoint. Output follows
the device's actual reported tuning; quantized tuning can change requested
frequency coverage. Driver-reported rates and frequencies are not an external
clock calibration.

The acquisition reader handles short packets and retains unused packet samples
between chunks. It aborts a measurement on reported overflow, abrupt termination
or detected hardware timestamp gaps. Without hardware timestamps, unreported
gaps cannot be detected. Time limits and interrupts are checked between device
reads and frequency hops; a blocking driver call or queued computation can delay
return. Existing asynchronous work drains before shutdown.

FFT bin spacing is fs/N. Window equivalent noise bandwidth (ENBW), logged at
setup, is fs*sum(w**2)/sum(w)**2; it is 1.5 bins for periodic Hann and one bin
for boxcar. These are distinct quantities: narrower FFT spacing alone does not
remove window leakage or resolve arbitrarily close signals. Raw segment counts
are not counts of statistically independent averages when windows overlap.

A coherent unit complex tone integrates to unit input power; a unit real cosine
integrates to one half. Integrating an arbitrary windowed record returns its
window-weighted mean-square power. Cropping, DC interpolation and detrending
change the measured band or signal, so full-band energy identities must be
applied before those operations. ``-n`` specifies integration length in FFT-sized
sample blocks; Welch segment count also depends on overlap.

Neither uncalibrated IQ amplitude nor device gain is enough to label values as
dBm, dBm/Hz or calibrated dBFS. An explicit measured ``--calibration`` profile
now enables input-referred dBm/Hz or mW/Hz with strict context/range checks. Absolute measurements need a defined IQ scale,
receiver gain/frequency calibration, impedance convention and external reference.
AGC, clipping, LO feedthrough, IQ images and analog filtering can change a
spectrum. Mean removal and DC-bin interpolation can also remove real signals
near zero frequency. No blind IQ/image correction or guessed absolute calibration is applied. Logarithms are taken after averaging linear power; this does not
make the dB estimator statistically unbiased.

Binary v2 retains its float32 payload and has no embedded units, gain, window,
IQ orientation or calibration metadata. JSON output records those fields. Preserve command/settings alongside the
file. Text output retains float64 values. Binary reads validate frequency
geometry and limit payloads to 256 MiB by default; the formatter's constructor
can change or disable that limit for trusted larger records.

Architecture, DC and measured calibration
-----------------------------------------

See `RADIO_THEORY.md <RADIO_THEORY.md>`_ for the architecture matrix, mathematical
contract, DC choices, reference-plane calibration procedure, profile schema and
measurement limits.

New options are explicit and optional:

- ``--dc-correction default|on|off`` and ``--iq-correction default|on|off`` use
  capability-checked driver controls. Default preserves driver correction policy.
- ``--tune-args OFFSET=250000`` forwards frequency arguments to supporting drivers.
  Do not assume a particular artifact position from this value alone.
- ``--dc-mask-width HZ --dc-offset HZ`` exclude a known raw-baseband artifact as
  NaN without inventing power. Conjugation and high-side conversion are handled.
- ``--remove-dc`` now requires boxcar and at least three bins. Use exclusion for
  window-spread contamination rather than one-bin interpolation.
- ``--calibration profile.json`` applies a measured RF-by-baseband response grid
  only for matching actual settings, fixed gain and frequencies inside its range.
  It converts density to dBm/Hz (or mW/Hz with ``--linear``).
- ``-F json`` emits one standard JSON record per hop with units, hardware context,
  segment/window metadata, calibration provenance and explicit masked/zero bins.

Profiles must come from reference measurements; none are supplied or guessed.
Fixed gain disables supported AGC and is reapplied after retuning. Explicit driver
correction modes are also reapplied after tuning. Complete hardware bursts stop
a sweep instead of being joined across a discontinuity. FFTW transform thread
counts are now per PSD instance rather than a library-global setting.

Ettus/USRP and noise diagnostics
--------------------------------

See `ETTUS.md <ETTUS.md>`_ for USRP clock, reference-lock and calibration
controls. `NOISE_CALIBRATION_AUDIT.md <NOISE_CALIBRATION_AUDIT.md>`_ contains
the colored Mermaid workflow and offline complex IQ noise analysis. Run
``soapy_power_noise_audit --help`` for the diagnostic CLI.

Receiver and external-LNA characterization
------------------------------------------

See `RECEIVER_CHARACTERIZATION.md <RECEIVER_CHARACTERIZATION.md>`_ for
multi-temperature receiver/cascade fitting, CW validation and correlated
uncertainty. Run ``soapy_power_calibration characterize --help``.

Optional relative PSD equalization
----------------------------------

Use ``soapy_power_calibration equalize --help`` for bounded, opt-in shaping
from a measured calibration profile. See `EQUALIZATION.md <EQUALIZATION.md>`_
for regularization, residual bias and separation from absolute power calibration.

Validated response flattening
-----------------------------

See `FLATTENING.md <FLATTENING.md>`_ for attenuation-only equalization, usable
bands, uncertainty-weighted response smoothing and independent holdout checks.

Passive reference diagnostics
-----------------------------

See `PASSIVE_REFERENCES.md <PASSIVE_REFERENCES.md>`_ for overlapping IF
response fitting, gain-state comparisons and selected-IQ diagnostics.
These are relative/diagnostic tools, not public-signal absolute calibration.
