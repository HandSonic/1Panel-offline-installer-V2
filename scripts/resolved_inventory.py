#!/usr/bin/env python3
"""Resolve complete package inventories without a checked-in version allowlist.

This module is deliberately transport-independent. Its caller must authenticate
the expected custom contract digest using the verified upstream release receipt.
Vendor responses are collected once from the canonical endpoints; the resulting
plan is immutable input to later download, validation, native and publication
jobs. Archive contents still require the existing independent byte validators.
"""
import hashlib
import json
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit

ARCHES = ('amd64', 'arm64', 'armv7', 'ppc64le', 's390x', 'loong64', 'riscv64')
NATIVE = ('amd64', 'arm64')
SOURCES = ('official', 'custom', 'enterprise-original', 'enterprise-docker')
INSTALLER_REQUIRED = {'install.sh', '1pctl'} | {
    f'initscript/1panel-{part}.{kind}' for part in ('core', 'agent')
    for kind in ('service', 'init', 'openrc', 'procd')} | {
    f'lang/{language}.sh' for language in ('en', 'fa', 'pt-BR', 'ru', 'zh')}
VERSION = re.compile(r'v2\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-((?:beta|dev)\.(?:0|[1-9][0-9]*)))?')
MAX_CONTROL = 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def object_bytes(raw):
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_CONTROL, 'Invalid control size')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate control field: ' + key)
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON value')))
    require(isinstance(value, dict), 'Control must be an object')
    return value


def exact_keys(value, keys, label):
    require(isinstance(value, dict) and set(value) == set(keys), 'Unsupported ' + label + ' fields')


def hash_value(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value), 'Invalid SHA-256')
    return value


def commit_value(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value), 'Immutable commit required')
    return value


def safe_path(value):
    require(isinstance(value, str) and value and '\\' not in value, 'Invalid input path')
    p = PurePosixPath(value)
    require(not p.is_absolute() and '..' not in p.parts and str(p) == value and value != '.',
            'Unsafe input path')
    return value


def file_facts(value):
    exact_keys(value, ('sha256', 'bytes'), 'file facts')
    hash_value(value['sha256'])
    require(type(value['bytes']) is int and value['bytes'] > 0, 'Invalid file size')


def file_map(value):
    require(isinstance(value, dict) and value, 'Missing immutable input files')
    for name, facts in value.items():
        safe_path(name)
        file_facts(facts)


def version_identity(version, mode):
    require(isinstance(version, str), 'Explicit version required')
    match = VERSION.fullmatch(version)
    require(match is not None and mode in ('stable', 'beta', 'dev'), 'Unsupported version or channel')
    suffix = match[3]
    require((mode == 'stable' and suffix is None) or
            (mode != 'stable' and suffix is not None and suffix.startswith(mode + '.')),
            'Version/channel mismatch')
    return match


def source_contract(raw, expected_digest, version, mode):
    """Validate the shared upper/lower resolved-input schema, independent of version."""
    version_identity(version, mode)
    value = object_bytes(raw)
    require(digest(value) == hash_value(expected_digest), 'Resolved source contract digest mismatch')
    exact_keys(value, ('schema', 'kind', 'version', 'mode', 'edition', 'architectures',
                       'source', 'installer', 'toolchain', 'resources', 'configuration',
                       'frontend_lock'), 'source contract')
    require(type(value['schema']) is int and value['schema'] == 1 and
            value['kind'] == '1panel-resolved-build-inputs' and value['version'] == version and
            value['mode'] == mode and value['edition'] == 'community', 'Source contract identity mismatch')
    require(isinstance(value['architectures'], list) and
            len(value['architectures']) == len(ARCHES) and set(value['architectures']) == set(ARCHES),
            'Complete supported custom architecture set required')
    source = value['source']
    exact_keys(source, ('repository', 'commit', 'files', 'absent'), 'source identity')
    require(source['repository'] == '1Panel-dev/1Panel', 'Unexpected application source repository')
    commit_value(source['commit'])
    file_map(source['files'])
    require(isinstance(source['absent'], list) and len(source['absent']) == len(set(source['absent'])),
            'Invalid source absence list')
    for path in source['absent']:
        safe_path(path)
        require(path not in source['files'], 'Input cannot be both present and absent')
    installer = value['installer']
    exact_keys(installer, ('repository', 'commit', 'resources', 'original_version'), 'installer identity')
    require(installer['repository'] == '1Panel-dev/installer', 'Unexpected installer repository')
    commit_value(installer['commit'])
    file_map(installer['resources'])
    require(INSTALLER_REQUIRED <= set(installer['resources']), 'Incomplete installer resource contract')
    require(isinstance(installer['original_version'], str) and
            (installer['original_version'] == 'version' or VERSION.fullmatch(installer['original_version'])),
            'Invalid original installer version')
    exact_keys(value['toolchain'], ('go', 'node', 'npm'), 'toolchain')
    for tool, pinned in value['toolchain'].items():
        require(isinstance(pinned, str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', pinned),
                'Exact ' + tool + ' toolchain version required')
    exact_keys(value['resources'], ('geoip',), 'resources')
    geoip = value['resources']['geoip']
    require(isinstance(geoip, dict), 'Missing GeoIP resource')
    origin = 'archive' if 'archive' in geoip else 'url'
    exact_keys(geoip, (origin, 'sha256', 'bytes'), 'GeoIP resource')
    file_facts({k: geoip[k] for k in ('sha256', 'bytes')})
    require(geoip['bytes'] <= 64 * 1024 ** 2, 'GeoIP exceeds resource limit')
    if origin == 'archive':
        archive = geoip['archive']
        exact_keys(archive, ('url', 'sha256', 'bytes', 'member'), 'GeoIP source archive')
        file_facts({k: archive[k] for k in ('sha256', 'bytes')})
        require(archive['bytes'] <= 512 * 1024 ** 2 and archive['url'] ==
                vendor_base(version, mode, 'official') + f'1panel-{version}-linux-amd64.tar.gz' and
                archive['member'] == f'1panel-{version}-linux-amd64/GeoIP.mmdb',
                'GeoIP archive must bind the exact canonical version and member')
    else:
        require(isinstance(geoip['url'], str), 'Invalid GeoIP resource URL')
        url = urlsplit(geoip['url'])
        require(url.scheme == 'https' and url.hostname in ('github.com', 'raw.githubusercontent.com', 'resource.fit2cloud.com')
                and url.username is None and url.password is None and url.port in (None, 443)
                and not url.fragment, 'Unsupported GeoIP resource origin')
    exact_keys(value['configuration'], ('core', 'agent'), 'configuration')
    for profile in value['configuration'].values():
        exact_keys(profile, ('path', 'source_sha256', 'source_bytes', 'normalized_sha256'), 'configuration profile')
        safe_path(profile['path'])
        require(type(profile['source_bytes']) is int and profile['source_bytes'] > 0, 'Invalid source config size')
        require(source['files'].get(profile['path']) ==
                {'sha256': hash_value(profile['source_sha256']), 'bytes': profile['source_bytes']},
                'Configuration is not bound to immutable source bytes')
        hash_value(profile['normalized_sha256'])
    lock = value['frontend_lock']
    require(isinstance(lock, dict) and lock.get('kind') in ('source', 'derived'), 'Unsupported frontend lock origin')
    fields = ('kind', 'sha256', 'manifest_sha256') + (('recipe_sha256',) if lock['kind'] == 'derived' else ())
    exact_keys(lock, fields, 'frontend lock')
    for key in fields[1:]:
        hash_value(lock[key])
    require(source['files'].get('frontend/package.json', {}).get('sha256') == lock['manifest_sha256'],
            'Frontend dependency manifest is not bound to source')
    if lock['kind'] == 'source':
        require(source['files'].get('frontend/package-lock.json', {}).get('sha256') == lock['sha256'],
                'Frontend lock is not bound to source')
    elif 'frontend/package-lock.json' not in source['files']:
        require('frontend/package-lock.json' in source['absent'],
                'Derived lock requires a pinned original lock or explicit source-lock absence')
    return value


def vendor_base(version, mode, source):
    version_identity(version, mode)
    require(source in ('official', 'enterprise'), 'Unsupported vendor source')
    edition = 'v2' if source == 'official' else 'enterprise'
    return f'https://resource.fit2cloud.com/1panel/package/{edition}/{mode}/{version}/release/'


def vendor_inventory(version, mode, source, checksums, observations):
    """Derive pins from a complete canonical manifest plus all known-arch probes.

    Network/auth/rate-limit failures are errors, never evidence of unavailable
    editions or architectures. The caller performs bounded canonical HTTPS reads.
    """
    base = vendor_base(version, mode, source)
    exact_keys(checksums, ('url', 'status', 'body'), 'checksum response')
    require(checksums['url'] == base + 'checksums.txt' and type(checksums['status']) is int,
            'Wrong vendor checksum endpoint')
    require(isinstance(observations, dict) and set(observations) == set(ARCHES),
            'Complete vendor architecture discovery required')
    for arch, row in observations.items():
        exact_keys(row, ('url', 'status', 'bytes'), 'archive observation')
        require(row['url'] == base + f'1panel-{version}-linux-{arch}.tar.gz' and
                type(row['status']) is int and row['status'] in (200, 404),
                'Unresolved vendor archive endpoint: ' + arch)
        require((row['status'] == 200 and type(row['bytes']) is int and row['bytes'] > 0) or
                (row['status'] == 404 and row['bytes'] is None), 'Invalid vendor archive size observation')
    require(isinstance(checksums['body'], bytes) and len(checksums['body']) <= MAX_CONTROL,
            'Invalid vendor checksum response size')
    evidence = {'checksums': {'url': checksums['url'], 'status': checksums['status'],
                             'body': checksums['body'].decode('utf-8') if checksums['status'] == 200 else ''},
                'archives': json.loads(canonical(observations))}
    if checksums['status'] == 404:
        require(source == 'enterprise' and all(row['status'] == 404 for row in observations.values()),
                'Missing checksum manifest cannot hide available vendor packages')
        return {'availability': 'absent', 'archives': {}, 'evidence': evidence}
    require(checksums['status'] == 200 and isinstance(checksums['body'], bytes) and
            0 < len(checksums['body']) <= MAX_CONTROL, 'Unresolved vendor checksum manifest')
    pins = {}
    for line in checksums['body'].decode('utf-8').splitlines():
        require(line.strip(), 'Empty vendor checksum record')
        match = re.fullmatch(r'([0-9a-f]{64}) [ *](1panel-' + re.escape(version) + r'-linux-([a-z0-9]+)\.tar\.gz)', line)
        require(match is not None, 'Unsupported vendor checksum record')
        checksum, _, arch = match.groups()
        require(arch in ARCHES and arch not in pins, 'Unknown or duplicate vendor architecture')
        observation = observations[arch]
        require(observation['status'] == 200, 'Advertised vendor archive is unavailable')
        pins[arch] = {'url': observation['url'], 'sha256': checksum,
                      'bytes': observation['bytes'], 'version': version}
    require(set(pins) == {arch for arch, row in observations.items() if row['status'] == 200},
            'Vendor manifest omits an available archive')
    require(set(NATIVE) <= set(pins), 'Vendor release lacks native validation targets')
    return {'availability': 'present', 'archives': pins, 'evidence': evidence}


def package_plan(version, mode, contract, contract_digest, official, enterprise, docker, compose):
    """One version-derived inventory; no per-version matrix/source file lookup."""
    source_contract(canonical(contract), contract_digest, version, mode)
    for label, inventory in [('official', official), ('enterprise', enterprise)]:
        exact_keys(inventory, ('availability', 'archives', 'evidence'), label + ' inventory')
        evidence = inventory['evidence']
        exact_keys(evidence, ('checksums', 'archives'), label + ' discovery evidence')
        exact_keys(evidence['checksums'], ('url', 'status', 'body'), label + ' checksum evidence')
        require(isinstance(evidence['checksums']['body'], str), 'Invalid checksum evidence body')
        response = dict(evidence['checksums'], body=evidence['checksums']['body'].encode('utf-8'))
        require(vendor_inventory(version, mode, label, response, evidence['archives']) == inventory,
                'Vendor inventory differs from canonical discovery evidence')
        require(isinstance(inventory['archives'], dict), 'Invalid vendor archive inventory')
        require(inventory['availability'] in ('present', 'absent'), 'Unresolved vendor availability')
        require((inventory['availability'] == 'present') == bool(inventory['archives']), 'Inconsistent vendor availability')
        require(label != 'official' or inventory['availability'] == 'present', 'Official source required')
        for arch, pin in inventory['archives'].items():
            require(arch in ARCHES, 'Unknown vendor architecture')
            exact_keys(pin, ('url', 'sha256', 'bytes', 'version'), 'vendor archive pin')
            file_facts({k: pin[k] for k in ('sha256', 'bytes')})
            require(pin['version'] == version and pin['url'] == vendor_base(version, mode, label) +
                    f'1panel-{version}-linux-{arch}.tar.gz', 'Vendor archive identity mismatch')
        require(not inventory['archives'] or set(NATIVE) <= set(inventory['archives']), 'Missing vendor native targets')
    matrix = {'official': [a for a in ARCHES if a in official['archives']], 'custom': list(ARCHES)}
    if enterprise['availability'] == 'present':
        matrix.update({key: [a for a in ARCHES if a in enterprise['archives']]
                       for key in ('enterprise-original', 'enterprise-docker')})
    for name, pins in [('docker', docker), ('compose', compose)]:
        require(isinstance(pins, dict) and set(ARCHES) <= set(pins), 'Missing ' + name + ' architecture pins')
        for arch in ARCHES:
            require(isinstance(pins[arch], dict) and {'url', 'version', 'sha256'} <= set(pins[arch]), 'Incomplete dependency pin')
            hash_value(pins[arch]['sha256'])
            require(isinstance(pins[arch]['url'], str) and pins[arch]['url'].startswith('https://') and
                    isinstance(pins[arch]['version'], str) and pins[arch]['version'], 'Unpinned dependency')
    rows = [{'source': source, 'arch': arch, 'key': source + '-' + arch}
            for source, arches in matrix.items() for arch in arches]
    native = [{'source': row['source'], 'arch': row['arch'], 'scenario': scenario}
              for row in rows if row['source'] != 'enterprise-original' and row['arch'] in NATIVE
              for scenario in ('existing', 'fresh')]
    upgrades = [{'source': source, 'arch': arch} for source in ('official', 'custom') for arch in NATIVE]
    value = {'schema': 1, 'version': version, 'mode': mode, 'source_contract_sha256': contract_digest,
             'official': official, 'enterprise': enterprise, 'docker': docker, 'compose': compose,
             'matrix': matrix, 'rows': rows, 'native_rows': native, 'upgrade_rows': upgrades}
    return value, digest(value)
