"""Synthetic legacy protocol coverage. No real evidence, network, or native execution."""
import copy
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import legacy_predecessor_source as legacy
import native_upgrade_input as upgrade
from resolved_inventory import ARCHES, INSTALLER_REQUIRED, canonical, vendor_base
from semantic_configuration import production
from validate_resolved_custom import byte_facts


def archive_bytes(files, root):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name, raw in files.items():
            item = tarfile.TarInfo(root + '/' + name)
            item.size, item.mode = len(raw), 0o755
            archive.addfile(item, io.BytesIO(raw))
    return stream.getvalue()


class Fixture:
    def __init__(self, work, version='v2.97.43', mode='stable'):
        self.work, self.version, self.mode = work, version, mode
        self.repo, self.tag = legacy.UPSTREAM, version
        self.source, self.installer, self.producer, self.workflow = ('a' * 40, 'b' * 40, 'c' * 40, 'd' * 40)
        self.config = {part: ('base:\n  mode: development\n' +
                            ('  version: development\n' if part == 'core' else '') +
                            '  is_demo: true\n  is_offline: true\n  optional: preserve\nlog:\n  level: debug\n').encode()
                       for part in ('core', 'agent')}
        self.original = {name: ('official resource ' + name + '\n').encode() for name in INSTALLER_REQUIRED}
        self.original['1pctl'] = b'#!/bin/sh\nORIGINAL_VERSION=version\nPANEL_EDITION=cn\n'
        self.files = dict(self.original)
        self.files['1pctl'] = self.original['1pctl'].replace(b'=version', b'=' + version.encode())
        self.files['GeoIP.mmdb'] = b'synthetic authenticated geography'
        self.files['LICENSE'] = b'synthetic official license'
        header = b'\x7fELF\x02\x01' + b'\0' * 12 + b'\x3e\x00' + b'\0' * 44
        for part in ('core', 'agent'):
            self.files['1panel-' + part] = header + production(self.config[part], version, part, mode)
            self.files['1panel-' + part + '.service'] = self.original['initscript/1panel-' + part + '.service']
        self.manifest = {'schema_version': 1, 'version': version, 'architecture': 'amd64',
                         'edition': 'community', 'mode': mode, 'source_commit': self.source,
                         'installer_commit': self.installer, 'build_repository_commit': self.producer,
                         'go_version': '1.99.4', 'node_version': '88.0.1', 'npm_version': '77.0.2'}
        self.release_value = {'id': 101, 'tag_name': version, 'draft': False,
                              'prerelease': mode != 'stable',
                              'url': f'https://api.github.com/repos/{self.repo}/releases/101'}
        self.run_value = {'id': 202, 'head_sha': self.workflow, 'run_attempt': 2,
                          'status': 'completed', 'conclusion': 'success',
                          'path': '.github/workflows/build.yml', 'event': 'workflow_dispatch',
                          'repository': {'full_name': self.repo}, 'head_repository': {'full_name': self.repo}}
        self.jobs = [{'id': 303, 'name': 'publication_revalidate', 'run_id': 202,
                      'head_sha': self.workflow, 'run_attempt': 2, 'status': 'completed', 'conclusion': 'success'}]
        self.calls, self.reads, self.downloads = [], [], []
        self.origin_raw = archive_bytes({'GeoIP.mmdb': self.files['GeoIP.mmdb'], 'unselected': b'complete origin'},
                                        f'1panel-{version}-linux-amd64')
        self.origin_pin = {'url': vendor_base(version, mode, 'official') + f'1panel-{version}-linux-amd64.tar.gz',
                           **byte_facts(self.origin_raw), 'version': version}
        self.repack()

    def repack(self):
        self.manifest['files'] = {name: {'size': len(raw), 'sha256': byte_facts(raw)['sha256']}
                                  for name, raw in self.files.items()}
        name = f'1panel-{self.version}-linux-amd64.tar.gz'
        self.raw = archive_bytes({**self.files, 'manifest.json': canonical(self.manifest)}, name[:-7])
        self.bodies, rows = {}, []
        for arch in ARCHES:
            filename = f'1panel-{self.version}-linux-{arch}.tar.gz'
            raw = self.raw if arch == 'amd64' else ('unselected archive ' + arch).encode()
            self.bodies[filename] = raw
            facts = byte_facts(raw)
            self.bodies[filename + '.sha256'] = (facts['sha256'] + '  ' + filename + '\n').encode()
            rows.append({'architecture': arch, 'file': filename, 'size': len(raw), 'sha256': facts['sha256'],
                         'source_commit': self.source, 'installer_commit': self.installer,
                         'build_repository_commit': self.producer})
        self.aggregate = {'schema_version': 1, 'version': self.version, 'artifacts': rows}
        self.bodies['build-manifest.json'] = canonical(self.aggregate)
        self.bodies['checksums.txt'] = b''.join(self.bodies[row['file'] + '.sha256'] for row in rows)
        self.bodies['build-inputs.env'] = (f'SOURCE_COMMIT={self.source}\nINSTALLER_REF={self.installer}\n').encode()
        self.refresh_proof()

    def refresh_proof(self):
        self.proof = {'schema': 1, 'contract': 'upstream7', 'version': self.version, 'release_tag': self.version,
                      'repository': self.repo, 'policy_fingerprint': 'e' * 64,
                      'workflow_run_id': 202, 'workflow_commit': self.workflow,
                      'files': {name: byte_facts(raw) for name, raw in self.bodies.items() if name != legacy.RECEIPT}}
        self.refresh_assets()

    def refresh_assets(self):
        self.bodies[legacy.RECEIPT] = canonical(self.proof)
        self.release_value['assets'] = [{'id': number, 'name': name, 'size': len(raw),
            'digest': 'sha256:' + byte_facts(raw)['sha256'], 'state': 'uploaded',
            'url': f'https://api.github.com/repos/{self.repo}/releases/assets/{number}'}
            for number, (name, raw) in enumerate(sorted(self.bodies.items()), 1001)]
        self.log = '2026-01-01T00:00:00.000Z VERIFIED_RELEASE_RECEIPT_SHA256=' + byte_facts(self.bodies[legacy.RECEIPT])['sha256'] + '\n'

    def release(self):
        return copy.deepcopy(self.release_value)

    def asset_bytes(self, asset):
        self.calls.append('asset:' + str(asset['id']))
        return self.bodies[asset['name']]

    def run(self, *args):
        endpoint = args[-1]
        self.calls.append(endpoint)
        if '/releases/101/assets?' in endpoint:
            return json.dumps(self.release_value['assets'])
        if endpoint.endswith('/releases/101'):
            return json.dumps(self.release_value)
        if '/jobs?' in endpoint:
            return json.dumps({'total_count': len(self.jobs), 'jobs': self.jobs})
        if endpoint.endswith('/jobs/303/logs'):
            return self.log
        if endpoint.endswith('/runs/202') or endpoint.endswith('/runs/202/attempts/2'):
            return json.dumps(self.run_value)
        raise AssertionError('Unexpected API read: ' + endpoint)

    def read(self, url, **kwargs):
        self.reads.append(url)
        source_base = f'https://raw.githubusercontent.com/{legacy.SOURCE}/{self.source}/'
        installer_base = f'https://raw.githubusercontent.com/{legacy.INSTALLER}/{self.installer}/'
        if url.startswith(source_base):
            name = url.removeprefix(source_base)
            body = {'LICENSE': b'synthetic official license',
                    **{part + '/config/config.yaml': raw for part, raw in self.config.items()}}.get(name)
        elif url.startswith(installer_base):
            body = self.original.get(url.removeprefix(installer_base))
        else:
            raise AssertionError('Unpinned or unexpected source read: ' + url)
        return {'status': 200 if body is not None else 404, 'body': body or b''}

    def download(self, asset, destination):
        self.downloads.append(asset['id'])
        destination.write_bytes(self.bodies[asset['name']])

    def discover(self, version, mode, source, **kwargs):
        assert (version, mode, source) == (self.version, self.mode, 'official')
        return {'archives': {'amd64': copy.deepcopy(self.origin_pin)}}

    def acquire(self, pin, cache):
        assert pin == self.origin_pin
        self.work.joinpath('origin.tar.gz').write_bytes(self.origin_raw)
        return self.work / 'origin.tar.gz'

    def execute(self):
        return legacy.materialize_legacy(self.version, self.mode, 'amd64', self.work, client=self,
            download=self.download, read=self.read, discover=self.discover, acquire=self.acquire)


class LegacyProtocolTests(unittest.TestCase):
    def test_unregistered_versions_and_channels_authenticate_without_registry_or_native_claim(self):
        for version, mode in [('v2.97.43', 'stable'), ('v2.87.3-beta.8', 'beta'), ('v2.66.99-dev.71', 'dev')]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(Path(td), version, mode)
                with patch('subprocess.run', side_effect=AssertionError('No network or native execution')):
                    body, result = fixture.execute()
                self.assertEqual(set(body), set(fixture.files) | {'manifest.json'})
                self.assertEqual(result['kind'], 'authenticated-legacy-custom-source')
                self.assertEqual(result['historical_native_acceptance'], 'not-claimed')
                self.assertIn('actual predecessor installation', result['required_acceptance'])
                self.assertFalse(result['resources']['producer_toolchain_independently_verified'])
                self.assertEqual(result['resources']['producer_toolchain_claims']['go_version'], '1.99.4')
                self.assertNotIn('toolchain', result['resources'])
                self.assertEqual(result['validator']['job_id'], 303)
                self.assertEqual(len(fixture.downloads), 1)
                self.assertTrue(all('/' + fixture.source + '/' in url or '/' + fixture.installer + '/' in url
                                    for url in fixture.reads))

    def test_receipt_identity_and_full_inventory_required_before_download(self):
        changes = [('schema', True), ('schema', 2), ('contract', 'upstream-matrix'), ('repository', 'evil/repo'),
                   ('version', 'v2.1.0'), ('release_tag', 'v2.97.43-staged'), ('policy_fingerprint', 'short'),
                   ('workflow_commit', 'main')]
        for key, value in changes:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(Path(td)); fixture.proof[key] = value; fixture.refresh_assets()
                with self.assertRaises(ValueError): fixture.execute()
                self.assertEqual(fixture.downloads, [])
        with tempfile.TemporaryDirectory() as td:
            fixture = Fixture(Path(td)); fixture.proof['files'].pop('build-inputs.env'); fixture.refresh_assets()
            with self.assertRaisesRegex(ValueError, 'inventory'): fixture.execute()

    def test_missing_digest_size_identity_duplicate_and_staged_assets_fail(self):
        for fault in ('digest', 'short-digest', 'size', 'id', 'url', 'state', 'duplicate-name', 'duplicate-id', 'staged'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(Path(td)); assets = fixture.release_value['assets']; asset = assets[0]
                if fault == 'digest': asset['digest'] = None
                elif fault == 'short-digest': asset['digest'] = 'sha256:abc'
                elif fault == 'size': asset['size'] += 1
                elif fault == 'id': asset['id'] = True
                elif fault == 'url': asset['url'] = 'https://example.invalid/asset'
                elif fault == 'state': asset['state'] = 'new'
                elif fault == 'duplicate-name': assets.append(copy.deepcopy(asset))
                elif fault == 'duplicate-id': assets[1]['id'] = asset['id']; assets[1]['url'] = asset['url']
                else:
                    extra = copy.deepcopy(asset); extra.update(id=7001, name=asset['name'] + '.staged-' + 'a' * 12,
                        url=f'https://api.github.com/repos/{fixture.repo}/releases/assets/7001'); assets.append(extra)
                with self.assertRaises(ValueError): fixture.execute()
                self.assertEqual(fixture.downloads, [])

    def test_unavailable_mismatched_or_ambiguous_validator_never_accepts_metadata(self):
        for fault in ('missing', 'wrong-receipt', 'duplicate-marker', 'run', 'head', 'attempt', 'status', 'conclusion'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(Path(td))
                if fault == 'missing': fixture.log = 'no receipt marker\n'
                elif fault == 'wrong-receipt': fixture.log = 'VERIFIED_RELEASE_RECEIPT_SHA256=' + 'f' * 64 + '\n'
                elif fault == 'duplicate-marker': fixture.log *= 2
                elif fault == 'run': fixture.jobs[0]['run_id'] = 999
                elif fault == 'head': fixture.jobs[0]['head_sha'] = 'e' * 40
                elif fault == 'attempt': fixture.jobs[0]['run_attempt'] = 3
                elif fault == 'status': fixture.jobs[0]['status'] = 'in_progress'
                else: fixture.jobs[0]['conclusion'] = 'failure'
                with self.assertRaises(ValueError): fixture.execute()
                self.assertEqual(fixture.downloads, [])

    def test_forged_success_job_cannot_borrow_a_failed_exact_run_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = Fixture(Path(td)); original = fixture.run
            def changed(*args):
                value = original(*args)
                if '/attempts/' in args[-1]:
                    return json.dumps(dict(fixture.run_value, conclusion='failure'))
                return value
            fixture.run = changed
            with self.assertRaisesRegex(ValueError, 'successful exact'): fixture.execute()

    def test_failed_foreign_incomplete_or_pull_request_runs_rejected(self):
        changes = [('conclusion', 'failure'), ('status', 'in_progress'), ('event', 'pull_request'),
                   ('head_sha', 'f' * 40), ('path', '.github/workflows/untrusted.yml'),
                   ('repository', {'full_name': 'evil/repo'}), ('head_repository', {'full_name': 'evil/repo'}),
                   ('run_attempt', True)]
        for key, value in changes:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(Path(td)); fixture.run_value[key] = value
                with self.assertRaises(ValueError): fixture.execute()

    def test_authenticated_aggregate_cannot_change_archives_or_mix_commits(self):
        for fault in ('missing', 'duplicate', 'source', 'size', 'file', 'schema'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(Path(td)); rows = fixture.aggregate['artifacts']
                if fault == 'missing': rows.pop()
                elif fault == 'duplicate': rows[0] = copy.deepcopy(rows[1])
                elif fault == 'source': rows[0]['source_commit'] = 'e' * 40
                elif fault == 'size': rows[0]['size'] += 1
                elif fault == 'file': rows[0]['file'] = '../escape.tar.gz'
                else: fixture.aggregate['schema_version'] = True
                fixture.bodies['build-manifest.json'] = canonical(fixture.aggregate); fixture.refresh_proof()
                with self.assertRaises(ValueError): fixture.execute()
                self.assertEqual(fixture.downloads, [])

    def test_selected_checksum_sidecar_must_bind_exact_archive_name_and_hash(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = Fixture(Path(td))
            name = f'1panel-{fixture.version}-linux-amd64.tar.gz.sha256'
            fixture.bodies[name] = ('f' * 64 + '  wrong-file.tar.gz\n').encode(); fixture.refresh_proof()
            with self.assertRaisesRegex(ValueError, 'selected archive checksum'): fixture.execute()
            self.assertEqual(fixture.downloads, [])

    def test_unchanged_pinned_archive_and_full_gzip_stream_required(self):
        for fault in ('byte', 'trailer', 'symlink'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(Path(td)); original = fixture.download
                def damaged(asset, path):
                    original(asset, path)
                    if fault == 'symlink':
                        original_path = path.with_name('real-source.tar.gz'); path.rename(original_path); path.symlink_to(original_path)
                    else:
                        path.write_bytes(path.read_bytes()[:-1] if fault == 'trailer' else b'changed')
                fixture.download = damaged
                with self.assertRaises(ValueError): fixture.execute()

    def test_self_consistent_but_wrong_official_resources_fail(self):
        for name in ('install.sh', 'lang/en.sh', '1pctl', '1panel-core.service', 'GeoIP.mmdb', 'LICENSE'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(Path(td)); fixture.files[name] += b'changed'; fixture.repack()
                with self.assertRaises(ValueError): fixture.execute()

    def test_binary_configuration_is_bound_to_immutable_source_not_archive_claims(self):
        for fault in ('development', 'wrong-version', 'wrong-elf', 'missing-setting', 'duplicate-key'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(Path(td)); raw = fixture.files['1panel-core']
                if fault == 'development': fixture.files['1panel-core'] = raw[:64] + fixture.config['core']
                elif fault == 'wrong-version': fixture.files['1panel-core'] = raw.replace(fixture.version.encode(), b'v2.88.1')
                elif fault == 'wrong-elf': fixture.files['1panel-core'] = b'NOT-ELF' + raw[7:]
                elif fault == 'missing-setting': fixture.config['core'] = b'base:\n  mode: development\n'
                else: fixture.config['core'] += b'base:\n  mode: duplicate\n'
                fixture.repack()
                with self.assertRaises(ValueError): fixture.execute()

    def test_unknown_missing_and_unmanifested_members_fail(self):
        for fault in ('unknown', 'missing', 'unmanifested'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(Path(td))
                if fault == 'unknown': fixture.files['extra/executable'] = b'not from official installer'
                elif fault == 'missing': fixture.files.pop('lang/en.sh')
                fixture.repack()
                if fault == 'unmanifested':
                    fixture.manifest['files'].pop('LICENSE')
                    name = f'1panel-{fixture.version}-linux-amd64.tar.gz'
                    fixture.bodies[name] = archive_bytes({**fixture.files, 'manifest.json': canonical(fixture.manifest)}, name[:-7])
                    facts = byte_facts(fixture.bodies[name]); row = fixture.aggregate['artifacts'][0]
                    row.update(size=facts['bytes'], sha256=facts['sha256'])
                    fixture.bodies[name + '.sha256'] = (facts['sha256'] + '  ' + name + '\n').encode()
                    fixture.bodies['checksums.txt'] = b''.join(fixture.bodies[r['file'] + '.sha256'] for r in fixture.aggregate['artifacts'])
                    fixture.bodies['build-manifest.json'] = canonical(fixture.aggregate); fixture.refresh_proof()
                with self.assertRaises(ValueError): fixture.execute()

    def test_no_invented_toolchain_fields_for_older_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = Fixture(Path(td))
            for key in ('go_version', 'node_version', 'npm_version'): fixture.manifest.pop(key)
            fixture.repack(); _, result = fixture.execute()
            self.assertEqual(result['resources']['producer_toolchain_claims'], {})
            self.assertFalse(result['resources']['producer_toolchain_independently_verified'])

    def test_geoip_origin_required_and_complete_origin_stream_authenticated(self):
        for fault in ('missing', 'wrong-version', 'oversized', 'truncated', 'member', 'changed'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(Path(td))
                if fault == 'missing': fixture.discover = lambda *args, **kwargs: {'archives': {}}
                elif fault == 'wrong-version': fixture.origin_pin['url'] = fixture.origin_pin['url'].replace(fixture.version, 'v2.1.0')
                elif fault == 'oversized': fixture.origin_pin['bytes'] = 512 * 1024 ** 2 + 1
                elif fault == 'truncated': fixture.origin_raw = fixture.origin_raw[:-4]
                elif fault == 'member':
                    fixture.origin_raw = archive_bytes({'GeoIP.mmdb': b'wrong'}, f'1panel-{fixture.version}-linux-amd64')
                    fixture.origin_pin.update(byte_facts(fixture.origin_raw))
                else:
                    original, calls = fixture.discover, []
                    def changed(*args, **kwargs):
                        calls.append(1); value = original(*args, **kwargs)
                        if len(calls) > 1: value['archives']['amd64']['sha256'] = 'f' * 64
                        return value
                    fixture.discover = changed
                with self.assertRaises(ValueError): fixture.execute()

    def test_post_validation_asset_id_or_run_change_blocks_return(self):
        for fault in ('asset-id', 'run'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(Path(td)); original = fixture.acquire
                def changed(*args):
                    path = original(*args)
                    if fault == 'asset-id':
                        asset = fixture.release_value['assets'][0]; asset['id'] = 9001
                        asset['url'] = f'https://api.github.com/repos/{fixture.repo}/releases/assets/9001'
                    else: fixture.run_value['run_attempt'] = 3
                    return path
                fixture.acquire = changed
                with self.assertRaisesRegex(ValueError, 'changed during authentication'): fixture.execute()

    def test_immutable_resource_missing_or_oversized_fails_without_local_fallback(self):
        for status, body in [(404, b''), (200, b'x' * (legacy.MAX_CONTROL + 1))]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(Path(td)); fixture.read = lambda *args, **kwargs: {'status': status, 'body': body}
                with self.assertRaisesRegex(ValueError, 'immutable official resource unavailable'): fixture.execute()


class ProtocolIntegrationTests(unittest.TestCase):
    def test_legacy_dispatch_is_explicit_and_modern_auth_failure_never_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            client = SimpleNamespace(repo=legacy.UPSTREAM, release=lambda: {'id': 101, 'assets': []},
                                     run=lambda *args: '[]')
            expected = ({'synthetic': b'body'}, {'kind': 'synthetic-legacy'})
            with patch.object(upgrade, 'ControlGitHub', return_value=client), \
                    patch.object(legacy, 'materialize_legacy', return_value=expected) as adapt, \
                    patch.object(upgrade, 'public_controls', side_effect=AssertionError('Wrong protocol')):
                self.assertEqual(upgrade.source_archive('v2.97.43', 'stable', 'custom', 'amd64', Path(td)), expected)
                adapt.assert_called_once()
            # Its absence from the embedded/truncated list cannot select legacy.
            client.run = lambda *args: '[{"name":"resolved-source.json"}]'
            with patch.object(upgrade, 'ControlGitHub', return_value=client), \
                    patch.object(legacy, 'materialize_legacy', side_effect=AssertionError('No downgrade')), \
                    patch.object(upgrade, 'public_controls', side_effect=ValueError('Broken modern contract')):
                with self.assertRaisesRegex(ValueError, 'Broken modern contract'):
                    upgrade.source_archive('v2.97.43', 'stable', 'custom', 'amd64', Path(td))

    def test_present_resolved_source_is_never_accepted_by_legacy_reader(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = Fixture(Path(td)); fixture.bodies['resolved-source.json'] = b'{}'; fixture.refresh_proof()
            with self.assertRaisesRegex(ValueError, 'Modern custom controls'): fixture.execute()
            self.assertEqual(fixture.downloads, [])


class VerifiedMaterializationTests(unittest.TestCase):
    def test_mode_inventory_is_from_same_descriptor_and_excludes_special_bits(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td); path = work / 'archive.tar.gz'
            with tarfile.open(path, 'w:gz') as archive:
                for name, mode in [('install.sh', 0o4755), ('data.txt', 0o2640)]:
                    item = tarfile.TarInfo('package/' + name); item.mode, item.size = mode, 4
                    archive.addfile(item, io.BytesIO(b'data'))
            modes = {}
            body = legacy.archive_bytes(path, byte_facts(path.read_bytes()), 'package', modes=modes)
            self.assertEqual(modes, {'install.sh': 0o755, 'data.txt': 0o640})
            path.write_bytes(b'replaced after protected validation')
            package = upgrade.write_verified_archive(body, modes, work / 'out', 'package')
            self.assertEqual((package / 'install.sh').stat().st_mode & 0o7777, 0o755)
            self.assertEqual((package / 'data.txt').stat().st_mode & 0o7777, 0o640)
            self.assertEqual((package / 'data.txt').read_bytes(), b'data')

    def test_predecessor_never_reopens_replaced_archive_for_extraction(self):
        from test_native_upgrade import IndependentArchiveTests
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            archive, binding, source, provenance, body = IndependentArchiveTests().fixture(work)
            protected = upgrade.archive_bytes
            def replaced(path, *args, **kwargs):
                result = protected(path, *args, **kwargs)
                path.write_bytes(b'untrusted replacement after verification')
                return result
            with patch.object(upgrade, 'archive_bytes', side_effect=replaced):
                package, _ = upgrade.unpack_predecessor(archive, work / 'out', binding,
                    'official', 'amd64', source, provenance, work)
            self.assertEqual({str(p.relative_to(package)): p.read_bytes() for p in package.rglob('*') if p.is_file()}, body)
            self.assertEqual((package / 'install.sh').stat().st_mode & 0o777, 0o755)

    def test_target_never_reopens_replaced_archive_for_extraction(self):
        from test_native_upgrade import IndependentArchiveTests
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            archive, binding, _, _, body = IndependentArchiveTests().fixture(work)
            inputs = work / 'inputs'; (inputs / 'official').mkdir(parents=True)
            path = inputs / 'official' / archive.name; archive.rename(path)
            identity = {'repository': 'synthetic/repository', 'run_id': 909, 'head_sha': 'a' * 40,
                        'run_attempt': 2, 'version': binding['version'], 'tag': binding['version'],
                        'row': {'source': 'official', 'arch': 'amd64'}}
            facts = byte_facts(path.read_bytes())
            provenance = {'repository': identity['repository'], 'workflow_run_id': 909,
                'workflow_commit': identity['head_sha'], 'workflow_run_attempt': 2,
                'version': binding['version'], 'release_tag': binding['version'],
                'source': 'official', 'arch': 'amd64', 'receipt_sha256': 'b' * 64,
                'controls': {'artifact_id': 808}, 'files': {'official/' + path.name: facts},
                'archive_sha256': facts['sha256']}
            protected = upgrade.archive_bytes
            def replaced(path, *args, **kwargs):
                result = protected(path, *args, **kwargs)
                path.write_bytes(b'untrusted replacement after verification')
                return result
            with patch('validate_release.validate'), patch.object(upgrade, 'archive_bytes', side_effect=replaced):
                package = upgrade.unpack_target(inputs, provenance, work / 'out', identity, 'b' * 64, '808', work)
            self.assertEqual({str(p.relative_to(package)): p.read_bytes() for p in package.rglob('*') if p.is_file()}, body)

    def test_materialization_rejects_unsafe_paths_modes_existing_outputs_and_missing_modes(self):
        for fault in ('path', 'mode', 'missing', 'root', 'existing'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                work = Path(td); body = {'file': b'content'}; modes = {'file': 0o644}; root = 'package'
                if fault == 'path': body = {'../escape': b'content'}; modes = {'../escape': 0o644}
                elif fault == 'mode': modes['file'] = 0o4755
                elif fault == 'missing': modes = {}
                elif fault == 'root': root = '../escape'
                else: (work / 'out').mkdir()
                with self.assertRaises((ValueError, FileExistsError)):
                    upgrade.write_verified_archive(body, modes, work / 'out', root)
                self.assertFalse((work / 'escape').exists())


if __name__ == '__main__':
    unittest.main()
