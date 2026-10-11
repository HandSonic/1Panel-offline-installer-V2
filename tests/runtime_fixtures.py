"""Authenticated, synthetic per-run inputs for isolated consumer regression tests."""
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

from resolved_inventory import ARCHES, canonical, digest, vendor_base, vendor_inventory
from runtime_contract import PLAN_PATH, PLAN_SHA
from runtime_inventory import plan,vendor

ROOT = Path(__file__).resolve().parents[1]


def dependencies(root):
    for name in ('docker', 'compose'):
        path = root / (name + '-sources.json')
        if not path.exists():
            path.write_bytes((ROOT / path.name).read_bytes())


def vendor_pins(version, source, pins, mode='stable'):
    base = vendor_base(version, mode, source)
    observations = {arch: {'url': base + f'1panel-{version}-linux-{arch}.tar.gz',
                           'status': 200 if arch in pins else 404,
                           'bytes': pins[arch]['bytes'] if arch in pins else None} for arch in ARCHES}
    body = ''.join(pins[a]['sha256'] + f'  1panel-{version}-linux-{a}.tar.gz\n' for a in ARCHES if a in pins)
    return vendor(version, mode, source, {'url': base + 'checksums.txt',
        'status': 200 if pins else 404, 'body': body.encode()}, observations)


def refresh(value, root=ROOT):
    contract = value['source_contract']
    value['source_contract_sha256'] = digest(contract)
    for row in value['upstream']['records'].values():
        row.update(source_commit=contract['source']['commit'], installer_commit=contract['installer']['commit'],
                   resolved_contract_sha256=digest(contract))
    current = value['inventory']
    value['inventory'], _ = plan(value['version'], value['mode'], contract, digest(contract),
        current['official'], current['enterprise'],
        *[json.loads((root / (name + '-sources.json')).read_text()) for name in ('docker', 'compose')])
    return value


def environment(directory, value):
    path = Path(directory) / 'runtime-plan.json'
    raw = canonical({'resolved': value}); path.write_bytes(raw)
    return {PLAN_PATH: str(path), PLAN_SHA: hashlib.sha256(raw).hexdigest()}


def activate(case, directory, value):
    case.enterContext(patch.dict(os.environ, environment(directory, value)))
    return value
