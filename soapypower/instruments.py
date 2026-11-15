"""Reference-source control over SCPI/TCP with hard safety limits and readback.

No third-party dependency. The generator is never left in an unknown state: any
failure while applying a state attempts RF OFF, and every applied value is read
back and compared before the state is acknowledged. Limits are mandatory.

Typical use is through tools/scpi_source_hook.py, which implements the
auto_calibration SourceController hook protocol with this class.
"""
import math
import socket
import time

from soapypower.numerics import finite

DEFAULT_COMMANDS = {
    'idn': '*IDN?', 'cls': '*CLS', 'opc': '*OPC?', 'error': 'SYST:ERR?',
    'freq_set': 'FREQ {value:.6f}', 'freq_get': 'FREQ?',
    'level_set': 'POW {value:.3f}', 'level_get': 'POW?',
    'out_on': 'OUTP ON', 'out_off': 'OUTP OFF', 'out_get': 'OUTP?',
}


class InstrumentError(RuntimeError):
    pass


class ScpiLink:
    """Newline-terminated ASCII over TCP (port 5025 raw-socket convention)."""

    def __init__(self, host, port=5025, timeout=5.0, connect=socket.create_connection):
        self.timeout = finite(timeout, 'timeout', positive=True)
        try:
            self._socket = connect((host, int(port)), self.timeout)
        except OSError as error:
            raise InstrumentError('cannot connect to {}:{}: {}'.format(host, port, error)) from None
        self._socket.settimeout(self.timeout)
        self._buffer = b''

    def write(self, command):
        if '\n' in command or '\r' in command:
            raise InstrumentError('command must be a single line')
        self._socket.sendall(command.encode('ascii') + b'\n')

    def read(self):
        while b'\n' not in self._buffer:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise InstrumentError('instrument closed the connection')
            self._buffer += chunk
            if len(self._buffer) > 65536:
                raise InstrumentError('instrument reply too long')
        line, self._buffer = self._buffer.split(b'\n', 1)
        return line.decode('ascii', 'replace').strip()

    def query(self, command):
        self.write(command)
        return self.read()

    def close(self):
        try:
            self._socket.close()
        except OSError:
            pass


def _truth(reply):
    return reply.strip().upper() in ('1', 'ON', '+1')


class SignalGenerator:
    """CW reference with frequency/level limits and verified readback.

    max_dbm is the *source output* limit; the caller supplies receiver damage
    limits through the calibration configuration separately. min/max_hz bound
    the frequency. Ordering when turning on: RF off, set frequency, set level,
    read both back, then RF on, then read OUTP? back.
    """

    def __init__(self, link, max_dbm, min_hz, max_hz, commands=None, settle_s=0.05,
                 level_tolerance_db=0.05, frequency_tolerance_hz=1.0, expected_idn=None, sleep=time.sleep):
        self.link = link
        self.max_dbm = finite(max_dbm, 'max_dbm')
        self.min_hz = finite(min_hz, 'min_hz', positive=True)
        self.max_hz = finite(max_hz, 'max_hz', positive=True)
        if self.max_hz <= self.min_hz:
            raise ValueError('max_hz must exceed min_hz')
        self.commands = dict(DEFAULT_COMMANDS, **(commands or {}))
        self.settle = finite(settle_s, 'settle_s', nonnegative=True)
        self.level_tolerance = finite(level_tolerance_db, 'level tolerance', positive=True)
        self.frequency_tolerance = finite(frequency_tolerance_hz, 'frequency tolerance', positive=True)
        self._sleep = sleep
        self.idn = link.query(self.commands['idn'])
        if expected_idn is not None and expected_idn not in self.idn:
            raise InstrumentError('unexpected instrument identity: ' + self.idn)
        self._cmd('cls')  # stale errors from earlier sessions must not be attributed to this one
        self.off()

    def _cmd(self, key, **fields):
        self.link.write(self.commands[key].format(**fields))

    def _wait(self):
        self.link.query(self.commands['opc'])
        self._sleep(self.settle)

    def _check_error_queue(self):
        reply = self.link.query(self.commands['error'])
        if not reply.startswith(('0', '+0')):
            raise InstrumentError('instrument error: ' + reply)

    def off(self):
        self._cmd('out_off')
        self._wait()
        if _truth(self.link.query(self.commands['out_get'])):
            raise InstrumentError('RF output did not switch off')

    def tone(self, hz, dbm):
        hz, dbm = finite(hz, 'frequency', positive=True), finite(dbm, 'level')
        if not self.min_hz <= hz <= self.max_hz:
            raise ValueError('frequency outside configured generator range')
        if dbm > self.max_dbm:
            raise ValueError('level {:.2f} dBm exceeds configured maximum {:.2f} dBm'.format(dbm, self.max_dbm))
        try:
            self.off()
            self._cmd('freq_set', value=hz)
            self._cmd('level_set', value=dbm)
            self._wait()
            got_f = float(self.link.query(self.commands['freq_get']))
            got_p = float(self.link.query(self.commands['level_get']))
            if (not math.isfinite(got_f) or not math.isfinite(got_p) or abs(got_f - hz) > self.frequency_tolerance
                    or abs(got_p - dbm) > self.level_tolerance or got_p > self.max_dbm + self.level_tolerance):
                raise InstrumentError('readback mismatch: {:.3f} Hz / {:.3f} dBm'.format(got_f, got_p))
            self._check_error_queue()
            self._cmd('out_on')
            self._wait()
            if not _truth(self.link.query(self.commands['out_get'])):
                raise InstrumentError('RF output did not switch on')
            self._check_error_queue()
        except BaseException as error:
            try:
                self.off()
            except BaseException as cleanup:
                raise InstrumentError('{}; RF OFF could not be verified afterwards: {}'.format(error, cleanup)) from error
            raise
        return {'frequency_hz': got_f, 'level_dbm': got_p}

    def apply(self, state, rf_hz, level_dbm=None):
        """Source-controller states 'on' (needs a level) and 'off'.

        'cold'/'terminated' are refused: an RF-off generator port is not a characterized
        thermal termination or noise-source cold state. Use a dedicated hook for those.
        """
        if state == 'on':
            if level_dbm is None:
                raise ValueError("state 'on' needs a level")
            return dict(self.tone(rf_hz, level_dbm), output='on')
        if state == 'off':
            if level_dbm is not None:
                raise ValueError("state 'off' takes no level")
            self.off()
            return {'output': 'off'}
        raise ValueError('a CW generator cannot realise state {!r}; use a dedicated hook'.format(state))

    def close(self, off=True):
        try:
            if off:
                self.off()
        finally:
            self.link.close()


class SimulatedLink:
    """In-memory SCPI instrument for tests and dry runs (deterministic)."""

    def __init__(self, drift_db=0.0, stuck_on=False, error='0,"No error"', idn='SIM,Generator,0,1',
                 stale_error=None, output=0):
        self.state = {'FREQ': 1e8, 'POW': -100.0, 'OUTP': output}
        self.drift, self.stuck_on, self.error, self._idn = drift_db, stuck_on, error, idn
        self.stale = stale_error  # queued before this session; *CLS clears it
        self.log = []
        self._reply = []

    def write(self, command):
        self.log.append(command)
        head, _, arg = command.partition(' ')
        if head == 'FREQ':
            self.state['FREQ'] = float(arg)
        elif head == 'POW':
            self.state['POW'] = float(arg) + self.drift
        elif head == 'OUTP':
            if arg == 'OFF' and self.stuck_on:
                return
            self.state['OUTP'] = 1 if arg == 'ON' else 0
        elif command == '*IDN?':
            self._reply.append(self._idn)
        elif command == '*CLS':
            self.stale = None
        elif command == '*OPC?':
            self._reply.append('1')
        elif command == 'SYST:ERR?':
            self._reply.append(self.stale or self.error)
        elif head == 'FREQ?':
            self._reply.append(repr(self.state['FREQ']))
        elif head == 'POW?':
            self._reply.append(repr(self.state['POW']))
        elif head == 'OUTP?':
            self._reply.append(str(self.state['OUTP']))
        else:
            raise InstrumentError('unknown simulated command: ' + command)

    def read(self):
        return self._reply.pop(0)

    def query(self, command):
        self.write(command)
        return self.read()

    def close(self):
        pass
