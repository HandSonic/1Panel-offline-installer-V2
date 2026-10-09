#!/usr/bin/env python3
"""Select and verify a reviewed upstream validator snapshot for this version."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

ROOT = Path(__file__).resolve().parents[1]


def validator_root(version, root=ROOT):
    routes = json.loads((root / 'config/upstream-validation.json').read_text())
    if version not in routes:
        raise ValueError('Unreviewed upstream validation version: ' + version)
    route = routes[version]
    name = route.get('directory', '')
    if not re.fullmatch(r'vendor/upstream-validation(?:-[a-z0-9-]+)?', name):
        raise ValueError('Unsafe upstream validator directory')
    directory = root / name
    if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve() / 'vendor'):
        raise ValueError('Unsafe upstream validator directory')
    raw = (directory / 'SOURCE.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != route.get('source_manifest_sha256'):
        raise ValueError('Upstream validator source manifest changed')
    record = json.loads(raw)
    if record.get('repository') != 'HandSonic/1Panel-Build-v2' or \
            record.get('commit') != route.get('commit') or \
            not re.fullmatch('[0-9a-f]{40}', route.get('commit', '')):
        raise ValueError('Upstream validator provenance mismatch')
    files = record.get('files', {})
    if not {'scripts/validate_artifacts.py', 'scripts/resolve_inputs.py', 'config/sources.json'} <= set(files):
        raise ValueError('Incomplete upstream validator snapshot')
    actual = set()
    for item in directory.rglob('*'):
        if item.is_symlink():
            raise ValueError('Symlink in upstream validator snapshot')
        if item.is_file() and '__pycache__' not in item.parts and item.suffix != '.pyc':
            actual.add(item.relative_to(directory).as_posix())
    if actual != set(files) | {'SOURCE.json'}:
        raise ValueError('Unexpected or missing upstream validator files')
    for name, expected in files.items():
        path = PurePosixPath(name)
        candidate = directory / path
        if path.is_absolute() or '..' in path.parts or str(path) != name or \
                candidate.is_symlink() or not candidate.is_file() or \
                not candidate.resolve().is_relative_to(directory.resolve()) or \
                not re.fullmatch('[0-9a-f]{64}', expected) or \
                hashlib.sha256(candidate.read_bytes()).hexdigest() != expected:
            raise ValueError('Unsafe or changed upstream validator file: ' + name)
    sources = json.loads((directory / 'config/sources.json').read_text())
    if version not in sources:
        raise ValueError('Selected validator has no source contract for ' + version)
    return directory


def validate_manifest_contract(data, version, arch, control, root=ROOT):
    """Recheck producer metadata/resources even after the offline Docker patch."""
    directory = validator_root(version, root)
    entry = json.loads((directory / 'config/sources.json').read_text())[version]
    expected = {'schema_version': 1, 'edition': 'community', 'version': version, 'architecture': arch}
    expected.update({key: entry[key] for key in ['source_commit', 'installer_commit', 'mode',
                                               'go_version', 'node_version', 'npm_version']})
    if any(data.get(key) != value for key, value in expected.items()):
        raise ValueError('Upstream manifest differs from pinned producer contract')
    facts = data.get('files', {})
    if facts.get('GeoIP.mmdb', {}).get('sha256') != entry['geoip_sha256']:
        raise ValueError('Upstream GeoIP differs from pinned producer contract')
    for name, expected_sha in entry['installer_sha256'].items():
        if name == '1pctl':
            normalized, count = re.subn(rb'(?m)^ORIGINAL_VERSION=[^\n]*',
                                       b'ORIGINAL_VERSION=' + entry['installer_original_version'].encode(), control)
            if count != 1 or hashlib.sha256(normalized).hexdigest() != expected_sha or \
                    not re.search(rb'(?m)^ORIGINAL_VERSION=' + re.escape(version.encode()) + rb'$', control):
                raise ValueError('Upstream 1pctl differs from pinned producer contract')
        elif facts.get(name, {}).get('sha256') != expected_sha:
            raise ValueError('Upstream installer resource differs from pinned producer contract: ' + name)
    return data
