#!/usr/bin/env python3
"""Independent consumer checks for upper schema2 terminal branch accounting."""
from datetime import datetime, timedelta
import re

from publication_contract import read_job_log
from resolved_inventory import ARCHES, commit_value, exact_keys, object_bytes, require

REPO = 'HandSonic/1Panel-Build-v2'

# Only these literal validator messages may cross the planner's otherwise
# class-only exception boundary. Never include arbitrary exception arguments.
PR_FAILURE_REASONS = frozenset((
    'Ambiguous PR producer head',
    'Missing upper checkout steps',
    'Missing or ambiguous upper checkout step',
    'Upper checkout step did not succeed',
    'Invalid upper checkout timestamp',
    'Invalid upper checkout interval',
    'Upper checkout log exceeds parser budget',
    'Missing or ambiguous upper checkout log evidence',
    'Upper checkout evidence is outside its authenticated step',
    'Upper checkout evidence crosses a later step boundary',
    'Upper checkout does not bind the executed PR merge',
    'Recovered PR does not bind the exact merged repository/base/head',
    'Executed PR merge does not bind the exact base/head parents',
))


def safe_pr_failure_reason(error):
    if type(error) is ValueError and len(error.args) == 1 and type(error.args[0]) is str:
        return error.args[0] if error.args[0] in PR_FAILURE_REASONS else None
    return None


def positive(value):
    return type(value) is int and value > 0


def merged_pr_checkout(job, head, commit, client):
    """Recover a removed run/PR link only from its exact successful checkout.

    Git objects alone do not establish that a workflow executed that merge. The
    authenticated job log must bind its PR ref and full executed SHA first. The
    closed PR then independently binds the frozen base/head, including after its
    live merge ref disappears. Unknown or ambiguous log formats fail closed.
    """
    steps = job.get('steps')
    require(isinstance(steps, list), 'Missing upper checkout steps')
    checkouts = [step for step in steps if isinstance(step, dict) and isinstance(step.get('name'), str) and
                 re.fullmatch(r'Run actions/checkout@(?:v[1-9][0-9]*|[0-9a-f]{40})', step.get('name', ''))]
    require(len(checkouts) == 1, 'Missing or ambiguous upper checkout step')
    step = checkouts[0]
    require(step.get('status') == 'completed' and step.get('conclusion') == 'success' and
            positive(step.get('number')), 'Upper checkout step did not succeed')

    def timestamp(value):
        require(isinstance(value, str) and re.fullmatch(
            r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z', value),
            'Invalid upper checkout timestamp')
        return datetime.fromisoformat(value.replace('Z', '+00:00'))

    start, end = timestamp(step.get('started_at')), timestamp(step.get('completed_at'))
    require(start <= end, 'Invalid upper checkout interval')
    # The jobs API rounds step times down to seconds; log times retain fractions.
    end += timedelta(seconds=1)
    log = read_job_log(client, job['id'])
    require(len(log.encode('utf-8')) <= 8 * 1024 * 1024, 'Upper checkout log exceeds parser budget')
    lines = []
    for line in log.splitlines():
        record = re.fullmatch(r'([0-9T:.-]+Z) (.*)', line)
        lines.append((record[1], record[2]) if record else (None, line))

    def one(pattern):
        matches = [(index, stamp, re.fullmatch(pattern, body))
                   for index, (stamp, body) in enumerate(lines) if re.fullmatch(pattern, body)]
        require(len(matches) == 1, 'Missing or ambiguous upper checkout log evidence')
        index, stamp, match = matches[0]
        require(start <= timestamp(stamp) < end, 'Upper checkout evidence is outside its authenticated step')
        return index, match

    opened, _ = one(r'##\[group\]' + re.escape(step['name']))
    synced, _ = one(r'Syncing repository: ' + re.escape(REPO))
    fetched, fetch = one(r'\[command\]/\S+/git -c protocol\.version=2 fetch --no-tags --prune '
                         r'--no-recurse-submodules --depth=1 origin \+([0-9a-f]{40}):refs/remotes/pull/([1-9][0-9]*)/merge')
    checked, checkout = one(r'\[command\]/\S+/git checkout --progress --force refs/remotes/pull/([1-9][0-9]*)/merge')
    described, description = one(r'HEAD is now at ([0-9a-f]{7,40}) Merge ([0-9a-f]{40}) into ([0-9a-f]{40})')
    queried, _ = one(r'\[command\]/\S+/git log -1 --format=%H')
    executed, execution = one(r'([0-9a-f]{40})')
    next_steps = [index for index, (_, body) in enumerate(lines) if index > opened and
                  re.match(r'##\[group\](?:Run |Post )', body)]
    require(not next_steps or executed < min(next_steps),
            'Upper checkout evidence crosses a later step boundary')
    require(opened < synced < fetched < checked < described < queried and executed == queried + 1 and
            fetch[1] == execution[1] == commit and commit.startswith(description[1]) and
            fetch[2] == checkout[1] and description[2] == head,
            'Upper checkout does not bind the executed PR merge')
    number, base_sha = int(fetch[2]), description[3]
    pr = object_bytes(client.run('api', f'repos/{REPO}/pulls/{number}').encode())
    require(pr.get('number') == number and pr.get('state') == 'closed' and pr.get('merged') is True and
            all(isinstance(pr.get(side), dict) and isinstance(pr[side].get('repo'), dict) and
                pr[side]['repo'].get('full_name') == REPO for side in ('base', 'head')) and
            pr.get('base', {}).get('sha') == base_sha and pr.get('head', {}).get('sha') == head,
            'Recovered PR does not bind the exact merged repository/base/head')
    return base_sha


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
    if run['event']=='pull_request':
        prs=run.get('pull_requests')
        require(isinstance(prs,list) and len(prs)<=1, 'Ambiguous PR producer head')
        if prs:
            require(isinstance(prs[0],dict) and isinstance(prs[0].get('head'),dict) and
                    isinstance(prs[0].get('base'),dict) and prs[0]['head'].get('sha')==head,
                    'Ambiguous PR producer head')
            base_sha=prs[0]['base'].get('sha');commit_value(base_sha)
        else:
            base_sha=merged_pr_checkout(exact_job('build'),head,commit,client)
        merge=object_bytes(client.run('api',f'repos/{REPO}/git/commits/{commit}').encode())
        parents=merge.get('parents')
        require(merge.get('sha')==commit and isinstance(parents,list) and
                all(isinstance(parent,dict) for parent in parents) and
                [parent.get('sha') for parent in parents]==[base_sha,head],
                'Executed PR merge does not bind the exact base/head parents')
    else:
        require(commit==head, 'Non-PR producer head differs from executed commit')
    return accepted
