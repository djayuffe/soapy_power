"""Build the optional native telemetry adapter in a new isolated directory.

Pass a clean SoapyUHD checkout at the supported revision. No system install,
firmware write, network access or source checkout mutation is performed.
"""
import argparse
import subprocess
from pathlib import Path

REVISION = 'd6f7feaf016e1559ea479007196def1cc55a463c'

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--cmake-arg', action='append', default=[])
    args = parser.parse_args()
    source, destination = args.source.resolve(), args.destination.resolve()
    patch = Path(__file__).resolve().parents[2] / 'patches/soapyuhd-native-telemetry.patch'
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=no'], text=True).strip()
    if revision != REVISION or dirty:
        parser.error('source must be a clean supported revision: ' + REVISION)
    if destination.exists() or source == destination or source in destination.parents:
        parser.error('destination must be new and outside the source checkout')
    destination.mkdir(parents=True)
    copied, build, prefix = destination/'source', destination/'build', destination/'prefix'
    subprocess.run(['git', '-C', str(source), 'archive', '--format=tar', '-o', str(destination/'source.tar'), 'HEAD'], check=True)
    copied.mkdir()
    subprocess.run(['tar', '-xf', str(destination/'source.tar'), '-C', str(copied)], check=True)
    subprocess.run(['git', 'apply', '--check', str(patch)], cwd=str(copied), check=True)
    subprocess.run(['git', 'apply', str(patch)], cwd=str(copied), check=True)
    subprocess.run(['cmake', '-S', str(copied), '-B', str(build), '-DBUILD_SOAPY_SUPPORT=OFF',
                    '-DCMAKE_NO_SYSTEM_FROM_IMPORTED=ON', '-DCMAKE_INSTALL_PREFIX='+str(prefix)] + args.cmake_arg, check=True)
    subprocess.run(['cmake', '--build', str(build), '--parallel', '4'], check=True)
    subprocess.run(['ctest', '--test-dir', str(build), '--output-on-failure'], check=True)
    subprocess.run(['cmake', '--install', str(build)], check=True)
    print('Isolated prefix:', prefix)
    print('Select its lib/SoapySDR/modules<ABI> with SOAPY_SDR_PLUGIN_PATH; keep the SDK and runtime ABI matched.')

if __name__ == '__main__':
    main()
