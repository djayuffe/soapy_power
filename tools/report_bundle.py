"""Publish complete report/reference generations through one atomic symlink replacement.

Raw acquisition directories must never be passed as the publication destination.
POSIX advisory locks serialize writers; a failed build leaves the previous generation intact.
Every published generation is kept for audit. `prune` (also `report_bundle.py prune DEST --keep N`)
removes older and abandoned generations on request; it always keeps the published one and never
touches archived legacy directories.
"""

import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid


PRIVATE_INPUTS = {'qth.json'}  # observer position: recorded by name only, never by digest


def recorded_inputs(digests, base):
    """Input record for a manifest: paths relative to the bundle's parent (no local home paths) and no digest
    of private inputs. The full digests are still compared before publication."""
    return {
        os.path.relpath(k, base): ('withheld (private input)' if Path(k).name in PRIVATE_INPUTS else v)
        for k, v in digests.items()
    }


def hashes(paths):
    out = {}
    for p in paths:
        p = Path(p)
        if p.is_dir():
            out.update(hashes(sorted(x for x in p.rglob('*') if x.is_file())))
        else:
            h = hashlib.sha256()
            with p.open('rb') as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b''):
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
    import fcntl  # POSIX only (macOS/Linux): advisory lock + atomic symlink publication

    dest = Path(destination).absolute()
    dest.parent.mkdir(parents=True, exist_ok=True)
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
            manifest = dict(
                version=1,
                status='completed',
                created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                inputs=recorded_inputs(before, dest.parent),
                files={str(Path(k).relative_to(stage)): v for k, v in files.items()},
            )
            (stage / 'bundle-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
            # Complete every file before exposing the generation to readers.
            for p in stage.rglob('*'):
                if p.is_file():
                    with p.open('rb') as f:
                        os.fsync(f.fileno())
            os.symlink(os.path.relpath(stage, dest.parent), link)
            if dest.exists() and not dest.is_symlink():
                archived = generations / ('legacy-' + uuid.uuid4().hex)
                os.replace(dest, archived)
            try:
                os.replace(link, dest)
            except BaseException:
                if archived is not None:
                    os.replace(archived, dest)
                raise
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        finally:
            if link.is_symlink():
                link.unlink()


def prune(destination, keep=2):
    """Keep the published generation and the newest `keep`-1 other completed ones; remove the rest.

    Abandoned builds (no manifest) are always removed. Runs under the writer lock, so it cannot remove a
    generation that is being built. Returns the removed paths."""
    import fcntl

    if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
        raise ValueError('keep must be a positive integer')
    dest = Path(destination).absolute()
    generations = dest.parent / ('.' + dest.name + '-generations')
    if not generations.is_dir():
        return []
    with (dest.parent / ('.' + dest.name + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = dest.resolve() if dest.is_symlink() else None

        def created(p):  # completed generations carry their publication time
            try:
                return json.loads((p / 'bundle-manifest.json').read_text())['created_utc']
            except (OSError, ValueError, KeyError, TypeError):
                return None

        olds = [
            p for p in generations.glob('generation-*') if p.is_dir() and not p.is_symlink() and p.resolve() != current
        ]
        complete = sorted((p for p in olds if created(p)), key=created, reverse=True)
        removed = [p for p in olds if not created(p)] + complete[keep - 1 if current is not None else keep :]
        for p in removed:
            shutil.rmtree(p)
        return removed


def verify(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'bundle-manifest.json').read_text())
    expected = manifest['files']
    actual = {
        str(Path(k).relative_to(directory)): v
        for k, v in hashes(
            [p for p in directory.rglob('*') if p.is_file() and p != directory / 'bundle-manifest.json']
        ).items()
    }
    if manifest.get('status') != 'completed' or actual != expected:
        raise ValueError('reference/report bundle integrity check failed')
    return manifest


def main():
    import argparse

    p = argparse.ArgumentParser(description='Verify a published report/reference bundle or prune its old generations.')
    sub = p.add_subparsers(dest='command', required=True)
    v = sub.add_parser('verify', help='check bundle-manifest.json against the published files')
    v.add_argument('destination')
    q = sub.add_parser('prune', help='remove old generations (archived legacy directories are kept)')
    q.add_argument('destination')
    q.add_argument('--keep', type=int, default=2, help='generations to keep, including the published one (default 2)')
    a = p.parse_args()
    if a.command == 'verify':
        print(json.dumps(dict(status=verify(a.destination)['status'], destination=str(Path(a.destination).resolve()))))
        return 0
    for path in prune(a.destination, a.keep):
        print('removed', path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
