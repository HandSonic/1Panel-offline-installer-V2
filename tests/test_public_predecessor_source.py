"""Original public predecessor sources, with synthetic bytes and mocked IO only."""
import copy
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
import native_upgrade_input as upgrade
import public_predecessor_source as source
from manual_publication import OriginalCIUnavailable
from resolved_inventory import ARCHES, canonical, vendor_base
from validate_resolved_custom import archive_bytes, byte_facts
from test_candidate_source_acquisition import SourceFixture


class PublicFixture(SourceFixture):
    def __init__(self, directory, official=False, public=False):
        super().__init__(directory, ci=not public)
        self.arch = 'amd64'
        self.row = self.runtime['upstream']['records'][self.arch]
        self.raw = self.raws / self.row['file']
        self.raw_body = archive_bytes(self.raw, byte_facts(self.raw.read_bytes()), self.row['file'][:-7])
        self.source = 'official' if official else 'custom'
        app = {'version': self.version, 'bytes': self.row['size'], 'sha256': self.row['sha256']}
        if official:
            app['url'] = vendor_base(self.version, 'stable', 'official') + self.row['file']
        elif public:
            app.update(source_kind='release', url=f'https://github.com/{source.UPSTREAM}/releases/download/{self.version}/{self.row["file"]}')
            app.update(checksum_url=app['url'] + '.sha256', upstream_sha256=app['sha256'])
        else:
            app.update(source_kind='github-actions-artifact', url=self.provenance['run_url'],
                       checksum_url=self.provenance['run_url'], artifact_name=self.row['file'],
                       expected_build_repository_commit=self.producer, upstream_sha256=app['sha256'])
        self.manifest = {'schema': 1, 'architecture': self.arch, 'source': self.source,
            'app_version': self.version, 'inputs': {'app': app},
            'upstream_manifest_sha256': byte_facts(self.raw_body['manifest.json'])['sha256'],
            'upstream_provenance': json.loads(self.raw_body['manifest.json'])}
        self.archive = directory / 'predecessor.tar.gz'
        self.name = f'1panel-{self.version}-{self.source}-offline-linux-{self.arch}.tar.gz'
        self.receipt = {'upstream_input': self.plan['upstream_input']}
        self.save()

    def save(self):
        with tarfile.open(self.archive, 'w:gz') as archive:
            for name, raw in {'offline-manifest.json': canonical(self.manifest),
                              'manifest.json': self.raw_body['manifest.json']}.items():
                info = tarfile.TarInfo(self.name[:-7] + '/' + name)
                info.size = len(raw); archive.addfile(info, io.BytesIO(raw))
        self.binding = {'version': self.version, 'mode': 'stable',
                        'archive': dict(name=self.name, **byte_facts(self.archive.read_bytes()))}
        self.receipt['files'] = {self.name: byte_facts(self.archive.read_bytes())}

    def fetch(self, *args, evidence=None):
        result = super().fetch(*args)
        if evidence is not None:
            evidence.update(run_attempt=2, job_id=902, availability='available')
        return result

    def call(self):
        return source.source_from_public_predecessor(self.archive, self.binding, self.receipt,
            self.source, self.arch, self.work, ROOT)


class RetainedUpper:
    repo = source.UPSTREAM

    def __init__(self, fixture, backups=False):
        self.tag = fixture.version
        self.state = dict(id=555, run_attempt=2, head_sha=fixture.producer, status='completed',
            conclusion='success', path='.github/workflows/build.yml', event='workflow_dispatch',
            repository={'id': 11, 'full_name': self.repo}, head_repository={'id': 11, 'full_name': self.repo})
        self.jobs = [dict(id=900 + i, run_id=555, run_attempt=2, head_sha=fixture.producer,
            name=name, status='completed', conclusion='success',
            html_url=f'https://github.com/{self.repo}/actions/runs/555/job/{900+i}')
            for i, name in enumerate(['tests', 'prepare', 'build'] + [f'compile ({a})' for a in ARCHES])]
        self.matrix = {'schema_version': 2, 'version': self.tag, 'producer_run_id': 555,
            'producer_run_attempt': 2, 'producer_head_sha': fixture.producer,
            'requested_architectures': list(ARCHES), 'artifacts': list(fixture.runtime['upstream']['records'].values()),
            'outcomes': [dict(architecture=a, status='success', stage='compile', reason='',
                job_id=job['id'], job_url=job['html_url']) for a, job in zip(ARCHES, self.jobs[3:])]}
        bodies = {p.name: p.read_bytes() for p in fixture.raws.iterdir()}
        bodies['build-manifest.json'] = canonical(self.matrix)
        proof = dict(schema=2, contract='upstream-matrix', version=self.tag, release_tag=self.tag,
            repository=self.repo, workflow_run_id=555, workflow_run_attempt=2, workflow_commit=fixture.producer,
            files={n: byte_facts(raw) for n, raw in bodies.items()}, upstream_input=fixture.provenance,
            requested_architectures=list(ARCHES), successful_architectures=list(ARCHES),
            outcomes=self.matrix['outcomes'], producer_run_id=555, producer_run_attempt=2,
            producer_head_sha=fixture.producer)
        bodies['release-validation.json'] = canonical(proof)
        self.log = 'VERIFIED_RELEASE_RECEIPT_SHA256=' + byte_facts(bodies['release-validation.json'])['sha256'] + '\n'
        self.assets, self.bodies, self.downloaded = [], {}, []
        for index, (name, raw) in enumerate(bodies.items()):
            self.add(name + ('.backup-123456789abc' if backups else ''), raw, index + 1000)
        if backups:
            self.add(fixture.row['file'], b'new canonical generation', 2000)
        self.authentication = {'repository': self.repo, 'artifact_id': 666,
            'artifact_name': f'verified-1panel-{self.tag}-{fixture.producer}', 'artifact_sha256': '9' * 64,
            'build_repository_commit': fixture.producer, 'run': self.state, 'latest_run': copy.deepcopy(self.state), 'run_attempt': 2,
            'job_id': 902, 'availability': 'expired'}

    def add(self, name, raw, identity):
        self.assets.append(dict(id=identity, name=name, size=len(raw), digest='sha256:' + byte_facts(raw)['sha256'],
            state='uploaded', url=f'https://api.github.com/repos/{self.repo}/releases/assets/{identity}'))
        self.bodies[identity] = raw

    def release(self):
        return copy.deepcopy(dict(id=999, tag_name=self.tag, draft=False, prerelease=False,
            url=f'https://api.github.com/repos/{self.repo}/releases/999', assets=self.assets))

    def run(self, *args):
        endpoint = args[-1]
        if endpoint.endswith('/releases/999'): return json.dumps(self.release())
        if '/releases/999/assets?' in endpoint: return json.dumps(self.assets)
        if endpoint.endswith('/attempts/2'): return json.dumps(self.state)
        if '/attempts/2/jobs?' in endpoint: return json.dumps({'total_count': len(self.jobs), 'jobs': self.jobs})
        if endpoint.endswith('/jobs/902/logs'): return self.log
        raise AssertionError('Unexpected network endpoint: ' + endpoint)

    def asset_bytes(self, asset, **kwargs):
        raw = self.bodies[asset['id']]
        if byte_facts(raw) != {'bytes': asset['size'], 'sha256': asset['digest'][7:]}:
            raise ValueError('Synthetic transport detected asset tampering')
        return raw

    def download_asset(self, asset, path):
        self.downloaded.append(asset['id']); path.write_bytes(self.asset_bytes(asset))


class PublicSourceTests(unittest.TestCase):
    def test_original_ci_selected_without_current_tag_discovery(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = PublicFixture(Path(td))
            with patch('manual_publication.fetch_ci_bundle', side_effect=fixture.fetch), \
                    patch.object(upgrade, 'canonical_read', side_effect=fixture.read), \
                    patch.object(source, 'ControlGitHub', side_effect=AssertionError('No current source discovery')):
                body, proof = fixture.call()
            self.assertEqual(body, fixture.raw_body)
            self.assertEqual(proof['original_upstream_input'], fixture.provenance)
            self.assertEqual(proof['acquisition']['kind'], 'exact-verified-ci-artifact')
            self.assertEqual(proof['acquisition']['original_ci_authentication']['run_attempt'], 2)

    def test_normal_materialize_reads_pinned_predecessor_before_resolving_original_source(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as td:
            fixture = PublicFixture(Path(td))
            fixture.receipt.update(schema=1, workflow_run_id=42)
            controls = {'release-validation.json': canonical(fixture.receipt), 'checksums.txt': b'synthetic checksum'}
            assets = [dict(name=name, size=len(raw)) for name, raw in controls.items()]
            assets.append(dict(name=fixture.name, size=fixture.archive.stat().st_size))
            release = {'id': 8, 'assets': assets}
            def download(asset, destination):
                destination.write_bytes(fixture.archive.read_bytes() if asset['name'] == fixture.name else controls[asset['name']])
            client = SimpleNamespace(repo=upgrade.REPOSITORY, download_asset=download)
            args = SimpleNamespace(version='v2.100.0', tag='', source='custom', arch='amd64',
                output=str(Path(td) / 'output'), provenance=str(Path(td) / 'evidence.json'),
                target_input=str(Path(td) / 'target-input'), target_provenance=str(Path(td) / 'target.json'))
            observed = []
            def unpack(archive, destination, binding, source_kind, arch, body, proof, root):
                observed.append(proof)
                self.assertEqual(body, fixture.raw_body)
                raise ValueError('synthetic stop before native installation')
            with patch.object(upgrade, 'current_identity', return_value={}), \
                    patch.object(upgrade, 'predecessor_context', return_value={}), \
                    patch.object(upgrade, 'array_pages', return_value=[]), \
                    patch.object(upgrade, 'select_predecessor', return_value={'release': {'id': 8}}), \
                    patch.object(upgrade, 'public_release', return_value=release), \
                    patch.object(upgrade, 'api', return_value={}), \
                    patch.object(upgrade, 'bind_public', return_value=fixture.binding), \
                    patch.object(upgrade, 'verify_receipt_log', return_value={}), \
                    patch.object(upgrade, 'source_archive', side_effect=AssertionError('No current tag source lookup')), \
                    patch('manual_publication.fetch_ci_bundle', side_effect=fixture.fetch), \
                    patch.object(upgrade, 'canonical_read', side_effect=fixture.read), \
                    patch.object(upgrade, 'unpack_predecessor', side_effect=unpack):
                fixture.binding['receipt_sha256'] = byte_facts(controls['release-validation.json'])['sha256']
                with self.assertRaisesRegex(ValueError, 'synthetic stop before native installation'):
                    upgrade.materialize(args, {'RUNNER_TEMP': td}, client)
            self.assertEqual(observed[0]['original_upstream_input'], fixture.provenance)

    def test_ci_authentication_failures_never_try_retained_transport(self):
        for reason in ('cancelled', 'wrong digest', 'wrong provenance', 'tampered ZIP', '403 unavailable'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as td:
                fixture = PublicFixture(Path(td))
                with patch('manual_publication.fetch_ci_bundle', side_effect=ValueError(reason)), \
                        patch.object(source, 'retained_public_source', side_effect=AssertionError('No fallback')):
                    with self.assertRaisesRegex(ValueError, reason): fixture.call()

    def test_available_ci_wrong_raw_or_contract_cannot_fallback(self):
        for fault in ('raw', 'contract', 'provenance'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = PublicFixture(Path(td))
                if fault == 'raw': fixture.raw.write_bytes(fixture.raw.read_bytes() + b'tampered')
                if fault == 'contract': (fixture.raws / 'resolved-source.json').write_bytes(b'{}')
                if fault == 'provenance': fixture.provenance = dict(fixture.provenance, artifact_id=667)
                with patch('manual_publication.fetch_ci_bundle', side_effect=fixture.fetch), \
                        patch.object(source, 'retained_public_source', side_effect=AssertionError('No fallback')):
                    with self.assertRaises(ValueError): fixture.call()

    def test_expired_or_missing_ci_uses_exact_canonical_or_verified_backups(self):
        for backups in (False, True):
            for availability in ('expired', 'missing'):
                with self.subTest(backups=backups, availability=availability), tempfile.TemporaryDirectory() as td:
                    fixture = PublicFixture(Path(td)); upper = RetainedUpper(fixture, backups)
                    upper.authentication['availability'] = availability
                    with patch('manual_publication.fetch_ci_bundle', side_effect=OriginalCIUnavailable(upper.authentication)), \
                            patch('manual_publication.github_json', return_value=upper.state), \
                            patch.object(source, 'ControlGitHub', return_value=upper), \
                            patch.object(upgrade, 'PredecessorGitHub', return_value=upper), \
                            patch.object(upgrade, 'canonical_read', side_effect=fixture.read):
                        body, proof = fixture.call()
                    self.assertEqual(body, fixture.raw_body)
                    acquisition = proof['acquisition']
                    self.assertEqual(acquisition['original_ci_authentication']['availability'], availability)
                    self.assertEqual(proof['original_upstream_input'], fixture.provenance)
                    self.assertEqual(acquisition['asset']['sha256'], fixture.row['sha256'])
                    self.assertNotIn(2000, upper.downloaded)
                    self.assertEqual('.backup-' in acquisition['asset']['name'], backups)

    def test_latest_run_change_during_retained_acquisition_is_rejected(self):
        for availability in ('expired', 'missing'):
            with self.subTest(availability=availability), tempfile.TemporaryDirectory() as td:
                fixture = PublicFixture(Path(td)); upper = RetainedUpper(fixture, backups=True)
                upper.authentication['availability'] = availability
                def observed(endpoint):
                    return (copy.deepcopy(upper.state) if '/attempts/' in endpoint else
                            dict(upper.state, run_attempt=3, conclusion='cancelled'))
                with patch('manual_publication.fetch_ci_bundle', side_effect=OriginalCIUnavailable(upper.authentication)), \
                        patch('manual_publication.github_json', side_effect=observed), \
                        patch.object(source, 'ControlGitHub', return_value=upper), \
                        patch.object(upgrade, 'PredecessorGitHub', return_value=upper), \
                        patch.object(upgrade, 'canonical_read', side_effect=fixture.read):
                    with self.assertRaises(ValueError): fixture.call()

    def test_modern_retained_transport_is_product_local_but_shared_controls_remain_required(self):
        from resolved_transport import public_controls
        for missing in ('unrelated-raw', 'unrelated-pair', 'selected-raw', 'selected-sidecar', 'shared-control', 'unrelated-sidecar'):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as td:
                fixture = PublicFixture(Path(td)); upper = RetainedUpper(fixture)
                name = {'unrelated-raw': f'1panel-{fixture.version}-linux-arm64.tar.gz',
                        'unrelated-pair': f'1panel-{fixture.version}-linux-arm64.tar.gz',
                        'selected-raw': fixture.row['file'], 'selected-sidecar': fixture.row['file'] + '.sha256',
                        'shared-control': 'checksums.txt',
                        'unrelated-sidecar': f'1panel-{fixture.version}-linux-arm64.tar.gz.sha256'}[missing]
                absent = {name, name + '.sha256'} if missing == 'unrelated-pair' else {name}
                upper.assets = [asset for asset in upper.assets if asset['name'] not in absent]
                # Full-inventory callers retain their original strict default.
                with self.assertRaises(ValueError): public_controls(fixture.version, 'stable', upper)
                with patch('manual_publication.fetch_ci_bundle', side_effect=OriginalCIUnavailable(upper.authentication)), \
                        patch('manual_publication.github_json', return_value=upper.state), \
                        patch.object(source, 'ControlGitHub', return_value=upper), \
                        patch.object(upgrade, 'PredecessorGitHub', return_value=upper), \
                        patch.object(upgrade, 'canonical_read', side_effect=fixture.read):
                    if missing in ('unrelated-raw', 'unrelated-pair', 'unrelated-sidecar'):
                        body, proof = fixture.call()
                        self.assertEqual(body, fixture.raw_body)
                        self.assertEqual(proof['pin']['sha256'], fixture.row['sha256'])
                    else:
                        with self.assertRaises(ValueError): fixture.call()

    def test_retained_authentication_rejects_missing_wrong_or_tampered_evidence(self):
        faults = ('raw-digest', 'raw-size', 'raw-bytes', 'missing-contract', 'contract-bytes',
                  'missing-receipt', 'receipt-log', 'producer-attempt', 'producer-head',
                  'cancelled', 'descriptor', 'post-download-change')
        for fault in faults:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = PublicFixture(Path(td)); upper = RetainedUpper(fixture, backups=True)
                raw = next(a for a in upper.assets if a['name'].startswith(fixture.row['file'] + '.backup'))
                contract = next(a for a in upper.assets if a['name'].startswith('resolved-source.json'))
                receipt = next(a for a in upper.assets if a['name'].startswith('release-validation.json'))
                if fault == 'raw-digest': raw['digest'] = 'sha256:' + '0' * 64
                if fault == 'raw-size': raw['size'] += 1
                if fault == 'raw-bytes': upper.bodies[raw['id']] += b'tampered'
                if fault == 'missing-contract': upper.assets.remove(contract)
                if fault == 'contract-bytes': upper.bodies[contract['id']] = b'{}'
                if fault == 'missing-receipt': upper.assets.remove(receipt)
                if fault == 'receipt-log': upper.log = ''
                if fault == 'producer-attempt': upper.authentication['run_attempt'] = 1
                if fault == 'producer-head': upper.authentication['run'] = dict(upper.state, head_sha='7' * 40)
                if fault == 'cancelled': upper.state['conclusion'] = 'cancelled'
                if fault == 'descriptor':
                    proof = json.loads(upper.bodies[receipt['id']]); proof['upstream_input']['artifact_id'] = 777
                    value = canonical(proof); upper.bodies[receipt['id']] = value
                    receipt.update(size=len(value), digest='sha256:' + byte_facts(value)['sha256'])
                    upper.log = 'VERIFIED_RELEASE_RECEIPT_SHA256=' + byte_facts(value)['sha256'] + '\n'
                download = upper.download_asset
                if fault == 'post-download-change':
                    def download(asset, path):
                        upper.download_asset(asset, path); raw['name'] += '.changed'
                with patch('manual_publication.fetch_ci_bundle', side_effect=OriginalCIUnavailable(upper.authentication)), \
                        patch('manual_publication.github_json', return_value=upper.state), \
                        patch.object(source, 'ControlGitHub', return_value=upper), \
                        patch.object(upgrade, 'PredecessorGitHub', return_value=type('Downloader', (), {'download_asset': staticmethod(download)})()), \
                        patch.object(upgrade, 'canonical_read', side_effect=fixture.read):
                    with self.assertRaises(ValueError): fixture.call()

    def test_public_input_recovers_authenticated_original_receipt_without_ci(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = PublicFixture(Path(td), public=True); upper = RetainedUpper(fixture, backups=True)
            with patch('manual_publication.fetch_ci_bundle', side_effect=AssertionError('Public source is independent of CI')) , \
                    patch.object(source, 'ControlGitHub', return_value=upper), \
                    patch.object(upgrade, 'PredecessorGitHub', return_value=upper), \
                    patch.object(upgrade, 'canonical_read', side_effect=fixture.read):
                _, proof = fixture.call()
            self.assertEqual(proof['original_upstream_input'], {'source_kind': 'verified-public-release'})

    def test_missing_malformed_lower_evidence_is_controlled_valueerror(self):
        for fault in ('app', 'receipt-input', 'manifest-copy', 'contract-hash', 'ci-url'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = PublicFixture(Path(td))
                if fault == 'app': del fixture.manifest['inputs']['app']
                if fault == 'receipt-input': del fixture.receipt['upstream_input']
                if fault == 'manifest-copy': fixture.manifest['upstream_provenance'] = {}
                if fault == 'contract-hash':
                    value = json.loads(fixture.raw_body['manifest.json']); value['resolved_contract_sha256'] = 'bad'
                    fixture.raw_body['manifest.json'] = canonical(value)
                    fixture.manifest.update(upstream_provenance=value,
                        upstream_manifest_sha256=byte_facts(fixture.raw_body['manifest.json'])['sha256'])
                if fault == 'ci-url': fixture.manifest['inputs']['app']['url'] += '/different'
                fixture.save()
                with patch('manual_publication.fetch_ci_bundle', side_effect=AssertionError('No transport for bad evidence')):
                    with self.assertRaises(ValueError): fixture.call()

    def test_legacy_backups_reuse_original_receipt_and_complete_legacy_validation(self):
        from types import SimpleNamespace
        import legacy_predecessor_source as legacy
        from test_legacy_predecessor_source import Fixture as LegacyFixture, archive_bytes as make_archive
        for fault in (None, 'unrelated-raw', 'unrelated-pair', 'selected-sidecar', 'raw-digest', 'missing-control', 'receipt-log', 'immutable-resource'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                work = Path(td); upper = LegacyFixture(work)
                raw_name = f'1panel-{upper.version}-linux-amd64.tar.gz'
                name = f'1panel-{upper.version}-custom-offline-linux-amd64.tar.gz'
                app = dict(byte_facts(upper.raw), version=upper.version, source_kind='release',
                    url=f'https://github.com/{source.UPSTREAM}/releases/download/{upper.version}/{raw_name}')
                app.update(upstream_sha256=app['sha256'], checksum_url=app['url'] + '.sha256')
                raw_manifest = canonical(upper.manifest)
                manifest = dict(schema=1, source='custom', architecture='amd64', app_version=upper.version,
                    inputs={'app': app}, upstream_provenance=upper.manifest,
                    upstream_manifest_sha256=byte_facts(raw_manifest)['sha256'])
                lower = work / 'lower.tar.gz'
                lower.write_bytes(make_archive({'manifest.json': raw_manifest,
                    'offline-manifest.json': canonical(manifest)}, name[:-7]))
                pin = byte_facts(lower.read_bytes())
                binding = dict(version=upper.version, mode='stable', archive=dict(name=name, **pin))
                receipt = dict(files={name: pin}, upstream_input={'source_kind': 'verified-public-release'})
                for asset in upper.release_value['assets']:
                    old_name = asset['name']; asset['name'] += '.backup-123456789abc'
                    upper.bodies[asset['name']] = upper.bodies[old_name]
                for index, (logical, raw) in enumerate(((raw_name, b'new-generation'), ('resolved-source.json', b'{}')), 5000):
                    upper.release_value['assets'].append(dict(id=index, name=logical, size=len(raw), state='uploaded',
                        digest='sha256:' + byte_facts(raw)['sha256'],
                        url=f'https://api.github.com/repos/{source.UPSTREAM}/releases/assets/{index}'))
                raw_asset = next(a for a in upper.release_value['assets'] if a['name'] == raw_name + '.backup-123456789abc')
                if fault in ('unrelated-raw', 'unrelated-pair'):
                    absent = {f'1panel-{upper.version}-linux-arm64.tar.gz.backup-123456789abc'}
                    if fault == 'unrelated-pair': absent.add(f'1panel-{upper.version}-linux-arm64.tar.gz.sha256.backup-123456789abc')
                    upper.release_value['assets'] = [a for a in upper.release_value['assets'] if a['name'] not in absent]
                if fault == 'selected-sidecar':
                    upper.release_value['assets'] = [a for a in upper.release_value['assets'] if a['name'] != raw_name + '.sha256.backup-123456789abc']
                if fault == 'raw-digest': raw_asset['digest'] = 'sha256:' + '0' * 64
                if fault == 'missing-control':
                    upper.release_value['assets'] = [a for a in upper.release_value['assets'] if not a['name'].startswith('build-manifest.json')]
                if fault == 'receipt-log': upper.log = ''
                if fault == 'immutable-resource': upper.original['install.sh'] += b'changed'
                materialize = legacy.materialize_legacy
                def execute(*args, **kwargs):
                    return materialize(*args, **kwargs, read=upper.read, discover=upper.discover, acquire=upper.acquire)
                with patch.object(source, 'ControlGitHub', return_value=upper), \
                        patch.object(upgrade, 'PredecessorGitHub', return_value=SimpleNamespace(download_asset=upper.download)), \
                        patch.object(legacy, 'materialize_legacy', side_effect=execute):
                    call = lambda: source.source_from_public_predecessor(lower, binding, receipt, 'custom', 'amd64', work, ROOT)
                    if fault not in (None, 'unrelated-raw', 'unrelated-pair'):
                        with self.assertRaises(ValueError): call()
                    else:
                        body, proof = call()
                        self.assertEqual(body['manifest.json'], raw_manifest)
                        self.assertEqual(proof['kind'], 'authenticated-legacy-custom-source')
                        self.assertIn('.backup-', proof['acquisition']['asset']['name'])
                        self.assertTrue(upper.reads)
                        self.assertEqual(upper.downloads, [raw_asset['id']])

    def test_default_legacy_controls_still_require_complete_raw_inventory(self):
        import legacy_predecessor_source as legacy
        from test_legacy_predecessor_source import Fixture as LegacyFixture
        with tempfile.TemporaryDirectory() as td:
            upper = LegacyFixture(Path(td))
            upper.release_value['assets'] = [a for a in upper.release_value['assets']
                if a['name'] != f'1panel-{upper.version}-linux-arm64.tar.gz']
            with self.assertRaisesRegex(ValueError, 'receipt differs from canonical asset'):
                legacy.authenticate_controls(upper.version, upper.mode, upper)

    def test_official_uses_own_pin_independent_of_custom_and_current_discovery(self):
        for fault in (None, 'changed-bytes', 'wrong-url'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = PublicFixture(Path(td), official=True)
                if fault == 'wrong-url': fixture.manifest['inputs']['app']['url'] = 'https://example.test/new'; fixture.save()
                def download(pin, path):
                    self.assertEqual(pin['sha256'], fixture.row['sha256'])
                    path.write_bytes(fixture.raw.read_bytes() + (b'changed' if fault == 'changed-bytes' else b''))
                with patch.object(upgrade, 'download_url', side_effect=download), \
                        patch.object(upgrade, 'discover_vendor', side_effect=AssertionError('No current discovery')), \
                        patch.object(source, 'ControlGitHub', side_effect=AssertionError('No custom controls')):
                    if fault:
                        with self.assertRaises(ValueError): fixture.call()
                    else:
                        body, proof = fixture.call()
                        self.assertEqual(body, fixture.raw_body)
                        self.assertEqual(proof['kind'], 'canonical-vendor')


if __name__ == '__main__': unittest.main()
