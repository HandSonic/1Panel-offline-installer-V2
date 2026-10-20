"""A partial publication retries missing products while keeping verified bytes."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import package_matrix
import package_retry as retry
from publication_outcomes import REPOSITORY, filename
from release_asset_repair import digest


def facts(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


class ExistingPackage:
    repo = REPOSITORY
    tag = 'v2.99.0'

    def __init__(self, source='official', draft=False):
        self.row = {'source': source, 'arch': 'amd64', 'key': source + '-amd64'}
        self.draft = draft
        self.downloads = []
        self.reads = 0
        self.mutate = None
        self.name = filename(self.tag, self.row)
        self.pin = dict(facts(b'current vendor original'), version=self.tag, url='https://vendor.invalid/app')
        self.dependencies = {key: dict(facts(key.encode()), version='1.0', url='https://vendor.invalid/' + key)
                             for key in ('docker', 'compose')}
        self.manifest = {'schema': 1, 'app_version': self.tag, 'source': source, 'architecture': 'amd64',
            'publication': {'format': 'package-only-v1', 'repository': REPOSITORY,
                'release_tag': self.tag, 'policy_fingerprint': 'a' * 64},
            'inputs': dict(self.dependencies, app=self.pin)}
        self.runtime = {'inventory': dict(self.dependencies), 'upstream': {'records': {
            'amd64': {'size': self.pin['bytes'], 'sha256': self.pin['sha256']}}}}
        self.runtime['inventory'] = {key: {'amd64': value} for key, value in self.dependencies.items()}
        for vendor in ('official', 'enterprise'):
            self.runtime['inventory'][vendor] = {'archives': {'amd64': self.pin}}
        self.facts = {'version': self.tag, 'tag': self.tag, 'repository': REPOSITORY, 'mode': 'stable',
            'resolved': self.runtime, 'rows': [self.row], 'upstream_input': {},
            'workflow_commit': 'c' * 40, 'workflow_run_id': '123'}
        self.bodies = {}
        self.replace_manifest()

    def replace_manifest(self):
        if self.row['source'] == 'enterprise-original':
            raw = b'current vendor original'
        else:
            inner = (f'1panel-{self.tag}-linux-amd64' if self.row['source'] == 'enterprise-docker'
                     else self.name[:-7])
            data = json.dumps(self.manifest).encode()
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode='w:gz') as archive:
                member = tarfile.TarInfo(inner + '/offline-manifest.json')
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            raw = stream.getvalue()
        self.bodies[self.name] = raw
        self.bodies[self.name + '.sha256'] = (facts(raw)['sha256'] + '  ' + self.name + '\n').encode()
        self.refresh()

    def refresh(self):
        self.assets = [dict(id=index, name=name, state='uploaded', size=len(raw),
            digest='sha256:' + facts(raw)['sha256'],
            url=f'https://api.github.com/repos/{REPOSITORY}/releases/assets/{index}')
            for index, (name, raw) in enumerate(self.bodies.items(), 1)]

    def release(self):
        self.reads += 1
        if self.mutate and self.reads == 2:
            self.mutate(self)
        return copy.deepcopy(dict(id=100, tag_name=self.tag, draft=self.draft,
                                  prerelease=False, assets=self.assets))

    def download_asset(self, asset, destination):
        # Immutable asset IDs, rather than a release's mutable filename URL.
        self.downloads.append(asset['id'])
        original = next(a for a in self.assets if a['id'] == asset['id'])
        Path(destination).write_bytes(self.bodies[original['name']])


class PackageRetryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / 'build/v2.99.0'
        policy = patch('runtime_contract.product_policy', return_value='a' * 64)
        self.policy = policy.start()
        self.addCleanup(policy.stop)

    def fetch(self, fixture):
        return retry.fetch_existing(fixture.facts, fixture.row, self.output, ROOT, fixture)

    def test_public_and_draft_reuse_exact_bytes_without_sibling_dependency(self):
        for draft in (False, True):
            with self.subTest(draft=draft):
                existing = ExistingPackage(draft=draft)
                # A different product's bad sidecar cannot veto this product.
                existing.bodies['broken-sibling.sha256'] = b'bad sibling'
                existing.refresh()
                pin = self.fetch(existing)
                self.assertIsNotNone(pin)
                checker = Mock()
                retry.validate_existing(existing.facts, existing.row, self.output, pin, ROOT, checker)
                checker.assert_called_once_with(self.output, existing.tag, 'official', ['amd64'])
                self.assertEqual((self.output / 'official' / existing.name).read_bytes(), existing.bodies[existing.name])
                self.assertEqual(len(existing.downloads), 2)
                retry.discard(self.output, existing.tag, existing.row)

    def test_missing_sidecar_keeps_archive_but_bad_existing_sidecar_rebuilds(self):
        for state in ('missing', 'wrong-checksum', 'wrong-api-hash'):
            with self.subTest(state=state):
                existing = ExistingPackage()
                if state == 'missing':
                    del existing.bodies[existing.name + '.sha256']
                else:
                    existing.bodies[existing.name + '.sha256'] = b'0' * 64 + b'  wrong.tar.gz\n'
                existing.refresh()
                if state == 'wrong-api-hash':
                    existing.assets[0]['digest'] = 'sha256:' + '0' * 64
                pin = self.fetch(existing)
                self.assertEqual(pin is not None, state == 'missing')
                retry.discard(self.output, existing.tag, existing.row)
                self.assertFalse((self.output / 'official' / existing.name).exists())

    def test_changed_selected_id_or_sidecar_presence_during_read_is_rejected(self):
        for fault in ('id', 'new-sidecar'):
            with self.subTest(fault=fault):
                existing = ExistingPackage()
                if fault == 'id':
                    existing.mutate = lambda f: f.assets[0].update(id=900)
                else:
                    del existing.bodies[existing.name + '.sha256']; existing.refresh()
                    def add_sidecar(f):
                        f.bodies[f.name + '.sha256'] = b'new sidecar'; f.refresh()
                    existing.mutate = add_sidecar
                self.assertIsNone(self.fetch(existing))
                self.assertFalse((self.output / 'official' / existing.name).exists())

    def test_current_product_policy_and_source_pins_are_required(self):
        for fault in ('policy', 'app', 'docker', 'compose'):
            with self.subTest(fault=fault):
                existing = ExistingPackage()
                if fault == 'policy':
                    existing.manifest['publication']['policy_fingerprint'] = 'b' * 64
                else:
                    existing.manifest['inputs'][fault] = dict(existing.manifest['inputs'][fault], sha256='b' * 64)
                existing.replace_manifest()
                pin = self.fetch(existing)
                checker = Mock()
                with self.assertRaises(ValueError):
                    retry.validate_existing(existing.facts, existing.row, self.output, pin, ROOT, checker)
                checker.assert_not_called()
                retry.discard(self.output, existing.tag, existing.row)

    def test_manifest_marker_alone_cannot_pass_the_real_static_validator(self):
        from manual_publication import isolated_check
        existing = ExistingPackage()
        pin = self.fetch(existing)
        # This archive has valid publisher metadata, but lacks the actual app,
        # installer and dependencies. The real full validator must still run.
        with self.assertRaises(retry.VALIDATION_ERRORS):
            retry.validate_existing(existing.facts, existing.row, self.output, pin, ROOT, isolated_check)

    def test_enterprise_original_and_docker_companion_use_current_vendor_bytes(self):
        original = ExistingPackage(source='enterprise-original')
        pin = self.fetch(original)
        checker = Mock()
        retry.validate_existing(original.facts, original.row, self.output, pin, ROOT, checker)
        checker.assert_called_once()
        retry.discard(self.output, original.tag, original.row)
        enhanced = ExistingPackage(source='enterprise-docker')
        def acquire(pin, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b'current vendor original')
            return pin
        with patch('prepare_enterprise.acquire', side_effect=acquire) as acquire_mock:
            pin = self.fetch(enhanced)
        acquire_mock.assert_called_once()
        original_path = self.output / 'enterprise-original' / filename(enhanced.tag, dict(enhanced.row, source='enterprise-original'))
        self.assertEqual(digest(original_path), {key: enhanced.pin[key] for key in ('bytes', 'sha256')})
        checker = Mock()
        retry.validate_existing(enhanced.facts, enhanced.row, self.output, pin, ROOT, checker)
        checker.assert_called_once()

    def run_shard(self, existing, checker, producer):
        work = self.root / 'input'; work.mkdir(exist_ok=True)
        (work / 'plan.json').write_text(json.dumps(existing.facts))
        args = SimpleNamespace(version=existing.tag, source=existing.row['source'], arch='amd64',
                               output=str(self.root / 'shard'))
        with patch.dict(os.environ, GH_TOKEN='synthetic-read-token', GITHUB_TOKEN='synthetic-other-token'), \
             patch.object(package_matrix, 'ROOT', self.root), \
             patch.object(package_matrix, 'load_plan', return_value=(work, existing.facts)), \
             patch('native_upgrade_input.PredecessorGitHub', return_value=existing) as client, \
             patch.object(package_matrix, 'isolated_check', side_effect=checker), \
             patch.object(package_matrix.subprocess, 'run', side_effect=producer) as build:
            package_matrix.shard(args)
            self.assertNotIn('GH_TOKEN', os.environ)
            self.assertNotIn('GITHUB_TOKEN', os.environ)
        client.assert_called_once_with(REPOSITORY, existing.tag, allow_drafts=True)
        return build, json.loads((self.root / 'shard/shard.json').read_text())

    def test_shard_reuses_original_bytes_with_current_job_receipt_and_no_credentials_in_checker(self):
        existing = ExistingPackage(draft=True)
        def checker(*args):
            self.assertNotIn('GH_TOKEN', os.environ)
            self.assertNotIn('GITHUB_TOKEN', os.environ)
        build, receipt = self.run_shard(existing, checker, AssertionError('valid existing product must not rebuild'))
        build.assert_not_called()
        relative = 'official/' + existing.name
        self.assertEqual((self.root / 'shard' / relative).read_bytes(), existing.bodies[existing.name])
        self.assertEqual(receipt['identity']['workflow_run_id'], '123')
        self.assertEqual(receipt['row'], existing.row)
        self.assertEqual(receipt['files'][relative], facts(existing.bodies[existing.name]))

    def test_failed_static_validation_cleans_candidate_and_builds_without_credentials(self):
        existing = ExistingPackage()
        calls = []
        def checker(directory, *args):
            self.assertNotIn('GH_TOKEN', os.environ)
            calls.append(1)
            if len(calls) == 1:
                raise ValueError('current static validator rejected the candidate')
        def producer(*args, **kwargs):
            self.assertNotIn('GH_TOKEN', os.environ)
            self.assertNotIn('GITHUB_TOKEN', os.environ)
            target = self.output / 'official' / existing.name
            self.assertFalse(target.exists())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'freshly rebuilt product')
        build, receipt = self.run_shard(existing, checker, producer)
        build.assert_called_once()
        self.assertEqual(len(calls), 2)
        self.assertEqual(receipt['files']['official/' + existing.name], facts(b'freshly rebuilt product'))


if __name__ == '__main__': unittest.main()
