#!/usr/bin/env python3
"""Verify one same-run package shard against the complete aggregate receipt.

Downloads only the small controls and selected shard ZIPs. No release API writes,
package rebuilding, extraction subprocesses, or installation occur here.
"""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

from package_matrix import matrix_rows
from release_inventory import native_rows
from publication_contract import (PROOF, REPOS, ROOT, digest_bytes, expected_names,
                                  policy_fingerprint, read_job_log)
from release_asset_repair import GitHub, digest

WORKFLOW = '.github/workflows/build-offline-v2.yml'
CONTROLS = {'publication-work/control/' + PROOF,
            'publication-work/release/checksums.txt', 'matrix-input/plan.json'}
CONTROL_LIMIT = 1024 * 1024
SHARD_LIMIT = 2 * 1024 ** 3
SHA = re.compile(r'[0-9a-f]{64}')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def json_object(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate JSON field')
            result[key] = value
        return result
    value = json.loads(data, object_pairs_hook=unique)
    require(isinstance(value, dict), 'Expected JSON object')
    return value


def positive(value):
    return type(value) is int and value > 0


def sha256(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def current_identity(version, tag, source, arch, env, root=ROOT):
    require(bool(re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?', version)),
            'Invalid candidate version')
    require(tag == version or bool(re.fullmatch(re.escape(version) + r'-[A-Za-z0-9._-]+', tag)),
            'Invalid candidate release tag')
    repository = REPOS['downstream17']
    require(env.get('GITHUB_ACTIONS') == 'true' and
            env.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'GitHub-hosted Actions required')
    require(env.get('GITHUB_REPOSITORY') == repository, 'Unexpected candidate repository')
    require(env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and
            env.get('PUBLICATION_OPERATION') in ('validate-repair', 'repair-existing'),
            'Unsupported candidate workflow event')
    require(env.get('GITHUB_SERVER_URL') == 'https://github.com' and
            env.get('GITHUB_API_URL') == 'https://api.github.com' and
            env.get('GH_HOST', 'github.com') == 'github.com', 'Unexpected GitHub host')
    for key in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'):
        require(bool(re.fullmatch(r'[1-9][0-9]*', env.get(key, ''))), 'Invalid current workflow identity')
    head = env.get('GITHUB_SHA', '')
    require(bool(re.fullmatch(r'[0-9a-f]{40}', head)), 'Invalid current workflow commit')
    require(env.get('GITHUB_WORKFLOW_SHA') == head and
            env.get('GITHUB_WORKFLOW_REF', '').startswith(repository + '/' + WORKFLOW + '@refs/'),
            'Unexpected current workflow path/commit')
    rows = matrix_rows(version, root)
    row = {'source': source, 'arch': arch, 'key': source + '-' + arch}
    require(arch in ('amd64', 'arm64') and row in rows, 'Unreviewed native candidate row')
    return {'repository': repository, 'run_id': int(env['GITHUB_RUN_ID']),
            'head_sha': head, 'run_attempt': int(env['GITHUB_RUN_ATTEMPT']),
            'event': env['GITHUB_EVENT_NAME'], 'version': version, 'tag': tag,
            'row': row, 'rows': rows, 'native_rows': native_rows(version, root)}


class CandidateGitHub(GitHub):
    def download_zip(self, artifact, destination):
        # Inherit gh authentication; never print stderr, signed URLs, or tokens.
        endpoint = f'repos/{self.repo}/actions/artifacts/{artifact["id"]}/zip'
        with open(destination, 'xb') as target, tempfile.TemporaryFile() as errors:
            with subprocess.Popen(['gh', 'api', endpoint], stdout=subprocess.PIPE, stderr=errors) as process:
                total = 0
                try:
                    while True:
                        block = process.stdout.read(min(1024 * 1024, artifact['size_in_bytes'] + 1 - total))
                        if not block:
                            break
                        total += len(block)
                        require(total <= artifact['size_in_bytes'], 'Downloaded artifact exceeds declared ZIP size')
                        target.write(block)
                    require(process.wait() == 0, 'GitHub artifact download failed')
                except BaseException:
                    process.kill()
                    process.wait()
                    raise


def api(client, endpoint):
    return json_object(client.run('api', endpoint))


def listed(client, endpoint, field):
    result = []
    for page in range(1, 101):
        data = api(client, endpoint + ('&' if '?' in endpoint else '?') + f'per_page=100&page={page}')
        entries = data.get(field)
        require(isinstance(entries, list) and all(isinstance(v, dict) for v in entries),
                'Invalid GitHub collection')
        result.extend(entries)
        if len(entries) < 100:
            require(type(data.get('total_count')) is int and data['total_count'] == len(result),
                    'Incomplete GitHub collection')
            return result
    raise ValueError('Oversized GitHub collection')


def verify_run(client, identity):
    run = api(client, f'repos/{client.repo}/actions/runs/{identity["run_id"]}')
    require(run.get('id') == identity['run_id'] and run.get('head_sha') == identity['head_sha'] and
            run.get('run_attempt') == identity['run_attempt'], 'Current run/head/attempt mismatch')
    require(run.get('path', '').split('@')[0] == WORKFLOW and run.get('event') == identity['event'],
            'Unexpected candidate workflow path/event')
    for key in ('repository', 'head_repository'):
        require(isinstance(run.get(key), dict) and run[key].get('full_name') == client.repo and
                positive(run[key].get('id')), 'Current run repository mismatch')
    require(run.get('status') in ('queued', 'in_progress', 'waiting', 'pending', 'completed') and
            (run.get('status') != 'completed' or run.get('conclusion') == 'success'),
            'Candidate run is not active or successful')
    return run


def verify_artifact(artifact, name, identity, run, limit):
    require(positive(artifact.get('id')) and artifact.get('name') == name and
            artifact.get('expired') is False, 'Artifact identity/name/expiry mismatch')
    origin = artifact.get('workflow_run', {})
    require(isinstance(origin, dict) and origin.get('id') == identity['run_id'] and
            origin.get('head_sha') == identity['head_sha'] and
            origin.get('repository_id') == run['repository']['id'] and
            origin.get('head_repository_id') == run['head_repository']['id'], 'Artifact run/head/repository mismatch')
    require(positive(artifact.get('size_in_bytes')) and artifact['size_in_bytes'] <= limit and
            isinstance(artifact.get('digest'), str) and artifact['digest'].startswith('sha256:') and
            sha256(artifact['digest'][7:]), 'Artifact ZIP digest/size invalid')


def select_shard(artifacts, identity, prepare_attempt):
    expected = {row['key'] for row in identity['rows']}
    selected, names, ids = {}, set(), set()
    for artifact in artifacts:
        name = artifact.get('name', '')
        require(isinstance(name, str) and positive(artifact.get('id')), 'Malformed run artifact')
        require(name not in names and artifact['id'] not in ids, 'Ambiguous run artifacts')
        names.add(name)
        ids.add(artifact['id'])
        if not name.startswith('package-shard-'):
            continue
        match = re.fullmatch(r'package-shard-([1-9][0-9]*)-(.+)', name)
        require(match is not None and match[2] in expected, 'Unexpected package shard name/row')
        attempt, key = int(match[1]), match[2]
        require(attempt <= identity['run_attempt'], 'Future package shard attempt')
        if attempt <= prepare_attempt and (key not in selected or attempt > selected[key][0]):
            selected[key] = (attempt, artifact)
    require(set(selected) == expected, 'Missing package shard row')
    return selected[identity['row']['key']]


def select_prepare_attempt(client, controls, artifacts, identity):
    # A failed native-only rerun inherits the immutable successful preparation.
    # Never silently replace its supplied ID with another candidate or receipt.
    pattern = r'publication-controls-' + str(identity['run_id']) + r'-([1-9][0-9]*)'
    match = re.fullmatch(pattern, controls.get('name', ''))
    require(match is not None, 'Unexpected controls artifact name')
    attempt = int(match[1])
    require(attempt <= identity['run_attempt'], 'Future controls artifact attempt')
    for artifact in artifacts:
        name = artifact.get('name', '')
        if not isinstance(name, str) or not name.startswith('publication-controls-'):
            continue
        other = re.fullmatch(pattern, name)
        require(other is not None and int(other[1]) <= attempt, 'Superseded or unexpected controls artifact')
    jobs = listed(client, f'repos/{client.repo}/actions/runs/{identity["run_id"]}/jobs?filter=all', 'jobs')
    preparations = [job for job in jobs if job.get('name') == 'publication_prepare' and job.get('conclusion') == 'success']
    require(preparations and all(positive(job.get('run_attempt')) and
            job['run_attempt'] <= identity['run_attempt'] and job.get('run_id') == identity['run_id'] and
            job.get('head_sha') == identity['head_sha'] and job.get('status') == 'completed' for job in preparations),
            'Invalid successful preparation identity')
    require(max(job['run_attempt'] for job in preparations) == attempt,
            'Supplied controls are not from latest successful preparation')
    return attempt


def successful_job(client, identity, attempt, prepare=False):
    jobs = listed(client, f'repos/{client.repo}/actions/runs/{identity["run_id"]}/attempts/{attempt}/jobs', 'jobs')
    row = identity['row']
    def named(job):
        if prepare:
            return job.get('name') == 'publication_prepare'
        match = re.fullmatch(r'publication_packages \(([^)]+)\)', job.get('name', ''))
        if not match:
            return False
        fields = match[1].split(', ')
        return len(fields) == 3 and set(fields) == {row['source'], row['arch'], row['key']}
    matches = [job for job in jobs if named(job)]
    require(len(matches) == 1, 'Missing or ambiguous candidate producer job')
    job = matches[0]
    require(positive(job.get('id')) and job.get('run_id') == identity['run_id'] and
            job.get('run_attempt') == attempt and job.get('head_sha') == identity['head_sha'] and
            job.get('status') == 'completed' and job.get('conclusion') == 'success',
            'Candidate producer job did not succeed for exact run/head/attempt')
    return job


def verify_upload_log(log, artifact, receipt_sha=None):
    # Pair each finalized upload with its immediately preceding upload digest.
    # Separate uploads in publication_prepare must never supply mixed ID/hash/name.
    pending, records = None, []
    for raw in log.splitlines():
        line = re.sub(r'^[0-9T:.-]+Z ', '', raw)
        digest_match = re.fullmatch(r'SHA256 digest of uploaded artifact zip is ([0-9a-f]{64})', line)
        if digest_match:
            pending = digest_match[1]
        finalized = re.fullmatch(r'Artifact (.+)\.zip successfully finalized\. Artifact ID (?:is )?([1-9][0-9]*)', line)
        if finalized:
            records.append((finalized[1], int(finalized[2]), pending))
            pending = None
    matching = [r for r in records if r[0] == artifact['name'] or r[1] == artifact['id']]
    require(matching == [(artifact['name'], artifact['id'], artifact['digest'][7:])],
            'Artifact not bound to exact successful producer upload log')
    if receipt_sha is not None:
        hashes = re.findall(r'(?m)^(?:[0-9T:.-]+Z )?VERIFIED_RELEASE_RECEIPT_SHA256=([0-9a-f]{64})[ \t]*$', log)
        require(hashes == [receipt_sha], 'Aggregate receipt not bound to current preparation log')


def extract_zip(path, destination, expected, limit):
    """Extract an exact file whitelist, bounded both before and during streaming."""
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        require(len(infos) == len(expected) and {i.filename for i in infos} == set(expected),
                'ZIP file whitelist mismatch or duplicate members')
        require(sum(i.file_size for i in infos) <= limit, 'ZIP expanded size exceeds limit')
        for info in infos:
            name = PurePosixPath(info.filename)
            kind = (info.external_attr >> 16) & 0o170000
            require(not name.is_absolute() and '..' not in name.parts and '\\' not in info.filename and
                    str(name) == info.filename and not info.is_dir() and kind in (0, stat.S_IFREG) and
                    info.orig_filename == info.filename and not (info.external_attr & 0x10) and
                    not (info.flag_bits & 1) and info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                    'ZIP contains an unsafe or special member')
            require(0 < info.file_size <= expected[info.filename], 'ZIP member size invalid')
        destination.mkdir()
        for info in infos:
            target = destination / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            count = 0
            with archive.open(info) as source, target.open('xb') as output:
                while True:
                    data = source.read(min(1024 * 1024, info.file_size + 1 - count))
                    if not data:
                        break
                    count += len(data)
                    require(count <= info.file_size, 'ZIP member exceeds declared size')
                    output.write(data)
            require(count == info.file_size, 'ZIP member truncated')


def download_verified(client, artifact, target):
    client.download_zip(artifact, target)
    require(digest(target) == {'bytes': artifact['size_in_bytes'], 'sha256': artifact['digest'][7:]},
            'Downloaded ZIP digest/size mismatch')


def verify_controls(directory, identity, receipt_sha, root=ROOT):
    receipt = (directory / 'publication-work/control' / PROOF).read_bytes()
    require(digest_bytes(receipt)['sha256'] == receipt_sha, 'Aggregate receipt hash mismatch')
    proof = json_object(receipt)
    expected = {'schema': 1, 'contract': 'downstream17', 'version': identity['version'],
                'release_tag': identity['tag'], 'repository': identity['repository'],
                'workflow_run_id': identity['run_id'], 'workflow_commit': identity['head_sha'],
                'policy_fingerprint': policy_fingerprint('downstream17', identity['version'], root)}
    require(type(proof.get('schema')) is int and positive(proof.get('workflow_run_id')) and
            all(proof.get(k) == v for k, v in expected.items()), 'Aggregate receipt identity/policy mismatch')
    files = proof.get('files')
    require(isinstance(files, dict) and set(files) == expected_names('downstream17', identity['version'], root),
            'Aggregate receipt package matrix mismatch')
    for facts in files.values():
        require(isinstance(facts, dict) and set(facts) == {'bytes', 'sha256'} and
                positive(facts['bytes']) and facts['bytes'] <= SHARD_LIMIT and sha256(facts['sha256']),
                'Aggregate receipt file digest/size invalid')
    checksums = (directory / 'publication-work/release/checksums.txt').read_bytes()
    require(digest_bytes(checksums) == files['checksums.txt'], 'Aggregate checksum content mismatch')
    sums = {}
    for line in checksums.decode().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([^/\\]+)', line)
        require(match is not None and match[2] not in sums, 'Malformed or duplicate flat checksums')
        sums[match[2]] = match[1]
    archives = set(files) - {'checksums.txt'}
    require(set(sums) == archives and all(sums[n] == files[n]['sha256'] for n in archives),
            'Aggregate checksum package matrix mismatch')
    plan_bytes = (directory / 'matrix-input/plan.json').read_bytes()
    plan = json_object(plan_bytes)
    plan_identity = {'version': identity['version'], 'repository': identity['repository'], 'tag': identity['tag'],
                     'workflow_run_id': str(identity['run_id']), 'workflow_commit': identity['head_sha']}
    require(all(plan.get(k) == v for k, v in plan_identity.items()) and plan.get('rows') == identity['rows'] and plan.get('native_rows') == identity['native_rows'] and
            plan.get('mode') in ('stable', 'beta', 'dev') and isinstance(plan.get('upstream_input'), dict) and
            plan['upstream_input'] == proof.get('upstream_input'), 'Candidate plan identity/rows/provenance mismatch')
    return proof, plan_identity, digest_bytes(plan_bytes)['sha256']


def materialize(args, env=None, client=None, root=ROOT):
    env = os.environ if env is None else env
    tag = args.tag or args.version
    identity = current_identity(args.version, tag, args.source, args.arch, env, root)
    require(bool(re.fullmatch(r'[1-9][0-9]*', str(args.controls_artifact_id))) and sha256(args.receipt_sha256),
            'Exact controls artifact ID and receipt SHA-256 required')
    output = Path(args.output).absolute()
    provenance = Path(args.provenance).absolute() if args.provenance else output.with_name(output.name + '.provenance.json')
    temporary_root = Path(env.get('RUNNER_TEMP', '/missing')).resolve()
    for path in (output, provenance):
        require('\n' not in str(path) and '\r' not in str(path) and temporary_root.is_dir() and
                path.resolve().is_relative_to(temporary_root) and path.resolve() != temporary_root and
                not path.exists() and not path.is_symlink(), 'Candidate output must be new and inside runner temporary directory')
    require(not provenance.resolve().is_relative_to(output.resolve()), 'Provenance must be outside archive-only input')
    client = client or CandidateGitHub(identity['repository'], tag)
    require(client.repo == identity['repository'], 'Candidate client repository mismatch')
    run = verify_run(client, identity)
    base = f'repos/{client.repo}/actions'
    controls = api(client, f'{base}/artifacts/{args.controls_artifact_id}')
    require(controls.get('id') == int(args.controls_artifact_id), 'Controls artifact ID mismatch')
    artifacts = listed(client, f'{base}/runs/{identity["run_id"]}/artifacts', 'artifacts')
    same_controls = [a for a in artifacts if a.get('name') == controls.get('name') or a.get('id') == controls['id']]
    require(same_controls == [controls], 'Controls artifact listing identity mismatch')
    prepare_attempt = select_prepare_attempt(client, controls, artifacts, identity)
    verify_artifact(controls, f'publication-controls-{identity["run_id"]}-{prepare_attempt}', identity, run, CONTROL_LIMIT)
    prepare = successful_job(client, identity, prepare_attempt, prepare=True)
    verify_upload_log(read_job_log(client, prepare['id']), controls, args.receipt_sha256)
    attempt, shard = select_shard(artifacts, identity, prepare_attempt)
    verify_artifact(shard, f'package-shard-{attempt}-{identity["row"]["key"]}', identity, run, SHARD_LIMIT)
    producer = successful_job(client, identity, attempt)
    verify_upload_log(read_job_log(client, producer['id']), shard)
    output.parent.mkdir(parents=True, exist_ok=True)
    provenance.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temporary_root, prefix='candidate-verify-') as work:
        work = Path(work)
        download_verified(client, controls, work / 'controls.zip')
        extract_zip(work / 'controls.zip', work / 'controls', {n: CONTROL_LIMIT for n in CONTROLS}, CONTROL_LIMIT)
        proof, plan_identity, plan_sha = verify_controls(work / 'controls', identity, args.receipt_sha256, root)
        sources = [args.source] + (['enterprise-original'] if args.source == 'enterprise-docker' else [])
        files = {f'{s}/1panel-{args.version}-{s}-offline-linux-{args.arch}.tar.gz':
                 proof['files'][f'1panel-{args.version}-{s}-offline-linux-{args.arch}.tar.gz'] for s in sources}
        download_verified(client, shard, work / 'shard.zip')
        whitelist = {name: facts['bytes'] for name, facts in files.items()}
        whitelist['shard.json'] = CONTROL_LIMIT
        extract_zip(work / 'shard.zip', work / 'shard', whitelist, SHARD_LIMIT)
        record = json_object((work / 'shard/shard.json').read_bytes())
        require(record.get('identity') == plan_identity and record.get('row') == identity['row'] and
                record.get('plan_sha256') == plan_sha and record.get('files') == files and
                all(positive(facts.get('bytes')) for facts in record['files'].values()),
                'Shard identity/row/plan/files differ from aggregate receipt')
        for name, facts in files.items():
            require(digest(work / 'shard' / name) == facts, 'Shard archive bytes differ from aggregate receipt')
        verify_run(client, identity)  # Do not expose bytes if a newer attempt superseded this job.
        def artifact_facts(artifact, job, artifact_attempt):
            return {'artifact_id': artifact['id'], 'name': artifact['name'], 'zip_sha256': artifact['digest'][7:],
                    'zip_bytes': artifact['size_in_bytes'], 'producer_job_id': job['id'], 'producer_attempt': artifact_attempt}
        selected = f'{args.source}/1panel-{args.version}-{args.source}-offline-linux-{args.arch}.tar.gz'
        facts = {'schema': 1, 'repository': identity['repository'], 'workflow_run_id': identity['run_id'],
                 'workflow_commit': identity['head_sha'], 'workflow_run_attempt': identity['run_attempt'],
                 'workflow_path': WORKFLOW, 'version': args.version, 'release_tag': tag,
                 'source': args.source, 'arch': args.arch, 'archive_sha256': files[selected]['sha256'],
                 'receipt_sha256': args.receipt_sha256, 'plan_sha256': plan_sha,
                 'controls': artifact_facts(controls, prepare, prepare_attempt),
                 'shard': artifact_facts(shard, producer, attempt), 'files': files}
        staged = work / 'input'
        staged.mkdir()
        for source in sources:
            shutil.move(work / 'shard' / source, staged / source)
        with provenance.open('x') as stream:
            stream.write(json.dumps(facts, indent=2, sort_keys=True) + '\n')
        staged.rename(output)
    summary = {'archive_path': str(output / selected), 'archive_sha256': files[selected]['sha256'],
               'provenance_path': str(provenance)}
    if env.get('GITHUB_OUTPUT'):
        with open(env['GITHUB_OUTPUT'], 'a') as stream:
            for name, value in summary.items():
                stream.write(f'{name}={value}\n')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('version', 'source', 'arch', 'controls-artifact-id', 'receipt-sha256', 'output'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--tag', default='')
    parser.add_argument('--provenance', default='')
    try:
        print(json.dumps(materialize(parser.parse_args()), sort_keys=True))
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError, zipfile.BadZipFile):
        # Server responses, URLs and arbitrary artifact strings are not diagnostics.
        print('Native candidate input verification failed; no input is authorized for installation.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
