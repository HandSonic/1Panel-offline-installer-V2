#!/usr/bin/env python3
"""Explicit requested/accepted package accounting for isolated branch failures."""
import re
from pathlib import Path

from resolved_inventory import ARCHES, SOURCES, digest, exact_keys, file_facts, hash_value, require

REPOSITORY = 'HandSonic/1Panel-offline-installer-V2'
OUTCOME_FIELDS = ('source', 'arch', 'key', 'status', 'stage', 'reason',
                  'producer_run_attempt', 'job_id', 'job_url')


class MissingPackageExecution(ValueError):
    """A recorded execution's immutable API row is not yet visible."""


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


def requested_products(plan, allow_empty=False):
    """Validate this run's work as an ordered subset of complete discovery."""
    inventory = plan['resolved']['inventory']
    complete = inventory['rows']
    require(complete == products(inventory['matrix']), 'Complete product discovery changed')
    rows = plan.get('rows')
    require(isinstance(rows, list) and (rows or allow_empty), 'No requested products')
    require(all(isinstance(row, dict) and row in complete for row in rows) and
            rows == [row for row in complete if row in rows],
            'Requested products must be an ordered unique discovery subset')
    included = {(row['source'], row['arch']) for row in rows}
    require(plan.get('native_rows') == [row for row in inventory['native_rows']
            if (row['source'], row['arch']) in included],
            'Requested native rows differ from selected products')
    return rows


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
    from native_candidate_input import latest_executed_job
    identity = {'run_id': run_id, 'run_attempt': attempt, 'head_sha': commit}
    result = []
    for row in requested:
        name = f'publication_packages ({row["source"]}, {row["arch"]})'
        matches = [job for job in jobs if job.get('name') == name]
        require(matches and all(positive(j.get('run_attempt')) and j['run_attempt'] <= attempt and
                j.get('run_id') == run_id and j.get('head_sha') == commit for j in matches),
                'Missing or foreign package branch job: ' + row['key'])
        job = latest_executed_job(matches, identity)
        latest = job['run_attempt']
        require(job.get('status') == 'completed' and job.get('conclusion') in ('success', 'failure', 'timed_out'),
                'Nonterminal, cancelled or skipped package branch cannot authorize publication')
        success = job['conclusion'] == 'success'
        result.append({**row, 'status': 'success' if success else 'failure', 'stage': 'package',
                       'reason': '' if success else 'GitHub package job ' + job['conclusion'],
                       'producer_run_attempt': latest, 'job_id': job['id'], 'job_url': job['html_url']})
    validate(requested, result, run_id, attempt)
    return result


def collect_recorded(jobs, requested, shards, plan, attempt):
    """Use uploaded step outcomes; API rows supply execution identity only.

    The native matrix dependency must have succeeded before this is called.
    Every real execution uploads a report, even after a package failure. The
    newest report wins; copied rerun API jobs do not invent a new execution.
    """
    from native_candidate_input import CONTROL_LIMIT, json_object, latest_executed_job
    run_id=int(plan['workflow_run_id']);commit=plan['workflow_commit']
    require(positive(attempt) and positive(run_id), 'Invalid recorded package identity')
    wanted={row['key']:row for row in requested}
    known={source+'-'+arch for source in SOURCES for arch in ARCHES}
    selected={}
    for folder in Path(shards).iterdir():
        match=re.fullmatch(r'package-shard-([1-9][0-9]*)-(.+)',folder.name)
        require(match is not None and match[2] in known and folder.is_dir() and not folder.is_symlink(),
                'Unexpected package outcome shard')
        producer,key=int(match[1]),match[2]
        require(producer<=attempt, 'Future package outcome attempt')
        if key not in wanted:
            require(producer<attempt, 'Unrequested current package outcome')
            continue
        if key not in selected or producer>selected[key][0]:selected[key]=(producer,folder)
    require(set(selected)==set(wanted), 'Every package branch needs its uploaded outcome record')
    result=[]
    expected_identity={k:plan[k] for k in ('version','repository','tag','workflow_commit','workflow_run_id')}
    for row in requested:
        producer,folder=selected[row['key']]
        path=folder/'shard.json'
        require(path.is_file() and not path.is_symlink() and 0<path.stat().st_size<=CONTROL_LIMIT,
                'Missing or unsafe package outcome receipt')
        record=json_object(path.read_bytes())
        exact_keys(record,('identity','row','files','companions','plan_sha256','outcome'),'package outcome record')
        require(record['identity']==expected_identity and record['row']==row and
                record['plan_sha256']==digest(plan), 'Package outcome plan or producer changed')
        outcome=record['outcome']
        exact_keys(outcome,('status','stage','reason','producer_run_attempt'),'package step outcome')
        require(outcome['status'] in ('success','failure') and outcome['stage']=='package' and
                outcome['producer_run_attempt']==producer and type(outcome['producer_run_attempt']) is int and
                outcome['reason']==('' if outcome['status']=='success' else 'Package build or validation command failed'),
                'Package step outcome differs from its execution')
        if outcome['status']=='failure':
            require(record['files']=={} and record['companions']=={} and
                    {str(p.relative_to(folder)) for p in folder.rglob('*')}=={'shard.json'},
                    'Failed package outcome must not carry unvalidated payloads')
        matches=[job for job in jobs if job.get('name')==f'publication_packages ({row["source"]}, {row["arch"]})']
        if not matches:raise MissingPackageExecution('Package execution identity not yet visible: '+row['key'])
        job=latest_executed_job(matches,{'run_id':run_id,'run_attempt':attempt,'head_sha':commit})
        if job['run_attempt']<producer:
            raise MissingPackageExecution('Recorded package attempt not yet visible: '+row['key'])
        require(positive(job.get('id')) and job.get('run_id')==run_id and job.get('head_sha')==commit and
                job['run_attempt']==producer and
                job.get('html_url')==f'https://github.com/{REPOSITORY}/actions/runs/{run_id}/job/{job["id"]}',
                'Recorded package belongs to another workflow execution')
        result.append({**row,**outcome,'job_id':job['id'],'job_url':job['html_url']})
    validate(requested,result,run_id,attempt)
    return result


def validate_preparation(proof, plan):
    from native_candidate_input import recorded_context
    context = recorded_context(plan, plan.get('version'))
    exact_keys(proof, ('schema', 'contract', 'version', 'release_tag', 'repository',
        'policy_fingerprint', 'workflow_run_id', 'workflow_run_attempt', 'workflow_commit',
        'plan_sha256', 'requested_products', 'outcomes', 'files', 'upstream_input') +
        tuple(context), 'preparation receipt')
    require(recorded_context(proof, plan['version']) == context,
            'Preparation recovery mode differs from authenticated plan')
    expected = {'schema': 2, 'contract': 'downstream-matrix', 'version': plan['version'],
                'release_tag': plan['tag'], 'repository': plan['repository'],
                'workflow_run_id': int(plan['workflow_run_id']), 'workflow_commit': plan['workflow_commit']}
    require(type(proof.get('schema')) is int and all(proof.get(k) == v for k, v in expected.items()) and
            positive(proof.get('workflow_run_attempt')), 'Preparation receipt identity mismatch')
    require(proof.get('plan_sha256') == digest(plan) and proof.get('upstream_input') == plan['upstream_input'],
            'Preparation plan digest or upstream transport changed')
    hash_value(proof['policy_fingerprint'])
    requested = requested_products(plan)
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
