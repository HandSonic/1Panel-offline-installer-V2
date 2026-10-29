"""Synthetic package-only releases; no network, application or native execution."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import offline_public_source as public
import native_upgrade_input as upgrade
import public_predecessor_selection as selection
import runtime_publication as publication
from resolved_inventory import canonical, digest
from test_native_upgrade import tar_bytes
from installer_source_fixtures import source_archive, source_contract, current_installer, mock_sources


def facts(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


class Release:
    def __init__(self, version='v2.100.0', number=10, rows=(('official', 'amd64'),), marker=True):
        self.version, self.id, self.rows = version, number, rows
        self.bodies, self.assets = {}, []
        self.manifests = {}
        for source, arch in rows:
            name = f'1panel-{version}-{source}-offline-linux-{arch}.tar.gz'
            inner = f'1panel-{version}-linux-{arch}' if source.startswith('enterprise-') else name[:-7]
            manifest = {'schema': 1, 'app_version': version, 'source': source, 'architecture': arch}
            app = source_archive(version, arch)
            vendor = 'enterprise' if source.startswith('enterprise-') else source
            manifest['inputs'] = {'app': dict(facts(app), version=version, url='https://fixture.invalid/' + vendor + '/' + arch)}
            if source == 'custom':
                manifest['upstream_provenance'] = {'resolved_contract_sha256': 'e' * 64}
            for component in ('docker', 'compose'):
                manifest['inputs'][component] = dict(facts((component + arch).encode()),
                    version='fixture', url='https://fixture.invalid/' + component + '/' + arch)
            if marker:
                manifest['publication'] = {'format': public.FORMAT, 'repository': public.REPOSITORY,
                    'release_tag': version, 'policy_fingerprint': 'a' * 64}
            payloads = {'install.sh': current_installer(),
                        'docker.service': (ROOT / 'docker.service').read_bytes()}
            if source in ('official', 'custom'):
                payloads['upgrade.sh'] = (ROOT / 'upgrade_offline.sh').read_bytes()
            manifest['payloads'] = {name: facts(raw) for name, raw in payloads.items()}
            raw = (app if source == 'enterprise-original' else
                   tar_bytes(dict(payloads, **{'offline-manifest.json': canonical(manifest)}), inner))
            self.manifests[name] = manifest
            self.add(name, raw)
        self.add_sums()

    def add(self, name, raw):
        self.bodies[name] = raw
        self.assets = [a for a in self.assets if a['name'] != name]
        identifier = self.id * 1000 + max([a['id'] % 1000 for a in self.assets] + [0]) + 1
        self.assets.append(dict(id=identifier, name=name, state='uploaded', size=len(raw),
            digest='sha256:' + facts(raw)['sha256'],
            url=f'https://api.github.com/repos/{public.REPOSITORY}/releases/assets/{identifier}'))

    def add_sums(self):
        for name, raw in list(sorted(self.bodies.items())):
            if name.endswith('.tar.gz'):
                self.add(name + '.sha256', (facts(raw)['sha256'] + '  ' + name + '\n').encode())

    def value(self):
        return copy.deepcopy(dict(id=self.id, tag_name=self.version, draft=False, prerelease=False,
            url=f'https://api.github.com/repos/{public.REPOSITORY}/releases/{self.id}', assets=self.assets))

    def plan(self):
        matrix = {}
        for source, arch in self.rows:
            matrix.setdefault(source, []).append(arch)
        inventory = {'matrix': matrix, 'official': {'archives': {}}, 'enterprise': {'archives': {}},
                     'docker': {}, 'compose': {}}
        records = {}
        for source, arch in self.rows:
            manifest = self.manifests[f'1panel-{self.version}-{source}-offline-linux-{arch}.tar.gz']
            inputs = copy.deepcopy(manifest['inputs'])
            for component in ('docker', 'compose'):
                inventory[component][arch] = inputs[component]
            if source == 'custom':
                records[arch] = {'size': inputs['app']['bytes'], 'sha256': inputs['app']['sha256']}
            else:
                inventory['official' if source == 'official' else 'enterprise']['archives'][arch] = inputs['app']
        return {'repository': public.REPOSITORY, 'version': self.version, 'tag': self.version, 'mode': 'stable',
                'resolved': {'inventory': inventory, 'source_contract': source_contract(), 'source_contract_sha256': 'e' * 64,
                             'configuration_sources': {}, 'upstream': {'records': records}}}


class Client:
    repo = public.REPOSITORY

    def __init__(self, *releases):
        self.releases = releases
        self.tag = releases[0].version
        self.requests, self.downloads = [], []
        self.tamper = None

    def release(self):
        return self.releases[0].value()

    def run(self, *args):
        endpoint = args[-1]; self.requests.append(endpoint)
        for release in self.releases:
            if endpoint == f'repos/{self.repo}/releases/{release.id}':
                return json.dumps(release.value())
            if endpoint == f'repos/{self.repo}/releases/{release.id}/assets?per_page=100&page=1':
                return json.dumps(release.assets)
        raise AssertionError('Unexpected request, including any old CI lookup: ' + endpoint)

    def download_asset(self, asset, path):
        self.downloads.append(asset['name'])
        for release in self.releases:
            if any(a['id'] == asset['id'] for a in release.assets):
                raw = release.bodies[asset['name']]
                Path(path).write_bytes(raw + b'tamper' if self.tamper == asset['name'] else raw)
                return
        raise AssertionError('Unknown asset')

    def select(self, work, source='official', arch='amd64'):
        candidates = upgrade.predecessor_candidates('v2.101.0', 'stable',
            [r.value() for r in self.releases], catalogue_complete=True)
        return selection.select_public_product(self, candidates, source, arch, work)


class PublicPackagesTests(unittest.TestCase):
    def setUp(self):
        self.source_download, self.source_read = mock_sources(self)

    def test_noop_requires_complete_inventory_current_inputs_and_exact_bytes_without_ci(self):
        release = Release(rows=(('official', 'amd64'), ('custom', 'arm64'), ('enterprise-original', 'amd64')))
        client = Client(release)
        with patch('runtime_contract.policy', return_value='a' * 64):
            self.assertTrue(publication.public_noop(client, release.plan()))
            plan = release.plan(); plan['resolved']['inventory']['matrix']['official'].append('arm64')
            self.assertFalse(publication.public_noop(client, plan))
        plan = release.plan(); plan['resolved']['inventory']['docker']['amd64']['sha256'] = 'b' * 64
        with self.assertRaisesRegex(ValueError, 'dependency source changed'):
            publication.public_noop(client, plan)
        client.tamper = next(name for name in release.bodies if '-official-' in name)
        with patch('runtime_contract.policy', return_value='a' * 64), self.assertRaises(ValueError):
            publication.public_noop(client, release.plan())
        self.assertFalse(any('/actions/' in call for call in client.requests))

    def test_publisher_policy_change_preserves_current_published_inputs(self):
        release = Release(rows=(('official', 'amd64'), ('custom', 'arm64')))
        client = Client(release)
        # A shared plan fingerprint changes when the custom origin recovers.
        # Existing individually current products must still pass the scheduled check.
        with patch('runtime_contract.policy', return_value='b' * 64), \
                patch('runtime_contract.product_policy', return_value='b' * 64):
            self.assertTrue(publication.public_noop(client, release.plan()))
            plan = release.plan()
            plan['resolved']['inventory']['matrix']['custom'].append('amd64')
            self.assertEqual(publication.release_status(client, plan), 'rebuild')

    def test_enterprise_original_only_noop_uses_current_vendor_hash_without_repacking(self):
        release = Release(rows=(('enterprise-original', 'amd64'),))
        original = copy.deepcopy(release.bodies)
        with patch('runtime_contract.policy', return_value='a' * 64):
            self.assertTrue(publication.public_noop(Client(release), release.plan()))
            plan = release.plan(); plan['resolved']['inventory']['enterprise']['archives']['amd64']['sha256'] = 'b' * 64
            with self.assertRaisesRegex(ValueError, 'vendor pin'):
                publication.public_noop(Client(release), plan)
        self.assertEqual(release.bodies, original)

    def test_selected_package_marker_and_checksum_are_mandatory(self):
        for fault in ('marker', 'checksum', 'digest', 'canonical-control', 'partial-control', 'staged'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                release = Release(marker=fault != 'marker'); client = Client(release)
                if fault == 'checksum': release.add(next(name for name in release.bodies if name.endswith('.sha256')), b'0' * 64 + b'  missing.tar.gz\n')
                if fault == 'digest': release.assets[0]['digest'] = 'sha256:' + 'b' * 64
                if fault == 'canonical-control': release.add(public.RECEIPT, b'{"workflow_run_id":1}')
                if fault == 'partial-control': release.add('native-acceptance.json', b'{}')
                if fault == 'staged': release.add('checksums.txt.staged-123456789abc', b'in progress')
                with self.assertRaises((ValueError, AssertionError)):
                    client.select(Path(td))

    def test_selected_product_survives_missing_sibling_but_missing_selected_never_skips(self):
        release = Release(rows=(('official', 'amd64'), ('custom', 'arm64')))
        missing = next(a for a in release.assets if '-custom-' in a['name'])
        release.assets.remove(missing)
        with tempfile.TemporaryDirectory() as td:
            result = Client(release).select(Path(td))
            self.assertEqual(result[5]['publication_format'], public.FORMAT)
            self.assertIsNone(result[4]); self.assertIsNone(result[5]['receipt_run'])
        with tempfile.TemporaryDirectory() as td, self.assertRaisesRegex(ValueError, 'presence differs'):
            Client(release).select(Path(td), source='custom', arch='arm64')

    def test_selected_product_ignores_malformed_sibling_bytes_and_metadata(self):
        for fault in ('digest', 'size', 'sidecar', 'orphan-sidecar'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                release = Release(rows=(('official', 'amd64'), ('custom', 'arm64')))
                sibling = next(a for a in release.assets if '-custom-' in a['name'] and a['name'].endswith('.tar.gz'))
                if fault == 'digest': sibling['digest'] = None
                if fault == 'size': sibling['size'] = -1
                if fault == 'sidecar': release.add(sibling['name'] + '.sha256', b'broken sibling checksum')
                if fault == 'orphan-sidecar': release.assets.remove(sibling)
                result = Client(release).select(Path(td))
                self.assertIn('-official-', result[5]['archive']['name'])

    def test_absence_can_use_an_intact_marker_after_a_damaged_sibling(self):
        for fault in ('digest', 'size', 'sidecar', 'archive'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                near = Release(rows=(('custom', 'amd64'), ('custom', 'arm64')))
                old = Release('v2.99.0', number=9)
                damaged = next(a for a in near.assets if a['name'].endswith('-amd64.tar.gz'))
                if fault == 'digest': damaged['digest'] = None
                if fault == 'size': damaged['size'] = -1
                if fault == 'sidecar': near.add(damaged['name'] + '.sha256', b'broken sibling checksum')
                if fault == 'archive':
                    near.add(damaged['name'], b'not an archive'); near.add_sums()
                self.assertEqual(Client(near, old).select(Path(td))[5]['version'], old.version)

    def test_absence_trail_uses_stable_api_inventory_and_is_rechecked(self):
        near = Release(rows=(('custom', 'arm64'),))
        old = Release('v2.99.0', number=9)
        client = Client(near, old)
        with tempfile.TemporaryDirectory() as td:
            result = client.select(Path(td)); binding = result[5]
            self.assertEqual(binding['version'], old.version)
            self.assertEqual(len(binding['product_absence_trail']), 1)
            selection.recheck_absences(client, binding)
            near.add('checksums.txt.backup-123456789abc', b'changed snapshot')
            with self.assertRaisesRegex(ValueError, 'changed'):
                selection.recheck_absences(client, binding)
        self.assertFalse(any('/actions/' in call for call in client.requests))

    def test_old_controls_are_only_ignored_under_recoverable_backup_names(self):
        release = Release()
        for name in public.CONTROLS:
            release.add(name + '.backup-123456789abc', b'old control')
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(Client(release).select(Path(td))[5]['publication_format'], public.FORMAT)

    def test_corrupt_public_archive_requests_rebuild_and_orphan_inventory_is_not_absence(self):
        release = Release(); name = next(n for n in release.bodies if n.endswith('.tar.gz'))
        release.add(name, b'not gzip'); release.add_sums()
        with patch('runtime_contract.policy', return_value='a' * 64):
            self.assertEqual(publication.release_status(Client(release), release.plan()), 'rebuild')
        release.assets = [a for a in release.assets if a['name'].endswith('.sha256')]
        with tempfile.TemporaryDirectory() as td, self.assertRaises(ValueError):
            Client(release).select(Path(td), source='custom')

    def test_marker_binds_authenticated_plan_and_policy_includes_adapter(self):
        import runtime_contract
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'plan.json'
            plan = {'repository': public.REPOSITORY, 'version': 'v2.100.0', 'tag': 'v2.100.0'}
            path.write_bytes(canonical(plan))
            payloads = {'install.sh': facts(b'generated installer')}
            with patch('runtime_contract.selected', return_value={'synthetic': True}), \
                 patch('runtime_contract.product_policy', return_value='a' * 64) as policy, \
                 patch.dict('os.environ', {runtime_contract.PLAN_PATH: str(path)}):
                self.assertEqual(public.publication_metadata(plan['version'], ROOT, 'official', 'amd64', payloads)['publication']['policy_fingerprint'], 'a' * 64)
                policy.assert_called_once_with({'synthetic': True}, 'official', 'amd64', payloads)
                plan['repository'] = 'other/repo'; path.write_bytes(canonical(plan))
                with self.assertRaises(ValueError): public.publication_metadata(plan['version'], ROOT, 'official', 'amd64', payloads)
        self.assertIn("'scripts/offline_public_source.py'", (ROOT / 'scripts/runtime_contract.py').read_text())

    def test_new_source_and_dependency_pins_do_not_read_old_actions_or_current_locks(self):
        from test_public_predecessor_source import PublicFixture, RetainedUpper
        for official in (False, True):
            with self.subTest(official=official), tempfile.TemporaryDirectory() as td:
                fixture = PublicFixture(Path(td), official=official, public=True)
                fixture.manifest['publication'] = {'format': public.FORMAT, 'repository': public.REPOSITORY,
                    'release_tag': fixture.version, 'policy_fingerprint': 'a' * 64}
                for component in ('docker', 'compose'):
                    fixture.manifest['inputs'][component] = json.loads((ROOT / (component + '-sources.json')).read_text())[fixture.arch]
                fixture.save()
                fixture.binding.update(publication_format=public.FORMAT, repository=public.REPOSITORY,
                    manifest_content_sha256=digest(fixture.manifest))
                upper = RetainedUpper(fixture, backups=True)
                def download(pin, path):
                    self.assertEqual({key: pin[key] for key in ('bytes', 'sha256')}, facts(fixture.raw.read_bytes()))
                    path.write_bytes(fixture.raw.read_bytes())
                with patch('resolved_transport.ControlGitHub', return_value=upper), \
                     patch('native_upgrade_input.PredecessorGitHub', return_value=upper), \
                     patch('native_upgrade_input.download_url', side_effect=download), \
                     patch('public_predecessor_dependencies.original_lock', side_effect=AssertionError('No historical lock lookup')), \
                     patch('native_upgrade_input.verify_receipt_log', side_effect=AssertionError('No old Actions logs')):
                    body, source, dependencies, proof = public.predecessor_inputs(fixture.archive,
                        fixture.binding, fixture.source, fixture.arch, fixture.work)
                self.assertEqual(body, fixture.raw_body)
                self.assertEqual(source['pin'], facts(fixture.raw.read_bytes()))
                self.assertEqual(proof['kind'], 'trusted-public-package-dependencies')
                self.assertEqual(dependencies['docker'][fixture.arch], fixture.manifest['inputs']['docker'])

    def test_control_migration_requires_confirmation_then_fills_missing_names(self):
        from test_release_repair import FakeGitHub
        from release_asset_repair import repair
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); client = FakeGitHub(); client.repo = public.REPOSITORY
            for number, name in enumerate(public.CONTROLS[:2], 3): client.assets[number] = (name, ('old ' + name).encode())
            before = copy.deepcopy(client.assets)
            files = []
            for name in ('package.tar.gz', 'package.tar.gz.sha256'):
                path = root / name; path.write_bytes(('new ' + name).encode()); files.append(path)
            with self.assertRaisesRegex(ValueError, 'deletion confirmation'):
                repair(client, files, root / 'journal.json', update_notes=False, retire_controls=True)
            state = json.loads((root / 'journal.json').read_text())
            self.assertEqual(state['phase'], 'confirmation_required')
            self.assertEqual({row['asset_id'] for row in state['conflicts']}, set(before))
            self.assertEqual(client.assets, before); self.assertEqual(client.calls, [])
            for row in state['conflicts']: del client.assets[row['asset_id']]  # Separately confirmed external deletion.
            repair(client, files, root / 'approved-rerun.json', update_notes=False, retire_controls=True)
            self.assertEqual({name for name, raw in client.assets.values()}, {p.name for p in files})
            self.assertEqual([call for call in client.calls if call[0] == 'upload'][-1][1], 'package.tar.gz.sha256')
            self.assertEqual(client.body, 'old release notes')

    def test_enterprise_only_absence_is_bound_to_known_vendor_bytes(self):
        near = Release(rows=(('enterprise-original', 'amd64'),))
        old = Release('v2.99.0', number=9)
        client = Client(near, old)
        vendor = near.plan()['resolved']['inventory']['enterprise']
        with tempfile.TemporaryDirectory() as td, patch('resolved_transport.discover_vendor', return_value=vendor):
            self.assertEqual(client.select(Path(td))[5]['version'], old.version)
        vendor['archives']['amd64']['sha256'] = 'b' * 64
        with tempfile.TemporaryDirectory() as td, patch('resolved_transport.discover_vendor', return_value=vendor), \
             self.assertRaisesRegex(ValueError, 'vendor bytes'):
            client.select(Path(td))

    def test_materialize_routes_new_source_pins_and_still_requires_unpack_and_native_target(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); release = Release(); client = Client(release)
            row = next(name for name in release.bodies if name.endswith('.tar.gz'))
            binding = {'publication_format': public.FORMAT, 'archive': {'name': row}}
            args = SimpleNamespace(version='v2.101.0', tag='', source='official', arch='amd64',
                output=str(root / 'output'), provenance=str(root / 'evidence.json'),
                target_input=str(root / 'target-input'), target_provenance=str(root / 'target.json'))
            body, source, dependencies, dep_proof = {'original': b'bytes'}, {'pin': {}}, {'own': 'pins'}, {'kind': 'embedded'}
            def unpack(archive, destination, received, source_kind, arch, original, proof, code_root, *, dependency_pins):
                self.assertEqual(archive.read_bytes(), release.bodies[row])
                self.assertEqual(original, body); self.assertEqual(proof, source)
                self.assertEqual(dependency_pins, dependencies)
                raise ValueError('synthetic stop at mandatory predecessor unpack')
            with patch.object(upgrade, 'current_identity', return_value={}), \
                 patch.object(upgrade, 'predecessor_context', return_value={}), \
                 patch.object(upgrade, 'array_pages', return_value=[]), \
                 patch.object(upgrade, 'predecessor_candidates', return_value=[{}]), \
                 patch.object(selection, 'select_public_product', return_value=(
                     {}, release.value(), None, None, None, binding, {a['name']: a for a in release.assets})), \
                 patch.object(public, 'predecessor_inputs', return_value=(body, source, dependencies, dep_proof)), \
                 patch('public_predecessor_dependencies.historical_dependency_pins', side_effect=AssertionError('No old dependencies')), \
                 patch('public_predecessor_source.source_from_public_predecessor', side_effect=AssertionError('No old source logs')), \
                 patch.object(upgrade, 'unpack_predecessor', side_effect=unpack):
                with self.assertRaisesRegex(ValueError, 'mandatory predecessor unpack'):
                    upgrade.materialize(args, {'RUNNER_TEMP': td}, client)
            self.assertFalse((root / 'output').exists())
            self.assertFalse((root / 'evidence.json').exists())

    def test_public_files_exclude_internal_admission_controls(self):
        with tempfile.TemporaryDirectory() as td:
            final = Path(td) / 'final'; final.mkdir()
            files = {'a.tar.gz': facts(b'archive'), 'native-acceptance.json': {}, 'checksums.txt': {}}
            (final / 'a.tar.gz').write_bytes(b'archive')
            paths = publication.publication_files(final, {'files': files})
            self.assertEqual([p.name for p in paths], ['a.tar.gz', 'a.tar.gz.sha256'])
            self.assertEqual(paths[1].read_text(), facts(b'archive')['sha256'] + '  a.tar.gz\n')
            self.assertEqual([p.name for p in final.iterdir()], ['a.tar.gz'])
            self.assertEqual(publication.publication_files(final, {'files': files}), paths)


if __name__ == '__main__':
    unittest.main()
