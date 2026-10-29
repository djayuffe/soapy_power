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


def _atomic_write(path, emit, overwrite=False, before_publish=None):
    """Publish only after the complete producer succeeds and the file is fsynced."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            emit(stream)
            stream.flush()
            os.fsync(stream.fileno())
        if before_publish is not None:
            before_publish()
        if overwrite:
            os.replace(temporary, str(path))
        else:
            os.link(temporary, str(path))
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def write_json(path, value, overwrite=False, before_publish=None):
    """Publish strict JSON atomically; existing files require explicit overwrite.

    Default hard-link publication never replaces an existing destination. A
    filesystem without hard links fails rather than weakening that guarantee.
    Explicit overwrite uses atomic os.replace.
    """
    data = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')
    _atomic_write(path, lambda stream: stream.write(data), overwrite, before_publish)


def write_json_lines(path, values, overwrite=False, before_publish=None):
    """Bounded-memory JSONL publication; a late producer failure publishes nothing."""
    def emit(stream):
        count = 0
        for value in values:
            stream.write((json.dumps(value, sort_keys=True, allow_nan=False) + '\n').encode('utf-8'))
            count += 1
        if not count:
            raise ValueError('cannot publish an empty JSONL artifact')
    _atomic_write(path, emit, overwrite, before_publish)


def file_signature(path):
    """Identity/version metadata for detecting ordinary concurrent file changes."""
    stat = Path(path).stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


class EvidenceSnapshot:
    """Reject source replacement/mutation before an offline artifact is published.

    Metadata checks detect ordinary concurrent writes, not a hostile filesystem
    that forges identity/time metadata. Content SHA-256 remains provenance data.
    """
    def __init__(self, paths=()):
        self._signatures = {}
        for path in paths:
            self.add(path)

    def add(self, path):
        # Keep the source spelling: resolving here would miss a symlink
        # retarget while its previous target remained unchanged.
        path = Path(path).absolute()
        signature = file_signature(path)
        if path in self._signatures and self._signatures[path] != signature:
            raise ValueError('input evidence changed during analysis: {}'.format(path))
        self._signatures[path] = signature

    def verify(self):
        for path, signature in self._signatures.items():
            if file_signature(path) != signature:
                raise ValueError('input evidence changed during analysis: {}'.format(path))

    def protect_output(self, output):
        if Path(output).resolve() in {path.resolve() for path in self._signatures}:
            raise ValueError('output must not replace input evidence')
