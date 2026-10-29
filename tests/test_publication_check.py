"""Daily completeness checks read metadata; only missing checksums can be written."""
import copy
import io
import json
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
        for removed in ({name}, {name + '.sha256'}, {name, name + '.sha256'}):
            with self.subTest(removed=removed):
                saved = self.release.assets
                self.release.assets = [a for a in saved if a['name'] not in removed]
                state = self.inspect()
                if removed == {name}:
                    self.assertEqual(state['missing_rows'], [])
                    self.assertEqual(state['checksum_names'], [])
                    self.assertIn('Orphan checksum requires restoring the original archive', state['issues'][0])
                else:
                    self.assertEqual(state['missing_rows'], [self.rows[0]] if name in removed else [])
                    self.assertEqual(state['checksum_names'], [name] if name not in removed else [])
                    self.assertEqual(state['issues'], [])
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

    def test_product_issues_do_not_block_missing_siblings_but_shared_errors_remain_fatal(self):
        missing = check.filename(self.release.version, self.rows[1])
        for fault in ('starter', 'zero', 'digest', 'sidecar-conflict', 'duplicate', 'channel'):
            with self.subTest(fault=fault):
                value = self.release.value()
                if fault == 'starter': value['assets'][0]['state'] = 'starter'
                if fault == 'zero': value['assets'][0]['size'] = 0
                if fault == 'digest': value['assets'][0]['digest'] = None
                if fault == 'sidecar-conflict':
                    next(a for a in value['assets'] if a['name'] ==
                         check.filename(self.release.version, self.rows[0]) + '.sha256')['digest'] = 'sha256:' + '0' * 64
                if fault == 'duplicate': value['assets'].append(copy.deepcopy(value['assets'][0]))
                if fault == 'channel': value['prerelease'] = True
                value['assets'] = [a for a in value['assets'] if a['name'] not in (missing, missing + '.sha256')]
                with patch.object(self.client, 'release', return_value=value):
                    if fault in ('duplicate', 'channel'):
                        with self.assertRaises(ValueError):
                            self.inspect()
                    else:
                        state = self.inspect()
                        self.assertEqual(state['missing_rows'], [self.rows[1]])
                        self.assertEqual(state['checksum_names'], [])
                        self.assertEqual(len(state['issues']), 1)
                        value['draft'] = True
                        with self.assertRaisesRegex(ValueError, 'Existing draft products require explicit recovery'):
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

    def test_checksum_completion_ignores_unrelated_product_issues(self):
        broken = check.filename(self.release.version, self.rows[0])
        healthy = check.filename(self.release.version, self.rows[1])
        self.release.assets = [a for a in self.release.assets if a['name'] not in (broken, healthy + '.sha256')]
        state = check.complete_checksums(self.client, self.release.version, self.release.version, 'stable', self.rows)
        self.assertEqual(self.client.uploads, [healthy + '.sha256'])
        self.assertEqual(state['missing_rows'], [])
        self.assertEqual(state['checksum_names'], [])
        self.assertEqual(len(state['issues']), 1)
        self.assertIn('Orphan checksum', state['issues'][0])
        self.assertEqual(self.client.downloads, [])

    def test_checksum_completion_rejects_its_own_new_conflict(self):
        name = next(a['name'] for a in self.release.assets if a['name'].endswith('.sha256'))
        self.release.assets = [a for a in self.release.assets if a['name'] != name]
        original = self.client.release
        reads = 0
        def changed():
            nonlocal reads
            reads += 1
            if reads == 2:
                self.release.add(name, b'conflicting checksum added by another writer')
            return original()
        with patch.object(self.client, 'release', side_effect=changed), self.assertRaisesRegex(ValueError, 'checksum conflicts'):
            check.complete_checksums(self.client, self.release.version, self.release.version, 'stable', self.rows)
        self.assertEqual(self.client.uploads, [])

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
            discovery = check.discover_rows(self.release.version, 'stable')
        rows = discovery['rows']
        self.assertEqual(discovery['issues'], [])
        self.assertEqual(len(rows), 9)
        self.assertEqual([r['arch'] for r in rows if r['source'] == 'custom'], list(check.ARCHES))
        with patch('resolved_transport.discover_vendor', return_value={'availability': 'failed', 'archives': {}}):
            discovery = check.discover_rows(self.release.version, 'stable')
        self.assertEqual(discovery['rows'], products({'custom': list(check.ARCHES)}))
        self.assertEqual(len(discovery['issues']), 2)

    def test_partial_vendor_discovery_preserves_independent_products(self):
        from resolved_transport import discover_vendor
        from test_resolved_inventory import responses
        for fault in ('official-arm64', 'enterprise-checksums'):
            observations = {}
            for source in ('official', 'enterprise'):
                checksum, probes = responses(self.release.version, 'stable', source, ('amd64', 'arm64'))
                observations[checksum['url']] = checksum
                observations.update({row['url']: row for row in probes.values()})
            def read(url, method='GET'):
                if ((fault == 'official-arm64' and '/v2/' in url and url.endswith('linux-arm64.tar.gz')) or
                        (fault == 'enterprise-checksums' and '/enterprise/' in url and url.endswith('checksums.txt'))):
                    raise OSError('synthetic timeout')
                return {key: value for key, value in observations[url].items() if key != 'url'}
            with self.subTest(fault=fault), patch('resolved_transport.discover_vendor',
                    side_effect=lambda v, m, s: discover_vendor(v, m, s, read)):
                discovery = check.discover_rows(self.release.version, 'stable')
            matrix = {'official': ['amd64'] if fault == 'official-arm64' else ['amd64', 'arm64'],
                      'custom': list(check.ARCHES)}
            if fault == 'official-arm64':
                matrix.update({source: ['amd64', 'arm64'] for source in ('enterprise-original', 'enterprise-docker')})
            self.assertEqual(discovery['rows'], products(matrix))
            self.assertEqual(len(discovery['issues']), 1)

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
                patch.object(check, 'discover_rows', return_value={'rows': self.rows, 'issues': ['enterprise source unavailable']}), \
                patch.object(check, 'GitHub', return_value=self.client), \
                patch('resolved_transport.resolve', side_effect=AssertionError('Full source resolution must not run')), \
                patch('sys.stdout', new_callable=io.StringIO):
            outputs = check.main(['check', '--repository', check.REPOSITORY, '--version', self.release.version,
                                  '--tag', self.release.version, '--mode', 'stable'])
            self.assertEqual(outputs['build_required'], 'false')
            self.assertEqual(outputs['checksum_required'], 'false')
            self.assertEqual(outputs['issues_present'], 'true')
            self.assertEqual(json.loads(outputs['issues_json']), ['enterprise source unavailable'])
            self.assertIn('build_required=false\n', (Path(temporary) / 'outputs').read_text())
            draft = self.release.value(); draft['draft'] = True
            with patch.object(self.client, 'release', return_value=draft):
                outputs = check.main(['check', '--repository', check.REPOSITORY, '--version', self.release.version,
                                      '--tag', self.release.version, '--mode', 'stable'])
            self.assertEqual(outputs['build_required'], 'true')
            self.assertEqual(outputs['issues_present'], 'true')


if __name__ == '__main__':
    unittest.main()
