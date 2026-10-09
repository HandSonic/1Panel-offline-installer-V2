#!/usr/bin/env python3
"""Bootstrap an actual canonical predecessor, then bind a same-run target.

Public v1 receipts authenticate publication bytes through GitHub asset/run/log
identities. They never assert historical native acceptance. The unchanged old
installer must be installed and tested afresh in this candidate run. Each source
is independently authenticated; official does not depend on custom controls.
No checked-in per-version predecessor records or expired Actions ZIPs are used.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import Request, urlopen

from native_candidate_input import (CandidateGitHub, CONTROL_LIMIT, SHARD_LIMIT,
    api, current_identity, json_object, listed, positive, require, sha256, verify_run)
from public_predecessor import (REPOSITORY, RECEIPT, API_ROOT, select_predecessor,
    release_identity, receipt_run_identity, channel, semver)
from publication_contract import ROOT, read_job_log
from release_asset_repair import digest
from resolved_inventory import ARCHES, canonical, file_facts, safe_path
from resolved_transport import (ControlGitHub, discover_vendor, public_controls,
    canonical_read, acquire_origin)
from validate_resolved_custom import archive_bytes, verify_archive, byte_facts
from validate_payload import APP_REQUIRED, PAYLOAD_REQUIRED, elf, docker
from installer_capabilities import inspect_installer

INPUT_STAGE = 'not-started'


def input_stage(name):
    global INPUT_STAGE
    INPUT_STAGE = name
    print('NATIVE_UPGRADE_INPUT_STAGE=' + name, flush=True)


def array_pages(client, endpoint):
    result = []
    for page in range(1, 101):
        data = json.loads(client.run('api', endpoint + f'?per_page=100&page={page}'))
        require(isinstance(data, list) and all(isinstance(row, dict) for row in data),
                'Malformed or failed GitHub pagination')
        result.extend(data)
        if len(data) < 100:
            return result
    raise ValueError('GitHub catalogue exceeds bounded pagination')


def public_release(client, release_id):
    release = api(client, f'repos/{client.repo}/releases/{release_id}')
    require(release.get('id') == release_id, 'Immutable release ID changed')
    release['assets'] = array_pages(client, f'repos/{client.repo}/releases/{release_id}/assets')
    return release


def asset_pin(asset, name, repository=REPOSITORY):
    require(isinstance(asset, dict) and positive(asset.get('id')) and asset.get('name') == name and
            asset.get('state') == 'uploaded' and
            asset.get('url') == 'https://api.github.com/repos/' + repository + '/releases/assets/' + str(asset['id']),
            'Public asset identity/endpoint mismatch')
    require(isinstance(asset.get('digest'), str) and asset['digest'].startswith('sha256:'),
            'Public asset has no server digest')
    facts = {'sha256': asset['digest'][7:], 'bytes': asset.get('size')}
    file_facts(facts)
    require(facts['bytes'] <= SHARD_LIMIT, 'Oversized public archive')
    return {'asset_id': asset['id'], 'name': name, **facts}


def release_snapshot(release, repository=REPOSITORY):
    # download_count changes when this verifier reads bytes; it is not identity.
    identity = {k: release.get(k) for k in ('id', 'tag_name', 'draft', 'prerelease', 'url')}
    assets = [asset_pin(a, a['name'], repository) for a in release['assets']]
    require(len({a['name'] for a in assets}) == len(assets) and
            len({a['asset_id'] for a in assets}) == len(assets), 'Ambiguous release snapshot')
    return {'release': identity, 'assets': sorted(assets, key=lambda a: a['asset_id'])}


def run_snapshot(run):
    return {k: run.get(k) for k in ('id', 'head_sha', 'run_attempt', 'path', 'event',
                                  'status', 'conclusion', 'repository', 'head_repository')}


class PredecessorGitHub(CandidateGitHub):
    def download_asset(self, asset, destination):
        pin = asset_pin(asset, asset['name'], self.repo)
        with Path(destination).open('xb') as output, tempfile.TemporaryFile() as errors:
            with subprocess.Popen(['gh', 'api', '-H', 'Accept: application/octet-stream',
                    f'repos/{self.repo}/releases/assets/{pin["asset_id"]}'],
                    stdout=subprocess.PIPE, stderr=errors) as process:
                total = 0
                try:
                    while True:
                        block = process.stdout.read(min(1024 * 1024, pin['bytes'] + 1 - total))
                        if not block:
                            break
                        total += len(block)
                        require(total <= pin['bytes'], 'Public archive exceeds pinned byte limit')
                        output.write(block)
                    require(process.wait(timeout=90) == 0, 'Public archive download failed')
                except BaseException:
                    process.kill(); process.wait()
                    raise
        require(digest(destination) == {k: pin[k] for k in ('bytes', 'sha256')},
                'Public archive download differs from server digest/size')


def verify_receipt_log(client, run, receipt_sha, proof=None):
    """Authenticate the receipt's validator, without claiming old native coverage."""
    jobs = listed(client, f'repos/{client.repo}/actions/runs/{run["id"]}/jobs?filter=all', 'jobs')
    modern = proof is not None and proof.get('schema') == 2
    allowed = ('publication_acceptance',) if modern else ('build', 'publication_prepare', 'publication_revalidate')
    matches = []
    for job in jobs:
        if job.get('name') not in allowed or job.get('conclusion') != 'success':
            continue
        require(positive(job.get('id')) and job.get('run_id') == run['id'] and
                job.get('head_sha') == run['head_sha'] and job.get('status') == 'completed' and
                positive(job.get('run_attempt')) and job['run_attempt'] <= run['run_attempt'],
                'Receipt validator run/head/attempt mismatch')
        if modern and job['run_attempt'] != proof['workflow_run_attempt']:
            continue
        log = read_job_log(client, job['id'])
        hashes = re.findall(r'(?m)^(?:[0-9T:.-]+Z )?VERIFIED_RELEASE_RECEIPT_SHA256=([0-9a-f]{64})[ \t]*$', log)
        require(len(hashes) <= 1, 'Ambiguous public receipt validation log')
        if hashes == [receipt_sha]:
            if modern:
                native = re.findall(r'(?m)^(?:[0-9T:.-]+Z )?NATIVE_ACCEPTANCE_SHA256=([0-9a-f]{64})[ \t]*$', log)
                require(native == [proof['files']['native-acceptance.json']['sha256']],
                        'Public native acceptance is not bound to exact acceptance job log')
            matches.append({'job_id': job['id'], 'job_name': job['name'],
                            'attempt': job['run_attempt'], 'log_sha256': hashlib.sha256(log.encode()).hexdigest()})
    require(matches, 'Public receipt validator log unavailable; bootstrap cannot authenticate this receipt')
    require(len({m['attempt'] for m in matches}) == len(matches), 'Ambiguous receipt validator jobs')
    return max(matches, key=lambda row: row['attempt'])


def public_controls_binding(release, receipt, checksums, run):
    identity = release_identity(release)
    require(not identity['draft'] and channel(identity['tag_name']) is not None and
            identity['prerelease'] == (channel(identity['tag_name']) != 'stable'), 'Public release is not canonical')
    proof = json_object(receipt)
    version = release['tag_name']
    modern = proof.get('schema') == 2
    require(type(proof.get('schema')) is int and proof['schema'] in (1, 2) and
            all(proof.get(k) == v for k, v in {'contract': 'downstream-matrix' if modern else 'downstream17', 'repository': REPOSITORY,
                'version': version, 'release_tag': version}.items()), 'Canonical receipt identity mismatch')
    if modern:
        from runtime_native_acceptance import preparation_rows
        require('native-acceptance.json' in proof.get('files', {}) and sha256(proof.get('preparation_receipt_sha256')),
                'Final public matrix receipt and native acceptance required')
        structural = dict(proof, files={n: f for n, f in proof['files'].items() if n != 'native-acceptance.json'})
        _, outcomes = preparation_rows(structural)
        require(positive(proof.get('workflow_run_attempt')) and run.get('run_attempt') >= proof['workflow_run_attempt'] and
                run.get('status') == 'completed' and run.get('conclusion') in ('success', 'failure'),
                'Public acceptance run is incomplete/cancelled')
        require(run['conclusion'] == 'success' or any(r['status'] == 'failure' for r in outcomes.values()),
                'Failed workflow has no explicit failed public outcome')
        # Reuse strict repository/path/head checks; only per-product failure is
        # admitted, then the exact successful acceptance job is checked below.
        run_identity = receipt_run_identity(dict(run, conclusion='success'), proof)
    else:
        run_identity = receipt_run_identity(run, proof)
    require(sha256(proof.get('policy_fingerprint')), 'Malformed historical policy claim')
    files = proof.get('files')
    require(isinstance(files, dict) and 'checksums.txt' in files, 'Missing public file inventory')
    pattern = re.compile(r'1panel-' + re.escape(version) + r'-(official|custom|enterprise-original|enterprise-docker)-offline-linux-(' + '|'.join(ARCHES) + r')\.tar\.gz')
    for name, facts in files.items():
        require(name in ('checksums.txt', 'native-acceptance.json') or pattern.fullmatch(name), 'Unexpected public package identity')
        file_facts(facts)
    require(byte_facts(checksums) == files['checksums.txt'], 'Public checksum control mismatch')
    sums = {}
    for line in checksums.decode().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([^/\\]+)', line)
        require(match is not None and match[2] not in sums, 'Malformed/duplicate public checksums')
        sums[match[2]] = match[1]
    require(sums == {n: p['sha256'] for n, p in files.items() if n.endswith('.tar.gz')},
            'Public receipt/checksums membership mismatch')
    pins, ids = {}, set()
    expected = {**files, RECEIPT: byte_facts(receipt)}
    recovery_bases = set(expected)
    if modern:
        # A failed product is deliberately retired to a recoverable backup. It
        # remains requested but is absent from the admitted public file set.
        recovery_bases.update(f'1panel-{version}-{row["source"]}-offline-linux-{row["arch"]}.tar.gz'
                              for row in proof['requested_products'])
    for asset in release['assets']:
        name = asset.get('name')
        require(isinstance(name, str) and name not in pins, 'Duplicate or malformed public asset')
        pin = asset_pin(asset, name)
        require(pin['asset_id'] not in ids, 'Duplicate public asset ID')
        ids.add(pin['asset_id'])
        if name not in expected:
            backup = re.fullmatch(r'(.+)\.backup-[0-9a-f]{12}', name)
            require(backup is not None and backup[1] in recovery_bases, 'Unexpected/staged public asset')
        else:
            require({k: pin[k] for k in ('bytes', 'sha256')} == expected[name],
                    'Public asset server bytes differ from receipt')
        pins[name] = pin
    require(set(expected) <= set(pins), 'Incomplete public release control/assets')
    return {'schema': 1, 'kind': 'public-controls-byte-binding',
            'version': version, 'mode': channel(version), 'repository': REPOSITORY,
            'release_id': release['id'], 'receipt_run': run_identity,
            'receipt_sha256': byte_facts(receipt)['sha256'], 'assets': pins}


def bind_public(selection, release, receipt, checksums, run, source, arch):
    require(release_identity(release) == selection['release'], 'Selected public release changed; no fallback')
    value = public_controls_binding(release, receipt, checksums, run)
    require(semver(value['version'], selection['mode']) < semver(selection['target_version'], selection['mode']),
            'Predecessor must be strictly earlier in the same channel')
    name = f'1panel-{value["version"]}-{source}-offline-linux-{arch}.tar.gz'
    require(name in json_object(receipt)['files'], 'Selected predecessor flavor/architecture was not published; no fallback')
    return dict(value, kind='current-run-public-predecessor-bootstrap', selection=selection,
                archive=value['assets'][name], historical_native_acceptance='not-claimed')


def verify_public_acceptance(client, release, receipt_bytes, checksum_bytes):
    """Authenticate schema2 final public controls for idempotent current checks.

    No predecessor selection or installation. Caller independently compares its
    current policy fingerprint and complete requested product matrix. Expired
    validator logs remain a hard blocker until signed-proof support is reviewed.
    """
    require(client.repo == REPOSITORY, 'Unexpected public acceptance repository')
    proof = json_object(receipt_bytes)
    require(proof.get('schema') == 2 and positive(proof.get('workflow_run_id')),
            'Final schema2 public acceptance receipt required')
    # Fetch complete assets ourselves; a truncated embedded release asset list
    # cannot authorize a no-op or hide a leftover staged asset.
    fresh = public_release(client, release['id'])
    require(release_identity(fresh) == release_identity(release), 'Public release changed before authentication')
    run = api(client, f'repos/{REPOSITORY}/actions/runs/{proof["workflow_run_id"]}')
    binding = public_controls_binding(fresh, receipt_bytes, checksum_bytes, run)
    validator = verify_receipt_log(client, run, binding['receipt_sha256'], proof)
    require(run_snapshot(api(client, f'repos/{REPOSITORY}/actions/runs/{run["id"]}')) == run_snapshot(run) and
            release_snapshot(public_release(client, release['id'])) == release_snapshot(fresh),
            'Public controls/run changed during authentication')
    return {'schema': 1, 'kind': 'authenticated-public-native-acceptance',
            'binding': binding, 'validator': validator, 'receipt': proof,
            'native_subject_sha256': proof['files']['native-acceptance.json']['sha256'],
            'authentication': 'exact successful publication_acceptance log; no signature claim'}


def download_url(pin, destination):
    file_facts({k: pin[k] for k in ('bytes', 'sha256')})
    require(pin['bytes'] <= SHARD_LIMIT, 'Source archive exceeds bounded size')
    with urlopen(Request(pin['url']), timeout=60) as response, Path(destination).open('xb') as output:
        require(response.status == 200 and response.geturl() == pin['url'], 'Canonical source redirected')
        total = 0
        while True:
            block = response.read(min(1024 * 1024, pin['bytes'] + 1 - total))
            if not block:
                break
            total += len(block)
            require(total <= pin['bytes'], 'Source archive exceeds pin')
            output.write(block)
    require(digest(destination) == {k: pin[k] for k in ('bytes', 'sha256')}, 'Source archive digest mismatch')


def source_archive(version, mode, source, arch, work, root=ROOT):
    """Official bootstrap never reads a custom producer contract or version table."""
    path = work / 'source.tar.gz'
    if source == 'official':
        inventory = discover_vendor(version, mode, 'official')
        require(arch in inventory['archives'], 'Predecessor vendor architecture unavailable')
        pin = inventory['archives'][arch]
        download_url(pin, path)
        provenance = {'kind': 'canonical-vendor', 'pin': pin,
                      'inventory_sha256': hashlib.sha256(canonical(inventory)).hexdigest()}
    else:
        require(source == 'custom', 'Only community packages use native upgrades')
        # Select by the public protocol's presence, never by catching a failed
        # modern authentication. Legacy evidence still requires fresh native
        # installation and actual target rollback/upgrade later in this run.
        upper = ControlGitHub('HandSonic/1Panel-Build-v2', version)
        release = upper.release()
        require(positive(release.get('id')), 'Custom source release identity unavailable')
        # The embedded release list can be truncated after retained backups.
        release['assets'] = array_pages(upper, f'repos/{upper.repo}/releases/{release["id"]}/assets')
        if not any(asset.get('name') == 'resolved-source.json' for asset in release['assets']):
            from legacy_predecessor_source import materialize_legacy
            custom = PredecessorGitHub(upper.repo, version)
            return materialize_legacy(version, mode, arch, work, client=upper,
                                      download=custom.download_asset)
        contract, contract_sha, upstream = public_controls(version, mode)
        record = upstream['records'][arch]
        pin = {'bytes': record['size'], 'sha256': record['sha256']}
        release = upper.release()
        assets = [a for a in release['assets'] if a.get('name') == record['file']]
        require(len(assets) == 1 and assets[0].get('size') == pin['bytes'] and
                assets[0].get('digest') == 'sha256:' + pin['sha256'] and positive(assets[0].get('id')),
                'Custom source canonical asset identity mismatch')
        # Use the same bounded immutable-ID download, with its actual repository.
        custom = PredecessorGitHub(upper.repo, version)
        custom.download_asset(assets[0], path)
        configs = {}
        for part, profile in contract['configuration'].items():
            response = canonical_read('https://raw.githubusercontent.com/1Panel-dev/1Panel/' + contract['source']['commit'] + '/' + profile['path'])
            require(response['status'] == 200 and byte_facts(response['body']) ==
                    {'bytes': profile['source_bytes'], 'sha256': profile['source_sha256']},
                    'Custom configuration source pin mismatch')
            configs[part] = response['body']
        origin = contract['resources']['geoip'].get('archive')
        origin_path = acquire_origin(origin, work / 'origin') if origin else None
        verify_archive(path, version, mode, arch, contract, contract_sha, pin,
                       upstream['producer_commit'], configs, record, origin_path)
        after = upper.release()
        fresh = [a for a in after['assets'] if a.get('name') == record['file']]
        require(after.get('id') == release['id'] and len(fresh) == 1 and
                asset_pin(fresh[0], record['file'], upper.repo) == asset_pin(assets[0], record['file'], upper.repo),
                'Custom source asset changed during verification')
        provenance = {'kind': 'resolved-custom-source', 'pin': pin, 'asset_id': assets[0]['id'],
                      'contract_sha256': contract_sha, 'upstream': upstream}
    body = archive_bytes(path, {k: pin[k] for k in ('bytes', 'sha256')}, f'1panel-{version}-linux-{arch}')
    return body, provenance


def write_verified_archive(body, modes, destination, archive_root):
    """Materialize only the protected reader's bytes and ordinary permissions."""
    require(set(body) == set(modes) and isinstance(archive_root, str) and archive_root and
            archive_root not in ('.', '..') and '\\' not in archive_root and
            Path(archive_root).name == archive_root, 'Invalid verified archive inventory/root')
    destination.mkdir(mode=0o700)
    package = destination / archive_root
    package.mkdir(mode=0o755)
    for name, raw in body.items():
        safe_path(name)
        require(type(modes[name]) is int and 0 <= modes[name] <= 0o777,
                'Unsafe verified archive permissions')
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(raw)
        target.chmod(modes[name])
    return package


def unpack_predecessor(path, destination, binding, source, arch, source_body, source_proof, root=ROOT):
    version = binding['version']
    pin = binding['archive']
    archive_root = Path(pin['name']).name.removesuffix('.tar.gz')
    modes = {}
    body = archive_bytes(path, {k: pin[k] for k in ('bytes', 'sha256')}, archive_root, modes=modes)
    require('offline-manifest.json' in body, 'Predecessor lacks offline manifest')
    manifest = json_object(body['offline-manifest.json'])
    require(manifest.get('source') == source and manifest.get('architecture') == arch and
            manifest.get('app_version') == version, 'Predecessor manifest identity mismatch')
    require(set(PAYLOAD_REQUIRED) == set(manifest['payloads']), 'Unexpected predecessor payload inventory')
    require(set(body) == set(source_body) | set(PAYLOAD_REQUIRED) | {'offline-manifest.json'},
            'Unmanifested or source-unbound predecessor files')
    for name, facts in manifest['payloads'].items():
        file_facts(facts)
        require(byte_facts(body[name]) == facts, 'Predecessor payload hash mismatch')
    app_pin = source_proof['pin']
    require(all(manifest['inputs']['app'].get(k) == app_pin[k] for k in ('bytes', 'sha256')),
            'Predecessor source archive pin mismatch')
    require(set(APP_REQUIRED) <= set(source_body), 'Authenticated source lacks required application resources')
    for name in set(source_body) - {'install.sh', 'upgrade.sh'}:
        require(body[name] == source_body[name], 'Predecessor source resource differs: ' + name)
    capabilities = inspect_installer(source_body['install.sh'], set(source_body), source)
    require(capabilities['edition_selection'] and byte_facts(body['install.sh'])['sha256'] == capabilities['patched_installer_sha256'],
            'Predecessor installer differs from authenticated source plus reviewed offline patch')
    require(body['docker.service'] == (root / 'docker.service').read_bytes(), 'Unreviewed predecessor Docker service')
    for component, payload in [('docker', 'docker.tgz'), ('compose', 'docker-compose')]:
        pin_dep = json_object((root / (component + '-sources.json')).read_bytes())[arch]
        actual = manifest['inputs'][component]
        require(all(actual.get(k) == pin_dep[k] for k in ('url', 'version', 'sha256')) and
                byte_facts(body[payload]) == {k: actual[k] for k in ('bytes', 'sha256')},
                'Unreviewed predecessor dependency source')
        if 'bytes' in pin_dep:
            require(actual['bytes'] == pin_dep['bytes'], 'Predecessor dependency size differs')
    require(docker(io.BytesIO(body['docker.tgz']), arch) == manifest['docker_binaries'], 'Predecessor Docker inventory mismatch')
    for name in ('1panel-core', '1panel-agent', 'docker-compose'):
        elf(body[name][:20], arch)
    package = write_verified_archive(body, modes, destination, archive_root)
    selected = {'source': source, 'arch': arch, 'files': {source + '/' + binding['archive']['name']:
        {k: binding['archive'][k] for k in ('bytes', 'sha256')}},
        'manifest_sha256': byte_facts(body['offline-manifest.json'])['sha256'],
        'installer_mode': capabilities['adapter'], 'source_provenance': source_proof,
        'binaries': {n: byte_facts(body[n])['sha256'] for n in ('1panel-core', '1panel-agent')}}
    return package, selected


def unpack_target(input_dir, provenance, destination, identity, receipt_sha, controls_id, root=ROOT):
    from validate_release import validate
    row = identity['row']
    expected = {'repository': identity['repository'], 'workflow_run_id': identity['run_id'],
                'workflow_commit': identity['head_sha'], 'workflow_run_attempt': identity['run_attempt'],
                'version': identity['version'], 'release_tag': identity['tag'],
                'source': row['source'], 'arch': row['arch'], 'receipt_sha256': receipt_sha}
    require(all(provenance.get(k) == v for k, v in expected.items()) and
            provenance['controls']['artifact_id'] == int(controls_id), 'Target same-run receipt identity mismatch')
    name = f'{row["source"]}/1panel-{identity["version"]}-{row["source"]}-offline-linux-{row["arch"]}.tar.gz'
    require(set(provenance['files']) == {name} and digest(input_dir / name) == provenance['files'][name] and
            provenance['archive_sha256'] == provenance['files'][name]['sha256'], 'Target whole archive hash mismatch')
    (input_dir / 'checksums.txt').write_text(provenance['archive_sha256'] + '  ' + Path(name).name + '\n')
    validate(input_dir, identity['version'], {row['source']: [row['arch']]}, root)
    # Apply bounded archive scan before extraction, independent of manifest claims.
    archive_root = Path(name).name.removesuffix('.tar.gz')
    modes = {}
    body = archive_bytes(input_dir / name, provenance['files'][name], archive_root, modes=modes)
    return write_verified_archive(body, modes, destination, archive_root)


def materialize(args, env=None, client=None, root=ROOT):
    input_stage('current-target-identity')
    env = os.environ if env is None else env
    identity = current_identity(args.version, args.tag or args.version, args.source, args.arch, env, root)
    require(args.source in ('official', 'custom') and args.arch in ('amd64', 'arm64'), 'Unsupported native upgrade row')
    mode = channel(args.version)
    require(mode is not None, 'Canonical target channel required')
    temp = Path(env['RUNNER_TEMP']).resolve()
    output, evidence = Path(args.output).absolute(), Path(args.provenance).absolute()
    target_input, target_proof = Path(args.target_input).resolve(), Path(args.target_provenance).resolve()
    for path in (output, evidence, target_input, target_proof):
        require('\n' not in str(path) and '\r' not in str(path) and
                path.resolve().is_relative_to(temp) and path.resolve() != temp and not path.is_symlink(),
                'Upgrade paths must be inside runner temporary directory')
    require(not output.exists() and not evidence.exists() and not evidence.is_relative_to(output) and
            not target_input.is_relative_to(output) and not target_proof.is_relative_to(output),
            'Upgrade outputs must be new and separate from inputs/evidence')
    client = client or PredecessorGitHub(REPOSITORY, args.version)
    require(client.repo == REPOSITORY, 'Wrong predecessor repository')
    input_stage('canonical-same-channel-predecessor')
    selection = select_predecessor(args.version, mode, array_pages(client, f'repos/{REPOSITORY}/releases'), catalogue_complete=True)
    release = public_release(client, selection['release']['id'])
    with tempfile.TemporaryDirectory(dir=temp, prefix='upgrade-bootstrap-') as work:
        work = Path(work)
        assets = {a['name']: a for a in release['assets']}
        require(len(assets) == len(release['assets']), 'Duplicate release asset names')
        controls = {}
        for name in (RECEIPT, 'checksums.txt'):
            require(name in assets and assets[name]['size'] <= CONTROL_LIMIT, 'Missing/oversized public control')
            client.download_asset(assets[name], work / name)
            controls[name] = (work / name).read_bytes()
        proof = json_object(controls[RECEIPT])
        require(positive(proof.get('workflow_run_id')), 'Invalid public receipt run')
        run = api(client, f'repos/{REPOSITORY}/actions/runs/{proof["workflow_run_id"]}')
        binding = bind_public(selection, release, controls[RECEIPT], controls['checksums.txt'], run, args.source, args.arch)
        binding['receipt_validator'] = verify_receipt_log(client, run, binding['receipt_sha256'], proof)
        if proof['schema'] == 2:
            native_asset = assets['native-acceptance.json']
            require(native_asset['size'] <= 16 * CONTROL_LIMIT, 'Public native acceptance exceeds limit')
            client.download_asset(native_asset, work / 'native-acceptance.json')
            require(digest(work / 'native-acceptance.json') == proof['files']['native-acceptance.json'],
                    'Public native acceptance bytes changed')
            binding['public_native_subject_sha256'] = proof['files']['native-acceptance.json']['sha256']
        input_stage('independent-predecessor-source-validation')
        body, source_proof = source_archive(binding['version'], mode, args.source, args.arch, work, root)
        archive = work / 'predecessor.tar.gz'
        client.download_asset(assets[binding['archive']['name']], archive)
        staging = work / 'verified'; staging.mkdir()
        predecessor, selected = unpack_predecessor(archive, staging / 'predecessor', binding, args.source, args.arch, body, source_proof, root)
        del body
        input_stage('current-target-independent-payload-validation')
        target_provenance = json_object(target_proof.read_bytes())
        target = unpack_target(target_input, target_provenance, staging / 'target', identity,
                               args.target_receipt_sha256, args.target_controls_id, root)
        input_stage('final-public-and-current-identities')
        require(release_snapshot(public_release(client, release['id'])) == release_snapshot(release),
                'Public predecessor changed during validation')
        require(run_snapshot(api(client, f'repos/{REPOSITORY}/actions/runs/{run["id"]}')) == run_snapshot(run),
                'Public receipt run changed')
        verify_run(client, identity)
        result = {'schema': 2, 'target': target_provenance,
                  'target_manifest_sha256': digest(target / 'offline-manifest.json')['sha256'],
                  'predecessor': binding, 'predecessor_package': selected,
                  'predecessor_binding_sha256': hashlib.sha256(canonical(binding)).hexdigest(),
                  'predecessor_upgrade_script': 'pinned historical bytes; never executed',
                  'required_acceptance': 'actual predecessor installation and target rollback/upgrade in this run',
                  'predecessor_package_path': str(output / predecessor.relative_to(staging)),
                  'target_package_path': str(output / target.relative_to(staging))}
        output.parent.mkdir(parents=True, exist_ok=True)
        evidence.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(output)
        with evidence.open('x') as stream:
            stream.write(json.dumps(result, indent=2, sort_keys=True) + '\n')
    return {'status': 'inputs-verified-native-acceptance-pending', 'provenance': str(evidence)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('version', 'source', 'arch', 'target-input', 'target-provenance', 'target-controls-id',
                 'target-receipt-sha256', 'output', 'provenance'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--tag', default='')
    try:
        print(json.dumps(materialize(parser.parse_args())))
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError):
        print('NATIVE_UPGRADE_INPUT_STAGE=' + INPUT_STAGE + ':failed', file=sys.stderr)
        print('Native upgrade input authentication failed; this source/architecture is blocked.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
