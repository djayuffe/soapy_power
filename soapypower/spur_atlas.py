"""Held-out RF-fixed/IF-fixed recurrence hypotheses, never automatic notches."""
import numpy as np
from soapypower.numerics import finite


def analyze_track(observations, tolerance_hz):
    """Test one externally associated feature across independent retunes.

    Each row declares center_hz, baseband_hz, sample_rate_hz, clock_state,
    partition ('train' or 'validation') and capture_id. Association itself is
    not inferred; unrelated peaks must not be grouped to force a hypothesis.
    Clock/rate domains are separate, and no removal rule is generated.
    """
    tolerance = finite(tolerance_hz,'spur tolerance',positive=True)
    if not isinstance(observations,list) or not observations:
        raise ValueError('spur track requires a nonempty observation list')
    groups,ids = {},set()
    for row in observations:
        identity = row.get('capture_id')
        if not isinstance(identity,str) or not identity or identity in ids:
            raise ValueError('independent unique capture IDs are required')
        ids.add(identity)
        partition = row.get('partition')
        if partition not in ('train','validation'):
            raise ValueError('spur observations require explicit disjoint partitions')
        center = finite(row['center_hz'],'spur center')
        bb = finite(row['baseband_hz'],'spur baseband')
        rate = finite(row['sample_rate_hz'],'spur sample rate',positive=True)
        clock = row.get('clock_state')
        if not isinstance(clock,str) or not clock or abs(bb)>=rate/2:
            raise ValueError('spur observation requires declared clock state and in-band IF')
        groups.setdefault((rate,clock),[]).append((center,bb,partition,identity))
    results = []
    for (rate,clock),rows in sorted(groups.items()):
        train = [r for r in rows if r[2]=='train']
        test = [r for r in rows if r[2]=='validation']
        hypotheses = []
        identifiable = bool(len(train)>=3 and len(test)>=2 and
                        np.ptp([r[0] for r in train])>4*tolerance and
                        np.ptp([r[0] for r in test])>4*tolerance)
        for name,fn in (('if_fixed',lambda r:r[1]),('rf_fixed',lambda r:r[0]+r[1])):
            anchor = float(np.median([fn(r) for r in train])) if train else None
            tr = max((abs(fn(r)-anchor) for r in train),default=None)
            va = max((abs(fn(r)-anchor) for r in test),default=None) if anchor is not None else None
            passed = bool(identifiable and tr<=tolerance and va<=tolerance)
            hypotheses.append(dict(hypothesis=name,frozen_training_anchor_hz=anchor,
                training_max_residual_hz=tr,validation_max_residual_hz=va,passed=passed))
        accepted = [h['hypothesis'] for h in hypotheses if h['passed']]
        results.append(dict(sample_rate_hz=rate,clock_state=clock,identifiable=identifiable,
            training_captures=[r[3] for r in train],validation_captures=[r[3] for r in test],
            hypotheses=hypotheses,diagnostic_class=accepted[0] if len(accepted)==1 else 'unresolved',
            exclusion_authorized=False,known_rf_reference_verified=False))
    return dict(version=1,groups=results,automatic_exclusions=[],source_modified=False)
