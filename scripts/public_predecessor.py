#!/usr/bin/env python3
"""Public predecessor selection and shared identity checks.

Selection ranks canonical releases in the same channel. Authentication and
native acceptance are performed by the production predecessor adapters.
"""
from resolved_inventory import (commit_value, digest, exact_keys, hash_value,
                                require, version_identity)

REPOSITORY = 'HandSonic/1Panel-offline-installer-V2'
UPSTREAM = 'HandSonic/1Panel-Build-v2'
API_ROOT = 'https://api.github.com/repos/' + REPOSITORY
WORKFLOW = '.github/workflows/build-offline-v2.yml'
RECEIPT = 'release-validation.json'


def positive(value):
    return type(value) is int and value > 0


def semver(version, mode):
    """Comparable key for the shared stable/-beta.N/-dev.N version contract."""
    match = version_identity(version, mode)
    identifiers = []
    if match[3] is not None:
        for item in match[3].split('.'):
            if item.isdigit():
                require(item == '0' or not item.startswith('0'),
                        'Noncanonical numeric prerelease identifier')
                identifiers.append((0, int(item)))
            else:
                identifiers.append((1, item))
    return (2, int(match[1]), int(match[2]), 1 if match[3] is None else 0,
            tuple(identifiers))


def channel(tag):
    """Return the channel only for canonical version tags; ignore other tags."""
    if not isinstance(tag, str):
        return None
    for mode in ('stable', 'beta', 'dev'):
        try:
            semver(tag, mode)
            return mode
        except ValueError:
            pass
    return None


def release_identity(release, repository=REPOSITORY):
    require(isinstance(release, dict) and positive(release.get('id')) and
            isinstance(release.get('tag_name'), str) and
            type(release.get('draft')) is bool and
            type(release.get('prerelease')) is bool,
            'Malformed release catalogue identity')
    require(release.get('url') == 'https://api.github.com/repos/' + repository + '/releases/' + str(release['id']),
            'Release repository/ID endpoint mismatch')
    return {key: release[key] for key in ('id', 'tag_name', 'draft', 'prerelease', 'url')}


def predecessor_candidates(target_version, mode, releases, *, catalogue_complete=False):
    """Rank a complete catalogue before authenticating each product's presence.

    catalogue_complete is an adapter assertion after all pages succeed, not
    authentication. An API error/rate limit must never turn into an empty page.
    Publication dates, API order and mutable "latest" pointers do not rank tags.
    """
    target_key = semver(target_version, mode)
    require(catalogue_complete is True, 'Complete release catalogue required')
    require(isinstance(releases, list), 'Release catalogue must be a list')
    snapshots, candidates, ids, tags = [], [], set(), set()
    for release in releases:
        identity = release_identity(release)
        require(identity['id'] not in ids and identity['tag_name'] not in tags,
                'Ambiguous duplicate release ID or tag')
        ids.add(identity['id'])
        tags.add(identity['tag_name'])
        snapshots.append(identity)
        candidate_mode = channel(identity['tag_name'])
        if identity['draft'] or candidate_mode is None:
            continue
        require(identity['prerelease'] == (candidate_mode != 'stable'),
                'Canonical release prerelease/channel mismatch')
        if candidate_mode == mode:
            key = semver(identity['tag_name'], mode)
            if key < target_key:
                candidates.append((key, identity))
    catalogue_sha = digest(sorted(snapshots, key=lambda row: row['id']))
    return [{'schema': 1, 'repository': REPOSITORY, 'target_version': target_version,
             'mode': mode, 'release': selected, 'catalogue_sha256': catalogue_sha}
            for _, selected in sorted(candidates, key=lambda row: row[0], reverse=True)]


def select_predecessor(target_version, mode, releases, *, catalogue_complete=False):
    """Select the nearest release without relaxing failed authentication."""
    candidates = predecessor_candidates(target_version, mode, releases,
                                         catalogue_complete=catalogue_complete)
    require(candidates, 'No strictly earlier canonical release in the target channel')
    return candidates[0]


def receipt_run_identity(run, proof):
    """Check supplied API fields; this does not authenticate the supplied object."""
    require(isinstance(run, dict) and positive(run.get('id')) and
            run['id'] == proof['workflow_run_id'] and
            run.get('head_sha') == proof['workflow_commit'] and
            positive(run.get('run_attempt')),
            'Receipt run/head/attempt identity mismatch')
    require(run.get('status') == 'completed' and run.get('conclusion') == 'success',
            'Predecessor receipt run must be completed and successful; no fallback')
    require(run.get('path') == WORKFLOW and
            run.get('event') in ('push', 'schedule', 'workflow_dispatch'),
            'Unexpected receipt workflow/event')
    repository_ids = []
    for field in ('repository', 'head_repository'):
        repository = run.get(field)
        require(isinstance(repository, dict) and repository.get('full_name') == REPOSITORY and
                positive(repository.get('id')), 'Receipt run repository mismatch')
        repository_ids.append(repository['id'])
    require(repository_ids[0] == repository_ids[1], 'Receipt run repository ID mismatch')
    return {'run_id': run['id'], 'run_attempt': run['run_attempt'],
            'head_sha': run['head_sha'], 'repository_id': repository_ids[0],
            'workflow': WORKFLOW, 'event': run['event']}


def upstream_input_claim(value):
    """Preserve the two producer formats without authenticating their claims."""
    require(isinstance(value, dict), 'Unsupported upstream_input claim')
    if 'source_kind' in value:
        exact_keys(value, ('source_kind',), 'public upstream_input')
        require(value['source_kind'] == 'verified-public-release',
                'Unsupported public upstream_input source kind')
    else:
        exact_keys(value, ('repository', 'run_id', 'artifact_id', 'artifact_sha256',
                           'build_repository_commit', 'run_url'), 'CI upstream_input')
        require(value['repository'] == UPSTREAM and positive(value['run_id']) and
                positive(value['artifact_id']), 'Invalid CI upstream_input identity')
        hash_value(value['artifact_sha256'])
        commit_value(value['build_repository_commit'])
        require(value['run_url'] == f'https://github.com/{UPSTREAM}/actions/runs/{value["run_id"]}',
                'CI upstream_input run URL/identity mismatch')
    return dict(value)
