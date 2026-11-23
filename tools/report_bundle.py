"""Publish complete report/reference generations through one atomic symlink replacement.

Raw acquisition directories must never be passed as the publication destination.
POSIX advisory locks serialize writers; a failed build leaves the previous generation intact.
"""
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid


def hashes(paths):
    out = {}
    for p in paths:
        p = Path(p)
        if p.is_dir():
            out.update(hashes(sorted(x for x in p.rglob('*') if x.is_file())))
        else:
            h = hashlib.sha256()
            with p.open('rb') as f:
                for chunk in iter(lambda: f.read(1024*1024), b''):
                    h.update(chunk)
            out[str(p.absolute())] = h.hexdigest()
    return out


@contextlib.contextmanager
def bundle(destination, inputs=()):
    """Yield a private staging directory, publish only after successful completion.

    Legacy real directories are archived intact during the first migration. That
    first migration has a brief name-availability gap; subsequent publication is atomic.
    Readers needing a consistent multi-file view should resolve the symlink once.
    """
    dest = Path(destination).absolute(); dest.parent.mkdir(parents=True, exist_ok=True)
    generations = dest.parent / ('.' + dest.name + '-generations')
    generations.mkdir(exist_ok=True)
    with (dest.parent / ('.' + dest.name + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = hashes(inputs)
        stage = Path(tempfile.mkdtemp(prefix='generation-', dir=generations))
        link = dest.parent / ('.' + dest.name + '-' + uuid.uuid4().hex)
        archived = None
        try:
            yield stage
            if hashes(inputs) != before:
                raise RuntimeError('source evidence changed during generation; publication refused')
            files = hashes(sorted(p for p in stage.rglob('*') if p.is_file()))
            manifest = dict(version=1, status='completed', created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            inputs=before, files={str(Path(k).relative_to(stage)): v for k,v in files.items()})
            (stage/'bundle-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
            # Complete every file before exposing the generation to readers.
            for p in stage.rglob('*'):
                if p.is_file():
                    with p.open('rb') as f: os.fsync(f.fileno())
            os.symlink(os.path.relpath(stage, dest.parent), link)
            if dest.exists() and not dest.is_symlink():
                archived = generations / ('legacy-' + uuid.uuid4().hex)
                os.replace(dest, archived)
            try:
                os.replace(link, dest)
            except BaseException:
                if archived is not None: os.replace(archived, dest)
                raise
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        finally:
            if link.is_symlink(): link.unlink()


def verify(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory/'bundle-manifest.json').read_text())
    expected = manifest['files']
    actual = {str(Path(k).relative_to(directory)): v for k,v in hashes(
        [p for p in directory.rglob('*') if p.is_file() and p != directory/'bundle-manifest.json']).items()}
    if manifest.get('status') != 'completed' or actual != expected:
        raise ValueError('reference/report bundle integrity check failed')
    return manifest
