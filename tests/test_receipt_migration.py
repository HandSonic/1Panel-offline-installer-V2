"""Receipt-only migration with a complete fake release; no network or builders."""
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import receipt_migration as migration
from publication_contract import PROOF, make_proof
from release_asset_repair import digest, repair

VERSION = 'v2.3.2'
REPO = 'HandSonic/1Panel-offline-installer-V2'
ENV = {'GITHUB_SHA': 'a' * 40, 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
       'GITHUB_EVENT_NAME': 'workflow_dispatch', 'PUBLICATION_OPERATION': 'refresh-receipt'}


class FakeGitHub:
    repo = REPO
    tag = VERSION

    def __init__(self, files, proof):
        self.assets = {i: (p.name, p.read_bytes()) for i, p in enumerate(files, 1)}
        self.assets[len(self.assets) + 1] = (PROOF, proof)
        self.original_receipt_sha = hashlib.sha256(proof).hexdigest()
        self.body = 'Original release notes, preserved verbatim.\n'
        self.release_id = 50
        self.draft = False
        self.calls = []
        self.failure = None
        self.failed = False

    def release(self):
        return {'id': self.release_id, 'tag_name': self.tag, 'draft': self.draft,
                'prerelease': False, 'body': self.body,
                'assets': [{'id': key, 'name': name, 'size': len(data),
                            'digest': 'sha256:' + hashlib.sha256(data).hexdigest()}
                           for key, (name, data) in self.assets.items()]}

    def run(self, *args):
        endpoint = args[-1]
        if endpoint.endswith('/jobs?filter=all&per_page=100'):
            return json.dumps({'jobs': [{'id': 88, 'name': 'publication_prepare', 'conclusion': 'success'}]})
        if endpoint.endswith('/logs'):
            return 'VERIFIED_RELEASE_RECEIPT_SHA256=' + self.original_receipt_sha + '\n'
        return json.dumps({'id': 99, 'head_sha': 'f' * 40, 'status': 'completed',
                           'conclusion': 'success', 'event': 'workflow_dispatch',
                           'path': '.github/workflows/build-offline-v2.yml'})

    def download(self, name, directory):
        data = next(data for filename, data in self.assets.values() if filename == name)
        if self.failure == 'readback' and '.staged-' in name:
            data = b'corrupt readback'
        (directory / name).write_bytes(data)

    def upload(self, path):
        self.calls.append(('upload', path.name))
        self.assets[max(self.assets) + 1] = (path.name, path.read_bytes())

    def rename(self, asset_id, name):
        self.calls.append(('rename', asset_id, name))
        if name in [n for i, (n, _) in self.assets.items() if i != asset_id]:
            raise ValueError('Remote name collision')
        self.assets[asset_id] = (name, self.assets[asset_id][1])
        if self.failure == 'switch' and name == PROOF and not self.failed:
            self.failed = True
            raise RuntimeError('Successful rename with a lost response')

    def notes(self, body):
        self.calls.append(('notes', body))
        self.body = body


class ReceiptMigrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(temporary)
        self.stack.enter_context(patch.dict(os.environ, ENV))
        original = self.root / 'published'
        files = migration.payload_paths(original, VERSION)
        for path in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.name != 'checksums.txt':
                path.write_bytes(('original published bytes: ' + path.name).encode())
        checksums = original / 'checksums.txt'
        # CRLF, reversed order and no final newline must remain byte-identical.
        checksums.write_bytes('\r\n'.join(digest(p)['sha256'] + '  ' + p.name
                                           for p in reversed(files) if p.name.endswith('.tar.gz')).encode())
        previous = make_proof(files, migration.CONTRACT, VERSION, VERSION, REPO, '99', 'f' * 40)
        previous['policy_fingerprint'] = '0' * 64
        previous['upstream_input'] = {'source_kind': 'verified-public-release'}
        self.previous = (json.dumps(previous, sort_keys=True) + '\n').encode()
        self.client = FakeGitHub(files, self.previous)
        self.before = copy.deepcopy(self.client.assets)
        self.before_body = self.client.body
        self.args = SimpleNamespace(version=VERSION, tag=VERSION, repository=REPO,
                                    work=self.root / 'work')
        self.stack.enter_context(patch.object(migration, 'GitHub', return_value=self.client))
        # Dedicated validators have real package fixtures elsewhere. This suite
        # exercises identity, full matrix/hash checks, receipts and the REAL writer.
        self.validate = self.stack.enter_context(patch.object(
            migration, 'validate_payloads', side_effect=lambda d, c, v: migration.payload_paths(d, v)))

    def prepare(self):
        migration.revalidate(self.args)
        proof = self.args.work / 'control' / PROOF
        os.environ['EXPECTED_VALIDATION_RECEIPT_SHA256'] = digest(proof)['sha256']
        return proof

    def test_readonly_revalidates_all_existing_bytes_without_builder_or_writer(self):
        import manual_publication
        with patch.object(migration, 'repair') as writer, patch.object(manual_publication, 'build_downstream') as builder:
            self.prepare()
        writer.assert_not_called()
        builder.assert_not_called()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.client.assets, self.before)
        self.validate.assert_called_once()
        local = {p.name: p.read_bytes() for p in migration.payload_paths(self.args.work / 'release', VERSION)}
        self.assertEqual(local, {name: body for name, body in self.before.values() if name != PROOF})
        self.assertEqual((self.args.work / 'control' / migration.PRIOR_PROOF).read_bytes(), self.previous)

    def test_refresh_preserves_all_packages_checksums_ids_notes_and_receipt_backup(self):
        proof = self.prepare()
        expected = proof.read_bytes()
        migration.refresh(self.args)
        self.assertEqual(self.validate.call_count, 2)
        self.assertEqual(self.client.body, self.before_body)
        old_receipt_id = next(i for i, (name, _) in self.before.items() if name == PROOF)
        for key, pair in self.before.items():
            if key != old_receipt_id:
                self.assertEqual(self.client.assets[key], pair)
        backup_name, backup_bytes = self.client.assets[old_receipt_id]
        self.assertRegex(backup_name, r'^release-validation.json\.backup-[0-9a-f]{12}$')
        self.assertEqual(backup_bytes, self.previous)
        self.assertEqual(next(body for name, body in self.client.assets.values() if name == PROOF), expected)
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual(self.client.calls[0][0], 'upload')
        self.assertTrue(all(call[-1].startswith(PROOF) for call in self.client.calls))
        journal = json.loads((self.args.work / 'control' / 'receipt-refresh-journal.json').read_text())
        self.assertEqual(journal['phase'], 'complete')
        self.assertEqual([op['canonical'] for op in journal['operations']], [PROOF])
        self.assertNotIn('notes_update_attempted', journal)

    def test_local_drift_and_injected_files_fail_before_writer(self):
        for fault in ['package', 'checksums', 'receipt', 'previous', 'extra', 'symlink']:
            with self.subTest(fault=fault):
                self.args.work = self.root / fault
                self.prepare()
                package = next((self.args.work / 'release').glob('*/*.tar.gz'))
                if fault == 'package':
                    package.write_bytes(b'changed package')
                elif fault == 'checksums':
                    p = self.args.work / 'release/checksums.txt'
                    p.write_bytes(p.read_bytes().replace(b'\r\n', b'\n'))
                elif fault in ['receipt', 'previous']:
                    p = self.args.work / 'control' / (PROOF if fault == 'receipt' else migration.PRIOR_PROOF)
                    p.write_bytes(p.read_bytes() + b' ')
                elif fault == 'extra':
                    (self.args.work / 'release/unexpected').write_bytes(b'extra')
                else:
                    data = package.read_bytes()
                    package.unlink()
                    backing = self.root / 'symlink-target'
                    backing.write_bytes(data)
                    package.symlink_to(backing)
                with patch.object(migration, 'repair') as writer, self.assertRaises(ValueError):
                    migration.refresh(self.args)
                writer.assert_not_called()
                self.assertEqual(self.client.calls, [])

    def test_remote_hash_or_identity_drift_rejected_before_writer(self):
        self.prepare()
        initial = copy.deepcopy(self.client.assets)
        for fault in ['package_hash', 'package_id', 'checksum_hash', 'receipt_hash', 'extra', 'release_id', 'draft', 'tag']:
            with self.subTest(fault=fault):
                self.client.assets = copy.deepcopy(initial)
                self.client.release_id, self.client.draft, self.client.tag = 50, False, VERSION
                if fault in ['package_hash', 'checksum_hash', 'receipt_hash']:
                    key = next(i for i, (n, _) in self.client.assets.items() if
                               (n.endswith('.tar.gz') if fault == 'package_hash' else n == ('checksums.txt' if fault == 'checksum_hash' else PROOF)))
                    name, body = self.client.assets[key]
                    self.client.assets[key] = (name, body + b'changed')
                elif fault == 'package_id':
                    self.client.assets[1000] = self.client.assets.pop(1)
                elif fault == 'extra':
                    self.client.assets[1000] = ('unexpected.tar.gz', b'extra')
                elif fault == 'release_id':
                    self.client.release_id = 51
                elif fault == 'draft':
                    self.client.draft = True
                else:
                    self.client.tag = 'v2.3.1'
                with patch.object(migration, 'repair') as writer, self.assertRaises(ValueError):
                    migration.refresh(self.args)
                writer.assert_not_called()
                self.assertEqual(self.client.calls, [])

    def test_same_run_commit_attempt_and_exact_receipt_hash_are_required(self):
        self.prepare()
        for key, value in [('GITHUB_RUN_ID', '124'), ('GITHUB_SHA', 'b' * 40),
                           ('GITHUB_RUN_ATTEMPT', '2'), ('EXPECTED_VALIDATION_RECEIPT_SHA256', '1' * 64)]:
            with self.subTest(key=key), patch.dict(os.environ, {key: value}), patch.object(migration, 'repair') as writer:
                with self.assertRaises(ValueError):
                    migration.refresh(self.args)
                writer.assert_not_called()
        self.assertEqual(self.client.calls, [])

    def test_explicit_manual_refresh_gate_runs_before_any_read_or_write(self):
        for event, operation in [('push', 'refresh-receipt'), ('schedule', 'refresh-receipt'),
                                 ('pull_request', 'refresh-receipt'), ('workflow_dispatch', 'validate-receipt'),
                                 ('workflow_dispatch', 'repair-existing'), ('workflow_dispatch', 'build')]:
            with self.subTest(event=event, operation=operation), patch.dict(os.environ, GITHUB_EVENT_NAME=event, PUBLICATION_OPERATION=operation):
                with patch.object(migration, 'GitHub') as client, self.assertRaisesRegex(ValueError, 'explicitly selected manual'):
                    migration.refresh(self.args)
                client.assert_not_called()

    def test_prior_receipt_may_have_only_policy_staleness_not_wrong_files_or_run(self):
        for fault in ['version', 'files', 'commit', 'run', 'policy', 'log']:
            with self.subTest(fault=fault):
                self.args.work = self.root / fault
                self.client.assets = copy.deepcopy(self.before)
                previous = json.loads(self.previous)
                if fault == 'version': previous['version'] = 'v2.3.1'
                if fault == 'files': previous['files'].pop('checksums.txt')
                if fault == 'commit': previous['workflow_commit'] = 'c' * 40
                if fault == 'run': previous['workflow_run_id'] = 100
                if fault == 'policy': previous['policy_fingerprint'] = 'unverified'
                body = json.dumps(previous).encode()
                key = next(i for i, (n, _) in self.client.assets.items() if n == PROOF)
                self.client.assets[key] = (PROOF, body)
                self.client.original_receipt_sha = hashlib.sha256(body).hexdigest() if fault != 'log' else '1' * 64
                with self.assertRaises(ValueError):
                    migration.revalidate(self.args)
                self.assertEqual(self.client.calls, [])
                self.assertFalse((self.args.work / 'control' / PROOF).exists())

    def test_invalid_payloads_never_produce_new_receipt(self):
        self.validate.side_effect = ValueError('Invalid archive')
        with self.assertRaisesRegex(ValueError, 'Invalid archive'):
            self.prepare()
        self.assertFalse((self.args.work / 'control' / PROOF).exists())
        self.assertEqual(self.client.calls, [])

    def test_remote_change_during_validation_fails_readonly(self):
        def mutate(directory, contract, version):
            name, body = self.client.assets[1]
            self.client.assets[1] = (name, body + b'concurrent')
            return migration.payload_paths(directory, version)
        self.validate.side_effect = mutate
        with self.assertRaisesRegex(ValueError, 'identity or asset hashes changed'):
            self.prepare()
        self.assertFalse((self.args.work / 'control' / PROOF).exists())
        self.assertEqual(self.client.calls, [])

    def test_writer_guard_rejects_package_and_notes_mutation(self):
        proof = self.prepare()
        baseline = json.loads(proof.read_text())['receipt_migration']['published_snapshot']
        guarded = migration.ReceiptOnlyClient(self.client, baseline, digest(proof))
        package = next((self.args.work / 'release').glob('*/*.tar.gz'))
        with self.assertRaises(ValueError): guarded.upload(package)
        with self.assertRaises(ValueError): guarded.rename(1, 'checksums.txt.backup-' + 'a' * 12)
        with self.assertRaises(ValueError): guarded.notes('changed notes')
        self.assertEqual(self.client.calls, [])

    def test_drift_at_writer_entry_still_causes_zero_remote_writes(self):
        self.prepare()
        def racing_writer(client, files, journal, **kwargs):
            self.client.assets[1000] = self.client.assets.pop(1)
            return repair(client, files, journal, **kwargs)
        with patch.object(migration, 'repair', side_effect=racing_writer), self.assertRaises(ValueError):
            migration.refresh(self.args)
        self.assertEqual(self.client.calls, [])
        self.assertFalse((self.args.work / 'control/receipt-refresh-journal.json').exists())

    def test_uncertain_receipt_switch_restores_original_without_package_writes(self):
        self.prepare()
        self.client.failure = 'switch'
        with self.assertRaisesRegex(RuntimeError, 'lost response'):
            migration.refresh(self.args)
        for key, pair in self.before.items(): self.assertEqual(self.client.assets[key], pair)
        self.assertEqual(self.client.body, self.before_body)
        journal = json.loads((self.args.work / 'control/receipt-refresh-journal.json').read_text())
        self.assertEqual(journal['phase'], 'rolled_back')
        self.assertTrue(all(call[-1].startswith(PROOF) for call in self.client.calls))

    def test_bad_staged_readback_keeps_original_receipt_and_all_packages(self):
        self.prepare()
        self.client.failure = 'readback'
        with self.assertRaisesRegex(ValueError, 'Staged bytes'):
            migration.refresh(self.args)
        for key, pair in self.before.items(): self.assertEqual(self.client.assets[key], pair)
        self.assertFalse(any(call[0] == 'rename' for call in self.client.calls))
        self.assertEqual(self.client.body, self.before_body)


    def test_refreshed_receipt_is_accepted_by_normal_release_checker(self):
        from publication_contract import existing_release_state
        proof = self.prepare()
        migration.refresh(self.args)
        receipt_sha = digest(proof)['sha256']
        def current_run(*args):
            endpoint = args[-1]
            if endpoint.endswith('/jobs?filter=all&per_page=100'):
                return json.dumps({'jobs': [{'id': 89, 'name': 'publication_revalidate', 'conclusion': 'success'}]})
            if endpoint.endswith('/logs'):
                return 'VERIFIED_RELEASE_RECEIPT_SHA256=' + receipt_sha + '\n'
            return json.dumps({'id': 123, 'head_sha': 'a' * 40, 'status': 'completed',
                               'conclusion': 'success', 'event': 'workflow_dispatch',
                               'path': '.github/workflows/build-offline-v2.yml'})
        self.client.run = current_run
        self.assertEqual(existing_release_state(self.client, migration.CONTRACT, VERSION, VERSION), 'verified')

    def test_existing_recovery_assets_remain_unchanged(self):
        self.client.assets[900] = (PROOF + '.backup-' + 'b' * 12, b'older recovery proof')
        recovery = copy.deepcopy(self.client.assets[900])
        self.prepare()
        migration.refresh(self.args)
        self.assertEqual(self.client.assets[900], recovery)

    def test_receipt_byte_change_during_validation_is_rejected_before_writer(self):
        proof = self.prepare()
        def mutate(directory, contract, version):
            proof.write_bytes(proof.read_bytes() + b' ')
            return migration.payload_paths(directory, version)
        self.validate.side_effect = mutate
        with patch.object(migration, 'repair') as writer, self.assertRaisesRegex(ValueError, 'receipt bytes changed'):
            migration.refresh(self.args)
        writer.assert_not_called()
        self.assertEqual(self.client.calls, [])


if __name__ == '__main__':
    unittest.main()
