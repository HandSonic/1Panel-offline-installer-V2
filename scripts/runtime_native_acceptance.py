#!/usr/bin/env python3
"""Authenticate native evidence and admit independently successful products.

An evidence document alone is never a trust root. collect() authenticates its
immutable Actions ZIP, exact successful producer upload log, workflow commit,
attempt and hosted runner before admission. The durable JSON is a signing
subject, NOT a signed attestation. Publication must retain/sign that subject
with a trusted workflow identity before using it after Actions evidence expires.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import tempfile
import subprocess
import zipfile

from native_candidate_input import (CandidateGitHub, CONTROL_LIMIT, api, current_identity,
    download_verified, extract_zip, json_object, listed, positive, require, sha256,
    verify_artifact, verify_run, verify_upload_log, WORKFLOW)
from publication_contract import ROOT, read_job_log
from resolved_inventory import ARCHES, canonical, file_facts
from release_asset_repair import digest

NATIVE = ('amd64', 'arm64')
ARCH_MAPPING = {'amd64': ('X64', 'x86_64', 'ubuntu-24.04'),
                'arm64': ('ARM64', 'aarch64', 'ubuntu-24.04-arm')}
EVIDENCE_FILE = 'native-evidence.json'
EVIDENCE_LIMIT = 2 * CONTROL_LIMIT


class SharedTrustError(ValueError):
    pass


def shared(condition, message):
    if not condition:
        raise SharedTrustError(message)


def result_key(kind, source, arch, scenario=None):
    require(kind in ('install', 'upgrade') and source in ('official', 'custom', 'enterprise-docker') and arch in NATIVE,
            'Unknown native evidence identity')
    require((kind == 'install' and scenario in ('fresh', 'existing')) or (kind == 'upgrade' and scenario is None),
            'Unknown native evidence scenario')
    return ':'.join([kind, source, arch] + ([scenario] if scenario else []))


def required_results(row):
    if row['arch'] not in NATIVE or row['source'] == 'enterprise-original':
        return []
    result = [result_key('install', row['source'], row['arch'], scenario) for scenario in ('fresh', 'existing')]
    if row['source'] in ('official', 'custom'):
        result.append(result_key('upgrade', row['source'], row['arch']))
    return result


def preparation_rows(preparation):
    require(preparation.get('schema') == 2 and preparation.get('contract') == 'downstream-matrix',
            'Authenticated matrix preparation required')
    requested = preparation.get('requested_products')
    require(isinstance(requested, list) and requested, 'Complete requested product rows required')
    rows = {}
    for row in requested:
        require(isinstance(row, dict) and set(row) == {'source', 'arch', 'key'} and
                row['source'] in ('official', 'custom', 'enterprise-original', 'enterprise-docker') and
                row['arch'] in ARCHES and row['key'] == row['source'] + '-' + row['arch'] and row['key'] not in rows,
                'Malformed/duplicate requested product row')
        rows[row['key']] = row
    outcomes = preparation.get('outcomes')
    require(isinstance(outcomes, list), 'Complete static outcomes required')
    mapped = {}
    for outcome in outcomes:
        require(isinstance(outcome, dict) and outcome.get('key') in rows and outcome['key'] not in mapped and
                all(outcome.get(k) == rows[outcome['key']][k] for k in ('source', 'arch', 'key')) and
                outcome.get('status') in ('success', 'failure') and
                isinstance(outcome.get('stage'), str) and bool(outcome['stage']) and len(outcome['stage']) <= 80 and
                isinstance(outcome.get('reason'), str) and len(outcome['reason']) <= 1024 and
                (outcome['reason'] == '' if outcome['status'] == 'success' else bool(outcome['reason'])),
                'Malformed/duplicate static outcome')
        mapped[outcome['key']] = outcome
    require(set(mapped) == set(rows), 'Static outcome coverage differs from requested products')
    require(sha256(preparation.get('plan_sha256')), 'Missing authenticated plan SHA')
    files = preparation.get('files')
    require(isinstance(files, dict), 'Missing static-success files')
    expected = {f'1panel-{preparation["version"]}-{r["source"]}-offline-linux-{r["arch"]}.tar.gz'
                for key, r in rows.items() if mapped[key]['status'] == 'success'}
    require('checksums.txt' in files and set(files) - {'checksums.txt'} == expected,
            'Static-success outcomes and archive set differ')
    for facts in files.values():
        file_facts(facts)
    return rows, mapped


def admit(preparation, authenticated_results, *, cancelled=False):
    """Pure admission after transport authentication; failures stay per product.

    Callers must pass the result of collect(), never receipt-supplied claims.
    This function cannot turn a success claim into authenticated provenance.
    """
    require(cancelled is False, 'Cancellation blocks publication')
    rows, static = preparation_rows(preparation)
    require(sha256(preparation.get('receipt_sha256')) and positive(preparation.get('controls_artifact_id')),
            'Missing authenticated preparation receipt/controls identity')
    require(isinstance(authenticated_results, dict), 'Missing authenticated result map')
    expected = {key for row in rows.values() for key in required_results(row)}
    require(set(authenticated_results) <= expected, 'Unexpected native result identity')
    outcomes, accepted = [], []
    for key, row in rows.items():
        original = static[key]
        outcome = dict(original)
        if original['status'] == 'success':
            needed = required_results(row)
            failures = []
            for name in needed:
                record = authenticated_results.get(name)
                if record is None or record.get('status') != 'success':
                    failures.append(name)
                    continue
                require(record.get('key') == name and record.get('repository') == preparation['repository'] and
                        record.get('run_id') == preparation['workflow_run_id'] and
                        record.get('head_sha') == preparation['workflow_commit'] and
                        record.get('plan_sha256') == preparation['plan_sha256'] and
                        record.get('receipt_sha256') == preparation['receipt_sha256'] and
                        record.get('controls_artifact_id') == preparation['controls_artifact_id'],
                        'Native evidence differs from shared run/plan/controls identity')
                archive = f'1panel-{preparation["version"]}-{row["source"]}-offline-linux-{row["arch"]}.tar.gz'
                require(record.get('archive_sha256') == preparation['files'][archive]['sha256'] and
                        sha256(record.get('evidence_sha256')) and positive(record.get('job_id')) and
                        positive(record.get('artifact_id')), 'Native evidence archive/result identity mismatch')
            if failures:
                outcome.update(status='failure', stage='native-acceptance',
                               reason='Required native acceptance missing or failed: ' + ', '.join(failures))
            else:
                outcome['acceptance'] = ('vendor-byte-identity' if row['source'] == 'enterprise-original' else
                    'native-install-and-upgrade' if row['source'] in ('official', 'custom') and row['arch'] in NATIVE else
                    'native-install' if row['arch'] in NATIVE else 'payload-validation-only')
                accepted.append(key)
        outcomes.append(outcome)
    return {'schema': 1, 'accepted_keys': accepted, 'outcomes': outcomes,
            'all_requested_passed': len(accepted) == len(rows)}


def check_result(result, candidate, kind, source, arch, scenario, upgrade=None, installed=None):
    require(result.get('schema') == 1 and result.get('status') == 'passed' and
            result.get('evidence_level') == 'native-' + kind and result.get('source') == source and
            result.get('architecture') == arch and result.get('version') == candidate['version'],
            'Native result identity/status mismatch')
    if kind == 'install':
        require(result.get('archive_sha256') == candidate['archive_sha256'] and
                result.get('docker_scenario') == scenario and sha256(result.get('manifest_sha256')) and
                result.get('installer_mode') in ('cli', 'interactive'), 'Install archive/scenario mismatch')
        for name in ('1panel-core', '1panel-agent'):
            process = result.get('panel_processes', {}).get(name, {})
            require(positive(process.get('pid')) and sha256(process.get('binary_sha256')), 'Missing real panel process identity')
    else:
        require(source in ('official', 'custom') and isinstance(upgrade, dict) and isinstance(installed, dict),
                'Upgrade requires actual predecessor install and bound inputs')
        require(result.get('target_archive_sha256') == candidate['archive_sha256'] and
                result.get('target_receipt_sha256') == candidate['receipt_sha256'] and
                result.get('target_run_id') == candidate['workflow_run_id'] and
                result.get('target_run_attempt') == candidate['workflow_run_attempt'] and
                result.get('target_commit') == candidate['workflow_commit'] and
                result.get('regional_edition') == 'intl', 'Upgrade target/run/edition mismatch')
        for flag in ('database_user_rows_preserved', 'durable_settings_preserved', 'user_configuration_preserved',
                     'persistent_user_file_preserved', 'docker_unchanged'):
            require(result.get(flag) is True, 'Upgrade preservation acceptance missing')
        require(result.get('rollback', '').startswith('passed; one-shot real systemd') and
                result.get('upgrade') == 'passed; unchanged fixed target upgrade.sh' and
                result.get('database_before') == result.get('database_after_rollback'),
                'Actual rollback/upgrade/database evidence missing')
        require(upgrade.get('schema') == 2 and upgrade.get('target') == candidate and
                result.get('input_provenance_sha256') == upgrade.get('_file_sha256') and
                installed.get('_file_sha256') == result.get('predecessor_install_result_sha256'),
                'Upgrade inputs/predecessor installation result bytes mismatch')
        predecessor = upgrade['predecessor']
        require(predecessor.get('kind') == 'current-run-public-predecessor-bootstrap' and
                predecessor.get('historical_native_acceptance') == 'not-claimed' and
                installed.get('status') == 'passed' and installed.get('evidence_level') == 'native-install' and
                installed.get('source') == source and installed.get('architecture') == arch and
                installed.get('version') == predecessor['version'] and installed.get('regional_edition') == 'intl' and
                installed.get('archive_sha256') == predecessor['archive']['sha256'] == result.get('predecessor_archive_sha256'),
                'Fresh predecessor native installation is missing or unrelated')
    docker = result.get('docker_process', {})
    require(positive(docker.get('pid')) and sha256(docker.get('binary_sha256')), 'Missing real Docker process identity')


def stamp(args, env=None, root=ROOT):
    env = os.environ if env is None else env
    identity = current_identity(args.version, args.tag or args.version, args.source, args.arch, env, root)
    key = result_key(args.kind, args.source, args.arch, args.scenario or None)
    expected_arch, machine, label = ARCH_MAPPING[args.arch]
    require(env.get('RUNNER_ARCH') == expected_arch and platform.machine() == machine and
            env.get('RUNNER_NAME') and env.get('RUNNER_OS') == 'Linux', 'Native runner architecture/identity mismatch')
    temp = Path(env['RUNNER_TEMP']).resolve()
    def read(path):
        path = Path(path)
        require(path.resolve().is_relative_to(temp) and path.is_file() and not path.is_symlink() and
                path.stat().st_size <= CONTROL_LIMIT, 'Unsafe/oversized native evidence file')
        value = json_object(path.read_bytes()); value['_file_sha256'] = digest(path)['sha256']
        return value
    candidate, result = read(args.candidate), read(args.result)
    candidate.pop('_file_sha256')
    upgrade = read(args.upgrade_input) if args.kind == 'upgrade' else None
    installed = read(args.predecessor_install) if args.kind == 'upgrade' else None
    require(all(candidate.get(k) == v for k, v in {'repository': identity['repository'],
        'workflow_run_id': identity['run_id'], 'workflow_run_attempt': identity['run_attempt'],
        'workflow_commit': identity['head_sha'], 'version': args.version, 'source': args.source, 'arch': args.arch}.items()),
        'Native candidate is not from this exact run/attempt/row')
    check_result(result, candidate, args.kind, args.source, args.arch, args.scenario or None, upgrade, installed)
    value = {'schema': 1, 'key': key, 'repository': identity['repository'], 'run_id': identity['run_id'],
        'run_attempt': identity['run_attempt'], 'head_sha': identity['head_sha'], 'workflow': WORKFLOW,
        'runner': {'name': env['RUNNER_NAME'], 'arch': expected_arch, 'machine': machine,
                   'os': 'Linux', 'environment': 'github-hosted', 'label': label},
        'candidate': candidate, 'result': result}
    if upgrade is not None:
        value.update(upgrade_input=upgrade, predecessor_install=installed)
    output = Path(args.output)
    require(output.resolve().is_relative_to(temp) and not output.exists() and not output.is_symlink(),
            'Evidence output must be new and runner-local')
    raw = canonical(value)
    require(len(raw) <= EVIDENCE_LIMIT, 'Evidence document exceeds limit')
    with output.open('xb') as stream:
        stream.write(raw)
    print('VERIFIED_NATIVE_EVIDENCE_SHA256=' + hashlib.sha256(raw).hexdigest())
    return value


def job_name(key):
    parts = key.split(':')
    return ('publication_native (' + ', '.join(parts[1:]) + ')' if parts[0] == 'install' else
            'publication_upgrade (' + ', '.join(parts[1:]) + ')')


def artifact_name(key, attempt):
    return 'native-evidence-' + str(attempt) + '-' + key.replace(':', '-')


def verify_evidence(value, key, identity, job, preparation):
    shared(value.get('schema') == 1 and value.get('key') == key and
            value.get('repository') == identity['repository'] and value.get('run_id') == identity['run_id'] and
            value.get('run_attempt') == job['run_attempt'] and value.get('head_sha') == identity['head_sha'] and
            value.get('workflow') == WORKFLOW, 'Native evidence workflow identity mismatch')
    kind, source, arch, *rest = key.split(':')
    runner = value.get('runner', {})
    architecture, machine, label = ARCH_MAPPING[arch]
    require(runner == {'name': job.get('runner_name'), 'arch': architecture, 'machine': machine,
            'os': 'Linux', 'environment': 'github-hosted', 'label': label} and
            isinstance(job.get('labels'), list) and label in job['labels'] and
            positive(job.get('runner_id')) and job.get('runner_group_name') == 'GitHub Actions',
            'Native evidence is not bound to the exact hosted runner/architecture')
    candidate = value['candidate']
    shared(all(candidate.get(k) == v for k, v in {'repository': identity['repository'],
        'workflow_run_id': identity['run_id'], 'workflow_run_attempt': job['run_attempt'],
        'workflow_commit': identity['head_sha'], 'version': preparation['version'],
        'release_tag': preparation['release_tag'], 'source': source, 'arch': arch,
        'receipt_sha256': preparation['receipt_sha256'], 'plan_sha256': preparation['plan_sha256']}.items()) and
        candidate.get('controls', {}).get('artifact_id') == preparation['controls_artifact_id'],
        'Native candidate differs from shared controls/plan/run')
    name = f'1panel-{preparation["version"]}-{source}-offline-linux-{arch}.tar.gz'
    require(candidate.get('archive_sha256') == preparation['files'][name]['sha256'], 'Native candidate archive differs')
    require(candidate.get('files') == {source + '/' + name: preparation['files'][name]},
            'Native candidate published file set changed')
    for label in ('controls', 'shard'):
        pin = candidate.get(label, {})
        require(positive(pin.get('artifact_id')) and positive(pin.get('producer_job_id')) and
                positive(pin.get('producer_attempt')) and pin['producer_attempt'] <= job['run_attempt'] and
                positive(pin.get('zip_bytes')) and sha256(pin.get('zip_sha256')),
                'Missing exact candidate artifact/producer identity')
        expected_name = (f'publication-controls-{identity["run_id"]}-{pin["producer_attempt"]}' if label == 'controls' else
                         f'package-shard-{pin["producer_attempt"]}-{source}-{arch}')
        require(pin.get('name') == expected_name, 'Candidate artifact name/attempt/source mismatch')
    check_result(value['result'], candidate, kind, source, arch, rest[0] if rest else None,
                 value.get('upgrade_input'), value.get('predecessor_install'))


def collect(client, identity, preparation, work):
    """Read-only transport; malformed row evidence suppresses that row only."""
    rows, static = preparation_rows(preparation)
    require(client.repo == identity['repository'] == preparation['repository'] and
            identity['run_id'] == preparation['workflow_run_id'] and
            identity['head_sha'] == preparation['workflow_commit'], 'Shared workflow identity mismatch')
    run = verify_run(client, identity)
    jobs = listed(client, f'repos/{client.repo}/actions/runs/{identity["run_id"]}/jobs?filter=all', 'jobs')
    artifacts = listed(client, f'repos/{client.repo}/actions/runs/{identity["run_id"]}/artifacts', 'artifacts')
    require(len({a.get('name') for a in artifacts}) == len(artifacts) and
            len({a.get('id') for a in artifacts}) == len(artifacts), 'Ambiguous shared artifact inventory')
    result = {}
    for row in rows.values():
        if static[row['key']]['status'] != 'success':
            continue
        for key in required_results(row):
            matches = [j for j in jobs if j.get('name') == job_name(key)]
            if not matches:
                result[key] = {'key': key, 'status': 'failure', 'reason': 'missing-native-job'}
                continue
            require(all(positive(j.get('run_attempt')) and j['run_attempt'] <= identity['run_attempt'] and
                    j.get('run_id') == identity['run_id'] and j.get('head_sha') == identity['head_sha'] for j in matches),
                    'Native job differs from shared run/head/attempt')
            latest = max(j['run_attempt'] for j in matches)
            matches = [j for j in matches if j['run_attempt'] == latest]
            require(len(matches) == 1, 'Ambiguous exact native job')
            job = matches[0]
            if job.get('status') != 'completed' or job.get('conclusion') != 'success':
                result[key] = {'key': key, 'status': 'failure', 'job_id': job.get('id'), 'reason': 'native-job-' + str(job.get('conclusion'))}
                continue
            try:
                wanted = artifact_name(key, latest)
                found = [a for a in artifacts if a.get('name') == wanted]
                require(len(found) == 1, 'Missing exact native evidence artifact')
                artifact = found[0]
                verify_artifact(artifact, wanted, identity, run, EVIDENCE_LIMIT)
                metadata = api(client, f'repos/{client.repo}/actions/artifacts/{artifact["id"]}')
                require(metadata == artifact, 'Native artifact immutable read/list mismatch')
                log = read_job_log(client, job['id'])
                verify_upload_log(log, artifact)
                with tempfile.TemporaryDirectory(dir=work, prefix='native-evidence-') as temporary:
                    directory = Path(temporary)
                    download_verified(client, artifact, directory / 'evidence.zip')
                    extract_zip(directory / 'evidence.zip', directory / 'content', {EVIDENCE_FILE: EVIDENCE_LIMIT}, EVIDENCE_LIMIT)
                    file = directory / 'content' / EVIDENCE_FILE
                    facts = digest(file)
                    marks = re.findall(r'(?m)^(?:[0-9T:.-]+Z )?VERIFIED_NATIVE_EVIDENCE_SHA256=([0-9a-f]{64})[ \t]*$', log)
                    require(marks == [facts['sha256']], 'Native result bytes differ from exact producer log')
                    evidence = json_object(file.read_bytes())
                    verify_evidence(evidence, key, identity, job, preparation)
                require(api(client, f'repos/{client.repo}/actions/artifacts/{artifact["id"]}') == artifact,
                        'Native artifact changed during authentication')
                result[key] = {'key': key, 'status': 'success', 'repository': identity['repository'],
                    'run_id': identity['run_id'], 'run_attempt': latest, 'head_sha': identity['head_sha'],
                    'job_id': job['id'], 'runner_id': job['runner_id'], 'runner': evidence['runner'],
                    'artifact_id': artifact['id'], 'artifact_name': wanted,
                    'artifact_zip_sha256': artifact['digest'][7:], 'artifact_zip_bytes': artifact['size_in_bytes'],
                    'evidence_sha256': facts['sha256'], 'evidence_bytes': facts['bytes'],
                    'result_sha256': evidence['result']['_file_sha256'], 'archive_sha256': evidence['candidate']['archive_sha256'],
                    'receipt_sha256': preparation['receipt_sha256'], 'plan_sha256': preparation['plan_sha256'],
                    'controls_artifact_id': preparation['controls_artifact_id'],
                    'candidate': evidence['candidate'], 'evidence': evidence}
            except SharedTrustError:
                raise
            except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, zipfile.BadZipFile):
                result[key] = {'key': key, 'status': 'failure', 'job_id': job['id'], 'reason': 'native-evidence-authentication-failed'}
    verify_run(client, identity)
    return result


def durable_subject(preparation, results, admission):
    require(admission == admit(preparation, results), 'Admission does not match authenticated evidence')
    value = {'schema': 1, 'kind': '1panel-native-acceptance-signing-subject',
             'repository': preparation['repository'], 'workflow': WORKFLOW,
             'workflow_run_id': preparation['workflow_run_id'], 'workflow_commit': preparation['workflow_commit'],
             'version': preparation['version'], 'release_tag': preparation['release_tag'],
             'plan_sha256': preparation['plan_sha256'], 'receipt_sha256': preparation['receipt_sha256'],
             'controls_artifact_id': preparation['controls_artifact_id'],
             'files': preparation['files'], 'admission': admission, 'native_results': results,
             'authentication': 'unsigned subject; verify trusted GitHub artifact attestation before historical use'}
    require(len(canonical(value)) <= 16 * CONTROL_LIMIT, 'Durable subject exceeds evidence budget')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    command = sub.add_parser('stamp')
    for name in ('version', 'source', 'arch', 'kind', 'candidate', 'result', 'output'):
        command.add_argument('--' + name, required=True)
    for name in ('tag', 'scenario', 'upgrade-input', 'predecessor-install'):
        command.add_argument('--' + name, default='')
    args = parser.parse_args()
    try:
        stamp(args)
    except (ValueError, KeyError, TypeError, OSError):
        print('Native result identity validation failed.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
