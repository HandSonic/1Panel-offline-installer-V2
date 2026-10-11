"""Synthetic authenticated per-product predecessor selection; no downloads."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import public_predecessor_selection as selection
import native_upgrade_input as upgrade
import test_native_upgrade as upgrade_tests
from test_public_predecessor import fixture as public_fixture, rebind_controls
from resolved_inventory import digest


def facts(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


class Candidate:
    def __init__(self, version, number, absent=False, modern=False, source='official', arch='amd64'):
        self.args = (upgrade_tests.PublicBootstrapTests().modern_fixture()[0] if modern else
                     public_fixture(version=version, target='v2.102.0'))
        self.release, self.run = self.args[1], self.args[5]
        self.proof = json.loads(self.args[2])
        self.release['id'] += number
        self.release['url'] = f'{upgrade.API_ROOT}/releases/{self.release["id"]}'
        self.run['id'] += number
        self.run['head_sha'] = str(number + 1) * 40
        self.proof.update(workflow_run_id=self.run['id'], workflow_commit=self.run['head_sha'])
        self.name = f'1panel-{version}-{source}-offline-linux-{arch}.tar.gz'
        if absent:
            self.proof['files'].pop(self.name, None)
            self.release['assets'] = [a for a in self.release['assets'] if a['name'] != self.name]
            if modern:
                for outcome in self.proof['outcomes']:
                    if outcome['source'] == source and outcome['arch'] == arch:
                        outcome.update(status='failure', stage='native-acceptance', reason='synthetic unavailable product')
        for asset in self.release['assets']:
            asset['id'] += number * 1000
            asset['url'] = f'{upgrade.API_ROOT}/releases/assets/{asset["id"]}'
        checksums = ''.join(p['sha256'] + '  ' + n + '\n' for n, p in self.proof['files'].items()
                            if n.endswith('.tar.gz')).encode()
        rebind_controls(self.args, self.proof, checksums)
        self.bytes = {upgrade.RECEIPT: self.args[2], 'checksums.txt': self.args[3]}
        if modern:
            self.bytes['native-acceptance.json'] = b'{"kind":"synthetic unsigned native subject"}'
        self.job = dict(id=610000 + number, name='publication_acceptance' if modern else 'publication_prepare',
            run_id=self.run['id'], run_attempt=self.run['run_attempt'], head_sha=self.run['head_sha'],
            status='completed', conclusion='success')
        self.log = 'VERIFIED_RELEASE_RECEIPT_SHA256=' + facts(self.args[2])['sha256'] + '\n'
        if modern:
            self.log += 'NATIVE_ACCEPTANCE_SHA256=' + self.proof['files']['native-acceptance.json']['sha256'] + '\n'
        self.selection = dict(self.args[0], target_version='v2.102.0', release=upgrade.release_identity(self.release))


class Catalog:
    repo = upgrade.REPOSITORY

    def __init__(self, *candidates):
        self.candidates = candidates
        catalog_sha = digest([c.selection['release'] for c in candidates])
        for c in candidates:
            c.selection['catalogue_sha256'] = catalog_sha
        self.requests, self.downloads = [], []
        self.release_reads, self.run_reads = {}, {}
        self.error_endpoint = None
        self.change_snapshot = None
        self.snapshot_read = 2

    def run(self, *args):
        endpoint = args[-1]
        self.requests.append(endpoint)
        if self.error_endpoint and self.error_endpoint in endpoint:
            raise ValueError('Synthetic API failure')
        for c in self.candidates:
            if endpoint == f'repos/{self.repo}/releases/{c.release["id"]}':
                count = self.release_reads.get(c.release['id'], 0) + 1
                self.release_reads[c.release['id']] = count
                value = copy.deepcopy(c.release)
                if self.change_snapshot == 'release' and count == self.snapshot_read:
                    value['prerelease'] = True
                return json.dumps(value)
            if endpoint == f'repos/{self.repo}/releases/{c.release["id"]}/assets?per_page=100&page=1':
                value = copy.deepcopy(c.release['assets'])
                if self.change_snapshot == 'asset' and self.release_reads[c.release['id']] == self.snapshot_read:
                    value[0]['digest'] = 'sha256:' + 'b' * 64
                return json.dumps(value)
            if endpoint == f'repos/{self.repo}/actions/runs/{c.run["id"]}':
                count = self.run_reads.get(c.run['id'], 0) + 1
                self.run_reads[c.run['id']] = count
                value = copy.deepcopy(c.run)
                if self.change_snapshot == 'run' and count == self.snapshot_read:
                    value['run_attempt'] += 1
                return json.dumps(value)
            if endpoint == f'repos/{self.repo}/actions/runs/{c.run["id"]}/jobs?filter=all&per_page=100&page=1':
                return json.dumps({'total_count': 1, 'jobs': [c.job]})
            if endpoint == f'repos/{self.repo}/actions/jobs/{c.job["id"]}/logs':
                return c.log
        raise AssertionError('Unexpected endpoint: ' + endpoint)

    def download_asset(self, asset, destination):
        self.downloads.append((asset['id'], Path(destination)))
        for c in self.candidates:
            if any(a['id'] == asset['id'] for a in c.release['assets']):
                # Only small controls are available; package access is an error.
                Path(destination).write_bytes(c.bytes[asset['name']])
                return
        raise AssertionError('Unknown download')

    def call(self, directory, source='official', arch='amd64'):
        return selection.select_public_product(self, [c.selection for c in self.candidates], source, arch, directory)


class ProductSelectionTests(unittest.TestCase):
    def pair(self, absent=True, modern=False, source='official', arch='amd64'):
        return Catalog(Candidate('v2.100.0', 1, absent, modern, source, arch),
                       Candidate('v2.99.0', 2, source=source, arch=arch))

    def test_nearest_present_product_stops_without_reading_older_release(self):
        with tempfile.TemporaryDirectory() as td:
            catalog = self.pair(absent=False)
            result = catalog.call(td)
            self.assertEqual(result[0]['release']['tag_name'], 'v2.100.0')
            self.assertEqual(result[5]['kind'], 'current-run-public-predecessor-bootstrap')
            self.assertEqual(result[5]['product_absence_trail'], [])
            self.assertNotIn(catalog.candidates[1].release['id'], catalog.release_reads)
            self.assertEqual(len(catalog.downloads), 2)

    def test_authenticated_absence_advances_with_small_complete_evidence(self):
        for modern in (False, True):
            for source, arch in (('official', 'amd64'), ('custom', 'arm64')):
                with self.subTest(modern=modern, source=source, arch=arch), tempfile.TemporaryDirectory() as td:
                    catalog = self.pair(modern=modern, source=source, arch=arch)
                    result = catalog.call(td, source, arch)
                    nearest, older = catalog.candidates
                    self.assertEqual(result[0]['release']['tag_name'], 'v2.99.0')
                    trail = result[5]['product_absence_trail']
                    self.assertEqual(len(trail), 1)
                    self.assertEqual(trail[0]['release'], nearest.selection['release'])
                    self.assertEqual(trail[0]['receipt_sha256'], facts(nearest.bytes[upgrade.RECEIPT])['sha256'])
                    self.assertEqual(trail[0]['receipt_validator']['job_id'], nearest.job['id'])
                    self.assertEqual(trail[0]['run_snapshot_sha256'], digest(upgrade.run_snapshot(nearest.run)))
                    if modern:
                        self.assertEqual(trail[0]['public_native_subject_sha256'],
                                         facts(nearest.bytes['native-acceptance.json'])['sha256'])
                    self.assertEqual(catalog.release_reads, {nearest.release['id']: 2, older.release['id']: 2})
                    self.assertEqual(catalog.run_reads, {nearest.run['id']: 2, older.run['id']: 2})
                    self.assertEqual(len({path.parent for _, path in catalog.downloads}), 2)
                    self.assertTrue(all(path.name in (upgrade.RECEIPT, 'checksums.txt', 'native-acceptance.json')
                                        for _, path in catalog.downloads))

    def test_present_modern_product_keeps_native_subject_binding(self):
        with tempfile.TemporaryDirectory() as td:
            catalog = self.pair(absent=False, modern=True)
            result = catalog.call(td)
            self.assertEqual(result[5]['public_native_subject_sha256'],
                             catalog.candidates[0].proof['files']['native-acceptance.json']['sha256'])

    def test_broken_nearer_controls_never_skip_to_older_product(self):
        for fault in ('api', 'missing-control', 'oversized-control', 'receipt-tamper', 'checksum-tamper',
                      'cancelled', 'wrong-head', 'missing-log', 'failed-job', 'native-tamper', 'native-missing'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                catalog = self.pair(modern=fault.startswith('native-'))
                near = catalog.candidates[0]
                if fault == 'api': catalog.error_endpoint = '/assets?'
                if fault == 'missing-control': near.release['assets'] = [a for a in near.release['assets'] if a['name'] != upgrade.RECEIPT]
                if fault == 'oversized-control': next(a for a in near.release['assets'] if a['name'] == upgrade.RECEIPT)['size'] = upgrade.CONTROL_LIMIT + 1
                if fault == 'receipt-tamper': near.bytes[upgrade.RECEIPT] += b'\n'
                if fault == 'checksum-tamper': near.bytes['checksums.txt'] += b'tampering'
                if fault == 'cancelled': near.run['conclusion'] = 'cancelled'
                if fault == 'wrong-head': near.run['head_sha'] = 'c' * 40
                if fault == 'missing-log': near.log = ''
                if fault == 'failed-job': near.job['conclusion'] = 'failure'
                if fault == 'native-tamper': near.bytes['native-acceptance.json'] += b'tampering'
                if fault == 'native-missing': near.release['assets'] = [a for a in near.release['assets'] if a['name'] != 'native-acceptance.json']
                with self.assertRaises(ValueError): catalog.call(td)
                self.assertNotIn(catalog.candidates[1].release['id'], catalog.release_reads)

    def test_expected_but_missing_package_is_not_absence(self):
        with tempfile.TemporaryDirectory() as td:
            catalog = self.pair(absent=False)
            near = catalog.candidates[0]
            near.release['assets'] = [a for a in near.release['assets'] if a['name'] != near.name]
            with self.assertRaisesRegex(ValueError, 'Incomplete public release'):
                catalog.call(td)
            self.assertNotIn(catalog.candidates[1].release['id'], catalog.release_reads)

    def test_unlisted_canonical_package_is_not_authenticated_absence(self):
        with tempfile.TemporaryDirectory() as td:
            catalog = self.pair()
            near = catalog.candidates[0]
            near.release['assets'].append(dict(id=777000, name=near.name, state='uploaded', size=1,
                digest='sha256:' + 'a' * 64, url=f'{upgrade.API_ROOT}/releases/assets/777000'))
            with self.assertRaisesRegex(ValueError, 'Unexpected/staged'):
                catalog.call(td)
            self.assertNotIn(catalog.candidates[1].release['id'], catalog.release_reads)

    def test_changed_snapshot_blocks_skip_and_selected_return(self):
        for absent in (False, True):
            for changed in ('release', 'asset', 'run'):
                with self.subTest(absent=absent, changed=changed), tempfile.TemporaryDirectory() as td:
                    catalog = self.pair(absent=absent)
                    catalog.change_snapshot = changed
                    with self.assertRaisesRegex(ValueError, 'changed'):
                        catalog.call(td)
                    self.assertNotIn(catalog.candidates[1].release['id'], catalog.release_reads)

    def test_all_absent_fails_and_never_becomes_initial_applicability(self):
        with tempfile.TemporaryDirectory() as td:
            catalog = Catalog(Candidate('v2.100.0', 1, absent=True), Candidate('v2.99.0', 2, absent=True))
            with self.assertRaisesRegex(ValueError, 'No authenticated public predecessor contains'):
                catalog.call(td)
            self.assertEqual(len(catalog.downloads), 4)
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, 'requires predecessor candidates'):
                Catalog().call(td)

    def test_candidate_order_and_context_must_be_complete_consistent_and_strict(self):
        for fault in ('order', 'duplicate', 'mixed-context', 'wrong-channel', 'equal-target'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                catalog = self.pair()
                if fault == 'order': catalog.candidates = tuple(reversed(catalog.candidates))
                if fault == 'duplicate': catalog.candidates = (catalog.candidates[0], catalog.candidates[0])
                if fault == 'mixed-context': catalog.candidates[1].selection['catalogue_sha256'] = 'b' * 64
                if fault == 'wrong-channel': catalog.candidates[0].selection['mode'] = 'beta'
                if fault == 'equal-target': catalog.candidates[0].selection['target_version'] = 'v2.100.0'
                with self.assertRaises(ValueError): catalog.call(td)
                self.assertEqual(catalog.requests, [])

    def test_absence_trail_limit_fails_closed(self):
        with tempfile.TemporaryDirectory() as td, patch.object(selection, 'MAX_ABSENCES', 0):
            catalog = self.pair()
            with self.assertRaisesRegex(ValueError, 'bounded limit'):
                catalog.call(td)
            self.assertNotIn(catalog.candidates[1].release['id'], catalog.release_reads)

    def test_absence_recheck_accepts_stable_skipped_release_and_run(self):
        for absent in (False, True):
            for modern in (False, True):
                with self.subTest(absent=absent, modern=modern), tempfile.TemporaryDirectory() as td:
                    catalog = self.pair(absent=absent, modern=modern)
                    binding = catalog.call(td)[5]
                    before = len(catalog.requests)
                    selection.recheck_absences(catalog, binding)
                    self.assertEqual(len(catalog.requests) - before, 3 if absent else 0)
                    if absent:
                        near = catalog.candidates[0]
                        self.assertEqual(catalog.release_reads[near.release['id']], 3)
                        self.assertEqual(catalog.run_reads[near.run['id']], 3)

    def test_absence_recheck_rejects_changes_after_selected_source_acquisition(self):
        for change in ('release', 'asset', 'run', 'new-product', 'api'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as td:
                catalog = self.pair()
                binding = catalog.call(td)[5]
                catalog.snapshot_read = 3
                if change in ('release', 'asset', 'run'):
                    catalog.change_snapshot = change
                elif change == 'api':
                    catalog.error_endpoint = '/assets?'
                else:
                    near = catalog.candidates[0]
                    near.release['assets'].append(dict(id=799990, name=near.name, state='uploaded', size=1,
                        digest='sha256:' + 'a' * 64, url=f'{upgrade.API_ROOT}/releases/assets/799990'))
                with self.assertRaises(ValueError):
                    selection.recheck_absences(catalog, binding)

    def test_absence_recheck_rejects_malformed_duplicate_and_unbounded_trails(self):
        for fault in ('missing', 'not-list', 'malformed-entry', 'missing-hash', 'wrong-hash', 'duplicate',
                      'product', 'run', 'validator', 'extra-field', 'selected-release', 'too-many'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                catalog = self.pair()
                binding = catalog.call(td)[5]
                trail = binding['product_absence_trail']
                if fault == 'missing': del binding['product_absence_trail']
                if fault == 'not-list': binding['product_absence_trail'] = {}
                if fault == 'malformed-entry': trail[0] = None
                if fault == 'missing-hash': del trail[0]['run_snapshot_sha256']
                if fault == 'wrong-hash': trail[0]['run_snapshot_sha256'] = 'invalid'
                if fault == 'duplicate': trail.append(copy.deepcopy(trail[0]))
                if fault == 'product': trail[0]['source'] = 'custom'
                if fault == 'run': trail[0]['receipt_run']['run_id'] = True
                if fault == 'validator': trail[0]['receipt_validator']['job_name'] = 'unrelated'
                if fault == 'extra-field': trail[0]['unreviewed'] = True
                if fault == 'selected-release': binding['release_id'] += 1
                if fault == 'too-many': binding['product_absence_trail'] = trail * (selection.MAX_ABSENCES + 1)
                requests = len(catalog.requests)
                with self.assertRaises(ValueError):
                    selection.recheck_absences(catalog, binding)
                self.assertEqual(len(catalog.requests), requests)


if __name__ == '__main__':
    unittest.main()
