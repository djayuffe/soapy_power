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


def write_json(path, value, overwrite=False):
    """Publish a complete JSON file; existing files require explicit overwrite.

    A same-directory temporary file is fsynced before publication. The default
    hard-link publication is atomic and never replaces an existing destination.
    Filesystems without hard links report an error rather than silently losing
    that guarantee. --force/overwrite uses atomic os.replace instead.
    """
    data = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
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
