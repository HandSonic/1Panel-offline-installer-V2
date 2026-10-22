#!/usr/bin/env python3
"""Publish only native-admitted runtime products, using authenticated Actions bytes.

The acceptance command is read-only on GitHub. Writers authenticate the final
immutable artifact and both exact acceptance-job log markers before any write.
The native document is an unsigned signing subject, never a signature claim.
"""
import argparse
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

import runtime_native_acceptance as native
from native_candidate_input import (CandidateGitHub, CONTROLS, CONTROL_LIMIT,
    SHARD_LIMIT, WORKFLOW, api, download_verified, extract_zip, json_object, listed,
    latest_executed_job, positive, require, select_prepare_attempt, sha256, verify_artifact,
    verify_run, verify_upload_log, RECOVERY_INPUT, recovery_request, predecessor_context, recorded_context)
from publication_contract import PROOF, ROOT, digest_bytes, read_job_log
from publication_outcomes import OUTCOME_FIELDS, filename, products, validate, validate_preparation
from release_asset_repair import check_asset_names, digest, repair
from resolved_inventory import canonical
from runtime_contract import PLAN_PATH, PLAN_SHA, policy, selected

NATIVE_FILE = 'native-acceptance.json'
SUBJECT_LIMIT = 16 * CONTROL_LIMIT
PUBLICATION_LIMIT = 64 * SHARD_LIMIT
PUBLICATION_ERRORS = (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, zipfile.BadZipFile)


class PublicationFailure(ValueError):
    def __init__(self, stage, code):
        self.publication_stage=stage
        self.publication_error_code=code
        super().__init__(code)


def stage_call(stage, code, action, *args, **kwargs):
    """Attach fixed diagnostics without exposing subprocess output or credentials."""
    try:
        return action(*args, **kwargs)
    except PUBLICATION_ERRORS as error:
        if not hasattr(error,'publication_stage'):error.publication_stage=stage
        if not hasattr(error,'publication_error_code'):error.publication_error_code=code
        raise


def read_regular(path, limit):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= limit,
            'Missing, unsafe or oversized publication control')
    return path.read_bytes()


def workflow_identity(version, tag, env=None, root=ROOT):
    """Registry-free identity for all runtime rows, including non-native-only sets."""
    env = os.environ if env is None else env
    require(re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?', version) and
            (tag == version or re.fullmatch(re.escape(version) + r'-[A-Za-z0-9._-]+', tag)),
            'Invalid publication version/tag')
    repository = 'HandSonic/1Panel-offline-installer-V2'
    require(env.get('GITHUB_ACTIONS') == 'true' and env.get('RUNNER_ENVIRONMENT') == 'github-hosted' and
            env.get('GITHUB_REPOSITORY') == repository and
            env.get('GITHUB_EVENT_NAME') in ('push', 'schedule', 'workflow_dispatch') and
            env.get('PUBLICATION_OPERATION') in ('build', 'validate-repair', 'repair-existing'),
            'Unsupported runtime publication environment')
    require(env.get('GITHUB_SERVER_URL') == 'https://github.com' and
            env.get('GITHUB_API_URL') == 'https://api.github.com' and
            env.get('GH_HOST', 'github.com') == 'github.com', 'Unexpected GitHub host')
    require(all(re.fullmatch(r'[1-9][0-9]*', env.get(key, '')) for key in
                ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT')) and
            re.fullmatch(r'[0-9a-f]{40}', env.get('GITHUB_SHA', '')), 'Invalid workflow identity')
    require(env.get('GITHUB_WORKFLOW_SHA') == env['GITHUB_SHA'] and
            env.get('GITHUB_WORKFLOW_REF', '').startswith(repository + '/' + WORKFLOW + '@refs/'),
            'Unexpected workflow path/commit')
    runtime = selected(version, root, env)
    require(runtime is not None, 'Authenticated runtime plan required; no registry fallback')
    raw = read_regular(env[PLAN_PATH], CONTROL_LIMIT)
    plan = json_object(raw)
    context = predecessor_context(version, env)
    require(recorded_context(plan, version) == context, 'Publication invocation differs from authenticated plan')
    require(raw == canonical(plan) and digest_bytes(raw)['sha256'] == env[PLAN_SHA],
            'Plan is not the exact canonical planner output')
    expected = {'version': version, 'repository': repository, 'tag': tag,
                'workflow_run_id': env['GITHUB_RUN_ID'], 'workflow_commit': env['GITHUB_SHA']}
    from publication_outcomes import requested_products
    rows = requested_products(plan, allow_empty=True)
    require(all(plan.get(k) == v for k, v in expected.items()) and plan.get('resolved') == runtime and
            plan.get('mode') == runtime['mode'], 'Runtime plan identity/matrix changed')
    require(rows == runtime['inventory']['rows'] or
            (env['PUBLICATION_OPERATION'] == 'build' and not context),
            'Partial product selection requires an ordinary build')
    identity = {'repository': repository, 'run_id': int(env['GITHUB_RUN_ID']),
                'run_attempt': int(env['GITHUB_RUN_ATTEMPT']), 'head_sha': env['GITHUB_SHA'],
                'event': env['GITHUB_EVENT_NAME'], 'version': version, 'tag': tag,
                'runtime':runtime,'runtime_plan_sha256':env[PLAN_SHA], **context}
    return identity, plan


def exact_job(client, identity, name, attempt):
    jobs = listed(client, f'repos/{client.repo}/actions/runs/{identity["run_id"]}/jobs?filter=all', 'jobs')
    matches = [j for j in jobs if j.get('name') == name and j.get('run_attempt') == attempt]
    require(len(matches) == 1, 'Missing or ambiguous exact publication job')
    job = matches[0]
    require(positive(job.get('id')) and job.get('run_id') == identity['run_id'] and
            job.get('run_attempt') == attempt and
            job.get('head_sha') == identity['head_sha'], 'Exact publication producer identity changed')
    return job


def artifact_inventory(client, identity):
    rows = listed(client, f'repos/{client.repo}/actions/runs/{identity["run_id"]}/artifacts', 'artifacts')
    require(len({a.get('name') for a in rows}) == len(rows) and
            len({a.get('id') for a in rows}) == len(rows), 'Ambiguous publication artifact inventory')
    return rows


def pinned_artifact(client, identity, run, artifacts, artifact_id, name, limit):
    require(re.fullmatch(r'[1-9][0-9]*', str(artifact_id)), 'Exact artifact ID required')
    artifact = api(client, f'repos/{client.repo}/actions/artifacts/{artifact_id}')
    require(artifact.get('id') == int(artifact_id) and
            [a for a in artifacts if a.get('id') == int(artifact_id) or a.get('name') == name] == [artifact],
            'Artifact immutable read/list mismatch')
    verify_artifact(artifact, name, identity, run, limit)
    return artifact


def verify_checksums(raw, files):
    sums = {}
    for line in raw.decode('utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([^/\\]+)', line)
        require(match is not None and match[2] not in sums, 'Malformed or duplicate archive checksums')
        sums[match[2]] = match[1]
    require(digest_bytes(raw) == files['checksums.txt'] and
            sums == {n: f['sha256'] for n, f in files.items() if n.endswith('.tar.gz')},
            'Checksums must cover exactly the accepted archives')


def validate_preparation_identity(proof, plan, identity, root=ROOT):
    validate_preparation(proof, plan)
    require(proof['workflow_run_attempt'] <= identity['run_attempt'] and
            proof['workflow_run_id'] == identity['run_id'] and proof['workflow_commit'] == identity['head_sha'] and
            proof['repository'] == identity['repository'] and proof['version'] == identity['version'] and
            proof['release_tag'] == identity['tag'] and proof['policy_fingerprint'] == policy(plan['resolved'], root),
            'Preparation identity/current policy changed')
    require(all(f['bytes'] <= SHARD_LIMIT for f in proof['files'].values()), 'Oversized prepared archive')


def authenticate_preparation(client, identity, plan, controls_id, artifact_id, receipt_sha, work, root=ROOT):
    """Authenticate same-run preparation after the native dependency gate."""
    require(client.repo == identity['repository'] and sha256(receipt_sha), 'Invalid preparation trust root')
    require(identity.get('runtime')==plan['resolved'] and
            identity.get('runtime_plan_sha256')==digest_bytes(canonical(plan))['sha256'],
            'Same-run preparation requires the authenticated native runtime plan')
    require(re.fullmatch(r'[1-9][0-9]*', str(controls_id)) and re.fullmatch(r'[1-9][0-9]*', str(artifact_id)),
            'Exact preparation artifact IDs required')
    work = Path(work)
    run = verify_run(client, identity)
    artifacts = artifact_inventory(client, identity)
    controls_metadata = api(client, f'repos/{client.repo}/actions/artifacts/{controls_id}')
    attempt = select_prepare_attempt(client, controls_metadata, artifacts, identity,same_run=True)
    controls = pinned_artifact(client, identity, run, artifacts, controls_id,
        f'publication-controls-{identity["run_id"]}-{attempt}', CONTROL_LIMIT)
    archive = pinned_artifact(client, identity, run, artifacts, artifact_id,
        f'verified-publication-{identity["run_id"]}-{attempt}', PUBLICATION_LIMIT)
    job = exact_job(client, identity, 'publication_prepare', attempt)
    log = read_job_log(client, job['id'])
    verify_upload_log(log, controls, receipt_sha)
    verify_upload_log(log, archive, receipt_sha)
    download_verified(client, controls, work / 'controls.zip')
    extract_zip(work / 'controls.zip', work / 'controls', {n: CONTROL_LIMIT for n in CONTROLS}, CONTROL_LIMIT)
    control = work / 'controls'
    receipt = read_regular(control / 'publication-work/control' / PROOF, CONTROL_LIMIT)
    require(digest_bytes(receipt)['sha256'] == receipt_sha, 'Preparation receipt marker differs from bytes')
    proof = json_object(receipt)
    validate_preparation_identity(proof, plan, identity, root)
    require(proof['workflow_run_attempt'] == attempt and
            read_regular(control / 'matrix-input/plan.json', CONTROL_LIMIT) == canonical(plan),
            'Preparation attempt or plan changed')
    sums = read_regular(control / 'publication-work/release/checksums.txt', CONTROL_LIMIT)
    verify_checksums(sums, proof['files'])
    expected = {'control/' + PROOF: len(receipt), 'control/plan.json': len(canonical(plan)),
                'release/checksums.txt': len(sums)}
    for row in proof['outcomes']:
        if row['status'] == 'success':
            name = filename(proof['version'], row)
            expected['release/' + row['source'] + '/' + name] = proof['files'][name]['bytes']
            if row['source'] == 'enterprise-docker':
                companion = filename(proof['version'], dict(row, source='enterprise-original'))
                pin = plan['resolved']['inventory']['enterprise']['archives'][row['arch']]
                expected['verification/enterprise-original/' + companion] = pin['bytes']
    download_verified(client, archive, work / 'preparation.zip')
    extract_zip(work / 'preparation.zip', work / 'prepared', expected, PUBLICATION_LIMIT)
    prepared = work / 'prepared'
    require(read_regular(prepared / 'control' / PROOF, CONTROL_LIMIT) == receipt and
            read_regular(prepared / 'control/plan.json', CONTROL_LIMIT) == canonical(plan) and
            read_regular(prepared / 'release/checksums.txt', CONTROL_LIMIT) == sums,
            'Full preparation differs from authenticated controls')
    for row in proof['outcomes']:
        if row['status'] == 'success':
            name = filename(proof['version'], row)
            require(digest(prepared / 'release' / row['source'] / name) == proof['files'][name],
                    'Prepared archive changed')
            if row['source'] == 'enterprise-docker':
                companion = filename(proof['version'], dict(row, source='enterprise-original'))
                pin = plan['resolved']['inventory']['enterprise']['archives'][row['arch']]
                require(digest(prepared / 'verification/enterprise-original' / companion) ==
                        {k: pin[k] for k in ('bytes', 'sha256')}, 'Prepared verification companion changed')
    for artifact in (controls, archive):
        require(api(client, f'repos/{client.repo}/actions/artifacts/{artifact["id"]}') == artifact,
                'Preparation artifact changed during authentication')
    verify_run(client, identity)
    return dict(proof, receipt_sha256=receipt_sha, controls_artifact_id=controls['id'],
                _receipt_text=receipt.decode('utf-8')), prepared / 'release'


def summary(admission, env):
    path = env.get('GITHUB_STEP_SUMMARY')
    if path:
        lines = ['## Native publication acceptance', '',
                 f'{len(admission["accepted_keys"])} of {len(admission["outcomes"])} requested products admitted.', '']
        for row in admission['outcomes']:
            detail = row.get('acceptance', row['stage']) if row['status'] == 'success' else row['reason']
            lines.append('- ' + html.escape(row['key']) + ': ' + row['status'] + ' (' + html.escape(detail) + ')')
        with open(path, 'a') as stream:
            stream.write('\n'.join(lines) + '\n')


def stripped(outcomes):
    return [{key: row[key] for key in OUTCOME_FIELDS} for row in outcomes]


def preparation_receipt(preparation):
    return {k: v for k, v in preparation.items() if k not in ('receipt_sha256', 'controls_artifact_id', '_receipt_text')}


def final_subject(preparation, results, admission, identity):
    require(all(record.get('status') != 'success' or (positive(record.get('run_attempt')) and
                preparation['workflow_run_attempt'] <= record['run_attempt'] <= identity['run_attempt'])
                for record in results.values()), 'Native result attempt differs from preparation/acceptance')
    value = native.durable_subject(preparation, results, admission, identity)
    value['workflow_run_attempt'] = identity['run_attempt']
    value['preparation_receipt'] = preparation_receipt(preparation)
    value['preparation_receipt_json'] = preparation['_receipt_text']
    return value


def finalize(preparation, plan, identity, results, release, output, env=None, root=ROOT):
    """Pure local finalization. Results must come from native.collect, never JSON input."""
    env = os.environ if env is None else env
    original = preparation_receipt(preparation)
    validate_preparation_identity(original, plan, identity, root)
    admission = native.admit(preparation, results, identity=identity)
    summary(admission, env)
    require(admission['accepted_keys'], 'Every product failed acceptance; nothing can be published')
    output, release = Path(output), Path(release)
    require(not output.exists() and not output.is_symlink(), 'Final publication output must be new')
    output.mkdir(parents=True)
    files = {}
    for row in admission['outcomes']:
        if row['status'] != 'success':
            continue
        name = filename(plan['version'], row)
        source = release / row['source'] / name
        require(source.is_file() and not source.is_symlink(), 'Missing or unsafe admitted archive')
        # Avoid doubling large validated archives on the runner.
        try:
            os.link(source, output / name)
        except OSError:
            shutil.copyfile(source, output / name)
        # Keep the authenticated facts; the final directory check below reads
        # each linked or copied archive once before returning admission.
        files[name] = preparation['files'][name]
    checksum_bytes = ''.join(files[n]['sha256'] + '  ' + n + '\n' for n in sorted(files)).encode()
    (output / 'checksums.txt').write_bytes(checksum_bytes)
    files['checksums.txt'] = digest_bytes(checksum_bytes)
    subject = final_subject(preparation, results, admission, identity)
    subject_raw = canonical(subject)
    require(len(subject_raw) <= SUBJECT_LIMIT, 'Final native subject exceeds budget')
    (output / NATIVE_FILE).write_bytes(subject_raw)
    files[NATIVE_FILE] = digest_bytes(subject_raw)
    proof = dict(original, workflow_run_attempt=identity['run_attempt'], outcomes=stripped(admission['outcomes']),
                 files=files, preparation_receipt_sha256=preparation['receipt_sha256'])
    (output / PROOF).write_bytes(canonical(proof))
    receipt_sha, native_sha = digest(output / PROOF)['sha256'], files[NATIVE_FILE]['sha256']
    verify_final_directory(output, plan, identity, receipt_sha, native_sha, root)
    return {'receipt_sha256': receipt_sha, 'native_acceptance_sha256': native_sha,
            'upload_directory': str(output.resolve()), 'accepted_count': len(admission['accepted_keys']),
            'all_requested_passed': admission['all_requested_passed']}


def verify_final_directory(directory, plan, identity, receipt_sha, native_sha, root=ROOT):
    """Recheck exact final files and admission before publication, without old registries."""
    require('read_only_recovery' not in plan, 'Read-only recovery plan cannot authorize publication')
    require(sha256(receipt_sha) and sha256(native_sha), 'Both acceptance markers are required')
    directory = Path(directory)
    require(directory.is_dir() and not directory.is_symlink(), 'Unsafe final publication directory')
    raw = read_regular(directory / PROOF, CONTROL_LIMIT)
    subject_raw = read_regular(directory / NATIVE_FILE, SUBJECT_LIMIT)
    require(digest_bytes(raw)['sha256'] == receipt_sha and digest_bytes(subject_raw)['sha256'] == native_sha,
            'Final receipt/native acceptance markers differ from bytes')
    proof, subject = json_object(raw), json_object(subject_raw)
    require(set(proof) == {'schema', 'contract', 'version', 'release_tag', 'repository', 'policy_fingerprint',
        'workflow_run_id', 'workflow_run_attempt', 'workflow_commit', 'plan_sha256', 'requested_products',
        'outcomes', 'files', 'upstream_input', 'preparation_receipt_sha256'} |
        set(recorded_context(plan, identity['version'])), 'Unexpected final receipt fields')
    require(proof.get('workflow_run_attempt') == identity['run_attempt'] and
            sha256(proof.get('preparation_receipt_sha256')), 'Final acceptance attempt/preparation binding changed')
    structural = {k: v for k, v in proof.items() if k != 'preparation_receipt_sha256'}
    structural['files'] = {n: f for n, f in proof['files'].items() if n != NATIVE_FILE}
    validate_preparation_identity(structural, plan, identity, root)
    accepted = validate(proof['requested_products'], proof['outcomes'], identity['run_id'], identity['run_attempt'])
    require(accepted and proof['files'].get(NATIVE_FILE) == digest_bytes(subject_raw), 'No accepted products or native byte binding')
    require({p.name for p in directory.iterdir()} == set(proof['files']) | {PROOF},
            'Final artifact contains missing or unexpected files')
    for name, facts in proof['files'].items():
        path = directory / name
        require(path.is_file() and not path.is_symlink() and digest(path) == facts, 'Final publication file differs from receipt')
    verify_checksums((directory / 'checksums.txt').read_bytes(), proof['files'])
    original = subject.get('preparation_receipt')
    require(isinstance(original, dict), 'Missing original preparation receipt in native subject')
    validate_preparation_identity(original, plan, identity, root)
    original_text = subject.get('preparation_receipt_json')
    require(isinstance(original_text, str) and json_object(original_text) == original and
            digest_bytes(original_text.encode())['sha256'] == proof['preparation_receipt_sha256'],
            'Original preparation receipt bytes changed')
    preparation = dict(original, receipt_sha256=proof['preparation_receipt_sha256'],
                       controls_artifact_id=subject.get('controls_artifact_id'), _receipt_text=original_text)
    admission = native.admit(preparation, subject.get('native_results'), identity=identity)
    require(subject == final_subject(preparation, subject['native_results'], admission, identity) and
            stripped(admission['outcomes']) == proof['outcomes'], 'Final receipt/native subject admission differs')
    for name, facts in structural['files'].items():
        if name.endswith('.tar.gz'):
            require(original['files'].get(name) == facts, 'Final archive is not an admitted prepared archive')
    return proof


def acceptance_identity(identity, attempt):
    require(positive(attempt) and attempt <= identity['run_attempt'],
            'Invalid or future acceptance attempt')
    return dict(identity, run_attempt=attempt)


def authenticate_final_artifact(client, identity, artifact_id, receipt_sha, native_sha, work):
    """Same-run writer gate: exact acceptance identity, ZIP and both log markers."""
    require(client.repo == identity['repository'] and sha256(receipt_sha) and sha256(native_sha),
            'Invalid final publication trust roots')
    require(isinstance(identity.get('runtime'),dict) and sha256(identity.get('runtime_plan_sha256')),
            'Same-run publication requires the authenticated native runtime plan')
    work = Path(work)
    run = verify_run(client, identity)
    artifacts = artifact_inventory(client, identity)
    require(re.fullmatch(r'[1-9][0-9]*', str(artifact_id)), 'Exact artifact ID required')
    supplied = [artifact for artifact in artifacts if artifact.get('id') == int(artifact_id)]
    require(len(supplied) == 1, 'Missing or ambiguous final artifact')
    match = re.fullmatch(r'native-admitted-publication-' + str(identity['run_id']) +
                         r'-([1-9][0-9]*)', supplied[0].get('name', ''))
    require(match is not None, 'Unexpected final artifact name')
    producer_identity = acceptance_identity(identity, int(match[1]))
    jobs = listed(client, f'repos/{client.repo}/actions/runs/{identity["run_id"]}/jobs?filter=all', 'jobs')
    job = latest_executed_job([job for job in jobs if job.get('name') == 'publication_acceptance'], identity)
    require(job['run_attempt'] == producer_identity['run_attempt'],
            'Final artifact is not from latest acceptance execution')
    artifact = pinned_artifact(client, identity, run, artifacts, artifact_id,
        supplied[0]['name'], PUBLICATION_LIMIT)
    log = read_job_log(client, job['id'])
    verify_upload_log(log, artifact, receipt_sha)
    marks = re.findall(r'(?m)^(?:[0-9T:.-]+Z )?NATIVE_ACCEPTANCE_SHA256=([0-9a-f]{64})[ \t]*$', log)
    require(marks == [native_sha], 'Native acceptance not bound to exact successful acceptance job')
    download_verified(client, artifact, work / 'final.zip')
    # Read only the bounded receipt before choosing the exact extraction whitelist.
    with zipfile.ZipFile(work / 'final.zip') as archive:
        infos = [i for i in archive.infolist() if i.filename == PROOF]
        require(len(infos) == 1 and 0 < infos[0].file_size <= CONTROL_LIMIT, 'Missing/oversized final artifact receipt')
        raw = archive.read(infos[0])
    require(digest_bytes(raw)['sha256'] == receipt_sha, 'Final artifact receipt SHA differs')
    proof = json_object(raw)
    require(proof.get('workflow_run_attempt') == producer_identity['run_attempt'],
            'Final receipt attempt differs from authenticated acceptance')
    expected = {PROOF: len(raw)}
    require(isinstance(proof.get('files'), dict), 'Missing final file inventory')
    for name, facts in proof['files'].items():
        require(name == Path(name).name and '\\' not in name and isinstance(facts, dict) and
                positive(facts.get('bytes')) and facts['bytes'] <= (SUBJECT_LIMIT if name == NATIVE_FILE else SHARD_LIMIT),
                'Unsafe final file whitelist')
        expected[name] = facts['bytes']
    extract_zip(work / 'final.zip', work / 'final', expected, PUBLICATION_LIMIT)
    # The selected writer verifies the extracted files and admission before
    # any release mutation; do not scan all archives twice in this call chain.
    require(api(client, f'repos/{client.repo}/actions/artifacts/{artifact["id"]}') == artifact,
            'Final artifact changed during writer authentication')
    verify_run(client, identity)
    return work / 'final'


def publication_files(directory, proof):
    directory = Path(directory)
    names = sorted(n for n in proof['files'] if n.endswith('.tar.gz'))
    # The authenticated final artifact remains byte-for-byte unchanged. Public
    # sidecars are derived from its already-verified archive facts in a sibling
    # directory, so internal receipt/checksum reauthentication keeps its scope.
    sidecars = directory.with_name(directory.name + '-public-checksums')
    require(not sidecars.is_symlink(), 'Unsafe public checksum workspace')
    sidecars.mkdir(exist_ok=True)
    require({p.name for p in sidecars.iterdir()} <= {name + '.sha256' for name in names},
            'Unexpected public checksum workspace files')
    for name in names:
        raw = (proof['files'][name]['sha256'] + '  ' + name + '\n').encode('ascii')
        path = sidecars / (name + '.sha256')
        if path.exists() or path.is_symlink():
            require(read_regular(path, 4096) == raw, 'Public checksum workspace changed')
        else:
            with path.open('xb') as stream:
                stream.write(raw)
    return [directory / name for name in names] + [sidecars / (name + '.sha256') for name in names]


def repair_existing(client, directory, plan, identity, receipt_sha, native_sha, journal, root=ROOT, *, isolate_products=False):
    """Call only after authenticate_final_artifact; preserves notes and recoverable originals."""
    if 'repair_predecessor' in plan:
        require(plan.get('repair_operation') == 'repair-existing' and identity.get('event') == 'workflow_dispatch',
                'Explicit predecessor writer requires manual repair-existing admission')
    proof = verify_final_directory(directory, plan, identity, receipt_sha, native_sha, root)
    require(client.repo == identity['repository'] and client.tag == identity['tag'] and
            (client.tag == identity['version'] or (isolate_products and
             re.fullmatch(re.escape(identity['version']) + r'-[A-Za-z0-9._-]+', client.tag))),
            'Repair repository/tag changed')
    before = client.release()
    require(before.get('draft') is False and positive(before.get('id')) and before.get('tag_name') == client.tag,
            'Repair requires an exact identified public release')
    client.pin_release(before['id'])
    require(not any('.staged-' in a['name'] for a in before.get('assets', [])),
            'Review recoverable staged assets before retrying public repair')
    class PublicClient:
        def __getattr__(self, name):
            return getattr(client, name)

        def release(self):
            observed = client.release()
            require(observed.get('id') == before['id'] and observed.get('tag_name') == client.tag and
                    observed.get('draft') is False, 'Public release identity changed during repair')
            return observed
    require(not Path(journal).exists(), 'Review the existing repair journal before retrying')
    failed = [filename(proof['version'], row) for row in proof['outcomes'] if row['status'] == 'failure']
    preserved = ()
    if isolate_products:
        from publication_outcomes import requested_products
        from offline_public_source import package_only, package_pattern
        requested = requested_products(plan)
        complete = products(plan['resolved']['inventory']['matrix'])
        requested_names = {filename(proof['version'], row) for row in requested}
        pattern = package_pattern(proof['version'])
        existing_products = set()
        for asset in before['assets']:
            name = re.sub(r'\.backup-[0-9a-f]{12}$', '', asset['name']).removesuffix('.sha256')
            if pattern.fullmatch(name):
                existing_products.add(name)
        unrequested = existing_products - requested_names
        if requested != complete or unrequested:
            require(package_only(before), 'Partial retry requires an existing package-only release')
            # Discovery may no longer offer an older product of this version.
            # Preserve its existing bytes without claiming current acceptance.
            preserved = tuple(name + suffix for name in sorted(unrequested) for suffix in ('', '.sha256'))
    return repair(PublicClient(), publication_files(directory, proof), Path(journal), update_notes=False,
                  retire_names=failed, retire_version=proof['version'], retire_controls=True,
                  isolate_products=isolate_products,preserved_names=preserved)


def publish_draft(client, directory, plan, identity, receipt_sha, native_sha, journal, root=ROOT, *, isolate_products=False):
    """Call only after authenticate_final_artifact; refuse to replace an existing public release."""
    require(not recorded_context(plan, identity['version']), 'Explicit repair cannot publish an ordinary draft')
    proof = verify_final_directory(directory, plan, identity, receipt_sha, native_sha, root)
    from publication_outcomes import requested_products
    require(requested_products(plan) == products(plan['resolved']['inventory']['matrix']),
            'Partial retry cannot create or publish a draft; replan the public release')
    require(client.repo == identity['repository'] and client.tag == identity['tag'], 'Draft destination changed')
    require(not Path(journal).exists(), 'Review the existing publication journal before retrying')
    try:
        release = stage_call('draft-discovery','DRAFT_DISCOVERY_FAILED',client.release)
    except subprocess.CalledProcessError as error:
        if getattr(client,'_release_id',None) is not None:
            raise PublicationFailure('draft-discovery','BOUND_RELEASE_UNAVAILABLE') from None
        require(re.search(r'HTTP 404', error.stderr or ''), 'Cannot establish release absence')
        created = json_object(stage_call('draft-creation','DRAFT_CREATE_FAILED',client.run,'api', '--method', 'POST', f'repos/{client.repo}/releases',
            '-f', 'tag_name=' + client.tag, '-f', 'target_commitish=' + identity['head_sha'],
            '-f', 'name=' + client.tag, '-f', 'body=', '-F', 'draft=true',
            '-F', 'prerelease=' + str(plan['mode'] != 'stable').lower()))
        require(positive(created.get('id')) and created.get('tag_name') == client.tag and
                created.get('draft') is True and created.get('prerelease') == (plan['mode'] != 'stable'),
                'Created draft identity/state mismatch')
        client.pin_release(created['id'])
        release = client.release()
    require(release.get('draft') is True and positive(release.get('id')) and release.get('tag_name') == client.tag,
            'Normal publication requires an exact identified draft')
    release_id = release['id']
    client.pin_release(release_id)
    require(not any('.staged-' in a['name'] for a in release.get('assets', [])),
            'Review recoverable staged assets before resuming draft publication')
    class DraftClient:
        def __getattr__(self, name):
            return getattr(client, name)

        def release(self):
            observed = client.release()
            require(observed.get('id') == release_id and observed.get('tag_name') == client.tag and
                    observed.get('draft') is True, 'Draft identity changed during publication')
            return observed
    failed = [filename(proof['version'], row) for row in proof['outcomes'] if row['status'] == 'failure']
    state = repair(DraftClient(), publication_files(directory, proof), Path(journal), update_notes=False,
                   retire_names=failed, retire_version=proof['version'], retire_controls=True,isolate_products=isolate_products)
    release = client.release()
    require(release.get('draft') is True and release.get('id') == release_id and release.get('tag_name') == client.tag,
            'Draft changed concurrently')
    expected = {name: facts for name, facts in proof['files'].items() if name.endswith('.tar.gz')}
    expected.update({name + '.sha256': digest_bytes((facts['sha256'] + '  ' + name + '\n').encode('ascii'))
                     for name, facts in list(expected.items())})
    retired = set(failed) | {name + '.sha256' for name in failed} | {PROOF, NATIVE_FILE, 'checksums.txt'}
    check_asset_names(release['assets'], set(expected) | retired)
    assets = {a['name']: a for a in release['assets']}
    require(not any('.staged-' in name for name in assets) and not retired & set(assets) and
            all(n in assets and assets[n].get('digest') == 'sha256:' + f['sha256'] and
            assets[n].get('size') == f['bytes'] for n, f in expected.items()), 'Draft readback differs from final accepted files')
    client.run('api', '--method', 'PATCH', f'repos/{client.repo}/releases/{release_id}',
               '-F', 'draft=false', '-f', 'make_latest=legacy')
    published = client.release()
    require(published.get('id') == release_id and published.get('tag_name') == client.tag and
            published.get('draft') is False, 'Published release identity/state could not be verified')
    return state


def release_status(client, plan, root=ROOT):
    """Stale or unverifiable public packages request rebuild, never a blind no-op."""
    try:
        release = client.release()
    except subprocess.CalledProcessError as error:
        if re.search(r'HTTP 404', error.stderr or ''):
            return 'absent'
        raise
    if release.get('draft'):
        return 'draft'
    try:
        return 'verified' if public_noop(client, plan, root) else 'rebuild'
    except (ValueError, KeyError, TypeError):
        return 'rebuild'
    except subprocess.CalledProcessError as error:
        if re.search(r'HTTP 404|no assets match', error.stderr or '', re.IGNORECASE):
            return 'rebuild'
        raise


def status(args, env=None, client=None, root=ROOT):
    env = os.environ if env is None else env
    identity, plan = stage_call('workflow-identity','WORKFLOW_IDENTITY_INVALID',workflow_identity,args.version, args.tag or args.version, env, root)
    require(args.repository == identity['repository'], 'Status repository changed')
    client = client or CandidateGitHub(identity['repository'], identity['tag'], allow_drafts=True)
    state = stage_call('release-status','RELEASE_STATUS_FAILED',release_status,client, plan, root)
    if env.get('GITHUB_OUTPUT'):
        with open(env['GITHUB_OUTPUT'], 'a') as stream:
            stream.write('state=' + state + '\nbuild_required=' + ('false' if state == 'verified' else 'true') + '\n')
    print(state)
    return state


def public_noop(client, plan, root=ROOT):
    """Read-only no-op check. Partial accepted releases remain eligible for retry."""
    from native_upgrade_input import verify_public_acceptance
    release = client.release()
    if release.get('draft'):
        return False
    from native_upgrade_input import public_release, PredecessorGitHub
    from offline_public_source import package_only, public_noop as package_noop
    release = public_release(client, release['id'])
    if package_only(release):
        # CandidateGitHub is the writer transport; bounded immutable asset reads
        # use its public-reader adapter without changing the selected repository.
        reader = client if hasattr(client, 'download_asset') else PredecessorGitHub(client.repo, client.tag)
        with tempfile.TemporaryDirectory() as temp:
            return package_noop(reader, release, plan, root, Path(temp))
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        client.download(PROOF, directory)
        client.download('checksums.txt', directory)
        authenticated = verify_public_acceptance(client, release, read_regular(directory / PROOF, CONTROL_LIMIT),
                                                 read_regular(directory / 'checksums.txt', CONTROL_LIMIT))
    proof = authenticated['receipt']
    require(proof.get('version') == plan['version'] and proof.get('release_tag') == plan['tag'] and
            proof.get('repository') == plan['repository'] and proof.get('policy_fingerprint') == policy(plan['resolved'], root) and
            proof.get('requested_products') == products(plan['resolved']['inventory']['matrix']) and
            proof.get('upstream_input') == plan['upstream_input'], 'Public release differs from current requested policy/matrix')
    accepted = validate(proof['requested_products'], proof['outcomes'], proof['workflow_run_id'], proof['workflow_run_attempt'])
    return len(accepted) == len(proof['requested_products'])


def recovery_report(preparation, plan, identity, results, output, env):
    """Preserve all real native evidence without producing publication admission."""
    request = recovery_request(env.get(RECOVERY_INPUT, ''), identity['version'], env)
    require(request is not None and preparation.get('read_only_recovery') == plan.get('read_only_recovery') == request,
            'Read-only recovery report requires the exact prepared plan input')
    rows, static = native.preparation_rows(preparation)
    outcomes = []
    for key, row in rows.items():
        needed = native.required_results(row)
        missing = [name for name in needed if results.get(name, {}).get('status') != 'success']
        outcomes.append({'key': key, 'static_status': static[key]['status'],
            'native_status': 'failed' if missing else 'passed' if needed else 'not-applicable',
            'missing_or_failed_native': missing, 'publication_eligible': False})
    value = {'schema': 1, 'kind': 'read-only-candidate-predecessor-recovery', 'publication_eligible': False,
        'read_only_recovery': request, 'repository': identity['repository'],
        'workflow_run_id': identity['run_id'], 'workflow_run_attempt': identity['run_attempt'],
        'workflow_commit': identity['head_sha'], 'version': identity['version'],
        'preparation_receipt': preparation_receipt(preparation),
        'preparation_receipt_sha256': preparation['receipt_sha256'],
        'plan_sha256': preparation['plan_sha256'], 'outcomes': outcomes, 'native_results': results,
        'scope': 'Rebuilt candidate bytes only; old published bytes and publication eligibility are not established'}
    raw = canonical(value)
    require(len(raw) <= SUBJECT_LIMIT, 'Read-only recovery report exceeds evidence budget')
    output = Path(output)
    require(not output.exists() and not output.is_symlink(), 'Recovery report output must be new')
    output.mkdir(parents=True)
    (output / 'read-only-native-recovery.json').write_bytes(raw)
    if env.get('GITHUB_STEP_SUMMARY'):
        with open(env['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write('Read-only candidate recovery; publication is disabled.\n')
            for row in outcomes:
                stream.write('- ' + row['key'] + ': static ' + row['static_status'] + ', native ' + row['native_status'] + '\n')
    return {'report_sha256': digest_bytes(raw)['sha256'], 'upload_directory': str(output.resolve()),
            'all_requested_passed':all(row['static_status']=='success' and not row['missing_or_failed_native'] for row in outcomes),
            'publication_eligible': False}


def acceptance(args, env=None, client=None, root=ROOT):
    env = os.environ if env is None else env
    identity, plan = stage_call('workflow-identity','WORKFLOW_IDENTITY_INVALID',workflow_identity,args.version, args.tag or args.version, env, root)
    context = predecessor_context(args.version, env)
    require(recorded_context(plan, args.version) == context, 'Acceptance mode differs from authenticated plan')
    request = context.get('read_only_recovery')
    require(getattr(args, 'repository', identity['repository']) == identity['repository'], 'Acceptance repository changed')
    client = client or CandidateGitHub(identity['repository'], identity['tag'])
    temporary = Path(env.get('RUNNER_TEMP', '/missing')).resolve()
    output = Path(args.output).absolute()
    require(temporary.is_dir() and output.resolve().is_relative_to(temporary) and output.resolve() != temporary and
            '\n' not in str(output) and '\r' not in str(output), 'Final output must be inside runner temporary directory')
    with tempfile.TemporaryDirectory(dir=temporary, prefix='publication-acceptance-') as work:
        preparation, release = stage_call('preparation-authentication','PREPARATION_INVALID',authenticate_preparation,client, identity, plan, args.controls_artifact_id,
            args.artifact_id, args.receipt_sha256, work, root)
        results = stage_call('native-evidence','NATIVE_EVIDENCE_INVALID',native.collect,client, identity, preparation, work,same_run=True)
        result = (recovery_report(preparation, plan, identity, results, output, env) if request is not None else
                   stage_call('product-acceptance','PRODUCT_ACCEPTANCE_FAILED',finalize,preparation, plan, identity, results, release, output, env, root))
        verify_run(client, identity)
    if env.get('GITHUB_OUTPUT'):
        with open(env['GITHUB_OUTPUT'], 'a') as stream:
            for key, value in result.items():
                stream.write(key + '=' + (str(value).lower() if isinstance(value, bool) else str(value)) + '\n')
    if request is not None:
        print('READ_ONLY_NATIVE_RECOVERY_SHA256=' + result['report_sha256'])
    else:
        print('VERIFIED_RELEASE_RECEIPT_SHA256=' + result['receipt_sha256'])
        print('NATIVE_ACCEPTANCE_SHA256=' + result['native_acceptance_sha256'])
    return result


def publish(args, env=None, client=None, root=ROOT):
    env = os.environ if env is None else env
    require(not env.get(RECOVERY_INPUT), 'Candidate predecessor mode cannot invoke a writer')
    identity, plan = stage_call('workflow-identity','WORKFLOW_IDENTITY_INVALID',workflow_identity,args.version, args.tag or args.version, env, root)
    require(args.repository == identity['repository'], 'Writer repository changed')
    operation = env.get('PUBLICATION_OPERATION')
    require(operation == 'build' or (operation == 'repair-existing' and env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'),
            'Writer requires build or explicitly selected manual repair')
    if 'repair_predecessor' in plan:
        require(operation == 'repair-existing' and plan['repair_operation'] == operation,
                'Explicit predecessor publication requires this manual repair-existing run')
    client = client or CandidateGitHub(identity['repository'], identity['tag'], allow_drafts=True)
    with tempfile.TemporaryDirectory(dir=env['RUNNER_TEMP'], prefix='publication-writer-') as work:
        directory = stage_call('final-artifact-authentication','FINAL_ARTIFACT_INVALID',authenticate_final_artifact,client, identity, args.artifact_id,
            args.receipt_sha256, args.native_acceptance_sha256, work)
        # Authentication above binds this receipt to the latest real acceptance
        # producer. A writer-only retry keeps that immutable producer attempt.
        producer_identity = acceptance_identity(identity,
            json_object(read_regular(directory / PROOF, CONTROL_LIMIT))['workflow_run_attempt'])
        Path(args.journal).parent.mkdir(parents=True, exist_ok=True)
        writer = repair_existing
        if operation == 'build':
            try:
                release = stage_call('release-discovery','RELEASE_DISCOVERY_FAILED',client.release)
            except subprocess.CalledProcessError as error:
                require(re.search(r'HTTP 404', error.stderr or ''), 'Cannot establish release absence')
                release = {'draft': True}
            if release.get('draft'):
                writer = publish_draft
        return stage_call('draft-publication' if writer is publish_draft else 'public-repair','PUBLICATION_FAILED',
            writer,client, directory, plan, producer_identity, args.receipt_sha256,
            args.native_acceptance_sha256, args.journal, root,isolate_products=operation=='build')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    command = sub.add_parser('status')
    for name in ('version', 'tag', 'repository'):
        command.add_argument('--' + name, required=True)
    for operation in ('accept', 'publish'):
        command = sub.add_parser(operation)
        for name in ('version', 'artifact-id', 'receipt-sha256'):
            command.add_argument('--' + name, required=True)
        command.add_argument('--tag', default='')
        command.add_argument('--repository', default='HandSonic/1Panel-offline-installer-V2')
        if operation == 'accept':
            command.add_argument('--controls-artifact-id', required=True)
            command.add_argument('--output', required=True)
        else:
            command.add_argument('--native-acceptance-sha256', required=True)
            command.add_argument('--journal', required=True)
    args = parser.parse_args(argv)
    try:
        {'accept': acceptance, 'publish': publish, 'status': status}[args.operation](args)
    except PUBLICATION_ERRORS as error:
        stage=getattr(error,'publication_stage',args.operation)
        code=getattr(error,'publication_error_code','GITHUB_API_FAILED' if isinstance(error,subprocess.SubprocessError) else 'VALIDATION_FAILED')
        if not isinstance(stage,str) or re.fullmatch(r'[a-z][a-z0-9-]{0,63}',stage) is None:stage=args.operation
        if not isinstance(code,str) or re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',code) is None:code='VALIDATION_FAILED'
        print(f'Runtime publication failed: stage={stage} code={code}. No unverified product is authorized.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
