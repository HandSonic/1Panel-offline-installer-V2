#!/usr/bin/env python3
"""Select the nearest public product only after authenticating every absence.

Missing or invalid evidence is fatal. Only an authenticated exhaustive receipt
and complete asset listing can establish that a product was not published.
This adapter reads small controls, never package archives or current sources.
"""
from pathlib import Path
import re

from public_predecessor import REPOSITORY, RECEIPT, WORKFLOW, release_identity, semver
from resolved_inventory import NATIVE, commit_value, digest, exact_keys, hash_value, require

MAX_ABSENCES = 100


def candidate_order(candidates):
    """Require one complete-catalogue selection context in strict SemVer order."""
    require(isinstance(candidates, list) and candidates,
            'Public product selection requires predecessor candidates')
    first, previous, ids = None, None, set()
    for candidate in candidates:
        exact_keys(candidate, ('schema', 'repository', 'target_version', 'mode',
                               'release', 'catalogue_sha256'), 'predecessor candidate')
        require(type(candidate['schema']) is int and candidate['schema'] == 1 and
                candidate['repository'] == REPOSITORY, 'Unexpected predecessor candidate identity')
        hash_value(candidate['catalogue_sha256'])
        context = {key: candidate[key] for key in ('target_version', 'mode', 'catalogue_sha256')}
        if first is None:
            first = context
        require(context == first, 'Mixed predecessor candidate catalogues/targets')
        identity = release_identity(candidate['release'])
        key = semver(identity['tag_name'], candidate['mode'])
        require(not identity['draft'] and identity['prerelease'] == (candidate['mode'] != 'stable') and
                key < semver(candidate['target_version'], candidate['mode']) and
                (previous is None or key < previous) and identity['id'] not in ids,
                'Predecessor candidates must be distinct and strictly descending in the target channel')
        previous = key
        ids.add(identity['id'])


def select_public_product(client, candidates, source, arch, work):
    """Return the selected controls/binding after proving any nearer absences.

    Returns (selection, release, receipt_bytes, receipt, run, binding, assets).
    An empty candidate catalogue is an applicability decision for the caller;
    nonempty catalogues with no authenticated product always fail here.
    """
    from native_upgrade_input import (CONTROL_LIMIT, api, asset_pin, bind_public,
        digest as file_digest, json_object, positive, public_controls_binding,
        public_release, release_snapshot, run_snapshot, verify_receipt_log)

    require(client.repo == REPOSITORY and source in ('official', 'custom') and arch in NATIVE,
            'Unsupported public predecessor product/repository')
    candidate_order(candidates)
    work = Path(work)
    require(work.is_dir() and not work.is_symlink(), 'Public predecessor control workspace required')
    absent = []
    for index, selection in enumerate(candidates):
        require(index <= MAX_ABSENCES, 'Public predecessor absence trail exceeds bounded limit')
        directory = work / f'public-predecessor-{index:04d}'
        directory.mkdir(mode=0o700)
        release = public_release(client, selection['release']['id'])
        require(release_identity(release) == selection['release'],
                'Public predecessor candidate changed before authentication')
        assets = {}
        for asset in release['assets']:
            name = asset.get('name')
            require(isinstance(name, str) and name not in assets, 'Duplicate/malformed public asset name')
            assets[name] = asset
        controls = {}
        for name in (RECEIPT, 'checksums.txt'):
            require(name in assets, 'Missing public predecessor control: ' + name)
            pin = asset_pin(assets[name], name)
            require(pin['bytes'] <= CONTROL_LIMIT, 'Oversized public predecessor control: ' + name)
            path = directory / name
            client.download_asset(assets[name], path)
            controls[name] = path.read_bytes()
        receipt_bytes = controls[RECEIPT]
        receipt = json_object(receipt_bytes)
        require(positive(receipt.get('workflow_run_id')), 'Invalid public predecessor receipt run')
        run = api(client, f'repos/{REPOSITORY}/actions/runs/{receipt["workflow_run_id"]}')
        authenticated = public_controls_binding(release, receipt_bytes, controls['checksums.txt'], run)
        validator = verify_receipt_log(client, run, authenticated['receipt_sha256'], receipt)
        native_sha = None
        if receipt['schema'] == 2:
            name = 'native-acceptance.json'
            pin = asset_pin(assets[name], name)
            require(pin['bytes'] <= 16 * CONTROL_LIMIT, 'Public native acceptance exceeds limit')
            path = directory / name
            client.download_asset(assets[name], path)
            require(file_digest(path) == receipt['files'][name], 'Public native acceptance bytes changed')
            native_sha = receipt['files'][name]['sha256']
        name = f'1panel-{selection["release"]["tag_name"]}-{source}-offline-linux-{arch}.tar.gz'
        present = name in receipt['files']
        require(present == (name in assets), 'Public predecessor product receipt/asset presence differs')
        # Re-fetch both immutable identities before accepting a skip or result.
        require(release_snapshot(public_release(client, release['id'])) == release_snapshot(release),
                'Public predecessor candidate changed during authentication')
        require(run_snapshot(api(client, f'repos/{REPOSITORY}/actions/runs/{run["id"]}')) == run_snapshot(run),
                'Public predecessor receipt run changed during selection')
        if present:
            binding = bind_public(selection, release, receipt_bytes, controls['checksums.txt'], run, source, arch)
            binding.update(receipt_validator=validator, product_absence_trail=absent)
            if native_sha is not None:
                binding['public_native_subject_sha256'] = native_sha
            return selection, release, receipt_bytes, receipt, run, binding, assets
        require(len(absent) < MAX_ABSENCES, 'Public predecessor absence trail exceeds bounded limit')
        absence = {'release': release_identity(release), 'source': source, 'arch': arch,
                   'receipt_sha256': authenticated['receipt_sha256'],
                   'receipt_run': authenticated['receipt_run'], 'receipt_validator': validator,
                   'release_snapshot_sha256': digest(release_snapshot(release)),
                   'run_snapshot_sha256': digest(run_snapshot(run)),
                   'reason': 'product-absent-from-authenticated-receipt-and-canonical-assets'}
        if native_sha is not None:
            absence['public_native_subject_sha256'] = native_sha
        absent.append(absence)
    raise ValueError('No authenticated public predecessor contains the requested product')


def recheck_absences(client, binding):
    """Revalidate every skipped public identity after selected-source acquisition."""
    from native_upgrade_input import api, positive, public_release, release_snapshot, run_snapshot

    require(client.repo == REPOSITORY and isinstance(binding, dict) and
            binding.get('kind') == 'current-run-public-predecessor-bootstrap' and
            binding.get('repository') == REPOSITORY,
            'Authenticated public product binding required for absence recheck')
    trail = binding.get('product_absence_trail')
    require(isinstance(trail, list) and len(trail) <= MAX_ABSENCES,
            'Missing/oversized public predecessor absence trail')
    selected = binding.get('selection')
    require(isinstance(selected, dict), 'Selected predecessor context unavailable')
    candidate_order([selected])
    require(binding.get('version') == selected['release']['tag_name'] and
            binding.get('release_id') == selected['release']['id'],
            'Absence recheck selected release identity mismatch')
    archive = binding.get('archive')
    require(isinstance(archive, dict) and isinstance(archive.get('name'), str),
            'Absence recheck selected package unavailable')
    product = re.fullmatch(r'1panel-' + re.escape(binding['version']) +
        r'-(official|custom)-offline-linux-(amd64|arm64)\.tar\.gz', archive['name'])
    require(product is not None, 'Absence recheck selected product mismatch')
    lower = semver(binding['version'], selected['mode'])
    previous = semver(selected['target_version'], selected['mode'])
    ids = {binding['release_id']}
    for entry in trail:
        require(isinstance(entry, dict), 'Malformed public predecessor absence entry')
        exact_keys(entry, ('release', 'source', 'arch', 'receipt_sha256', 'receipt_run',
                          'receipt_validator', 'release_snapshot_sha256', 'run_snapshot_sha256', 'reason') +
                   (('public_native_subject_sha256',) if 'public_native_subject_sha256' in entry else ()),
                   'public predecessor absence')
        release = release_identity(entry['release'])
        key = semver(release['tag_name'], selected['mode'])
        require(release == entry['release'] and not release['draft'] and
                release['prerelease'] == (selected['mode'] != 'stable') and
                lower < key < previous and release['id'] not in ids and
                (entry['source'], entry['arch']) == product.groups() and
                entry['reason'] == 'product-absent-from-authenticated-receipt-and-canonical-assets',
                'Absence trail release/product/order mismatch')
        previous = key
        ids.add(release['id'])
        for field in ('receipt_sha256', 'release_snapshot_sha256', 'run_snapshot_sha256'):
            hash_value(entry[field])
        run = entry['receipt_run']
        exact_keys(run, ('run_id', 'run_attempt', 'head_sha', 'repository_id', 'workflow', 'event'),
                   'absence receipt run')
        commit_value(run['head_sha'])
        require(all(positive(run[k]) for k in ('run_id', 'run_attempt', 'repository_id')) and
                run['workflow'] == WORKFLOW and run['event'] in ('push', 'schedule', 'workflow_dispatch'),
                'Invalid absence receipt run identity')
        validator = entry['receipt_validator']
        exact_keys(validator, ('job_id', 'job_name', 'attempt', 'log_sha256'), 'absence receipt validator')
        hash_value(validator['log_sha256'])
        modern = 'public_native_subject_sha256' in entry
        if modern:
            hash_value(entry['public_native_subject_sha256'])
        allowed = ('publication_acceptance',) if modern else ('build', 'publication_prepare', 'publication_revalidate')
        require(positive(validator['job_id']) and positive(validator['attempt']) and
                validator['attempt'] <= run['run_attempt'] and validator['job_name'] in allowed,
                'Invalid absence receipt validator identity')
    for entry in trail:
        release = public_release(client, entry['release']['id'])
        require(digest(release_snapshot(release)) == entry['release_snapshot_sha256'],
                'Skipped public predecessor release changed after selection')
        run = api(client, f'repos/{REPOSITORY}/actions/runs/{entry["receipt_run"]["run_id"]}')
        require(digest(run_snapshot(run)) == entry['run_snapshot_sha256'],
                'Skipped public predecessor receipt run changed after selection')
