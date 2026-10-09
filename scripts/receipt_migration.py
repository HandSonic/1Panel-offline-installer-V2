#!/usr/bin/env python3
"""Revalidate published downstream bytes; migrate only their validation receipt.

A stale policy fingerprint is the sole waived field of the prior receipt. All
published payloads, checksum bytes, asset IDs and prior validation provenance must
still agree. This module never calls a package builder or general publication.
"""
import json
import os
import re
from pathlib import Path

from publication_contract import (
    PROOF, ROOT, digest_bytes, expected_names, make_proof, policy_fingerprint,
    validate_payloads, verify_receipt, verify_validation_log,
)
from release_asset_repair import GitHub, check_asset_names, digest, repair

CONTRACT = 'downstream17'
PRIOR_PROOF = 'previous-release-validation.json'
KIND = 'receipt-policy-refresh'


def target(args):
    from manual_publication import check_identity
    if check_identity(args.version, args.tag, args.repository) != CONTRACT:
        raise ValueError('Receipt migration is downstream-only')
    # Fail before downloading anything if this checkout lacks a reviewed matrix.
    names = expected_names(CONTRACT, args.version)
    if not names or not any(name.endswith('.tar.gz') for name in names):
        raise ValueError('Receipt migration requires a complete reviewed matrix')
    return names


def asset_record(asset):
    if (type(asset.get('id')) is not int or asset['id'] <= 0 or
            type(asset.get('size')) is not int or asset['size'] <= 0 or
            not isinstance(asset.get('digest'), str) or
            not re.fullmatch(r'sha256:[0-9a-f]{64}', asset['digest'])):
        raise ValueError('Published assets require immutable IDs, sizes and SHA-256 digests')
    return {key: asset[key] for key in ['id', 'name', 'size', 'digest']}


def snapshot(release, names, tag):
    if (type(release.get('id')) is not int or release['id'] <= 0 or
            release.get('tag_name') != tag or release.get('draft') is not False):
        raise ValueError('Receipt migration requires the exact existing public release')
    canonical = names | {PROOF}
    check_asset_names(release['assets'], canonical)
    assets = {a['name']: asset_record(a) for a in release['assets']}
    if not canonical <= set(assets) or len({a['id'] for a in assets.values()}) != len(assets):
        raise ValueError('Published canonical asset matrix is incomplete or has duplicate IDs')
    return {
        'release_id': release['id'], 'tag_name': tag,
        'prerelease': release.get('prerelease', False),
        'body': release.get('body') or '', 'assets': assets,
    }


def assert_snapshot(client, baseline, names, tag):
    if snapshot(client.release(), names, tag) != baseline:
        raise ValueError('Published release identity or asset hashes changed; receipt refresh refused')


def verify_download(path, record):
    if path.is_symlink() or not path.is_file() or digest(path) != {
            'bytes': record['size'], 'sha256': record['digest'].removeprefix('sha256:')}:
        raise ValueError(f'Published asset download differs from its identity/hash: {path.name}')


def payload_paths(directory, version):
    matrix = json.loads((ROOT / f'release-matrix-{version}.json').read_text())
    paths = [directory / source / f'1panel-{version}-{source}-offline-linux-{arch}.tar.gz'
             for source, arches in matrix.items() for arch in arches]
    return sorted(paths + [directory / 'checksums.txt'], key=lambda p: p.name)


def verify_local_payloads(directory, version, baseline):
    paths = payload_paths(directory, version)
    actual = list(directory.rglob('*'))
    if directory.is_symlink() or any(p.is_symlink() for p in actual):
        raise ValueError('Receipt migration inputs must not contain symlinks')
    if {p for p in actual if p.is_file()} != set(paths):
        raise ValueError('Receipt migration payload matrix changed')
    for path in paths:
        verify_download(path, baseline['assets'][path.name])
    files = validate_payloads(directory, CONTRACT, version)
    if set(files) != set(paths):
        raise ValueError('Receipt migration validation returned a different payload matrix')
    # Detect any mutation during validation too; never regenerate checksum bytes.
    for path in files:
        verify_download(path, baseline['assets'][path.name])
    return files


def verify_previous(client, previous_bytes, checksums, baseline, args):
    previous = json.loads(previous_bytes)
    fingerprint = previous.get('policy_fingerprint')
    if not isinstance(fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', fingerprint):
        raise ValueError('Previous receipt has no valid policy fingerprint')
    record = baseline['assets'][PROOF]
    if digest_bytes(previous_bytes) != {'bytes': record['size'], 'sha256': record['digest'][7:]}:
        raise ValueError('Previous receipt bytes differ from the published asset')
    # Reuse the full receipt contract, waiving ONLY the stale policy fingerprint.
    # The log still binds the exact original receipt bytes to their original run.
    verify_validation_log(client, previous, record['digest'][7:])
    run = json.loads(client.run('api', f'repos/{client.repo}/actions/runs/{previous["workflow_run_id"]}'))
    if run.get('event') in ['pull_request', 'pull_request_target']:
        raise ValueError('A PR run cannot authorize a published receipt')
    compatible = dict(previous, policy_fingerprint=policy_fingerprint(CONTRACT, args.version))
    verify_receipt(compatible, checksums, list(baseline['assets'].values()), run,
                   CONTRACT, args.version, args.tag, args.repository)
    return previous


def make_migration(files, previous, baseline, args):
    proof = make_proof(files, CONTRACT, args.version, args.tag, args.repository,
                       os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_SHA'])
    if 'upstream_input' in previous:
        proof['upstream_input'] = previous['upstream_input']
    proof['receipt_migration'] = {
        'kind': KIND, 'previous_policy_fingerprint': previous['policy_fingerprint'],
        'workflow_run_attempt': int(os.environ.get('GITHUB_RUN_ATTEMPT', '1')),
        'published_snapshot': baseline,
    }
    return proof


def revalidate(args):
    """Download and check existing public assets with no remote write operations."""
    names = target(args)
    work = Path(args.work)
    if work.exists():
        raise ValueError('Receipt revalidation requires a new empty work directory')
    client = GitHub(args.repository, args.tag)
    baseline = snapshot(client.release(), names, args.tag)
    release = work / 'release'
    control = work / 'control'
    release.mkdir(parents=True)
    control.mkdir()
    # Download the exact canonical set; recovery assets stay remote and untouched.
    for path in payload_paths(release, args.version):
        path.parent.mkdir(parents=True, exist_ok=True)
        client.download(path.name, path.parent)
        verify_download(path, baseline['assets'][path.name])
    client.download(PROOF, control)
    old_path = control / PROOF
    verify_download(old_path, baseline['assets'][PROOF])
    previous_bytes = old_path.read_bytes()
    old_path.rename(control / PRIOR_PROOF)
    previous = verify_previous(client, previous_bytes, (release / 'checksums.txt').read_bytes(), baseline, args)
    files = verify_local_payloads(release, args.version, baseline)
    assert_snapshot(client, baseline, names, args.tag)
    proof = make_migration(files, previous, baseline, args)
    (control / PROOF).write_text(json.dumps(proof, indent=2, sort_keys=True) + '\n')
    print(json.dumps({'state': 'validated_existing_bytes_no_release_writes',
                      'package_count': len(names) - 1, 'repository': args.repository,
                      'tag': args.tag, 'receipt_sha256': digest(control / PROOF)['sha256']}))


class ReceiptOnlyClient:
    """Limit the general recovery writer to one receipt and verify all other IDs.

    Every remote mutation is preceded by a read-only consistency check. GitHub
    has no cross-asset compare-and-swap; concurrent external mutations still need
    journal review, and this guard never repairs packages to hide such drift.
    """
    def __init__(self, client, baseline, proof_facts):
        self.client = client
        self.repo, self.tag = client.repo, client.tag
        self.baseline = baseline
        self.proof_facts = dict(proof_facts)
        self.stage_name = None
        self.backup_name = None
        self.new_id = None

    def release(self):
        result = self.client.release()
        if (result.get('id') != self.baseline['release_id'] or
                result.get('tag_name') != self.tag or result.get('draft') is not False or
                result.get('prerelease', False) != self.baseline['prerelease']):
            raise ValueError('Published release identity changed during receipt refresh')
        assets = result['assets']
        if len({a['name'] for a in assets}) != len(assets) or len({a['id'] for a in assets}) != len(assets):
            raise ValueError('Duplicate remote asset identity during receipt refresh')
        by_id = {a['id']: asset_record(a) for a in assets}
        for name, record in self.baseline['assets'].items():
            observed = by_id.pop(record['id'], None)
            if name == PROOF:
                if observed is None or observed['name'] not in {PROOF, self.backup_name}:
                    raise ValueError('Original receipt identity changed during refresh')
                observed = dict(observed, name=PROOF)
            if observed != record:
                raise ValueError(f'Published asset identity/hash changed during refresh: {name}')
        for record in by_id.values():
            if (not self.stage_name or record['name'] not in {self.stage_name, PROOF} or
                    record['size'] != self.proof_facts['bytes'] or
                    record['digest'] != 'sha256:' + self.proof_facts['sha256'] or
                    (self.new_id is not None and record['id'] != self.new_id)):
                raise ValueError('Unexpected remote asset during receipt-only refresh')
            self.new_id = record['id']
        if len(by_id) > 1 or (self.new_id is not None and self.new_id not in by_id):
            raise ValueError('Staged receipt identity changed during refresh')
        return result

    def download(self, name, directory):
        self.release()
        if name not in {PROOF, self.stage_name}:
            raise ValueError('Receipt writer may only read back receipt assets')
        return self.client.download(name, directory)

    def upload(self, path):
        self.release()
        if (self.stage_name is not None or
                not re.fullmatch(re.escape(PROOF) + r'\.staged-[0-9a-f]{12}', path.name) or
                digest(path) != self.proof_facts):
            raise ValueError('Receipt-only refresh cannot upload package or checksum bytes')
        self.stage_name = path.name
        return self.client.upload(path)

    def rename(self, asset_id, name):
        self.release()
        old_id = self.baseline['assets'][PROOF]['id']
        backup = self.stage_name.replace('.staged-', '.backup-') if self.stage_name else None
        if asset_id == old_id and name in {PROOF, backup}:
            self.backup_name = backup
        elif asset_id == self.new_id and name in {PROOF, self.stage_name}:
            pass
        else:
            raise ValueError('Receipt-only refresh cannot rename package or checksum assets')
        return self.client.rename(asset_id, name)

    def notes(self, body):
        raise ValueError('Receipt-only refresh cannot change release notes')


def refresh(args):
    """Explicit same-run write gate; old receipt is retained by the repair writer."""
    if (os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or
            os.environ.get('PUBLICATION_OPERATION') != 'refresh-receipt'):
        raise ValueError('Receipt refresh requires explicitly selected manual refresh-receipt mode')
    names = target(args)
    work = Path(args.work)
    proof_path = work / 'control' / PROOF
    previous_path = work / 'control' / PRIOR_PROOF
    if (proof_path.is_symlink() or previous_path.is_symlink() or
            (work / 'control').is_symlink() or work.is_symlink()):
        raise ValueError('Receipt migration control inputs must not contain symlinks')
    proof_facts = digest(proof_path)
    if proof_facts['sha256'] != os.environ.get('EXPECTED_VALIDATION_RECEIPT_SHA256'):
        raise ValueError('Downloaded receipt differs from the same-run read-only revalidation')
    proof = json.loads(proof_path.read_text())
    migration = proof.get('receipt_migration', {})
    if migration.get('kind') != KIND:
        raise ValueError('An ordinary build receipt cannot authorize receipt-only refresh')
    baseline = migration['published_snapshot']
    client = GitHub(args.repository, args.tag)
    assert_snapshot(client, baseline, names, args.tag)
    previous = verify_previous(client, previous_path.read_bytes(),
                               (work / 'release' / 'checksums.txt').read_bytes(), baseline, args)
    files = verify_local_payloads(work / 'release', args.version, baseline)
    if proof != make_migration(files, previous, baseline, args):
        raise ValueError('Receipt migration identity, policy, inputs or same-run provenance changed')
    journal = work / 'control' / 'receipt-refresh-journal.json'
    if journal.exists():
        raise ValueError('Review the existing receipt-refresh journal before retrying')
    assert_snapshot(client, baseline, names, args.tag)
    # The writer requires the full canonical set. All payloads are verified equal
    # and skipped, while the old proof becomes a recoverable .backup asset.
    if digest(proof_path) != proof_facts:
        raise ValueError('Same-run receipt bytes changed during revalidation')
    guarded = ReceiptOnlyClient(client, baseline, proof_facts)
    state = repair(guarded, files + [proof_path], journal, update_notes=False)
    print(json.dumps({'state': state['phase'], 'operation': KIND,
                      'repository': args.repository, 'tag': args.tag}))
