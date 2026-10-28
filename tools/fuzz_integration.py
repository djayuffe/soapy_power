#!/usr/bin/env python3
"""Check fractional integration against exact float-input rational intervals."""
import argparse
from fractions import Fraction
import math
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from soapypower.records import integrate_record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trials', type=int, default=10000)
    parser.add_argument('--seed', type=int, default=8395)
    args = parser.parse_args()
    if args.trials < 1:
        parser.error('trials must be positive')
    rng = random.Random(args.seed)
    worst = 0.0
    for trial in range(args.trials):
        first = rng.choice((0.0, 1e6, 1e9))
        step = rng.choice((0.125, 1.0, 1000.0))
        size = 16
        low = first + (rng.randrange(1, size-2)-0.5)*step
        high = low + rng.uniform(0.01, 1.9)*step
        low = math.nextafter(low, rng.choice((-math.inf, math.inf))) if sys.version_info >= (3, 9) else low
        record = dict(version=2, start_hz=first, stop_hz=first+size*step, step_hz=step,
                      first_bin_center_hz=first, bin_width_hz=step, coverage_start_hz=first-step/2,
                      coverage_stop_hz=first+(size-0.5)*step, samples=64, unit='mW/Hz',
                      power=[2.0]*size, masked_bins=[], estimated_bins=[], zero_power_bins=[])
        actual = integrate_record(record, low, high)['power']
        expected = float(2 * (Fraction(high) - Fraction(low)))
        error = abs(actual-expected) / abs(__import__('numpy').spacing(expected))
        worst = max(worst, error)
        if error > 8:
            raise AssertionError((trial, low, high, actual, expected, error))
    print('{} fractional intervals passed; maximum error {:.1f} ULP'.format(args.trials, worst))


if __name__ == '__main__':
    main()
