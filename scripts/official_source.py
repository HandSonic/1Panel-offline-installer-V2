#!/usr/bin/env python3
"""Bind community-vendor packages to a reviewed per-version source lock."""
import json
from pathlib import Path
import re
import sys
from validate_payload import digest

ROOT = Path(__file__).resolve().parents[1]


def source(version, arch, root=ROOT):
    if not re.fullmatch(r'v2\.[0-9]+\.[0-9]+(?:[.-][A-Za-z0-9.-]+)?', version):
        raise ValueError('Explicit supported official version required')
    path = root / f'official-sources-{version}.json'
    if not path.is_file():
        raise ValueError('Unreviewed official source version: ' + version)
    pins = json.loads(path.read_text())
    if arch not in pins:
        raise ValueError('No reviewed official source for ' + version + '/' + arch)
    pin = pins[arch]
    if pin.get('version') != version or type(pin.get('bytes')) is not int or pin['bytes'] <= 0 or \
            not re.fullmatch('[0-9a-f]{64}', pin.get('sha256', '')) or \
            not re.fullmatch(r'https://resource\.fit2cloud\.com/1panel/package/v2/(stable|beta|dev)/' +
                             re.escape(version) + r'/release/1panel-' + re.escape(version) +
                             '-linux-' + re.escape(arch) + r'\.tar\.gz', pin.get('url', '')):
        raise ValueError('Malformed official source contract')
    return pin


def verify(path, version, arch, url, root=ROOT):
    pin = source(version, arch, root)
    if pin['url'] != url or digest(path) != {k: pin[k] for k in ['bytes', 'sha256']}:
        raise ValueError('Official package differs from the reviewed source bytes')
    return pin


if __name__ == '__main__':
    if sys.argv[1] == 'pin':
        pin = source(sys.argv[2], sys.argv[3])
        print(pin['url'], pin['sha256'])
    elif sys.argv[1] == 'verify':
        verify(Path(sys.argv[2]), *sys.argv[3:6])
    else:
        raise SystemExit('Unknown official source operation')
