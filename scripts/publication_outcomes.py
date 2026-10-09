#!/usr/bin/env python3
"""Explicit requested/accepted package accounting for isolated branch failures."""
import re

from resolved_inventory import ARCHES, SOURCES, digest, exact_keys, file_facts, hash_value, require

REPOSITORY = 'HandSonic/1Panel-offline-installer-V2'
OUTCOME_FIELDS = ('source', 'arch', 'key', 'status', 'stage', 'reason',
                  'producer_run_attempt', 'job_id', 'job_url')


def positive(value):
    return type(value) is int and value > 0


def products(matrix):
    require(isinstance(matrix, dict) and set(matrix) <= set(SOURCES), 'Invalid product source inventory')
    result = []
    for source in SOURCES:
        arches = matrix.get(source, [])
        require(isinstance(arches, list) and len(arches) == len(set(arches)) and
                all(arch in ARCHES for arch in arches), 'Invalid product architecture inventory')
        result += [{'source': source, 'arch': arch, 'key': source + '-' + arch}
                   for arch in ARCHES if arch in arches]
    require(result, 'No requested products')
    return result


def filename(version, row):
    return f'1panel-{version}-{row["source"]}-offline-linux-{row["arch"]}.tar.gz'


def validate(requested, outcomes, run_id, attempt):
    require(positive(run_id) and positive(attempt), 'Invalid outcome workflow identity')
    require(isinstance(requested, list) and requested and isinstance(outcomes, list) and
            len(requested) == len(outcomes), 'Every requested product needs an explicit terminal outcome')
    seen, job_ids, successes = set(), set(), []
    for expected, row in zip(requested, outcomes):
        exact_keys(expected, ('source', 'arch', 'key'), 'requested product')
        require(expected['source'] in SOURCES and expected['arch'] in ARCHES and
                expected['key'] == expected['source'] + '-' + expected['arch'] and
                expected['key'] not in seen, 'Ambiguous requested product')
        seen.add(expected['key'])
        exact_keys(row, OUTCOME_FIELDS, 'product outcome')
        require(all(row[k] == v for k, v in expected.items()) and row['status'] in ('success', 'failure') and
                positive(row['producer_run_attempt']) and row['producer_run_attempt'] <= attempt and
                positive(row['job_id']) and row['job_id'] not in job_ids and row['job_url'] ==
                f'https://github.com/{REPOSITORY}/actions/runs/{run_id}/job/{row["job_id"]}',
                'Outcome differs from requested product or exact job identity')
        job_ids.add(row['job_id'])
        require(isinstance(row['stage'], str) and re.fullmatch(r'[a-z][a-z0-9-]{0,63}', row['stage']) and
                isinstance(row['reason'], str) and len(row['reason']) <= 512 and
                not any(ord(c) < 32 for c in row['reason']), 'Malformed outcome stage/reason')
        require((row['status'] == 'success') == (row['reason'] == ''),
                'Success cannot have a failure reason; failure must explain itself')
        if row['status'] == 'success':
            successes.append(expected)
    return successes


def collect(jobs, requested, run_id, attempt, commit):
    """Latest actual branch execution wins; an old success cannot hide a new failure."""
    result = []
    for row in requested:
        name = f'publication_packages ({row["source"]}, {row["arch"]})'
        matches = [job for job in jobs if job.get('name') == name]
        require(matches and all(positive(j.get('run_attempt')) and j['run_attempt'] <= attempt and
                j.get('run_id') == run_id and j.get('head_sha') == commit for j in matches),
                'Missing or foreign package branch job: ' + row['key'])
        latest = max(j['run_attempt'] for j in matches)
        selected = [j for j in matches if j['run_attempt'] == latest]
        require(len(selected) == 1, 'Duplicate latest package branch job')
        job = selected[0]
        require(job.get('status') == 'completed' and job.get('conclusion') in ('success', 'failure', 'timed_out'),
                'Nonterminal, cancelled or skipped package branch cannot authorize publication')
        success = job['conclusion'] == 'success'
        result.append({**row, 'status': 'success' if success else 'failure', 'stage': 'package',
                       'reason': '' if success else 'GitHub package job ' + job['conclusion'],
                       'producer_run_attempt': latest, 'job_id': job['id'], 'job_url': job['html_url']})
    validate(requested, result, run_id, attempt)
    return result


def validate_preparation(proof, plan):
    exact_keys(proof, ('schema', 'contract', 'version', 'release_tag', 'repository',
        'policy_fingerprint', 'workflow_run_id', 'workflow_run_attempt', 'workflow_commit',
        'plan_sha256', 'requested_products', 'outcomes', 'files', 'upstream_input'), 'preparation receipt')
    expected = {'schema': 2, 'contract': 'downstream-matrix', 'version': plan['version'],
                'release_tag': plan['tag'], 'repository': plan['repository'],
                'workflow_run_id': int(plan['workflow_run_id']), 'workflow_commit': plan['workflow_commit']}
    require(type(proof.get('schema')) is int and all(proof.get(k) == v for k, v in expected.items()) and
            positive(proof.get('workflow_run_attempt')), 'Preparation receipt identity mismatch')
    require(proof.get('plan_sha256') == digest(plan) and proof.get('upstream_input') == plan['upstream_input'],
            'Preparation plan digest or upstream transport changed')
    hash_value(proof['policy_fingerprint'])
    requested = products(plan['resolved']['inventory']['matrix'])
    require(proof.get('requested_products') == requested, 'Preparation requested products changed')
    successes = validate(requested, proof.get('outcomes'), proof['workflow_run_id'], proof['workflow_run_attempt'])
    wanted = {filename(plan['version'], row) for row in successes} | {'checksums.txt'}
    require(isinstance(proof.get('files'), dict) and set(proof['files']) == wanted,
            'Preparation files must match exactly the declared successful products')
    for facts in proof['files'].values():
        file_facts(facts)
    return successes


def accepted_matrix(requested, outcomes, run_id, attempt):
    rows = validate(requested, outcomes, run_id, attempt)
    result = {}
    for row in rows:
        result.setdefault(row['source'], []).append(row['arch'])
    return result


def report(outcomes):
    """Actions-only visibility; this function never changes Release notes."""
    import os
    lines = ['Package branch results', '']
    for row in outcomes:
        if row['status'] == 'failure':
            message = row['key'] + ': ' + row['reason']
            escaped = message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
            print('::warning::' + escaped)
            lines.append('- Missing ' + message + ' (' + row['job_url'] + ')')
    if len(lines) == 2:lines.append('Every requested package branch passed static validation.')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write('\n'.join(lines) + '\n')
