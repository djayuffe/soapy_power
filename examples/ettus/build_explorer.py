#!/usr/bin/env python3
"""Build a standalone explorer from a validated B200 full-band display bundle.

This packages display data, not raw SDR acquisition. See README.md for inputs.
"""
import argparse
import base64
import io
import json
import math
from pathlib import Path

TEXTURES = ('mean', 'clean', 'raw', 'rawCount', 'cleanCount', 'first', 'last',
            'rawFirst', 'rawLast', 'offset', 'rawOffset')


def build(metadata, textures):
    from PIL import Image
    meta = metadata['meta']
    # The text and acquisition assumptions in this B200 example describe this
    # grid format. Reject other grids rather than silently mislabel measurements.
    expected = dict(rows=60, columns=16384, time_cell_seconds=30,
                    start_hz=42000000, stop_hz=6008000000,
                    raw_fft_spacing_hz=6835.9375,
                    tooltip_density_quantization_db=0.01,
                    tooltip_host_bound_quantization_seconds=0.1)
    if any(meta.get(k) != v for k, v in expected.items()):
        raise ValueError('Unsupported full-band display bundle geometry or encoding')
    if not isinstance(meta.get('source_records'), int) or meta['source_records'] < 1:
        raise ValueError('Invalid source record count')
    for name in ('first_capture_local', 'last_capture_local'):
        from datetime import datetime
        if datetime.fromisoformat(meta[name]).utcoffset() is None:
            raise ValueError('Capture dates must have a timezone')
    for mode in ('mean', 'clean', 'raw', 'delta'):
        low, high = (metadata['stats'][mode][k] for k in ('min', 'max'))
        if not all(math.isfinite(v) for v in (low, high)) or high <= low:
            raise ValueError('Invalid color limits')
    for name in ('magma', 'viridis', 'cividis'):
        lut = metadata['luts'][name]
        if len(lut) != 256 or any(len(rgb) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in rgb) for rgb in lut):
            raise ValueError('Invalid color palette')
    if set(textures) != set(TEXTURES):
        raise ValueError('Missing or unexpected data textures')
    for name, encoded in textures.items():
        content = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(content)) as im:
            if im.format != 'PNG' or im.mode not in ('RGB', 'RGBA') or im.size != (meta['columns'], meta['rows']):
                raise ValueError('Invalid data texture: ' + name)
            im.load()
    info = {k: metadata[k] for k in ('meta', 'stats', 'luts')}
    info['planned_centers_mhz'] = [45.4931640625 + i * 6.986328125 for i in range(854)]
    payload = json.dumps(dict(info=info, textures=textures), separators=(',', ':'), allow_nan=False).replace('<', '\\u003c')
    here = Path(__file__).resolve().parent
    result = (here/'explorer.html').read_text(encoding='utf-8')
    for marker, value in (('__ANALYSIS_JS__', (here/'heatmap_analysis.js').read_text(encoding='utf-8')),
                          ('__EXPLORER_JS__', (here/'explorer.js').read_text(encoding='utf-8')),
                          ('__PAYLOAD__', payload)):
        result = result.replace(marker, value)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('metadata', type=Path, help='hires-viewer-meta.json')
    parser.add_argument('textures', type=Path, help='hires-textures.json')
    parser.add_argument('-o', '--output', type=Path, required=True)
    args = parser.parse_args()
    html = build(json.loads(args.metadata.read_text(encoding='utf-8')), json.loads(args.textures.read_text(encoding='utf-8')))
    args.output.write_text(html, encoding='utf-8')
    print('Wrote {} ({:,} bytes)'.format(args.output, len(html.encode())))


if __name__ == '__main__':
    main()
