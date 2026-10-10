#!/usr/bin/env python3
"""Read generic dependency pins from an authenticated public producer's commit.

The caller first authenticates the public receipt and selected package. This
adapter rechecks that receipt's run and validator before reading the two small
original lock files at its immutable lower repository commit. A package's own
manifest, current checkout locks, mutable tags and alternate releases are never
dependency trust roots. Payload hashes, sizes and ELF/archive contents still
must be checked by unpack_predecessor before materialization.
"""
import base64
import binascii
import hashlib
import re

from native_candidate_input import api, json_object, positive
from public_predecessor import REPOSITORY, RECEIPT, receipt_run_identity
from resolved_inventory import (MAX_CONTROL, NATIVE, commit_value, file_facts,
                                hash_value, object_bytes, require)
from validate_resolved_custom import byte_facts


def original_lock(client, commit, component):
    """Read only one named regular file through the exact immutable Contents API."""
    require(client.repo == REPOSITORY and component in ('docker', 'compose'),
            'Unexpected historical dependency repository/file')
    commit_value(commit)
    name = component + '-sources.json'
    endpoint = f'repos/{REPOSITORY}/contents/{name}?ref={commit}'
    response = client.run('api', endpoint)
    require(isinstance(response, str) and 0 < len(response.encode('utf-8')) <= 3 * MAX_CONTROL,
            'Missing/oversized historical dependency Contents response')
    value = json_object(response)
    require(value.get('type') == 'file' and value.get('name') == name and
            value.get('path') == name and value.get('url') == 'https://api.github.com/' + endpoint and
            'target' not in value and 'submodule_git_url' not in value,
            'Historical dependency lock path/ref/type mismatch')
    blob = commit_value(value.get('sha'))
    require(value.get('git_url') == f'https://api.github.com/repos/{REPOSITORY}/git/blobs/{blob}',
            'Historical dependency lock blob repository mismatch')
    require(type(value.get('size')) is int and 0 < value['size'] <= MAX_CONTROL and
            value.get('encoding') == 'base64' and isinstance(value.get('content'), str) and
            0 < len(value['content']) <= 2 * MAX_CONTROL,
            'Missing/oversized historical dependency lock bytes')
    try:
        raw = base64.b64decode(value['content'].replace('\n', ''), validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError('Malformed historical dependency lock encoding') from error
    require(len(raw) == value['size'] and
            hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() == blob,
            'Historical dependency lock bytes differ from Git blob/size')
    return object_bytes(raw), {'path': name, 'url': value['url'], 'git_blob_sha': blob,
                               **byte_facts(raw)}


def official_pin(component, inventory, arch):
    """The reviewed native origins are fixed; dependency versions are not."""
    require(arch in NATIVE and component in ('docker', 'compose'),
            'Unsupported historical dependency product')
    pin = inventory.get(arch)
    require(isinstance(pin, dict) and {'url', 'version', 'sha256'} <= set(pin),
            'Missing historical native dependency pin')
    version = pin['version']
    numeric = r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'
    require(isinstance(version, str) and re.fullmatch(('v' if component == 'compose' else '') + numeric, version),
            'Invalid historical dependency version')
    platform = {'amd64': 'x86_64', 'arm64': 'aarch64'}[arch]
    expected = (f'https://download.docker.com/linux/static/stable/{platform}/docker-{version}.tgz'
                if component == 'docker' else
                f'https://github.com/docker/compose/releases/download/{version}/docker-compose-linux-{platform}')
    require(pin['url'] == expected, 'Historical dependency origin is not canonical')
    hash_value(pin['sha256'])
    selected = {key: pin[key] for key in ('url', 'version', 'sha256')}
    if 'bytes' in pin:
        file_facts({key: pin[key] for key in ('bytes', 'sha256')})
        selected['bytes'] = pin['bytes']
    return selected


def historical_dependency_pins(client, binding, receipt_bytes, source, arch):
    """Return (selected architecture pins, small authenticated source evidence).

    This is only the ordinary public predecessor path. Explicit candidate input
    keeps its separately authenticated runtime inventory. Old generic locks may
    omit size: their SHA-256 still binds the entire payload, while the receipt-
    bound package manifest supplies the size checked against the actual bytes.
    No missing proof or failed check enables a current-lock/manifest fallback.
    """
    from native_upgrade_input import verify_receipt_log

    require(client.repo == REPOSITORY and source in ('official', 'custom') and arch in NATIVE and
            binding.get('kind') == 'current-run-public-predecessor-bootstrap' and
            binding.get('repository') == REPOSITORY,
            'Authenticated ordinary public predecessor required for historical dependencies')
    receipt = object_bytes(receipt_bytes)
    receipt_facts = byte_facts(receipt_bytes)
    require(receipt_facts['sha256'] == binding.get('receipt_sha256'),
            'Historical dependency receipt bytes changed')
    modern = receipt.get('schema') == 2
    require(type(receipt.get('schema')) is int and receipt['schema'] in (1, 2) and
            receipt.get('contract') == ('downstream-matrix' if modern else 'downstream17') and
            receipt.get('version') == binding.get('version') == receipt.get('release_tag') and
            receipt.get('repository') == REPOSITORY and positive(receipt.get('workflow_run_id')),
            'Historical dependency receipt identity mismatch')
    commit = commit_value(receipt.get('workflow_commit'))
    name = f'1panel-{binding["version"]}-{source}-offline-linux-{arch}.tar.gz'
    archive = binding.get('archive')
    require(isinstance(archive, dict) and archive.get('name') == name and
            isinstance(receipt.get('files'), dict) and name in receipt['files'],
            'Historical dependency package is not receipt-bound')
    pin = {key: archive.get(key) for key in ('bytes', 'sha256')}
    file_facts(pin)
    require(receipt['files'][name] == pin and binding.get('assets', {}).get(name) == archive and
            all(binding.get('assets', {}).get(RECEIPT, {}).get(k) == v for k, v in receipt_facts.items()),
            'Historical dependency package/receipt asset binding mismatch')
    validator = binding.get('receipt_validator')
    require(isinstance(validator, dict) and set(validator) == {'job_id', 'job_name', 'attempt', 'log_sha256'} and
            positive(validator['job_id']) and positive(validator['attempt']),
            'Authenticated predecessor receipt validator required')
    hash_value(validator['log_sha256'])
    run = api(client, f'repos/{REPOSITORY}/actions/runs/{receipt["workflow_run_id"]}')
    if modern:
        require(run.get('status') == 'completed' and run.get('conclusion') in ('success', 'failure') and
                validator['job_name'] == 'publication_acceptance' and
                validator['attempt'] == receipt.get('workflow_run_attempt'),
                'Historical dependency acceptance run is incomplete/cancelled or changed')
        outcomes = receipt.get('outcomes')
        require(isinstance(outcomes, list), 'Historical dependency outcomes unavailable')
        selected = [r for r in outcomes if isinstance(r, dict) and
                    r.get('source') == source and r.get('arch') == arch]
        require(len(selected) == 1 and selected[0].get('status') == 'success' and
                (run['conclusion'] == 'success' or
                 any(isinstance(r, dict) and r.get('status') == 'failure' for r in outcomes)),
                'Historical dependency product lacks successful public acceptance')
        identity = receipt_run_identity(dict(run, conclusion='success'), receipt)
    else:
        identity = receipt_run_identity(run, receipt)
    require(identity == binding.get('receipt_run') and identity['head_sha'] == commit,
            'Historical dependency producer run/head/attempt changed')
    require(verify_receipt_log(client, run, receipt_facts['sha256'], receipt) == validator,
            'Historical dependency receipt validator changed')
    pins, controls = {}, {}
    for component in ('docker', 'compose'):
        inventory, controls[component] = original_lock(client, commit, component)
        pins[component] = {arch: official_pin(component, inventory, arch)}
    return pins, {'kind': 'authenticated-public-producer-dependencies', 'repository': REPOSITORY,
                  'workflow_commit': commit, 'receipt_sha256': receipt_facts['sha256'],
                  'receipt_run': identity, 'receipt_validator': dict(validator),
                  'archive': dict(archive), 'locks': controls,
                  'size_authority': {c: ('original-lock' if 'bytes' in pins[c][arch] else
                      'receipt-bound-package-manifest') for c in pins}}
