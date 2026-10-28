#!/usr/bin/env python

from pathlib import Path
from setuptools import setup
from runpy import run_path

__version__ = run_path(str(Path(__file__).parent / 'soapypower' / 'version.py'))['__version__']

setup(
    name='soapy_power',
    version=__version__,
    description='Obtain power spectrum from SoapySDR devices (RTL-SDR, Airspy, SDRplay, HackRF, bladeRF, USRP, LimeSDR, etc.)',
    long_description=(Path(__file__).parent / 'README.rst').read_text(encoding='utf-8'),
    long_description_content_type='text/x-rst',
    author='Michal Krenek (Mikos)',
    author_email='m.krenek@gmail.com',
    url='https://github.com/djayuffe/soapy_power',
    project_urls={'Upstream': 'https://github.com/xmikos/soapy_power'},
    license='MIT',
    packages=['soapypower'],
    entry_points={
        'console_scripts': [
            'soapy_power=soapypower.__main__:main',
            'soapy_power_calibration=soapypower.calibration_cli:main',
            'soapy_power_noise_audit=soapypower.noise_audit:main'
        ],
    },
    python_requires='>=3.6',
    extras_require={'fft': ['scipy'], 'fftw': ['pyfftw']},
    install_requires=[
        'numpy',
        'simplesoapy>=1.5.0',
        'simplespectral'
    ],
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: Console',
        'Intended Audience :: End Users/Desktop',
        'Intended Audience :: Science/Research',
        'Intended Audience :: Telecommunications Industry',
        'License :: OSI Approved :: MIT License',
        'Natural Language :: English',
        'Operating System :: OS Independent',
        'Programming Language :: Python :: 3',
        'Topic :: Scientific/Engineering',
        'Topic :: Communications :: Ham Radio',
        'Topic :: Utilities'
    ]
)
