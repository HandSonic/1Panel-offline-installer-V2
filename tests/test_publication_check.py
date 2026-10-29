"""Daily completeness checks read metadata; only missing checksums can be written."""
import copy
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import publication_check as check
import package_retry
from publication_outcomes import products
from test_offline_public_source import Client, Release


class Writer(Client):
    def __init__(self, release):
        super().__init__(release)
        self.uploads = []
        self.pinned = None

    def pin_release(self, identifier):
        self.pinned = identifier

    def upload(self, path):
        assert self.pinned == self.releases[0].id
        self.uploads.append(path.name)
        self.releases[0].add(path.name, path.read_bytes())
        return next(a for a in self.releases[0].assets if a['name'] == path.name)


class PublicationCheckTests(unittest.TestCase):
    def setUp(self):
        self.release = Release(rows=(('official', 'amd64'), ('custom', 'arm64')))
        self.client = Writer(self.release)
        self.rows = products(self.release.plan()['resolved']['inventory']['matrix'])

    def inspect(self):
        return check.inspect_publication(self.client, self.release.version,
                                         self.release.version, 'stable', self.rows)

    def test_complete_release_never_downloads_old_archives_or_sidecars(self):
        state = self.inspect()
        self.assertEqual(state['missing_rows'], [])
        self.assertEqual(state['checksum_names'], [])
        self.assertEqual(self.client.downloads, [])

    def test_missing_archive_and_checksum_only_gaps_are_separate(self):
        name = next(a['name'] for a in self.release.assets if '-official-' in a['name'] and a['name'].endswith('.tar.gz'))
        for removed in (name, name + '.sha256'):
            with self.subTest(removed=removed):
                saved = self.release.assets
                self.release.assets = [a for a in saved if a['name'] != removed]
                state = self.inspect()
                self.assertEqual(state['missing_rows'], [self.rows[0]] if removed == name else [])
                self.assertEqual(state['checksum_names'], [name] if removed.endswith('.sha256') else [])
                self.release.assets = saved

    def test_new_release_and_draft_require_build_but_network_failure_does_not(self):
        endpoint = f'repos/{check.REPOSITORY}/releases/tags/{self.release.version}'
        missing = subprocess.CalledProcessError(1, ['gh', 'api', endpoint], stderr='HTTP 404')
        with patch.object(self.client, 'release', side_effect=missing):
            self.assertEqual(self.inspect()['missing_rows'], self.rows)
        for status in ('403', '429', '500'):
            error = subprocess.CalledProcessError(1, ['gh', 'api'], stderr='HTTP ' + status)
            with self.subTest(status=status), patch.object(self.client, 'release', side_effect=error):
                with self.assertRaises(subprocess.CalledProcessError):
                    self.inspect()
        for endpoint in (f'repos/{check.REPOSITORY}/releases?per_page=100&page=1',
                         f'repos/{check.REPOSITORY}/releases/123/assets?per_page=100&page=1'):
            error = subprocess.CalledProcessError(1, ['gh', 'api', endpoint], stderr='HTTP 404')
            with self.subTest(endpoint=endpoint), patch.object(self.client, 'release', side_effect=error):
                with self.assertRaises(subprocess.CalledProcessError):
                    self.inspect()
        release = self.release.value(); release['draft'] = True
        with patch.object(self.client, 'release', return_value=release):
            self.assertEqual(self.inspect()['missing_rows'], self.rows)
            self.assertEqual(self.inspect()['checksum_names'], [])

    def test_incomplete_conflicting_or_ambiguous_assets_fail_without_building(self):
        for fault in ('starter', 'zero', 'digest', 'sidecar-conflict', 'duplicate', 'channel'):
            with self.subTest(fault=fault):
                value = self.release.value()
                if fault == 'starter': value['assets'][0]['state'] = 'starter'
                if fault == 'zero': value['assets'][0]['size'] = 0
                if fault == 'digest': value['assets'][0]['digest'] = None
                if fault == 'sidecar-conflict':
                    next(a for a in value['assets'] if a['name'].endswith('.sha256'))['digest'] = 'sha256:' + '0' * 64
                if fault == 'duplicate': value['assets'].append(copy.deepcopy(value['assets'][0]))
                if fault == 'channel': value['prerelease'] = True
                with patch.object(self.client, 'release', return_value=value), self.assertRaises(ValueError):
                    self.inspect()
        self.assertEqual(self.client.uploads, [])

    def test_checksum_completion_only_adds_missing_small_file_and_is_idempotent(self):
        name = next(a['name'] for a in self.release.assets if a['name'].endswith('.sha256'))
        self.release.assets = [a for a in self.release.assets if a['name'] != name]
        before = copy.deepcopy(self.release.assets)
        for unused in range(2):
            result = check.complete_checksums(self.client, self.release.version,
                                               self.release.version, 'stable', self.rows)
            self.assertEqual(result['checksum_names'], [])
        self.assertEqual(self.client.uploads, [name])
        self.assertEqual([a for a in self.release.assets if a['name'] != name], before)
        self.assertEqual(self.client.downloads, [])

    def test_checksum_completion_rejects_archive_identity_change_before_write(self):
        name = next(a['name'] for a in self.release.assets if a['name'].endswith('.sha256'))
        self.release.assets = [a for a in self.release.assets if a['name'] != name]
        original = self.client.release
        reads = 0
        def changed():
            nonlocal reads
            reads += 1
            value = original()
            if reads > 1:
                asset = next(a for a in value['assets'] if a['name'] + '.sha256' == name)
                asset['id'] += 1000
                asset['url'] = f'https://api.github.com/repos/{check.REPOSITORY}/releases/assets/{asset["id"]}'
            return value
        with patch.object(self.client, 'release', side_effect=changed), self.assertRaisesRegex(ValueError, 'Archive changed'):
            check.complete_checksums(self.client, self.release.version, self.release.version, 'stable', self.rows)
        self.assertEqual(self.client.uploads, [])

    def test_discovery_uses_dynamic_vendor_architectures_and_all_custom_targets(self):
        def vendor(version, mode, source):
            return {'availability': 'present', 'archives': {'amd64': {}, 'arm64': {}}} if source == 'official' else {'availability': 'absent', 'archives': {}}
        with patch('resolved_transport.discover_vendor', side_effect=vendor):
            rows = check.discover_rows(self.release.version, 'stable')
        self.assertEqual(len(rows), 9)
        self.assertEqual([r['arch'] for r in rows if r['source'] == 'custom'], list(check.ARCHES))
        with patch('resolved_transport.discover_vendor', return_value={'availability': 'failed', 'archives': {}}):
            with self.assertRaisesRegex(ValueError, 'retry discovery without building'):
                check.discover_rows(self.release.version, 'stable')

    def test_explicit_repair_still_requests_every_product(self):
        facts = self.release.plan(); facts['rows'] = self.rows
        for operation in ('validate-repair', 'repair-existing'):
            with self.subTest(operation=operation), patch.dict(os.environ, PUBLICATION_OPERATION=operation), \
                    patch.object(self.client, 'release') as read:
                self.assertEqual(package_retry.pending_rows(facts, ROOT, self.client), self.rows)
                read.assert_not_called()

    def test_check_cli_outputs_both_gates_without_resolving_or_validating_payloads(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, GITHUB_OUTPUT=str(Path(temporary) / 'outputs')), \
                patch.object(check, 'discover_rows', return_value=self.rows), \
                patch.object(check, 'GitHub', return_value=self.client), \
                patch('resolved_transport.resolve', side_effect=AssertionError('Full source resolution must not run')), \
                patch('sys.stdout', new_callable=io.StringIO):
            outputs = check.main(['check', '--repository', check.REPOSITORY, '--version', self.release.version,
                                  '--tag', self.release.version, '--mode', 'stable'])
            self.assertEqual(outputs['build_required'], 'false')
            self.assertEqual(outputs['checksum_required'], 'false')
            self.assertIn('build_required=false\n', (Path(temporary) / 'outputs').read_text())


if __name__ == '__main__':
    unittest.main()
