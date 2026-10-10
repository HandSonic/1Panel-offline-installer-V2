#!/usr/bin/env python3
"""Pure public predecessor selection and byte binding; NOT native acceptance.

No network, repository files, checked-in run records, downloads or installation
are used here. A transport adapter must obtain a complete authenticated release
catalogue, then re-fetch the selected release and its assets by immutable IDs.
Selection is the highest strictly earlier SemVer in the SAME channel. Drafts,
other channels and noncanonical tags are ineligible. A selected broken release
is an error, never permission to try an older release or a different channel.

The output is a deterministic, integrity-bound snapshot, not authenticated
provenance. Even a successful receipt run and mutually consistent SHA-256s do
not prove that its producer or native jobs tested these bytes. The existing
manually reviewed native-upgrade gate remains authoritative and untouched.
"""
import hashlib
import re

from resolved_inventory import (ARCHES, MAX_CONTROL, NATIVE, SOURCES,
                                commit_value, digest, exact_keys, file_facts,
                                hash_value, object_bytes, require,
                                version_identity)

REPOSITORY = 'HandSonic/1Panel-offline-installer-V2'
UPSTREAM = 'HandSonic/1Panel-Build-v2'
API_ROOT = 'https://api.github.com/repos/' + REPOSITORY
WORKFLOW = '.github/workflows/build-offline-v2.yml'
RECEIPT = 'release-validation.json'

# This is a list of missing integration checks, never affirmative evidence.
NATIVE_INTEGRATION_REQUIREMENTS = (
    'Authenticate complete GitHub API reads, resolve the trusted workflow/policy '
    'revision and independently derive the complete source/matrix contract; '
    'receipt hashes and a receipt-supplied policy_fingerprint are not trust roots.',
    'Bind the receipt to the exact successful publication_prepare job log and '
    'controls artifact: repository, run ID, head SHA, attempt, job ID, artifact '
    'ID/name, upload-log digest, downloaded ZIP hash/size, receipt and plan bytes. '
    'A receipt refresh/revalidation job alone is insufficient.',
    'Verify each package producer job and shard artifact against the same '
    'successful run/head and its explicit producing attempt, including immutable '
    'artifact IDs, upload logs, ZIP hashes/sizes and shard plan/row/file membership. '
    'Do not infer attempts, silently inherit unrelated reruns or use expired '
    'artifacts as evidence.',
    'Verify the complete required native install matrix (official/custom and '
    'enterprise-docker when present, amd64/arm64, fresh/existing): successful '
    'exact jobs plus result provenance bound to the same receipt, controls, '
    'shards, package hashes and runner architecture. Job success alone is '
    'insufficient. Cancelled writers and receipt-only refreshes cannot stand in '
    'for missing same-run native evidence.',
    'Download only pinned public asset IDs on disposable runners; enforce byte '
    'limits, whole-archive hashes, safe archive inventories, manifest identity, '
    'source-specific binary/configuration pins and independent payload validation. '
    'Re-read release/asset and run identities before use to catch replacement.',
    'Bind the current target separately to its same-run candidate inputs and run '
    'the real native upgrade/rollback/data-preservation acceptance. Historical '
    'installer evidence never approves an old upgrade.sh or proves target runtime '
    'acceptance.',
)


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


def select_predecessor(target_version, mode, releases, *, catalogue_complete=False):
    """Select from a complete API snapshot; do not inspect assets to pick a winner.

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
    require(candidates, 'No strictly earlier canonical release in the target channel')
    selected = max(candidates, key=lambda row: row[0])[1]
    return {'schema': 1, 'repository': REPOSITORY, 'target_version': target_version,
            'mode': mode, 'release': selected,
            'catalogue_sha256': digest(sorted(snapshots, key=lambda row: row['id']))}


def expected_archives(version, matrix):
    """Validate a separately resolved complete inventory, never infer from assets."""
    require(isinstance(matrix, dict) and {'official', 'custom'} <= set(matrix) <= set(SOURCES),
            'Unsupported or incomplete predecessor source matrix')
    for arches in matrix.values():
        require(isinstance(arches, list) and arches and
                all(isinstance(arch, str) and arch in ARCHES for arch in arches) and
                len(arches) == len(set(arches)) and set(NATIVE) <= set(arches),
                'Invalid predecessor architecture matrix or missing native pair')
    require(set(matrix['custom']) == set(ARCHES), 'Complete custom architecture set required')
    require(('enterprise-original' in matrix) == ('enterprise-docker' in matrix),
            'Both enterprise variants must be present or absent')
    if 'enterprise-original' in matrix:
        require(set(matrix['enterprise-original']) == set(matrix['enterprise-docker']),
                'Enterprise variants have incompatible architecture sets')
    return {f'1panel-{version}-{source}-offline-linux-{arch}.tar.gz'
            for source, arches in matrix.items() for arch in arches}


def byte_facts(raw):
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_CONTROL, 'Invalid control size')
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


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


def bind_predecessor(selection, release, receipt_bytes, checksum_bytes, matrix, receipt_run):
    """Pin exact canonical assets; return (snapshot, canonical snapshot SHA-256).

    The caller must supply the SELECTED release, not an alternative after failure.
    matrix must come from independent source discovery/contract verification.
    Supplied API objects and the matrix are only structurally checked here.
    Existing v1 receipts lack attempt, plan and native-result bindings, so even
    this successful function always returns native_acceptance='blocked'.
    """
    exact_keys(selection, ('schema', 'repository', 'target_version', 'mode',
                           'release', 'catalogue_sha256'), 'predecessor selection')
    require(type(selection['schema']) is int and selection['schema'] == 1 and
            selection['repository'] == REPOSITORY, 'Unexpected selection identity')
    hash_value(selection['catalogue_sha256'])
    selected = release_identity(selection['release'])
    require(release_identity(release) == selected and not selected['draft'] and
            selected['prerelease'] == (selection['mode'] != 'stable'),
            'Selected predecessor release changed; no fallback')
    version = selected['tag_name']
    require(semver(version, selection['mode']) < semver(selection['target_version'], selection['mode']),
            'Selected release is not strictly earlier in the target channel')
    archives = expected_archives(version, matrix)
    expected_files = archives | {'checksums.txt'}
    proof = object_bytes(receipt_bytes)
    proof_keys = ('schema', 'contract', 'version', 'release_tag', 'repository',
                  'policy_fingerprint', 'workflow_run_id', 'workflow_commit', 'files')
    exact_keys(proof, proof_keys + (('upstream_input',) if 'upstream_input' in proof else ()),
               'public receipt')
    upstream = upstream_input_claim(proof['upstream_input']) if 'upstream_input' in proof else None
    require(type(proof['schema']) is int and proof['schema'] == 1 and
            proof['contract'] == 'downstream17' and proof['version'] == version and
            proof['release_tag'] == version and proof['repository'] == REPOSITORY and
            positive(proof['workflow_run_id']), 'Public predecessor receipt identity mismatch')
    hash_value(proof['policy_fingerprint'])
    commit_value(proof['workflow_commit'])
    require(isinstance(proof['files'], dict) and set(proof['files']) == expected_files,
            'Receipt canonical membership differs from complete source matrix')
    for facts in proof['files'].values():
        file_facts(facts)
    require(byte_facts(checksum_bytes) == proof['files']['checksums.txt'],
            'Checksum manifest hash/size mismatch')
    sums = {}
    for line in checksum_bytes.decode('utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([^/\\]+)', line)
        require(match is not None and match[2] not in sums, 'Malformed or duplicate checksum record')
        sums[match[2]] = match[1]
    require(set(sums) == archives and
            all(sums[name] == proof['files'][name]['sha256'] for name in archives),
            'Checksum canonical membership/hash mismatch')
    require(isinstance(release.get('assets'), list), 'Complete release asset list required')
    assets, recovery_assets, ids, names = {}, {}, set(), set()
    expected_assets = expected_files | {RECEIPT}
    for asset in release['assets']:
        require(isinstance(asset, dict) and positive(asset.get('id')) and
                isinstance(asset.get('name'), str), 'Invalid release asset identity')
        name = asset['name']
        require(name not in names and asset['id'] not in ids, 'Ambiguous duplicate asset name or ID')
        # Repair deliberately keeps older bytes. The UUID token is a naming
        # convention, not a content digest or proof of successful repair.
        backup = re.fullmatch(r'(.+)\.backup-[0-9a-f]{12}', name)
        require(name in expected_assets or (backup is not None and backup[1] in expected_assets),
                'Unexpected or staged release asset; explicit recovery review required')
        require(asset.get('url') == API_ROOT + '/releases/assets/' + str(asset['id']) and
                asset.get('state') == 'uploaded', 'Release asset endpoint/state mismatch')
        require(isinstance(asset.get('digest'), str) and asset['digest'].startswith('sha256:'),
                'Release/recovery asset digest unavailable; immutable byte binding required')
        facts = {'sha256': asset['digest'][7:], 'bytes': asset.get('size')}
        file_facts(facts)
        pin = {'asset_id': asset['id'], 'url': asset['url'], **facts}
        if name in expected_assets:
            expected = byte_facts(receipt_bytes) if name == RECEIPT else proof['files'][name]
            require(facts == expected, 'Release asset SHA-256/size mismatch')
            assets[name] = pin
        else:
            recovery_assets[name] = {'canonical_name': backup[1], **pin}
        ids.add(asset['id'])
        names.add(name)
    require(set(assets) == expected_assets, 'Incomplete canonical release assets')
    run = receipt_run_identity(receipt_run, proof)
    snapshot = {'schema': 1, 'kind': 'public-predecessor-binding-prototype',
                'repository': REPOSITORY, 'target_version': selection['target_version'],
                'version': version, 'mode': selection['mode'], 'release_id': selected['id'],
                'release_url': selected['url'], 'catalogue_sha256': selection['catalogue_sha256'],
                'matrix': {source: [a for a in ARCHES if a in matrix[source]]
                           for source in SOURCES if source in matrix},
                'receipt_run': run, 'policy_fingerprint_claim': proof['policy_fingerprint'],
                'assets': {name: assets[name] for name in sorted(assets)},
                'recovery_assets': {name: recovery_assets[name] for name in sorted(recovery_assets)},
                'native_acceptance': 'blocked',
                'blocker': 'Public v1 receipt has no verified same-run producer/native provenance; '
                           'successful receipt refreshes do not close this gap.',
                'required_integration': list(NATIVE_INTEGRATION_REQUIREMENTS)}
    if upstream is not None:
        snapshot['upstream_input_claim'] = upstream
    return snapshot, digest(snapshot)


def require_native_acceptance(binding):
    """Deliberate fail-closed boundary until a real provenance verifier is wired.

    No caller-supplied flag, altered snapshot or collection of self-consistent
    hashes can authorize native execution through this prototype.
    """
    raise ValueError('Native predecessor acceptance blocked: implement and verify same-run '
                     'producer/native provenance; retain the manually reviewed historical gate')
