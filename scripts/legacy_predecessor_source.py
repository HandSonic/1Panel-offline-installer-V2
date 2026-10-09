#!/usr/bin/env python3
"""Authenticate a legacy upstream7 predecessor without inventing a source contract.

Only absence of resolved-source.json selects this protocol. An invalid modern
contract never falls back here. Receipt and immutable GitHub asset identities
authenticate the old bytes; current independent official-source reads authenticate
configuration and installer resources. This is input evidence, never historical
or current native acceptance. No per-version files or packaged code are used.
"""
import hashlib
import json
import re
from pathlib import Path

from native_candidate_input import api, listed, positive, SHARD_LIMIT
from publication_contract import read_job_log
from resolved_inventory import (ARCHES, INSTALLER_REQUIRED, MAX_CONTROL, canonical,
    commit_value, exact_keys, file_facts, hash_value, object_bytes, require,
    safe_path, version_identity)
from resolved_transport import (UPSTREAM, ControlGitHub, acquire_origin,
    canonical_read, discover_vendor)
from semantic_configuration import production
from validate_payload import elf
from validate_resolved_custom import archive_bytes, byte_facts

RECEIPT = 'release-validation.json'
SOURCE = '1Panel-dev/1Panel'
INSTALLER = '1Panel-dev/installer'
GENERATED = {'1panel-core', '1panel-agent', '1panel-core.service',
             '1panel-agent.service', 'GeoIP.mmdb', 'manifest.json'}
SOURCE_DOCUMENTS = {'LICENSE', 'README.md'}


def asset_pin(asset):
    require(isinstance(asset, dict) and positive(asset.get('id')) and
            isinstance(asset.get('name'), str) and '/' not in asset['name'] and
            '\\' not in asset['name'] and asset.get('state') == 'uploaded' and
            asset.get('url') == f'https://api.github.com/repos/{UPSTREAM}/releases/assets/{asset["id"]}',
            'Legacy custom asset identity/endpoint mismatch')
    digest = asset.get('digest')
    require(isinstance(digest, str) and digest.startswith('sha256:'),
            'Legacy custom asset requires a full server SHA-256')
    facts = {'bytes': asset.get('size'), 'sha256': digest[7:]}
    file_facts(facts)
    require(facts['bytes'] <= SHARD_LIMIT, 'Legacy custom archive exceeds size limit')
    return {'asset_id': asset['id'], 'name': asset['name'], **facts}


def snapshot(release):
    pins = [asset_pin(asset) for asset in release['assets']]
    require(len({p['name'] for p in pins}) == len(pins) and
            len({p['asset_id'] for p in pins}) == len(pins), 'Ambiguous legacy custom release assets')
    return {'release': {key: release.get(key) for key in ('id', 'tag_name', 'draft', 'prerelease', 'url')},
            'assets': sorted(pins, key=lambda pin: pin['asset_id'])}


def complete_release(client, version, mode, release_id=None):
    release = client.release() if release_id is None else api(client, f'repos/{UPSTREAM}/releases/{release_id}')
    require(positive(release.get('id')) and release.get('tag_name') == version and
            release.get('draft') is False and release.get('prerelease') is (mode != 'stable') and
            release.get('url') == f'https://api.github.com/repos/{UPSTREAM}/releases/{release["id"]}' and
            (release_id is None or release['id'] == release_id),
            'Legacy custom canonical release identity mismatch')
    assets = []
    for page in range(1, 101):
        rows = json.loads(client.run('api', f'repos/{UPSTREAM}/releases/{release["id"]}/assets?per_page=100&page={page}'))
        require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows),
                'Malformed legacy custom asset catalogue')
        assets.extend(rows)
        if len(rows) < 100:
            break
    else:
        raise ValueError('Legacy custom asset catalogue exceeds bound')
    release['assets'] = assets
    snapshot(release)
    return release


def run_identity(run, proof, attempt=None):
    require(positive(proof.get('workflow_run_id')) and positive(run.get('run_attempt')) and
            run.get('id') == proof['workflow_run_id'] and
            run.get('head_sha') == commit_value(proof.get('workflow_commit')) and
            run.get('status') == 'completed' and run.get('conclusion') == 'success' and
            run.get('path', '').split('@')[0] == '.github/workflows/build.yml' and
            run.get('event') in ('push', 'schedule', 'workflow_dispatch') and
            all(isinstance(run.get(key), dict) and run[key].get('full_name') == UPSTREAM
                for key in ('repository', 'head_repository')) and
            (attempt is None or run['run_attempt'] == attempt),
            'Legacy custom receipt requires a successful exact trusted workflow run')
    return {key: run.get(key) for key in ('id', 'head_sha', 'run_attempt', 'path', 'event',
                                        'status', 'conclusion', 'repository', 'head_repository')}


def validator(client, proof, receipt_sha, run):
    jobs = listed(client, f'repos/{UPSTREAM}/actions/runs/{run["id"]}/jobs?filter=all', 'jobs')
    matches = []
    for job in jobs:
        if job.get('name') not in ('build', 'publication_prepare', 'publication_revalidate') or job.get('conclusion') != 'success':
            continue
        require(positive(job.get('id')) and job.get('run_id') == run['id'] and
                job.get('head_sha') == run['head_sha'] and job.get('status') == 'completed' and
                positive(job.get('run_attempt')) and job['run_attempt'] <= run['run_attempt'],
                'Legacy custom validator job run/head/attempt mismatch')
        log = read_job_log(client, job['id'])
        require(len(log.encode()) <= 16 * MAX_CONTROL, 'Legacy custom validator log exceeds bound')
        hashes = re.findall(r'(?m)^(?:[0-9T:.-]+Z )?VERIFIED_RELEASE_RECEIPT_SHA256=([0-9a-f]{64})[ \t]*$', log)
        require(len(hashes) <= 1, 'Ambiguous legacy custom receipt log')
        if hashes == [receipt_sha]:
            exact = api(client, f'repos/{UPSTREAM}/actions/runs/{run["id"]}/attempts/{job["run_attempt"]}')
            run_identity(exact, proof, job['run_attempt'])
            matches.append({'job_id': job['id'], 'job_name': job['name'], 'attempt': job['run_attempt'],
                            'log_sha256': hashlib.sha256(log.encode()).hexdigest()})
    require(matches and len({row['attempt'] for row in matches}) == len(matches),
            'Legacy custom exact successful receipt validator unavailable or ambiguous')
    return max(matches, key=lambda row: row['attempt'])


def authenticate_controls(version, mode, client):
    version_identity(version, mode)
    require(client.repo == UPSTREAM and client.tag == version, 'Wrong legacy custom transport target')
    release = complete_release(client, version, mode)
    assets = {asset['name']: asset for asset in release['assets']}
    require('resolved-source.json' not in assets,
            'Modern custom controls cannot use legacy predecessor adaptation')
    controls = (RECEIPT, 'build-manifest.json', 'checksums.txt', 'build-inputs.env')
    require(set(controls) <= set(assets), 'Legacy custom receipt or controls are missing')
    bodies = {}
    for name in controls:
        require(asset_pin(assets[name])['bytes'] <= MAX_CONTROL, 'Oversized legacy custom control')
        raw = client.asset_bytes(assets[name])
        require(isinstance(raw, bytes) and byte_facts(raw) ==
                {key: asset_pin(assets[name])[key] for key in ('bytes', 'sha256')},
                'Legacy custom control differs from immutable server asset')
        bodies[name] = raw
    proof = object_bytes(bodies[RECEIPT])
    require(type(proof.get('schema')) is int and proof['schema'] == 1 and
            all(proof.get(key) == value for key, value in {'contract': 'upstream7',
                'repository': UPSTREAM, 'version': version, 'release_tag': version}.items()),
            'Legacy custom receipt identity mismatch')
    hash_value(proof.get('policy_fingerprint'))
    names = {f'1panel-{version}-linux-{arch}.tar.gz' for arch in ARCHES}
    expected = names | {name + '.sha256' for name in names} | set(controls[1:])
    require(isinstance(proof.get('files'), dict) and set(proof['files']) == expected,
            'Legacy custom receipt must bind the complete upstream7 inventory')
    for name, facts in proof['files'].items():
        file_facts(facts)
        require(name in assets and {key: asset_pin(assets[name])[key] for key in ('bytes', 'sha256')} == facts,
                'Legacy custom receipt differs from canonical asset: ' + name)
    for name in set(assets) - expected - {RECEIPT}:
        match = re.fullmatch(r'(.+)\.backup-[0-9a-f]{12}', name)
        require(match is not None and match[1] in expected | {RECEIPT},
                'Unexpected or staged legacy custom release asset')
    for name in controls[1:]:
        require(byte_facts(bodies[name]) == proof['files'][name], 'Legacy custom control is not receipt-bound')
    aggregate = object_bytes(bodies['build-manifest.json'])
    exact_keys(aggregate, ('schema_version', 'version', 'artifacts'), 'legacy aggregate')
    rows = aggregate['artifacts']
    require(type(aggregate['schema_version']) is int and aggregate['schema_version'] == 1 and
            aggregate['version'] == version and isinstance(rows, list) and len(rows) == len(ARCHES) and
            all(isinstance(row, dict) for row in rows) and
            {row.get('architecture') for row in rows} == set(ARCHES),
            'Legacy custom aggregate identity/architecture mismatch')
    for row in rows:
        exact_keys(row, ('architecture', 'file', 'sha256', 'size', 'source_commit',
                         'installer_commit', 'build_repository_commit'), 'legacy aggregate record')
        for key in ('source_commit', 'installer_commit', 'build_repository_commit'):
            commit_value(row[key])
        require(row['file'] == f'1panel-{version}-linux-{row["architecture"]}.tar.gz' and
                type(row['size']) is int and proof['files'].get(row['file']) ==
                {'bytes': row['size'], 'sha256': row['sha256']},
                'Legacy custom aggregate archive is not receipt-bound')
    require(len({tuple(row[key] for key in ('source_commit', 'installer_commit', 'build_repository_commit'))
                 for row in rows}) == 1, 'Mixed legacy custom producer/source identities')
    sums = {}
    for line in bodies['checksums.txt'].decode().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([^/\\]+)', line)
        require(match is not None and match[2] not in sums, 'Malformed legacy custom checksums')
        sums[match[2]] = match[1]
    require(sums == {row['file']: row['sha256'] for row in rows}, 'Legacy custom checksums differ from aggregate')
    run = api(client, f'repos/{UPSTREAM}/actions/runs/{proof["workflow_run_id"]}')
    identity = run_identity(run, proof)
    receipt_sha = byte_facts(bodies[RECEIPT])['sha256']
    validation = validator(client, proof, receipt_sha, run)
    return {'release': release, 'proof': proof, 'records': {row['architecture']: row for row in rows},
            'run': identity, 'validator': validation, 'receipt_sha256': receipt_sha,
            'aggregate_sha256': byte_facts(bodies['build-manifest.json'])['sha256']}


def immutable_bytes(repository, commit, path, read):
    commit_value(commit)
    safe_path(path)
    url = f'https://raw.githubusercontent.com/{repository}/{commit}/{path}'
    response = read(url)
    raw = response.get('body')
    require(response.get('status') == 200 and isinstance(raw, bytes) and 0 < len(raw) <= MAX_CONTROL,
            'Legacy custom immutable official resource unavailable: ' + path)
    return raw


def verify_source(path, version, mode, arch, record, origin_path, origin_pin, *, read=canonical_read):
    """No manifest toolchain claim is promoted into independently verified facts."""
    version_identity(version, mode)
    for key in ('source_commit', 'installer_commit', 'build_repository_commit'):
        commit_value(record[key])
    require(record.get('architecture') == arch and record.get('file') ==
            f'1panel-{version}-linux-{arch}.tar.gz', 'Legacy custom aggregate target mismatch')
    pin = {'bytes': record['size'], 'sha256': record['sha256']}
    file_facts(pin)
    require(pin['bytes'] <= SHARD_LIMIT and arch in ARCHES, 'Invalid legacy custom archive target/size')
    data = archive_bytes(path, pin, f'1panel-{version}-linux-{arch}')
    require(GENERATED | INSTALLER_REQUIRED <= set(data), 'Legacy custom archive lacks required resources')
    manifest = object_bytes(data['manifest.json'])
    require(type(manifest.get('schema_version')) is int and manifest['schema_version'] == 1 and
            all(manifest.get(key) == value for key, value in {'version': version, 'mode': mode,
                'architecture': arch, 'edition': 'community', **{key: record[key] for key in
                ('source_commit', 'installer_commit', 'build_repository_commit')}}.items()),
            'Legacy custom manifest differs from authenticated aggregate identity')
    require('resolved_contract_sha256' not in manifest, 'Modern manifest cannot use legacy adaptation')
    files = manifest.get('files')
    require(isinstance(files, dict) and set(files) == set(data) - {'manifest.json'},
            'Legacy custom manifest must enumerate the entire archive')
    for name, facts in files.items():
        require(isinstance(facts, dict) and type(facts.get('size')) is int and facts ==
                {'size': len(data[name]), 'sha256': byte_facts(data[name])['sha256']},
                'Legacy custom manifest member mismatch: ' + name)
    installer_facts, source_facts, configs = {}, {}, {}
    resources = set(data) - GENERATED
    require(len(resources) <= 256, 'Legacy custom resource inventory exceeds bound')
    resource_bytes = 0
    for name in sorted(resources):
        if name in SOURCE_DOCUMENTS:
            original = immutable_bytes(SOURCE, record['source_commit'], name, read)
            source_facts[name] = byte_facts(original)
        else:
            require(name in ('install.sh', '1pctl') or
                    re.fullmatch(r'(?:lang|initscript)/[A-Za-z0-9_.-]+', name),
                    'Legacy custom resource has no supported immutable official origin: ' + name)
            original = immutable_bytes(INSTALLER, record['installer_commit'], name, read)
            installer_facts[name] = byte_facts(original)
        resource_bytes += len(original)
        require(resource_bytes <= 16 * MAX_CONTROL, 'Legacy custom official resources exceed bound')
        expected = original
        if name == '1pctl':
            versions = re.findall(rb'(?m)^ORIGINAL_VERSION=([^\n]*)$', original)
            require(len(versions) == 1 and (versions[0] == b'version' or
                    re.fullmatch(rb'v2\.[0-9]+\.[0-9]+(?:-(?:beta|dev)\.[0-9]+)?', versions[0])),
                    'Legacy custom original control version is ambiguous')
            expected, count = re.subn(rb'(?m)^ORIGINAL_VERSION=[^\n]*',
                                     b'ORIGINAL_VERSION=' + version.encode(), original)
            require(count == 1, 'Legacy custom control substitution is ambiguous')
        require(data[name] == expected, 'Legacy custom resource differs from immutable official bytes: ' + name)
    for part in ('core', 'agent'):
        name = part + '/cmd/server/conf/app.yaml'
        original = immutable_bytes(SOURCE, record['source_commit'], name, read)
        normalized = production(original, version, part, mode)
        binary = data['1panel-' + part]
        elf(binary, arch)
        require(normalized in binary and (original == normalized or original not in binary),
                'Legacy custom binary has wrong immutable production configuration: ' + part)
        require(data['1panel-' + part + '.service'] == data['initscript/1panel-' + part + '.service'],
                'Legacy custom root service copy differs from official installer')
        source_facts[name] = byte_facts(original)
        configs[part] = {'source': byte_facts(original), 'normalized': byte_facts(normalized)}
    from resolved_inventory import vendor_base
    require(isinstance(origin_pin, dict) and origin_pin.get('url') == vendor_base(version, mode, 'official') +
            f'1panel-{version}-linux-amd64.tar.gz', 'Legacy custom GeoIP requires exact canonical vendor origin')
    origin_facts = {key: origin_pin[key] for key in ('bytes', 'sha256')}
    file_facts(origin_facts)
    require(origin_facts['bytes'] <= 512 * 1024 ** 2 and origin_path is not None,
            'Legacy custom GeoIP official origin is unavailable or oversized')
    origin = archive_bytes(origin_path, origin_facts, f'1panel-{version}-linux-amd64', selected={'GeoIP.mmdb'})
    require(set(origin) == {'GeoIP.mmdb'} and origin['GeoIP.mmdb'] == data['GeoIP.mmdb'],
            'Legacy custom GeoIP differs from authenticated official archive')
    claims = {key: manifest[key] for key in ('go_version', 'node_version', 'npm_version') if key in manifest}
    return data, {'manifest_sha256': byte_facts(data['manifest.json'])['sha256'],
                  'source_commit': record['source_commit'], 'installer_commit': record['installer_commit'],
                  'producer_commit': record['build_repository_commit'], 'source_files': source_facts,
                  'installer_resources': installer_facts, 'configuration': configs,
                  'geoip': {'archive': origin_pin, 'member': byte_facts(data['GeoIP.mmdb'])},
                  'producer_toolchain_claims': claims, 'producer_toolchain_independently_verified': False}


def materialize_legacy(version, mode, arch, work, *, client=None, download,
                       read=canonical_read, discover=discover_vendor, acquire=acquire_origin):
    """Authenticate before acquisition, then recheck server identities before return."""
    client = client or ControlGitHub(UPSTREAM, version)
    require(arch in ARCHES, 'Unsupported legacy custom predecessor architecture')
    control = authenticate_controls(version, mode, client)
    record = control['records'][arch]
    selected = next(asset for asset in control['release']['assets'] if asset['name'] == record['file'])
    sidecar = next(asset for asset in control['release']['assets'] if asset['name'] == record['file'] + '.sha256')
    require(sidecar['size'] <= MAX_CONTROL, 'Oversized legacy custom selected checksum')
    sidecar_raw = client.asset_bytes(sidecar)
    require(byte_facts(sidecar_raw) == control['proof']['files'][sidecar['name']] and
            sidecar_raw == (record['sha256'] + '  ' + record['file'] + '\n').encode(),
            'Legacy custom selected archive checksum is not bound to exact bytes/name')
    path = Path(work) / 'source.tar.gz'
    download(selected, path)
    inventory = discover(version, mode, 'official', read=read)
    require('amd64' in inventory['archives'], 'Legacy custom GeoIP official origin cannot be authenticated')
    origin_pin = inventory['archives']['amd64']
    from resolved_inventory import vendor_base
    file_facts({key: origin_pin[key] for key in ('bytes', 'sha256')})
    require(origin_pin['bytes'] <= 512 * 1024 ** 2 and origin_pin.get('url') ==
            vendor_base(version, mode, 'official') + f'1panel-{version}-linux-amd64.tar.gz',
            'Legacy custom GeoIP origin must be canonical and bounded before acquisition')
    origin_path = acquire(origin_pin, Path(work) / 'legacy-origin')
    body, resources = verify_source(path, version, mode, arch, record, origin_path, origin_pin, read=read)
    require(discover(version, mode, 'official', read=read)['archives'].get('amd64') == origin_pin,
            'Legacy custom GeoIP canonical origin changed during authentication')
    require(snapshot(complete_release(client, version, mode, control['release']['id'])) == snapshot(control['release']),
            'Legacy custom release changed during authentication')
    fresh = api(client, f'repos/{UPSTREAM}/actions/runs/{control["run"]["id"]}')
    require(run_identity(fresh, control['proof']) == control['run'],
            'Legacy custom receipt run changed during authentication')
    return body, {'kind': 'authenticated-legacy-custom-source',
                  'pin': {'bytes': record['size'], 'sha256': record['sha256']},
                  'asset_id': selected['id'], 'release_id': control['release']['id'],
                  'repository': UPSTREAM, 'receipt_sha256': control['receipt_sha256'],
                  'aggregate_sha256': control['aggregate_sha256'], 'record': record,
                  'validation_run': control['run'], 'validator': control['validator'],
                  'resources': resources, 'historical_native_acceptance': 'not-claimed',
                  'required_acceptance': 'actual predecessor installation and target rollback/upgrade in this run'}
