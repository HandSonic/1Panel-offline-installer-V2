#!/usr/bin/env python3
"""Read immutable upper contracts and canonical vendor discovery into a run plan.

Only the hosted workflow calls this transport. Tests inject readers; local
validation never downloads package bodies implicitly.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import re
import selectors
import time
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from publication_contract import read_job_log, verify_validation_log
from release_asset_repair import GitHub, digest as file_digest
from resolved_inventory import (ARCHES, MAX_CONTROL, canonical, digest, file_facts,
    object_bytes, package_plan, require, source_contract, vendor_base, vendor_inventory)
from runtime_contract import byte_facts, validate

UPSTREAM = 'HandSonic/1Panel-Build-v2'


class ControlGitHub(GitHub):
    def asset_bytes(self, asset):
        require(type(asset.get('id')) is int and asset['id'] > 0 and
                type(asset.get('size')) is int and 0 < asset['size'] <= MAX_CONTROL,
                'Invalid small upstream control asset')
        endpoint = f'repos/{self.repo}/releases/assets/{asset["id"]}'
        with tempfile.TemporaryFile() as errors:
            with subprocess.Popen(['gh', 'api', '-H', 'Accept: application/octet-stream', endpoint],
                                  stdout=subprocess.PIPE, stderr=errors) as process:
                try:
                    chunks,total,deadline = [],0,time.monotonic()+90
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout,selectors.EVENT_READ)
                        while True:
                            remaining=deadline-time.monotonic()
                            require(remaining>0 and selector.select(remaining), 'Upstream control download timed out')
                            block=process.stdout.read1(min(65536,asset['size']+1-total))
                            if not block:break
                            chunks.append(block);total+=len(block)
                            require(total<=asset['size'], 'Upstream control exceeds pinned size')
                    raw=b''.join(chunks)
                    require(len(raw) == asset['size'] and process.wait(timeout=max(0.01,deadline-time.monotonic())) == 0,
                            'Upstream control download size/status mismatch')
                except BaseException:
                    process.kill(); process.wait()
                    raise
        require(asset.get('digest') == 'sha256:' + hashlib.sha256(raw).hexdigest(),
                'Upstream control differs from immutable GitHub asset digest')
        return raw


def canonical_read(url, method='GET', limit=MAX_CONTROL):
    try:
        with urlopen(Request(url, method=method, headers={'User-Agent': '1Panel-offline-input-validator'}), timeout=60) as response:
            require(response.geturl() == url and response.status == 200,
                    'Canonical vendor/source endpoint redirected or changed status')
            if method == 'HEAD':
                length = response.headers.get('Content-Length', '')
                require(length.isdigit() and int(length) > 0, 'Canonical archive size unavailable')
                return {'status': 200, 'bytes': int(length)}
            raw = response.read(limit + 1)
            require(len(raw) <= limit, 'Control response exceeds bounded size')
            return {'status': 200, 'body': raw}
    except HTTPError as error:
        if error.code == 404:
            return {'status': 404, **({'bytes': None} if method == 'HEAD' else {'body': b''})}
        raise ValueError('Canonical endpoint unavailable: HTTP ' + str(error.code)) from None


def discover_vendor(version, mode, source, read=canonical_read):
    from runtime_inventory import vendor
    base = vendor_base(version, mode, source)
    def observed(url, method='GET'):
        try:
            return read(url, method=method)
        except (OSError, ValueError, TimeoutError):
            return {'status': 0, **({'bytes': None} if method == 'HEAD' else {'body': b''})}
    checksum = {'url': base + 'checksums.txt', **observed(base + 'checksums.txt')}
    probes = {arch: {'url': base + f'1panel-{version}-linux-{arch}.tar.gz',
                     **observed(base + f'1panel-{version}-linux-{arch}.tar.gz', method='HEAD')}
              for arch in ARCHES}
    return vendor(version, mode, source, checksum, probes)


def acquire_origin(pin, cache):
    """Bounded official GeoIP origin download, used only by package jobs."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / ('geoip-origin-' + pin['sha256'] + '.tar.gz')
    if target.exists():
        require(target.is_file() and not target.is_symlink(), 'Unsafe cached origin archive')
        # The protected archive validator rechecks full content before use.
        return target
    partial = target.with_suffix('.part')
    require(not partial.exists() and not partial.is_symlink(), 'Stale GeoIP origin partial file')
    try:
        with urlopen(Request(pin['url']), timeout=60) as response, partial.open('xb') as output:
            require(response.status == 200 and response.geturl() == pin['url'], 'GeoIP origin endpoint changed')
            checksum, total = hashlib.sha256(), 0
            while True:
                block = response.read(min(1024 * 1024, pin['bytes'] + 1 - total))
                if not block:
                    break
                total += len(block)
                require(total <= pin['bytes'] <= 512 * 1024 ** 2, 'GeoIP origin archive exceeds pinned size')
                checksum.update(block); output.write(block)
            require(total == pin['bytes'] and checksum.hexdigest() == pin['sha256'], 'GeoIP origin archive pin mismatch')
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def verify_current_validation(client, proof, receipt_sha):
    """Bind dynamic receipt bytes to one successful job of its exact attempt."""
    run_id,attempt=proof['workflow_run_id'],proof['workflow_run_attempt']
    require(type(run_id) is int and run_id>0 and type(attempt) is int and attempt>0,
            'Invalid dynamic validation run identity')
    jobs=[]
    for page in range(1,101):
        response=object_bytes(client.run('api',f'repos/{UPSTREAM}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}').encode())
        rows=response.get('jobs');require(isinstance(rows,list),'Invalid validation jobs response')
        jobs.extend(rows)
        if len(rows)<100:
            require(response.get('total_count')==len(jobs),'Truncated validation job collection');break
    else:raise ValueError('Validation job page limit exceeded')
    matched=[]
    for job in jobs:
        if job.get('name') not in ('build','publication_prepare') or job.get('conclusion')!='success':continue
        require(type(job.get('id')) is int and job['id']>0 and job.get('run_id')==run_id and
                job.get('run_attempt')==attempt and job.get('head_sha')==proof['workflow_commit'] and
                job.get('status')=='completed','Receipt validator job identity mismatch')
        marks=re.findall(r'(?m)^(?:[0-9T:.-]+Z )?VERIFIED_RELEASE_RECEIPT_SHA256=([0-9a-f]{64})[ \t]*$',read_job_log(client,job['id']))
        require(len(marks)<=1,'Ambiguous receipt validation markers')
        if marks==[receipt_sha]:matched.append(job['id'])
    require(len(matched)==1,'Receipt is not bound to exactly one successful validation job')


def public_controls(version, mode, client=None):
    client = client or ControlGitHub(UPSTREAM, version)
    require(client.repo == UPSTREAM and client.tag == version, 'Wrong upstream transport target')
    release = client.release()
    require(type(release.get('id')) is int and release['id'] > 0 and
            release.get('tag_name') == version and release.get('draft') is False and
            release.get('prerelease') == (mode != 'stable'), 'Upstream canonical release identity mismatch')
    assets = {row['name']: row for row in release['assets']}
    require(len(assets) == len(release['assets']), 'Ambiguous upstream assets')
    controls = ('release-validation.json', 'resolved-source.json', 'build-manifest.json', 'checksums.txt')
    require(set(controls) <= set(assets), 'Resolved upstream contract has not been published')
    bodies = {name: client.asset_bytes(assets[name]) for name in controls}
    proof = object_bytes(bodies['release-validation.json'])
    manifest = object_bytes(bodies['build-manifest.json'])
    matrix = manifest if manifest.get('schema_version') == 2 else None
    if matrix is not None:
        from upstream_outcomes import validate as validate_matrix
        accepted = validate_matrix(matrix, version)
    else:
        accepted = list(ARCHES)
    expected = {f'1panel-{version}-linux-{a}.tar.gz' for a in accepted}
    expected |= {name + '.sha256' for name in tuple(expected)}
    expected |= {'resolved-source.json', 'build-manifest.json', 'build-inputs.env', 'checksums.txt'}
    require(type(proof.get('schema')) is int and proof.get('schema') == (2 if matrix is not None else 1) and
            proof.get('contract') == ('upstream-matrix' if matrix is not None else 'upstream7') and
            proof.get('version') == version and proof.get('release_tag') == version and
            proof.get('repository') == UPSTREAM and set(proof.get('files', {})) == expected,
            'Upstream receipt identity or full architecture inventory mismatch')
    for name, pin in proof['files'].items():
        file_facts(pin)
        require(name in assets and assets[name].get('size') == pin['bytes'] and
                assets[name].get('digest') == 'sha256:' + pin['sha256'],
                'Upstream receipt differs from current canonical asset: ' + name)
    for name in controls[1:]:
        require(byte_facts(bodies[name]) == proof['files'][name], 'Upstream control is not receipt-bound')
    attempt=proof.get('workflow_run_attempt')
    require(matrix is None or (type(attempt) is int and attempt>0), 'Exact receipt validation attempt required')
    run_endpoint=f'repos/{UPSTREAM}/actions/runs/{proof["workflow_run_id"]}' + (f'/attempts/{attempt}' if matrix else '')
    run = object_bytes(client.run('api', run_endpoint).encode())
    require(run.get('id') == proof['workflow_run_id'] and run.get('head_sha') == proof.get('workflow_commit') and
            (matrix is None or run.get('run_attempt')==attempt) and
            run.get('status') == 'completed' and run.get('conclusion') in (('success', 'failure') if matrix else ('success',)) and
            run.get('path', '').split('@')[0] == '.github/workflows/build.yml' and
            run.get('event') in ('push', 'schedule', 'workflow_dispatch') and
            all(run.get(key, {}).get('full_name') == UPSTREAM for key in ('repository', 'head_repository')),
            'Upstream receipt is not a successful trusted repository workflow')
    receipt_sha = hashlib.sha256(bodies['release-validation.json']).hexdigest()
    (verify_current_validation if matrix is not None else verify_validation_log)(client, proof, receipt_sha)
    rows = manifest.get('artifacts')
    require(isinstance(rows, list) and len(rows) == len(accepted) and rows and
            {r.get('architecture') for r in rows} == set(accepted), 'Incomplete upstream aggregate')
    if matrix is not None:
        from upstream_outcomes import verify_jobs
        verify_jobs(matrix, version, rows[0]['build_repository_commit'], client)
        require(proof.get('requested_architectures') == matrix['requested_architectures'] and
                proof.get('successful_architectures') == accepted and proof.get('outcomes') == matrix['outcomes'] and
                proof.get('producer_run_id') == matrix['producer_run_id'] and
                proof.get('producer_run_attempt') == matrix['producer_run_attempt'] and
                proof.get('producer_head_sha') == matrix['producer_head_sha'],
                'Upstream receipt outcomes differ from validated producer matrix')
    records = {r['architecture']: r for r in rows}
    for row in rows:
        require(proof['files'].get(row.get('file')) == {'bytes': row.get('size'), 'sha256': row.get('sha256')},
                'Upstream aggregate archive differs from receipt')
    sums = {}
    for line in bodies['checksums.txt'].decode().splitlines():
        sha, name = line.split('  ', 1)
        require(name not in sums, 'Duplicate upper checksum row')
        sums[name] = sha
    require(sums == {r['file']: r['sha256'] for r in rows}, 'Upper checksums differ from aggregate')
    contract_sha = proof['files']['resolved-source.json']['sha256']
    contract = source_contract(bodies['resolved-source.json'], contract_sha, version, mode)
    require(canonical(contract) == bodies['resolved-source.json'], 'Upper resolved source must have canonical bytes')
    # Asset IDs, immutable hashes and identity must remain unchanged across reads.
    after = client.release()
    keys = ('id', 'name', 'size', 'digest')
    require(after.get('id') == release['id'] and after.get('tag_name') == version and
            after.get('draft') is False and
            {a['name']: {k: a.get(k) for k in keys} for a in after['assets']} ==
            {name: {k: a.get(k) for k in keys} for name, a in assets.items()},
            'Upstream release changed during resolution')
    return contract, contract_sha, {'repository': UPSTREAM, 'source_kind': 'verified-public-release',
        'validation_sha256': receipt_sha, 'validation_run_id': proof['workflow_run_id'],
        'validation_commit': proof['workflow_commit'], 'producer_commit': rows[0]['build_repository_commit'],
        'records': records, 'matrix_manifest': matrix}


def ci_controls(directory, version, mode, provenance):
    """The caller has authenticated the exact ZIP and its successful producer."""
    directory = Path(directory)
    raw = (directory / 'resolved-source.json').read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    contract = source_contract(raw, sha, version, mode)
    require(raw == canonical(contract), 'CI source contract is not canonical')
    manifest = object_bytes((directory / 'build-manifest.json').read_bytes())
    matrix = manifest if manifest.get('schema_version') == 2 else None
    if matrix is not None:
        from upstream_outcomes import verify_jobs
        accepted = verify_jobs(matrix, version, provenance['build_repository_commit'], ControlGitHub(UPSTREAM, version))
        require(matrix['producer_run_id'] == provenance['run_id'], 'CI matrix producer run differs from exact input')
    else:
        accepted = list(ARCHES)
    rows = manifest.get('artifacts')
    require(isinstance(rows, list) and len(rows) == len(accepted) and rows and
            {r.get('architecture') for r in rows} == set(accepted), 'CI aggregate architecture inventory mismatch')
    records = {r['architecture']: r for r in rows}
    wanted = {f'1panel-{version}-linux-{arch}.tar.gz' for arch in accepted}
    expected = wanted | {name + '.sha256' for name in wanted} | {
        'resolved-source.json', 'build-manifest.json', 'build-inputs.env', 'checksums.txt'}
    require({p.name for p in directory.iterdir()} == expected and
            all(p.is_file() and not p.is_symlink() for p in directory.iterdir()),
            'CI aggregate file inventory mismatch')
    sums = {}
    for line in (directory / 'checksums.txt').read_text().splitlines():
        value, name = line.split('  ', 1)
        require(name not in sums, 'Duplicate CI aggregate checksum')
        sums[name] = value
    require(sums == {row['file']: row['sha256'] for row in rows}, 'CI aggregate checksum set mismatch')
    for row in rows:
        require(row['file'] in wanted and row.get('build_repository_commit') == provenance['build_repository_commit'] and
                row.get('resolved_contract_sha256') == sha, 'CI aggregate producer/contract identity mismatch')
        require(file_digest(directory / row['file']) == {'sha256': row['sha256'], 'bytes': row['size']},
                'CI aggregate archive differs from authenticated manifest')
        require((directory / (row['file'] + '.sha256')).read_text().strip() == row['sha256'] + '  ' + row['file'],
                'CI archive sidecar differs from aggregate')
    return contract, sha, {'repository': UPSTREAM, 'source_kind': 'verified-ci-artifact',
        'validation_sha256': provenance['artifact_sha256'], 'validation_run_id': provenance['run_id'],
        'validation_commit': provenance['build_repository_commit'],
        'producer_commit': provenance['build_repository_commit'], 'records': records, 'matrix_manifest': matrix}


def resolve(version, mode, root, *, client=None, read=canonical_read,
            upstream_directory=None, provenance=None, custom_error=None):
    contract, sha, upstream, sources, failure = None, None, None, {}, None
    try:
        if custom_error:raise ValueError(custom_error)
        if upstream_directory is None:
            contract, sha, upstream = public_controls(version, mode, client)
        else:
            contract, sha, upstream = ci_controls(upstream_directory, version, mode, provenance)
        for component, profile in contract['configuration'].items():
            url = ('https://raw.githubusercontent.com/1Panel-dev/1Panel/' +
                   contract['source']['commit'] + '/' + profile['path'])
            response = read(url)
            require(response['status'] == 200 and byte_facts(response['body']) ==
                    {'sha256': profile['source_sha256'], 'bytes': profile['source_bytes']},
                    'Immutable official source configuration mismatch')
            sources[component] = response['body'].decode('utf-8')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        # This origin has no authority over independent canonical vendor bytes.
        detail = ' '.join(str(error).split())[:384] if isinstance(error, ValueError) else type(error).__name__
        failure = 'Custom input authentication failed: ' + detail
        contract, sha, upstream, sources = None, None, None, {}
    official = discover_vendor(version, mode, 'official', read)
    enterprise = discover_vendor(version, mode, 'enterprise', read)
    dependencies = {name: object_bytes((root / (name + '-sources.json')).read_bytes())
                    for name in ('docker', 'compose')}
    from runtime_inventory import plan as runtime_plan
    inventory, _ = runtime_plan(version, mode, contract, sha, official, enterprise,
                                dependencies['docker'], dependencies['compose'])
    value = {'schema': 1, 'kind': '1panel-resolved-offline-inputs', 'version': version,
             'mode': mode, 'source_contract': contract, 'source_contract_sha256': sha,
             'inventory': inventory, 'upstream': upstream, 'configuration_sources': sources}
    if failure:value['custom_failure'] = failure
    return validate(value, version, root)
