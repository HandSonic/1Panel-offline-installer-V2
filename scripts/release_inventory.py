#!/usr/bin/env python3
"""Complete reviewed version inventory, including explicit vendor unavailability.

Download failures never change membership. An empty enterprise lock requires the
reviewed 404 observations for its checksum endpoint and both native archives.
"""
import datetime
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
CUSTOM_ARCHES = {'amd64', 'arm64', 'armv7', 'ppc64le', 's390x', 'riscv64', 'loong64'}


def read_record(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate reviewed inventory field: ' + key)
            result[key] = value
        return result
    if not path.is_file():
        raise ValueError('Missing reviewed inventory file: ' + path.name)
    return json.loads(path.read_text(), object_pairs_hook=unique)


def unavailable_enterprise(version, root=ROOT):
    path = root / 'config/source-availability' / (version + '.json')
    if not path.is_file():
        raise ValueError('Empty enterprise inventory requires explicit reviewed unavailability')
    record = read_record(path)
    if set(record) != {'schema', 'version', 'source', 'availability', 'observations'} or \
            type(record['schema']) is not int or record['schema'] != 1 or \
            record['version'] != version or record['source'] != 'enterprise' or \
            record['availability'] != 'unavailable-at-reviewed-canonical-endpoints':
        raise ValueError('Malformed enterprise unavailability evidence')
    base = f'https://resource.fit2cloud.com/1panel/package/enterprise/stable/{version}/release/'
    expected = {(base + 'checksums.txt', 'GET')} | {
        (base + f'1panel-{version}-linux-{arch}.tar.gz', 'HEAD') for arch in ('amd64', 'arm64')}
    observations = record['observations']
    if not isinstance(observations, list) or len(observations) != len(expected):
        raise ValueError('Incomplete enterprise unavailability evidence')
    observed = set()
    for row in observations:
        if not isinstance(row, dict) or set(row) != {'url', 'method', 'http_status', 'observed_at_utc'} or \
                type(row['http_status']) is not int or row['http_status'] != 404:
            raise ValueError('Only explicit canonical HTTP 404 observations establish unavailability')
        observed.add((row['url'], row['method']))
        try:
            when = datetime.datetime.fromisoformat(row['observed_at_utc'])
            if when.utcoffset() != datetime.timedelta(0):
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError('Unavailability observation requires a UTC timestamp') from None
    if observed != expected:
        raise ValueError('Enterprise unavailability endpoint set mismatch')
    return record


def resolved_matrix(version, root=ROOT):
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?', version):
        raise ValueError('Invalid release inventory version')
    path = root / f'release-matrix-{version}.json'
    if not path.is_file():
        raise ValueError(f'No resolved release matrix for {version}; complete source/edition/architecture discovery first')
    matrix = read_record(path)
    if not isinstance(matrix, dict):
        raise ValueError('Release matrix must be an object')
    for arches in matrix.values():
        if not isinstance(arches, list) or not arches or \
                any(not isinstance(arch, str) or arch not in CUSTOM_ARCHES for arch in arches) or \
                len(arches) != len(set(arches)):
            raise ValueError('Invalid, empty or duplicate matrix architecture set')
    official = read_record(root / f'official-sources-{version}.json')
    enterprise = read_record(root / f'enterprise-sources-{version}.json')
    if not isinstance(official, dict) or not official or not isinstance(enterprise, dict):
        raise ValueError('Missing or malformed reviewed source architecture inventory')
    expected_sources = {'official', 'custom'}
    if enterprise:
        expected_sources |= {'enterprise-original', 'enterprise-docker'}
        if (root / 'config/source-availability' / (version + '.json')).exists():
            raise ValueError('Available enterprise sources conflict with unavailability evidence')
        if set(matrix.get('enterprise-original', [])) != set(enterprise) or \
                matrix.get('enterprise-original') != matrix.get('enterprise-docker'):
            raise ValueError('Enterprise original/enhanced matrix differs from reviewed source inventory')
    else:
        unavailable_enterprise(version, root)
    if set(matrix) != expected_sources or set(matrix.get('official', [])) != set(official) or \
            set(matrix.get('custom', [])) != CUSTOM_ARCHES:
        raise ValueError('Release matrix differs from complete reviewed source architecture inventory')
    from upstream_validation_contract import validator_root
    validator_root(version, root)
    return matrix


def native_rows(version, root=ROOT):
    """Every installable source's native arch, in both required Docker scenarios."""
    matrix = resolved_matrix(version, root)
    rows = [{'source': source, 'arch': arch, 'scenario': scenario}
            for source, arches in matrix.items() if source != 'enterprise-original'
            for arch in arches if arch in ('amd64', 'arm64')
            for scenario in ('existing', 'fresh')]
    if not rows:
        raise ValueError('Empty native release matrix')
    return rows
