"""Synthetic package-only predecessor transport; no Actions or application execution."""
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
from test_package_public_source import Publisher
from resolved_inventory import canonical
from validate_resolved_custom import archive_bytes, byte_facts
import native_upgrade_input as upgrade
import public_predecessor_source as source


class PackagePredecessorTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.publisher = Publisher()
        self.version = self.publisher.tag
        self.arch = 'amd64'
        self.name = f'1panel-{self.version}-linux-{self.arch}.tar.gz'
        self.raw = self.publisher.bodies[self.name]
        path = self.root / 'original.tar.gz'
        path.write_bytes(self.raw)
        self.body = archive_bytes(path, byte_facts(self.raw), self.name[:-7])
        manifest = json.loads(self.body['manifest.json'])
        name = f'1panel-{self.version}-custom-offline-linux-{self.arch}.tar.gz'
        url = f'https://github.com/{self.publisher.repo}/releases/download/{self.version}/{self.name}'
        offline = {'schema': 1, 'architecture': self.arch, 'source': 'custom',
                   'app_version': self.version, 'inputs': {'app': dict(
                       byte_facts(self.raw), version=self.version, source_kind='release',
                       url=url, checksum_url=url + '.sha256',
                       upstream_sha256=byte_facts(self.raw)['sha256'])},
                   'upstream_manifest_sha256': byte_facts(self.body['manifest.json'])['sha256'],
                   'upstream_provenance': manifest}
        self.archive = self.root / 'offline.tar.gz'
        with tarfile.open(self.archive, 'w:gz') as archive:
            for relative, raw in {'offline-manifest.json': canonical(offline),
                                  'manifest.json': self.body['manifest.json']}.items():
                info = tarfile.TarInfo(name[:-7] + '/' + relative)
                info.size = len(raw)
                archive.addfile(info, io.BytesIO(raw))
        facts = byte_facts(self.archive.read_bytes())
        self.binding = {'version': self.version, 'mode': 'stable',
                        'archive': dict(name=name, **facts)}
        self.receipt = {'files': {name: facts},
                        'upstream_input': {'source_kind': 'verified-public-release'}}

    def retained(self):
        work = self.root / 'retained'
        work.mkdir()
        with patch.object(source, 'ControlGitHub', return_value=self.publisher), \
                patch.object(upgrade, 'PredecessorGitHub', return_value=self.publisher), \
                patch.object(upgrade, 'canonical_read', side_effect=self.publisher.read):
            return source.source_from_public_predecessor(
                self.archive, self.binding, self.receipt, 'custom', self.arch, work, ROOT)

    def retain_backups(self):
        for asset in self.publisher.assets:
            if asset['name'] in (self.name, self.name + '.sha256'):
                old = asset['name']
                asset['name'] += '.backup-123456789abc'
                self.publisher.bodies[asset['name']] = self.publisher.bodies[old]

    def assert_original(self, body, proof):
        self.assertEqual(body, self.body)
        self.assertEqual(proof['acquisition']['kind'], 'exact-retained-package-source')
        self.assertEqual(proof['upstream']['source_kind'], 'trusted-package-release')
        self.assertEqual(proof['pin'], byte_facts(self.raw))
        self.assertEqual(proof['original_upstream_input'], self.receipt['upstream_input'])

    def test_canonical_package_public_predecessor_without_actions_history(self):
        self.assert_original(*self.retained())

    def test_retained_backup_package_public_predecessor_without_actions_history(self):
        self.retain_backups()
        self.assert_original(*self.retained())
        self.assertIn(self.name + '.backup-123456789abc', self.publisher.downloaded)

    def test_package_only_explicit_candidate_predecessor_without_actions_history(self):
        runtime = self.publisher.runtime()
        self.assertEqual(runtime['upstream']['source_kind'], 'trusted-package-release')
        plan = {'resolved': runtime, 'version': self.version, 'mode': 'stable',
                'upstream_input': {'source_kind': 'verified-public-release'}}
        with patch.object(upgrade, 'ControlGitHub', return_value=self.publisher), \
                patch.object(upgrade, 'PredecessorGitHub', return_value=self.publisher), \
                patch.object(upgrade, 'array_pages', return_value=self.publisher.assets), \
                patch.object(upgrade, 'public_release', side_effect=lambda *args: self.publisher.release()), \
                patch.object(upgrade, 'canonical_read', side_effect=self.publisher.read):
            body, proof = upgrade.source_archive_from_candidate(
                plan, runtime, 'custom', self.arch, self.root)
        self.assertEqual(body, self.body)
        self.assertEqual(proof['upstream'], runtime['upstream'])
        self.assertEqual(proof['pin'], byte_facts(self.raw))
        self.assertEqual(proof['acquisition']['kind'], 'exact-public-asset')

    def test_retained_selected_asset_mutation_rejected(self):
        self.retain_backups()
        selected = next(asset for asset in self.publisher.assets
                        if asset['name'] == self.name + '.backup-123456789abc')
        self.publisher.mutate = lambda publisher: selected.update(digest='sha256:' + 'f' * 64)
        with self.assertRaisesRegex(ValueError, 'changed during authentication'):
            self.retained()


if __name__ == '__main__':
    unittest.main()
