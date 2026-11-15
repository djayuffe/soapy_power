#!/usr/bin/env python3
"""Source hook for a SCPI CW generator (soapypower.auto_calibration protocol).

Protocol: one JSON request on stdin; one JSON acknowledgment on stdout echoing
every request field plus "confirmed": true. Non-zero exit is a refusal.
Configuration is the JSON file named by $SCPI_SOURCE_CONFIG (default:
scpi_source.json next to this script):

  {"host": "192.0.2.10", "port": 5025, "max_dbm": -10, "min_hz": 9e3, "max_hz": 3e9,
   "settle_s": 0.1, "timeout": 5, "expected_idn": "Rohde",
   "commands": {"level_set": "SOUR:POW {value:.3f}"}}

host, max_dbm, min_hz and max_hz are mandatory. max_dbm limits the generator
output; the receiver ceiling and path loss belong in the calibration/experiment
configuration. Supported states are 'on' and 'off'. Every connection clears the
error queue and switches RF off first; RF stays in the acknowledged state between
calls and the session runner issues the final OFF. The reply carries the
instrument identity, readback and the SHA-256 of the configuration used.

Run it with the interpreter that has soapy_power installed (shebang, or a
one-line wrapper script that execs that interpreter).
"""
import hashlib
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from soapypower.instruments import InstrumentError, ScpiLink, SignalGenerator  # noqa: E402

ALLOWED = {'host', 'port', 'timeout', 'max_dbm', 'min_hz', 'max_hz', 'settle_s', 'expected_idn',
           'commands', 'level_tolerance_db', 'frequency_tolerance_hz'}


def load_config():
    path = Path(os.environ.get('SCPI_SOURCE_CONFIG') or Path(__file__).with_name('scpi_source.json'))
    raw = path.read_bytes()
    config = json.loads(raw.decode('utf-8'))
    if not isinstance(config, dict) or set(config) - ALLOWED:
        raise ValueError('configuration must be an object with known fields only')
    for key in ('host', 'max_dbm', 'min_hz', 'max_hz'):
        if key not in config:
            raise ValueError('configuration missing mandatory field: ' + key)
    for key in ('max_dbm', 'min_hz', 'max_hz'):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(key + ' must be a finite number')
    if not isinstance(config['host'], str) or not config['host']:
        raise ValueError('host must be a nonempty string')
    return config, hashlib.sha256(raw).hexdigest()


def main(stdin=sys.stdin, stdout=sys.stdout, link_factory=None):
    link = generator = None
    try:
        request = json.loads(stdin.readline())
        if not isinstance(request, dict) or not isinstance(request.get('state'), str):
            raise ValueError('malformed source request')
        config, digest = load_config()
        link = (link_factory or ScpiLink)(config['host'], config.get('port', 5025), config.get('timeout', 5.0))
        generator = SignalGenerator(link, config['max_dbm'], config['min_hz'], config['max_hz'],
                                    commands=config.get('commands'), settle_s=config.get('settle_s', .05),
                                    level_tolerance_db=config.get('level_tolerance_db', .05),
                                    frequency_tolerance_hz=config.get('frequency_tolerance_hz', 1.0),
                                    expected_idn=config.get('expected_idn'))
        result = generator.apply(request['state'], request['rf_hz'], request.get('input_dbm'))
        stdout.write(json.dumps(dict(request, confirmed=True, instrument=generator.idn,
                                     readback=result, config_sha256=digest)) + '\n')
        stdout.flush()
        generator.close(off=False)
        return 0
    except (InstrumentError, ValueError, KeyError, TypeError, OSError) as error:
        print('source hook refused: {}'.format(error), file=sys.stderr)
        try:
            if generator is not None:
                generator.close(off=True)
            elif link is not None:
                link.close()  # identity not confirmed: send nothing further to an unknown instrument
        except Exception as cleanup:
            print('source hook cleanup failed: {}'.format(cleanup), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
