#!/usr/bin/env python3
"""Complete branch requests with explicit, isolated discovery failures."""
import re
from resolved_inventory import (ARCHES, NATIVE, SOURCES, canonical, digest, exact_keys,
    file_facts, hash_value, require, source_contract, vendor_base, vendor_inventory)


def vendor(version, mode, source, checksum, observations):
    # Keep canonical successful/absent discovery identical to the pure validator.
    try:
        return vendor_inventory(version, mode, source, checksum, observations)
    except ValueError:
        pass
    base = vendor_base(version, mode, source)
    require(isinstance(checksum, dict) and checksum.get('url') == base + 'checksums.txt' and
            set(checksum) == {'url', 'status', 'body'} and isinstance(checksum['body'], bytes),
            'Malformed vendor discovery transport')
    require(isinstance(observations, dict) and set(observations) == set(ARCHES), 'Incomplete vendor observations')
    for arch, row in observations.items():
        exact_keys(row, ('url', 'status', 'bytes'), 'vendor observation')
        require(row['url'] == base + f'1panel-{version}-linux-{arch}.tar.gz' and
                type(row['status']) is int and row['status'] in (0, 200, 404) and
                ((row['status'] == 200 and type(row['bytes']) is int and row['bytes'] > 0) or
                 (row['status'] != 200 and row['bytes'] is None)), 'Invalid vendor observation')
    evidence = {'checksums': {'url': checksum['url'], 'status': checksum['status'],
                              'body': checksum['body'].decode('utf-8', errors='strict')},
                'archives': observations}
    pins, failure = {}, None
    if checksum['status'] != 200:
        failure = 'Vendor checksum manifest unavailable; availability is unresolved'
    else:
        for line in checksum['body'].decode('utf-8').splitlines():
            match = re.fullmatch(r'([0-9a-f]{64}) [ *](1panel-' + re.escape(version) + r'-linux-([a-z0-9]+)\.tar\.gz)', line)
            if match is None or match[3] not in ARCHES or match[3] in pins:
                failure = 'Vendor checksum manifest is malformed or ambiguous'; break
            pins[match[3]] = match[1]
        if not pins:
            failure = 'Vendor checksum manifest has no usable archive records'
        if any(row['status'] == 200 and arch not in pins for arch, row in observations.items()):
            failure = 'Vendor manifest omits an available archive'
    # An unreadable/contradictory shared manifest blocks this vendor origin only.
    requested = list(ARCHES) if failure else [a for a in ARCHES if a in pins or observations[a]['status'] == 0]
    archives, failures = {}, {}
    for arch in requested:
        row = observations[arch]
        reason = failure or ('Vendor archive endpoint unavailable' if row['status'] != 200 else
                             'Vendor archive cannot be authenticated by the manifest' if arch not in pins else '')
        if reason:
            failures[arch] = reason
        else:
            archives[arch] = {'url': row['url'], 'sha256': pins[arch], 'bytes': row['bytes'], 'version': version}
    return {'availability': 'partial' if archives else 'failed', 'archives': archives,
            'requested_architectures': requested, 'failures': failures, 'evidence': evidence}


def plan(version, mode, contract, contract_sha, official, enterprise, docker, compose):
    if contract is not None:
        source_contract(canonical(contract), contract_sha, version, mode)
    else:
        require(contract_sha is None, 'Absent custom contract cannot have a digest')
    inventories = {'official': official, 'enterprise': enterprise}
    requested = {}
    for label, inventory in inventories.items():
        require(isinstance(inventory, dict), 'Missing vendor inventory')
        evidence = inventory.get('evidence', {})
        exact_keys(evidence, ('checksums', 'archives'), 'vendor evidence')
        response = dict(evidence['checksums'])
        require(isinstance(response.get('body'), str), 'Invalid checksum evidence')
        response['body'] = response['body'].encode('utf-8')
        require(vendor(version, mode, label, response, evidence['archives']) == inventory,
                'Runtime vendor inventory differs from observations')
        requested[label] = inventory.get('requested_architectures', [a for a in ARCHES if a in inventory['archives']])
    matrix = {'official': requested['official'], 'custom': list(ARCHES)}
    if enterprise['availability'] != 'absent':
        matrix.update({s: requested['enterprise'] for s in ('enterprise-original', 'enterprise-docker')})
    for name, pins in [('docker', docker), ('compose', compose)]:
        require(isinstance(pins, dict) and set(ARCHES) <= set(pins), 'Missing ' + name + ' generic pins')
        for arch in ARCHES:
            require(isinstance(pins[arch], dict) and {'url', 'version', 'sha256'} <= set(pins[arch]), 'Incomplete generic dependency pin')
            hash_value(pins[arch]['sha256'])
            require(isinstance(pins[arch]['url'], str) and pins[arch]['url'].startswith('https://') and
                    isinstance(pins[arch]['version'], str) and pins[arch]['version'], 'Unpinned generic dependency')
    rows = [{'source': s, 'arch': a, 'key': s + '-' + a} for s in SOURCES for a in matrix.get(s, [])]
    native = [{'source': r['source'], 'arch': r['arch'], 'scenario': scenario} for r in rows
              if r['source'] != 'enterprise-original' and r['arch'] in NATIVE for scenario in ('existing', 'fresh')]
    upgrades = [{'source': r['source'], 'arch': r['arch']} for r in rows if r['source'] in ('official', 'custom') and r['arch'] in NATIVE]
    value = {'schema': 1, 'version': version, 'mode': mode, 'source_contract_sha256': contract_sha,
             'official': official, 'enterprise': enterprise, 'docker': docker, 'compose': compose,
             'matrix': matrix, 'rows': rows, 'native_rows': native, 'upgrade_rows': upgrades}
    return value, digest(value)
