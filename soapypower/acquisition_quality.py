"""Bounded live headroom policy and durable rejected-IQ evidence."""
import hashlib
from pathlib import Path
import numpy as np
from soapypower.files import _atomic_write, write_json
from soapypower.numerics import finite, integer
from soapypower.receiver_quality import headroom_metrics, headroom_passes


class HeadroomRejected(RuntimeError):
    pass


class QualityPolicy:
    def __init__(self, config):
        fields = {'max_component','p999_component','max_retries','workspace_bytes','evidence_dir'}
        if not isinstance(config, dict) or set(config)-fields:
            raise ValueError('invalid acquisition quality policy')
        self.maximum = finite(config.get('max_component',.85),'maximum component',positive=True)
        self.percentile = finite(config.get('p999_component',.60),'P99.9 component',positive=True)
        if not self.percentile <= self.maximum <= 1:
            raise ValueError('headroom thresholds require 0 < P99.9 <= maximum <= 1')
        self.retries = integer(config.get('max_retries',0),'headroom retries')
        if self.retries > 64:
            raise ValueError('headroom retries must not exceed 64')
        self.budget = integer(config.get('workspace_bytes',64*1024**2),'headroom workspace',1)
        directory = config.get('evidence_dir')
        if not isinstance(directory,(str,Path)) or not str(directory):
            raise ValueError('headroom requires a fresh rejected-IQ evidence directory')
        self.directory = Path(directory)

    def check_budget(self, samples):
        # Raw chunks, concatenation, double-precision statistics and percentile
        # temporaries are bounded separately from the existing DSP workspace.
        if samples * 64 > self.budget:
            raise ValueError('whole-hop headroom exceeds its workspace budget; shorten integration or explicitly raise the headroom budget')

    def evaluate(self, samples):
        metrics = headroom_metrics(samples)
        return metrics, headroom_passes(metrics,self.maximum,self.percentile)


class RejectionEvidence:
    """A fresh directory, atomic raw/metadata files, and fsynced manifest.

    A producer failure never publishes partial NPZ bytes. If later metadata
    publication fails, the complete raw file remains recoverable and scanning
    stops; an evidence failure must never silently discard the unsafe trial.
    """
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True,exist_ok=False)
        self.entries = []
        write_json(self.directory/'index.json',{'version':1,'outcome':'rejected acquisitions','records':[]})

    def save(self, samples, metadata):
        name = 'rejected-{:06d}'.format(len(self.entries))
        iq = self.directory/(name+'.npz')
        z = np.asarray(samples)
        if z.ndim != 1 or z.dtype.kind != 'c':
            raise ValueError('rejected evidence must be one-dimensional complex IQ')
        _atomic_write(iq,lambda stream:np.savez_compressed(stream,iq=z))
        digest = hashlib.sha256()
        with iq.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
        entry = dict(metadata,iq_file=iq.name,iq_sha256=digest.hexdigest(),samples=int(z.size),source_samples_modified=False)
        write_json(self.directory/(name+'.json'),entry)
        self.entries.append(dict(metadata_file=name+'.json',iq_file=iq.name,iq_sha256=digest.hexdigest(),samples=int(z.size)))
        write_json(self.directory/'index.json',{'version':1,'outcome':'rejected acquisitions','records':self.entries},overwrite=True)
        return entry


class IQEvidenceBuffer:
    """Bounded native-CF32 prefix storage, independent of packet count."""
    def __init__(self, samples):
        self._buffer = np.empty(integer(samples,'evidence samples',1),dtype=np.complex64)
        self.count = 0

    def __len__(self):
        return self.count

    def append(self, samples):
        z = np.asarray(samples)
        if z.ndim != 1 or z.dtype != np.dtype(np.complex64):
            raise ValueError('live evidence requires unchanged native CF32 input')
        stop = self.count+len(z)
        if stop>len(self._buffer):
            raise ValueError('rejected-IQ prefix exceeds the declared acquisition budget')
        self._buffer[self.count:stop] = z
        self.count = stop

    def samples(self):
        return self._buffer[:self.count]
