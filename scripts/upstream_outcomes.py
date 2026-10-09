#!/usr/bin/env python3
"""Independent consumer checks for upper schema2 terminal branch accounting."""
from resolved_inventory import ARCHES, commit_value, exact_keys, object_bytes, require

REPO = 'HandSonic/1Panel-Build-v2'


def positive(value):
    return type(value) is int and value > 0


def validate(manifest, version):
    exact_keys(manifest, ('schema_version', 'version', 'artifacts', 'requested_architectures',
                         'producer_run_id', 'producer_run_attempt', 'producer_head_sha', 'outcomes'), 'upper matrix manifest')
    require(type(manifest['schema_version']) is int and manifest['schema_version'] == 2 and
            manifest['version'] == version and positive(manifest['producer_run_id']) and
            positive(manifest['producer_run_attempt']), 'Upper matrix identity mismatch')
    commit_value(manifest['producer_head_sha'])
    requested = manifest['requested_architectures']
    require(isinstance(requested, list) and len(requested) == len(ARCHES) and set(requested) == set(ARCHES),
            'Upper must account for every supported requested architecture')
    outcomes = manifest['outcomes']
    require(isinstance(outcomes, list) and len(outcomes) == len(requested), 'Missing upper branch outcome')
    accepted, ids = [], set()
    for arch, row in zip(requested, outcomes):
        exact_keys(row, ('architecture', 'status', 'stage', 'reason', 'job_id', 'job_url'), 'upper outcome')
        require(row['architecture'] == arch and row['status'] in ('success', 'failure') and row['stage'] == 'compile'
                and positive(row['job_id']) and row['job_id'] not in ids and row['job_url'] ==
                f'https://github.com/{REPO}/actions/runs/{manifest["producer_run_id"]}/job/{row["job_id"]}',
                'Upper branch identity/status mismatch')
        ids.add(row['job_id'])
        require(isinstance(row['reason'], str) and len(row['reason']) <= 512 and
                not any(ord(c) < 32 for c in row['reason']) and
                (row['status'] == 'success') == (row['reason'] == ''), 'Invalid upper outcome reason')
        if row['status'] == 'success': accepted.append(arch)
    require(isinstance(manifest['artifacts'], list) and
            [row.get('architecture') for row in manifest['artifacts']] == accepted,
            'Upper accepted artifacts differ from complete branch outcomes')
    return accepted


def verify_jobs(manifest, version, commit, client):
    accepted = validate(manifest, version)
    run_id, attempt = manifest['producer_run_id'], manifest['producer_run_attempt']
    head=manifest['producer_head_sha'];commit_value(commit)
    base = f'repos/{REPO}/actions/runs/{run_id}/attempts/{attempt}'
    run = object_bytes(client.run('api', base).encode())
    require(run.get('id') == run_id and run.get('run_attempt') == attempt and run.get('head_sha') == head and
            run.get('status') == 'completed' and run.get('conclusion') in ('success', 'failure') and
            run.get('path', '').split('@')[0] == '.github/workflows/build.yml' and
            run.get('event') in ('push', 'schedule', 'workflow_dispatch', 'pull_request') and
            all(run.get(k, {}).get('full_name') == REPO for k in ('repository', 'head_repository')),
            'Upper producer attempt is not an authenticated terminal build')
    if run['event']=='pull_request':
        prs=run.get('pull_requests')
        require(isinstance(prs,list) and len(prs)==1 and prs[0].get('head',{}).get('sha')==head, 'Ambiguous PR producer head')
        base_sha=prs[0].get('base',{}).get('sha');commit_value(base_sha)
        merge=object_bytes(client.run('api',f'repos/{REPO}/git/commits/{commit}').encode())
        require(merge.get('sha')==commit and [p.get('sha') for p in merge.get('parents',[])]==[base_sha,head], 'Executed PR merge does not bind the exact base/head parents')
    else:
        require(commit==head, 'Non-PR producer head differs from executed commit')
    jobs = []
    for page in range(1, 101):
        response = object_bytes(client.run('api', base + f'/jobs?per_page=100&page={page}').encode())
        entries = response.get('jobs')
        require(isinstance(entries, list), 'Invalid upper job collection')
        jobs.extend(entries)
        if len(entries) < 100:
            require(response.get('total_count') == len(jobs), 'Incomplete upper job collection')
            break
    else: raise ValueError('Upper job collection limit exceeded')
    def exact_job(name):
        matches = [job for job in jobs if job.get('name') == name]
        require(len(matches) == 1, 'Missing or ambiguous upper job: ' + name)
        job = matches[0]
        require(job.get('run_id') == run_id and job.get('run_attempt') == attempt and
                job.get('head_sha') == head and job.get('status') == 'completed' and positive(job.get('id')),
                'Upper job has a different identity or is not terminal')
        return job
    for name in ('tests', 'prepare', 'build'):
        require(exact_job(name).get('conclusion') == 'success', 'Upper shared trust gate failed: ' + name)
    for row in manifest['outcomes']:
        job = exact_job('compile (' + row['architecture'] + ')')
        require(job['id'] == row['job_id'] and job.get('html_url') == row['job_url'] and
                job.get('conclusion') in ('success', 'failure', 'timed_out') and
                (job['conclusion'] == 'success') == (row['status'] == 'success'),
                'Upper outcome is not the exact terminal compile job')
    failures = {r['job_id'] for r in manifest['outcomes'] if r['status'] == 'failure'}
    require({job['id'] for job in jobs if job.get('conclusion') not in ('success', 'skipped')} == failures and
            (run['conclusion'] == 'success') == (not failures),
            'Upper workflow failure is not fully explained by declared branches')
    return accepted
