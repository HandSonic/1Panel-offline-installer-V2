#!/usr/bin/env python3
"""Resolve reviewed immutable inputs; never substitute latest for historical releases."""
import json, pathlib, re, shlex, sys
LOCK = pathlib.Path(__file__).resolve().parents[1] / 'config/sources.json'
def resolve(version):
    if not re.fullmatch(r'v2\.\d+\.\d+(?:-(?:beta|dev)\.[0-9]+)?', version):
        raise ValueError('Expected an explicit supported v2 release version')
    data = json.loads(LOCK.read_text())
    if version not in data:
        raise ValueError(f'{version} has no reviewed input lock; add compatible immutable inputs first')
    entry = data[version]
    for key in ('source_commit', 'installer_commit'):
        if not re.fullmatch('[0-9a-f]{40}', entry[key]): raise ValueError(f'Unpinned {key}')
    expected = 'beta' if '-beta.' in version else 'dev' if '-dev.' in version else 'stable'
    if entry['mode'] != expected: raise ValueError('Version/channel mismatch')
    return entry
if __name__ == '__main__':
    try:
        entry = resolve(sys.argv[1])
        for name, key in [('SOURCE_COMMIT','source_commit'),('INSTALLER_REF','installer_commit'),('GO_VERSION','go_version'),('NODE_VERSION','node_version'),('NPM_VERSION','npm_version'),('GEOIP_SHA256','geoip_sha256')]:
            print(f'{name}={shlex.quote(entry[key])}')
    except (IndexError, KeyError, ValueError) as exc:
        sys.exit(str(exc))
