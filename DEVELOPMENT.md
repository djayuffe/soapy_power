# Development, regression and release routine

## Reproducible local checks

Use a dedicated Python environment and save exact dependency versions with the
results. The mathematical tests do not need SoapySDR or an attached radio: they
replace only the hardware boundary while exercising real DSP, queues, writers
and CLI code.

```sh
python -m venv .venv
# Activate .venv using the platform's normal shell command.
python -m pip install numpy scipy pyfftw simplespectral build twine
python -m pip install --no-deps -e .
python tools/validate.py --report validation.json
python -m pip freeze > validation-dependencies.txt
```

The routine starts a fresh process for each installed FFT backend. Missing
optional backends are reported as unavailable. To **require** a backend use
`--backend numpy`, `--backend scipy` or `--backend fftw`; absence or a test
failure gives a nonzero exit. A report path must be new. A backend-specific test
may skip when another backend is selected; inspect the test log. To run the
suite directly: `python -m unittest discover -s tests -v`.

The GitHub workflow runs required NumPy, SciPy and FFTW checks on Linux, macOS
and Windows with Python 3.9 and 3.12. It also builds and checks distributions and
CLI help without SDR bindings. A configured CI matrix is not evidence that
those platforms have passed until the workflow actually runs. Python >=3.6
remains the declared source language minimum; these older versions require
compatible dependency releases and are not in the validation matrix.

Preserve numerical contracts when modifying DSP: two-sided complex-IQ Welch
normalization, linear averaging weighted by complete segment count, periodic
window energy/ENBW, exact framing across contiguous chunks, no framing across
retunes/loss/bursts, and explicit raw-baseband to RF coordinates. Test tone and
noise integrals, noncoherent leakage, odd/even FFT lengths, all inversion signs
and relevant precision/backend paths. Performance changes must preserve these
contracts. Compare realistic workloads rather than assuming one backend or
thread count is always fastest.

Test new failure paths as well as successful values: calibration coverage and
context mismatch, short reads/writes, timestamp gaps, output failure and worker
shutdown. Do not add hardware assumptions based only on a model name. Physical
acceptance follows [OPERATIONS.md](OPERATIONS.md).

## Build and distribution checks

```sh
python -m build
python -m twine check dist/*
```

Inspect the sdist for the tests, theory and operations guides, validation tool,
workflow and package sources. Build the wheel from the sdist as well as from the
checkout when changing packaging. In a second clean environment install the
wheel and its required Python dependencies, then verify:

```sh
soapy_power --help
soapy_power_calibration --help
```

Exercise the offline `context`, `build`, `validate` and `integrate` commands with
saved representative records and **measured** calibration fixtures before a
release. The tests contain clearly synthetic fixtures for mathematical checks;
they are not coefficients for a physical device. The receiver entry point needs
separately installed SoapySDR bindings/driver for actual acquisition.

Review the diff and run `git diff --check`. Update README/help when flags change,
RADIO_THEORY.md when numerical or hardware contracts change, OPERATIONS.md for
procedures and AUDIT.md with actual test environments and known limitations.
Keep legacy binary/CSV compatibility explicit; JSON has a versioned record and
separate missing/zero indices. A calibration schema change requires an explicit
compatibility policy and tests. Existing version-1 profiles without a measurement
CSV hash remain readable; the builder adds the optional provenance hash.

Before publishing a release, run the hardware/reference checks for each claimed
supported configuration and retain the exact source revision, dependency
versions, firmware/driver versions, capture context, measurement CSV, profile
hash and acceptance results. Only tag or publish after the intended checks pass.
No software-only routine can certify analog linearity, noise figure, absolute
power uncertainty or sustained device throughput.

## Additional integrity and calibration checks

The suite includes Decimal direct-DFT/aggregation references and queue, timing,
partial-result, settling, v3 checksum and measured-manifest fault tests. Run
`python tools/fuzz_integration.py --trials 100000` for randomized fractional
interval checks against exact rational float-input widths. This is a finite
seeded test, not an exhaustive proof for all floating inputs.

See [REMEDIATION.md](REMEDIATION.md) for per-finding disposition and remaining
architecture/metrology limits, and [CALIBRATION.md](CALIBRATION.md) for real
RTL-SDR/termination/tone/noise procedures.
