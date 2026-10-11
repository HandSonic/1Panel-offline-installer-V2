#!/usr/bin/env python3
"""Bind community-vendor packages to authenticated runtime discovery."""
from pathlib import Path
import sys
from validate_payload import digest

ROOT = Path(__file__).resolve().parents[1]


def source(version, arch, root=ROOT):
    from runtime_contract import require_selected
    pin = require_selected(version, root)['inventory']['official']['archives'].get(arch)
    if pin is None:
        raise ValueError('Official architecture is absent from authenticated discovery')
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
