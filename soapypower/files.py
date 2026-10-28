"""Strict JSON and atomic, explicit-overwrite artifact writing."""
import json
import math
import os
import tempfile
from pathlib import Path


def _constant(value):
    raise ValueError('non-finite JSON constant: {}'.format(value))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field: {}'.format(key))
        result[key] = value
    return result


def _float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('JSON number exceeds float64 range')
    return value


def loads_json(text):
    return json.loads(text, parse_constant=_constant, parse_float=_float, object_pairs_hook=_object)


def load_json(path):
    return loads_json(Path(path).read_text(encoding='utf-8'))


def _atomic_write(path, emit, overwrite=False):
    """Publish only after the complete producer succeeds and the file is fsynced."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            emit(stream)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, str(path))
        else:
            os.link(temporary, str(path))
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def write_json(path, value, overwrite=False):
    """Publish strict JSON atomically; existing files require explicit overwrite.

    Default hard-link publication never replaces an existing destination. A
    filesystem without hard links fails rather than weakening that guarantee.
    Explicit overwrite uses atomic os.replace.
    """
    data = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')
    _atomic_write(path, lambda stream: stream.write(data), overwrite)


def write_json_lines(path, values, overwrite=False):
    """Bounded-memory JSONL publication; a late producer failure publishes nothing."""
    def emit(stream):
        count = 0
        for value in values:
            stream.write((json.dumps(value, sort_keys=True, allow_nan=False) + '\n').encode('utf-8'))
            count += 1
        if not count:
            raise ValueError('cannot publish an empty JSONL artifact')
    _atomic_write(path, emit, overwrite)
