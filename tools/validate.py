#!/usr/bin/env python3
"""Run the mathematical and simulated-device suite per installed FFT backend."""
import argparse
import importlib.util
import json
from pathlib import Path
import platform
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def worker(backend):
    import numpy
    import simplespectral
    if backend == 'fftw' and not simplespectral.fft_pyfftw:
        raise SystemExit('requested FFTW backend is unavailable')
    if backend == 'scipy' and not simplespectral.fft_scipy:
        raise SystemExit('requested SciPy backend is unavailable')
    simplespectral.use_pyfftw = backend == 'fftw'
    simplespectral.fft_scipy = backend == 'scipy'
    versions = {'python': platform.python_version(), 'numpy': numpy.__version__, 'backend': backend}
    for name in ('scipy', 'pyfftw'):
        if importlib.util.find_spec(name) is not None:
            versions[name] = __import__(name).__version__
    print(json.dumps(versions, sort_keys=True), flush=True)
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=['numpy', 'scipy', 'fftw'], help='require one particular backend')
    parser.add_argument('--report', help='write a new JSON report (never overwrite an existing path)')
    parser.add_argument('--worker', choices=['numpy', 'scipy', 'fftw'], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)
    selected = [args.backend] if args.backend else ['numpy', 'scipy', 'fftw']
    results = []
    for backend in selected:
        dependency = {'numpy': 'numpy', 'scipy': 'scipy', 'fftw': 'pyfftw'}[backend]
        if not args.backend and backend != 'numpy' and importlib.util.find_spec(dependency) is None:
            results.append({'backend': backend, 'status': 'unavailable'})
            print('Skipping unavailable backend: ' + backend, flush=True)
            continue
        print('Validating backend: ' + backend, flush=True)
        process = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', backend], cwd=str(ROOT))
        results.append({'backend': backend, 'status': 'passed' if process.returncode == 0 else 'failed'})
    if args.report:
        from soapypower.files import write_json
        write_json(args.report, {'python': sys.version, 'platform': platform.platform(), 'results': results})
    return int(any(item['status'] == 'failed' for item in results))


if __name__ == '__main__':
    raise SystemExit(main())
