"""Exact-state terminated-noise diagnostics, separate from power calibration."""
import copy
import hashlib
import json
from pathlib import Path
import numpy as np
from soapypower.files import EvidenceSnapshot, _atomic_write
from soapypower.calibration import require_reference_context
from soapypower.spectral_comparison import edges
from soapypower.numerics import numeric_array, finite


def _context(value):
    require_reference_context(value)
    if not isinstance(value,dict) or value.get('agc') is not False:
        raise ValueError('relative reference requires observed fixed gain context')
    for key in ('driver','hardware','serial','sample_rate','gains','sample_format'):
        if not value.get(key):
            raise ValueError('relative reference requires observable '+key)
    if value.get('sample_format') != 'CF32' or not isinstance(value['gains'],dict):
        raise ValueError('relative reference requires CF32 and named observed gains')
    finite(value['sample_rate'],'sample rate',positive=True)
    for gain in value['gains'].values():
        finite(gain,'observed gain')
    return json.dumps(value,sort_keys=True,allow_nan=False,separators=(',',':'))


class RelativeNoiseReference:
    """One measured context/grid. Deliberately no frequency or gain interpolation."""
    def __init__(self, frequency_edges_hz, density, context, method, reference):
        self.edges = edges(frequency_edges_hz).copy()
        self.density = numeric_array(density,'reference density').copy()
        if self.density.shape != (len(self.edges)-1,) or np.isinf(self.density).any() or np.any(self.density<0):
            raise ValueError('invalid relative reference density')
        if not isinstance(method,dict) or not all(k in method for k in ('fft_bins','window','normalization')):
            raise ValueError('relative reference requires FFT/window/normalization method')
        if not isinstance(reference,dict) or not all(k in reference for k in ('termination','reference_plane','temperature_c')):
            raise ValueError('relative reference needs load, reference plane and temperature (null if unknown)')
        if reference.get('headroom_accepted') is not True:
            raise ValueError('terminated reference requires explicit accepted raw headroom evidence')
        if reference['temperature_c'] is not None:
            finite(reference['temperature_c'],'load temperature')
        if any(not isinstance(reference[k],str) or not reference[k].strip() for k in ('termination','reference_plane')):
            raise ValueError('load and reference plane must be documented')
        self.context,self.method,self.reference = copy.deepcopy(context),copy.deepcopy(method),copy.deepcopy(reference)
        self._context_key = _context(context)
        self._method_key = json.dumps(method,sort_keys=True,allow_nan=False)
        self.edges.setflags(write=False)
        self.density.setflags(write=False)

    def compare(self, frequency_edges_hz, density, context, method):
        if _context(context) != self._context_key or json.dumps(method,sort_keys=True,allow_nan=False) != self._method_key:
            raise ValueError('relative reference context or DSP method mismatch')
        if not np.array_equal(edges(frequency_edges_hz),self.edges):
            raise ValueError('relative reference frequency grid mismatch; interpolation is forbidden')
        p = numeric_array(density,'antenna density')
        if p.shape != self.density.shape or np.isinf(p).any() or np.any(p<0):
            raise ValueError('invalid antenna density')
        good = np.isfinite(p)&np.isfinite(self.density)&(self.density>0)
        ratio = np.full(len(p),np.nan)
        with np.errstate(divide='ignore'):
            ratio[good] = 10*(np.log10(p[good])-np.log10(self.density[good]))
        excess = np.where(good,p-self.density,np.nan)
        return dict(noise_ratio_db=ratio,signed_excess_density=excess,supported=good,
                    absolute_power=False,snr=False,reference=copy.deepcopy(self.reference))

    def save(self,path,overwrite=False,before_publish=None):
        if _context(self.context) != self._context_key or json.dumps(self.method,sort_keys=True,allow_nan=False) != self._method_key:
            raise ValueError('relative reference metadata was mutated after construction')
        metadata = dict(version=1,type='relative_terminated_noise',context=self.context,
                        method=self.method,reference=self.reference)
        _atomic_write(path,lambda f:np.savez_compressed(f,frequency_edges_hz=self.edges,
            density=self.density,metadata=np.array(json.dumps(metadata,allow_nan=False))),overwrite,before_publish)

    @classmethod
    def load(cls,path):
        # Load/decompress once, close the archive, retain an immutable snapshot.
        path = Path(path)
        snapshot = EvidenceSnapshot([path])
        from soapypower.files import loads_json
        with np.load(path,allow_pickle=False) as z:
            if set(z.files) != {'frequency_edges_hz','density','metadata'}:
                raise ValueError('invalid relative reference archive')
            meta = loads_json(str(z['metadata'].item()))
            if meta.get('version') != 1 or meta.get('type') != 'relative_terminated_noise' or set(meta) != {'version','type','context','method','reference'}:
                raise ValueError('invalid relative reference schema')
            result = cls(z['frequency_edges_hz'],z['density'],meta['context'],meta['method'],meta['reference'])
        digest = hashlib.sha256()
        with path.open('rb') as f:
            for block in iter(lambda:f.read(1024**2),b''):digest.update(block)
        snapshot.verify()
        result.sha256 = digest.hexdigest()
        return result
