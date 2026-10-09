"""Synthetic publication bytes and fake GitHub APIs; no downloads or native execution."""
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import runtime_publication as publication
import runtime_native_acceptance as native
from publication_outcomes import filename, products
from resolved_inventory import canonical, digest as object_digest


def facts(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def zipped(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    return output.getvalue()


class Fixture:
    def __init__(self, root, matrix=None):
        self.root = root
        self.identity = {'repository': 'HandSonic/1Panel-offline-installer-V2', 'version': 'v2.101.0',
            'tag': 'v2.101.0', 'run_id': 900001, 'run_attempt': 2, 'head_sha': 'a' * 40, 'event': 'workflow_dispatch'}
        matrix = matrix or {'official': ['amd64'], 'custom': ['arm64', 'riscv64'],
            'enterprise-original': ['amd64'], 'enterprise-docker': ['amd64']}
        self.rows = products(matrix)
        self.plan = {'version': 'v2.101.0', 'tag': 'v2.101.0', 'repository': self.identity['repository'],
            'workflow_run_id': '900001', 'workflow_commit': 'a' * 40, 'mode': 'stable',
            'rows': self.rows, 'native_rows': [], 'upstream_input': {'source_kind': 'verified-public-release'},
            'resolved': {'mode': 'stable', 'source_contract': None,
                         'inventory': {'matrix': matrix, 'rows': self.rows, 'native_rows': []}}}
        self.release = root / 'release'; self.release.mkdir()
        self.files = {}
        self.bodies = {}
        outcomes = []
        for index, row in enumerate(self.rows):
            name = filename(self.plan['version'], row)
            raw = ('synthetic archive: ' + row['key']).encode()
            path = self.release / row['source'] / name; path.parent.mkdir(exist_ok=True); path.write_bytes(raw)
            self.files[name] = facts(raw); self.bodies['release/' + row['source'] + '/' + name] = raw
            outcomes.append(dict(row, status='success', stage='package', reason='', producer_run_attempt=1,
                job_id=600100 + index, job_url=f'https://github.com/{self.identity["repository"]}/actions/runs/900001/job/{600100 + index}'))
        sums = ''.join(self.files[n]['sha256'] + '  ' + n + '\n' for n in sorted(self.files)).encode()
        self.files['checksums.txt'] = facts(sums); (self.release / 'checksums.txt').write_bytes(sums)
        self.proof = {'schema': 2, 'contract': 'downstream-matrix', 'version': self.plan['version'],
            'release_tag': self.plan['tag'], 'repository': self.identity['repository'],
            'policy_fingerprint': 'b' * 64, 'workflow_run_id': 900001, 'workflow_run_attempt': 1,
            'workflow_commit': 'a' * 40, 'plan_sha256': object_digest(self.plan),
            'requested_products': self.rows, 'outcomes': outcomes, 'files': self.files,
            'upstream_input': self.plan['upstream_input']}
        self.refresh_preparation()
        self.results = {}
        for index, row in enumerate(self.rows):
            for key in native.required_results(row):
                self.results[key] = {'key': key, 'status': 'success', 'repository': self.identity['repository'],
                    'run_id': 900001, 'run_attempt': 2, 'head_sha': 'a' * 40,
                    'plan_sha256': self.proof['plan_sha256'], 'receipt_sha256': self.preparation['receipt_sha256'],
                    'controls_artifact_id': 700001, 'job_id': 610000 + index, 'artifact_id': 710000 + index,
                    'evidence_sha256': facts(('synthetic evidence: ' + key).encode())['sha256'],
                    'archive_sha256': self.files[filename(self.plan['version'], row)]['sha256']}

    def refresh_preparation(self):
        self.receipt = (json.dumps(self.proof, indent=2, sort_keys=True) + '\n').encode()
        self.preparation = dict(self.proof, receipt_sha256=facts(self.receipt)['sha256'],
                                controls_artifact_id=700001, _receipt_text=self.receipt.decode())

    def finalize(self, output='final', results=None):
        self.output = self.root / output
        self.result = publication.finalize(self.preparation, self.plan, self.identity,
            self.results if results is None else results, self.release, self.output,
            {'GITHUB_STEP_SUMMARY': str(self.root / 'summary.md')})
        return self.result

    def verify(self):
        return publication.verify_final_directory(self.output, self.plan, self.identity,
            self.result['receipt_sha256'], self.result['native_acceptance_sha256'])

    def transport(self, final=False):
        identity = self.identity
        run = {'id': 900001, 'head_sha': 'a' * 40, 'run_attempt': 2, 'path': publication.WORKFLOW,
            'event': 'workflow_dispatch', 'status': 'in_progress', 'conclusion': None,
            'repository': {'id': 101, 'full_name': identity['repository']},
            'head_repository': {'id': 101, 'full_name': identity['repository']}}
        blobs, artifacts = {}, []
        def add(identifier, name, entries):
            raw = zipped(entries); blobs[identifier] = raw
            item = {'id': identifier, 'name': name, 'expired': False, 'size_in_bytes': len(raw),
                'digest': 'sha256:' + facts(raw)['sha256'], 'workflow_run': {'id': 900001,
                    'head_sha': 'a' * 40, 'repository_id': 101, 'head_repository_id': 101}}
            artifacts.append(item)
            return item
        if final:
            add(700003, 'native-admitted-publication-900001-2', {p.name: p.read_bytes() for p in self.output.iterdir()})
            receipt_sha = self.result['receipt_sha256']; job_name = 'publication_acceptance'; attempt = 2
        else:
            controls = {'publication-work/control/' + publication.PROOF: self.receipt,
                        'publication-work/release/checksums.txt': (self.release / 'checksums.txt').read_bytes(),
                        'matrix-input/plan.json': canonical(self.plan)}
            add(700001, 'publication-controls-900001-1', controls)
            entries = dict(self.bodies, **{'release/checksums.txt': (self.release / 'checksums.txt').read_bytes(),
                'control/' + publication.PROOF: self.receipt, 'control/plan.json': canonical(self.plan)})
            for row in self.rows:
                if row['source'] == 'enterprise-docker':
                    name = filename(self.plan['version'], dict(row, source='enterprise-original'))
                    entries['verification/enterprise-original/' + name] = self.bodies['release/enterprise-original/' + name]
            add(700002, 'verified-publication-900001-1', entries)
            receipt_sha = self.preparation['receipt_sha256']; job_name = 'publication_prepare'; attempt = 1
        job = {'id': 600001, 'name': job_name, 'run_id': 900001, 'head_sha': 'a' * 40,
               'run_attempt': attempt, 'status': 'completed', 'conclusion': 'success'}
        log = 'VERIFIED_RELEASE_RECEIPT_SHA256=' + receipt_sha + '\n'
        if final:
            log += 'NATIVE_ACCEPTANCE_SHA256=' + self.result['native_acceptance_sha256'] + '\n'
        for artifact in artifacts:
            log += 'SHA256 digest of uploaded artifact zip is ' + artifact['digest'][7:] + '\n'
            log += 'Artifact ' + artifact['name'] + '.zip successfully finalized. Artifact ID ' + str(artifact['id']) + '\n'
        state = {'run': run, 'job': job, 'artifacts': artifacts, 'log': log, 'blobs': blobs, 'calls': []}
        def api(*args):
            endpoint = args[-1]; state['calls'].append(endpoint)
            if endpoint.endswith('/runs/900001'): return json.dumps(state['run'])
            if '/jobs?filter=all' in endpoint: return json.dumps({'total_count': 1, 'jobs': [state['job']]})
            if '/artifacts?' in endpoint: return json.dumps({'total_count': len(state['artifacts']), 'artifacts': state['artifacts']})
            if '/actions/artifacts/' in endpoint:
                return json.dumps(next(a for a in state['artifacts'] if a['id'] == int(endpoint.rsplit('/', 1)[1])))
            if endpoint.endswith('/jobs/600001/logs'): return state['log']
            raise AssertionError(endpoint)
        client = SimpleNamespace(repo=identity['repository'], tag=identity['tag'], run=api,
            download_zip=lambda artifact, path: path.write_bytes(state['blobs'][artifact['id']]))
        return client, state


class PublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.policy = patch.object(publication, 'policy', return_value='b' * 64)
        self.policy.start(); self.addCleanup(self.policy.stop)

    def test_final_receipt_binds_exact_pretty_preparation_and_native_subject(self):
        fixture = Fixture(self.root); fixture.finalize(); proof = fixture.verify()
        self.assertEqual(proof['workflow_run_attempt'], 2)
        self.assertEqual(proof['preparation_receipt_sha256'], facts(fixture.receipt)['sha256'])
        self.assertEqual(proof['files'][publication.NATIVE_FILE], publication.digest(fixture.output / publication.NATIVE_FILE))
        self.assertNotIn('acceptance', proof['outcomes'][0])
        subject = json.loads((fixture.output / publication.NATIVE_FILE).read_text())
        self.assertIn('acceptance', subject['admission']['outcomes'][0])
        self.assertEqual(subject['preparation_receipt_json'].encode(), fixture.receipt)
        self.assertNotIn(publication.NATIVE_FILE, (fixture.output / 'checksums.txt').read_text())

    def test_native_failure_filters_only_that_product_and_preserves_summary(self):
        fixture = Fixture(self.root)
        del fixture.results['upgrade:official:amd64']
        fixture.finalize(); proof = fixture.verify()
        failed = filename(fixture.plan['version'], fixture.rows[0])
        self.assertFalse((fixture.output / failed).exists())
        self.assertIn('official-amd64: failure', (self.root / 'summary.md').read_text())
        self.assertEqual(proof['outcomes'][0]['stage'], 'native-acceptance')
        self.assertEqual(len([p for p in fixture.output.iterdir() if p.name.endswith('.tar.gz')]), len(fixture.rows) - 1)
        self.assertFalse(fixture.result['all_requested_passed'])
        self.assertTrue((fixture.release / 'official' / failed).is_file())

    def test_empty_admission_has_failure_summary_and_no_output(self):
        fixture = Fixture(self.root, {'official': ['amd64']})
        with self.assertRaisesRegex(ValueError, 'Every product failed'):
            fixture.finalize(results={})
        self.assertIn('0 of 1', (self.root / 'summary.md').read_text())
        self.assertFalse((self.root / 'final').exists())

    def test_source_independent_enterprise_only_is_valid(self):
        fixture = Fixture(self.root, {'enterprise-original': ['arm64']})
        fixture.finalize(); self.assertTrue(fixture.verify())
        self.assertIsNone(fixture.plan['resolved']['source_contract'])

    def test_writer_rejects_changed_bytes_attempt_plan_and_membership(self):
        for fault in ('archive', 'receipt', 'native', 'extra', 'symlink', 'attempt', 'plan'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.root) as temp:
                fixture = Fixture(Path(temp)); fixture.finalize()
                archive = next(fixture.output.glob('*.tar.gz'))
                if fault == 'archive': archive.write_bytes(b'changed')
                if fault == 'receipt': (fixture.output / publication.PROOF).write_bytes(b'{}')
                if fault == 'native': (fixture.output / publication.NATIVE_FILE).write_bytes(b'{}')
                if fault == 'extra': (fixture.output / 'unexpected').write_bytes(b'bad')
                if fault == 'symlink': archive.unlink(); archive.symlink_to(self.root / 'missing')
                if fault == 'attempt': fixture.identity['run_attempt'] = 3
                if fault == 'plan': fixture.plan['workflow_commit'] = 'd' * 40
                with self.assertRaises(ValueError): fixture.verify()

    def test_malicious_final_rows_cannot_add_acceptance_or_reintroduce_failure(self):
        fixture = Fixture(self.root); fixture.finalize()
        proof = json.loads((fixture.output / publication.PROOF).read_text())
        proof['outcomes'][0]['acceptance'] = 'pretend-native'
        raw = canonical(proof); (fixture.output / publication.PROOF).write_bytes(raw)
        fixture.result['receipt_sha256'] = facts(raw)['sha256']
        with self.assertRaises(ValueError): fixture.verify()

    def test_preparation_transport_authenticates_full_zip_and_companions(self):
        fixture = Fixture(self.root)
        companion = filename(fixture.plan['version'], {'source': 'enterprise-original', 'arch': 'amd64'})
        fixture.plan['resolved']['inventory']['enterprise'] = {'archives': {'amd64': fixture.files[companion]}}
        fixture.proof['plan_sha256'] = object_digest(fixture.plan); fixture.refresh_preparation()
        client, state = fixture.transport()
        work = self.root / 'transport'; work.mkdir()
        preparation, release = publication.authenticate_preparation(client, fixture.identity, fixture.plan,
            700001, 700002, fixture.preparation['receipt_sha256'], work)
        self.assertEqual(preparation['_receipt_text'].encode(), fixture.receipt)
        self.assertEqual(publication.digest(release / 'enterprise-original' / companion), fixture.files[companion])
        self.assertGreaterEqual(state['calls'].count('repos/' + client.repo + '/actions/artifacts/700002'), 2)

    def test_preparation_cannot_use_wrong_upload_job_run_or_plan(self):
        for fault in ('upload', 'job', 'run', 'plan', 'duplicate'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.root) as temp:
                fixture = Fixture(Path(temp), {'official': ['amd64']}); client, state = fixture.transport()
                if fault == 'upload': state['log'] = state['log'].replace('Artifact ID 700002', 'Artifact ID 700009')
                if fault == 'job': state['job']['conclusion'] = 'failure'
                if fault == 'run': state['run']['head_sha'] = 'f' * 40
                if fault == 'plan': fixture.plan['tag'] += '-changed'
                if fault == 'duplicate': state['artifacts'].append(state['artifacts'][0])
                work = Path(temp) / 'transport'; work.mkdir()
                with self.assertRaises(ValueError):
                    publication.authenticate_preparation(client, fixture.identity, fixture.plan,
                        700001, 700002, fixture.preparation['receipt_sha256'], work)

    def test_final_artifact_authenticates_both_exact_markers_and_successful_attempt(self):
        for fault in (None, 'native-marker', 'duplicate-marker', 'receipt-marker', 'attempt', 'job-failed', 'zip'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.root) as temp:
                fixture = Fixture(Path(temp)); fixture.finalize(); client, state = fixture.transport(final=True)
                if fault == 'native-marker': state['log'] = state['log'].replace('NATIVE_ACCEPTANCE_SHA256=', 'OTHER=')
                if fault == 'duplicate-marker': state['log'] += 'NATIVE_ACCEPTANCE_SHA256=' + fixture.result['native_acceptance_sha256'] + '\n'
                if fault == 'receipt-marker': state['log'] = state['log'].replace(fixture.result['receipt_sha256'], 'e' * 64)
                if fault == 'attempt': state['job']['run_attempt'] = 1
                if fault == 'job-failed': state['job']['conclusion'] = 'failure'
                if fault == 'zip': state['blobs'][700003] += b'changed'
                work = Path(temp) / 'writer'; work.mkdir()
                def verify():
                    return publication.authenticate_final_artifact(client, fixture.identity, fixture.plan, 700003,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], work)
                if fault:
                    with self.assertRaises(ValueError): verify()
                else:
                    self.assertEqual({p.name for p in verify().iterdir()}, {p.name for p in fixture.output.iterdir()})

    def test_repair_retires_failed_canonical_name_without_changing_notes(self):
        from test_release_repair import FakeGitHub
        fixture = Fixture(self.root); del fixture.results['upgrade:official:amd64']; fixture.finalize()
        client = FakeGitHub(); client.repo = fixture.identity['repository']; client.tag = fixture.identity['tag']
        release = client.release
        client.release = lambda: dict(release(), id=800001, tag_name=client.tag, draft=False)
        failed = filename(fixture.plan['version'], fixture.rows[0])
        client.assets = {1: (failed, b'synthetic retired old archive')}
        state = publication.repair_existing(client, fixture.output, fixture.plan, fixture.identity,
            fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], self.root / 'journal.json')
        self.assertEqual(state['phase'], 'complete'); self.assertEqual(client.body, 'old release notes')
        self.assertTrue(client.assets[1][0].startswith(failed + '.backup-'))
        self.assertEqual(client.assets[1][1], b'synthetic retired old archive')
        self.assertEqual([c for c in client.calls if c[0] == 'rename'][-1][2], 'checksums.txt')

    def test_writer_never_mutates_before_final_authentication(self):
        fixture = Fixture(self.root); client = SimpleNamespace(repo=fixture.identity['repository'])
        args = SimpleNamespace(version=fixture.plan['version'], tag=fixture.plan['tag'], repository=client.repo,
            artifact_id='700003', receipt_sha256='a' * 64, native_acceptance_sha256='b' * 64,
            journal=str(self.root / 'journal.json'))
        with patch.object(publication, 'workflow_identity', return_value=(fixture.identity, fixture.plan)), \
             patch.object(publication, 'authenticate_final_artifact', side_effect=ValueError('bad marker')), \
             patch.object(publication, 'repair_existing') as repair, patch.object(publication, 'publish_draft') as draft:
            with self.assertRaises(ValueError):
                publication.publish(args, {'PUBLICATION_OPERATION': 'build', 'RUNNER_TEMP': str(self.root)}, client)
            repair.assert_not_called(); draft.assert_not_called()

    def test_acceptance_prints_no_markers_when_final_verification_fails(self):
        fixture = Fixture(self.root); client = SimpleNamespace(repo=fixture.identity['repository'])
        args = SimpleNamespace(version=fixture.plan['version'], tag=fixture.plan['tag'], artifact_id='700002',
            controls_artifact_id='700001', receipt_sha256=fixture.preparation['receipt_sha256'], output=str(self.root / 'final'))
        stream = io.StringIO()
        with patch.object(publication, 'workflow_identity', return_value=(fixture.identity, fixture.plan)), \
             patch.object(publication, 'authenticate_preparation', return_value=(fixture.preparation, fixture.release)), \
             patch.object(native, 'collect', return_value=fixture.results), \
             patch.object(publication, 'verify_final_directory', side_effect=ValueError('bad final bytes')), \
             patch('sys.stdout', stream):
            with self.assertRaises(ValueError): publication.acceptance(args, {'RUNNER_TEMP': str(self.root)}, client)
        self.assertEqual(stream.getvalue(), '')

    def test_status_distinguishes_missing_draft_current_and_rebuild(self):
        fixture = Fixture(self.root)
        client = SimpleNamespace(release=lambda: {'draft': False})
        for valid in (True, False):
            with patch.object(publication, 'public_noop', return_value=valid):
                self.assertEqual(publication.release_status(client, fixture.plan), 'verified' if valid else 'rebuild')
        with patch.object(publication, 'public_noop', side_effect=ValueError('legacy receipt')):
            self.assertEqual(publication.release_status(client, fixture.plan), 'rebuild')
        self.assertEqual(publication.release_status(SimpleNamespace(release=lambda: {'draft': True}), fixture.plan), 'draft')
        def error(status):
            raise subprocess.CalledProcessError(1, 'synthetic', stderr='HTTP ' + status)
        self.assertEqual(publication.release_status(SimpleNamespace(release=lambda: error('404')), fixture.plan), 'absent')
        with self.assertRaises(subprocess.CalledProcessError):
            publication.release_status(SimpleNamespace(release=lambda: error('403')), fixture.plan)

    def test_public_noop_requires_current_policy_complete_requested_matrix_and_no_failures(self):
        fixture = Fixture(self.root); fixture.finalize(); proof = fixture.verify()
        client = SimpleNamespace(repo=fixture.identity['repository'], release=lambda: {'draft': False},
            download=lambda name, directory: (directory / name).write_bytes((fixture.output / name).read_bytes()))
        with patch('native_upgrade_input.verify_public_acceptance', return_value={'receipt': proof}):
            self.assertTrue(publication.public_noop(client, fixture.plan))
            proof['outcomes'][0].update(status='failure', reason='synthetic failed test')
            self.assertFalse(publication.public_noop(client, fixture.plan))
            proof['policy_fingerprint'] = 'c' * 64
            with self.assertRaises(ValueError): publication.public_noop(client, fixture.plan)

    def draft_client(self, fixture):
        from test_release_repair import FakeGitHub
        class Draft(FakeGitHub):
            def __init__(self):
                super().__init__()
                self.repo = fixture.identity['repository']; self.tag = fixture.identity['tag']
                self.assets = {1: ('checksums.txt', b'synthetic old sums')}
                self.draft = True; self.release_id = 800001; self.remote_tag = self.tag

            def release(self):
                return dict(super().release(), id=self.release_id, tag_name=self.remote_tag, draft=self.draft)

            def run(self, *args):
                self.calls.append(args)
                if 'draft=false' in args:
                    self.draft = False
                return '{}'
        return Draft()

    def test_existing_draft_resumes_and_preserves_notes_and_latest_policy(self):
        fixture = Fixture(self.root); fixture.finalize(); client = self.draft_client(fixture)
        state = publication.publish_draft(client, fixture.output, fixture.plan, fixture.identity,
            fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], self.root / 'journal.json')
        self.assertEqual(state['phase'], 'complete'); self.assertFalse(client.draft)
        self.assertEqual(client.body, 'old release notes')
        publish_calls = [call for call in client.calls if 'draft=false' in call]
        self.assertEqual(len(publish_calls), 1)
        self.assertIn('make_latest=legacy', publish_calls[0])
        self.assertFalse(any('create' in call for call in client.calls))

    def test_draft_missing_extra_digest_size_or_identity_race_never_publishes(self):
        original_repair = publication.repair
        for fault in ('missing', 'extra', 'digest', 'zero-size', 'tag', 'id', 'draft'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.root) as temp:
                fixture = Fixture(Path(temp)); fixture.finalize(); client = self.draft_client(fixture)
                def tamper(*args, **kwargs):
                    state = original_repair(*args, **kwargs)
                    archive_id = next(i for i, (n, _) in client.assets.items() if n.endswith('.tar.gz'))
                    if fault == 'missing': del client.assets[archive_id]
                    if fault == 'extra': client.assets[9999] = ('unexpected.tar.gz', b'foreign')
                    if fault == 'digest': client.assets[archive_id] = (client.assets[archive_id][0], b'wrong bytes')
                    if fault == 'zero-size': client.assets[archive_id] = (client.assets[archive_id][0], b'')
                    if fault == 'tag': client.remote_tag = 'v2.99.0'
                    if fault == 'id': client.release_id += 1
                    if fault == 'draft': client.draft = False
                    return state
                with patch.object(publication, 'repair', side_effect=tamper), self.assertRaises(ValueError):
                    publication.publish_draft(client, fixture.output, fixture.plan, fixture.identity,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], Path(temp) / 'journal.json')
                self.assertFalse(any('draft=false' in call for call in client.calls))

    def test_draft_upload_or_api_failure_never_publishes(self):
        for fault in ('upload', 'api'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.root) as temp:
                fixture = Fixture(Path(temp)); fixture.finalize(); client = self.draft_client(fixture)
                failing = patch.object(client, 'upload' if fault == 'upload' else 'release',
                                       side_effect=RuntimeError('synthetic failure'))
                with failing, self.assertRaises(RuntimeError):
                    publication.publish_draft(client, fixture.output, fixture.plan, fixture.identity,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], Path(temp) / 'journal.json')
                self.assertFalse(any('draft=false' in call for call in client.calls))

    def test_unknown_initial_draft_state_cannot_write(self):
        for field, changed in (('release_id', 0), ('remote_tag', 'v2.99.0'), ('draft', False)):
            with self.subTest(field=field), tempfile.TemporaryDirectory(dir=self.root) as temp:
                fixture = Fixture(Path(temp)); fixture.finalize(); client = self.draft_client(fixture)
                setattr(client, field, changed)
                with self.assertRaises(ValueError):
                    publication.publish_draft(client, fixture.output, fixture.plan, fixture.identity,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], Path(temp) / 'journal.json')
                self.assertEqual(client.calls, [])

    def test_workflow_identity_requires_authenticated_canonical_plan_without_registry_fallback(self):
        fixture = Fixture(self.root)
        plan_path = self.root / 'plan.json'; plan_path.write_bytes(canonical(fixture.plan))
        env = {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted',
            'GITHUB_REPOSITORY': fixture.identity['repository'], 'GITHUB_EVENT_NAME': 'workflow_dispatch',
            'PUBLICATION_OPERATION': 'build', 'GITHUB_SERVER_URL': 'https://github.com',
            'GITHUB_API_URL': 'https://api.github.com', 'GITHUB_RUN_ID': '900001', 'GITHUB_RUN_ATTEMPT': '2',
            'GITHUB_SHA': 'a' * 40, 'GITHUB_WORKFLOW_SHA': 'a' * 40,
            'GITHUB_WORKFLOW_REF': fixture.identity['repository'] + '/' + publication.WORKFLOW + '@refs/heads/main',
            publication.PLAN_PATH: str(plan_path), publication.PLAN_SHA: facts(plan_path.read_bytes())['sha256']}
        with patch.object(publication, 'selected', return_value=fixture.plan['resolved']):
            self.assertEqual(publication.workflow_identity('v2.101.0', 'v2.101.0', env)[0], fixture.identity)
            plan_path.write_bytes(canonical(fixture.plan) + b'\n')
            with self.assertRaises(ValueError): publication.workflow_identity('v2.101.0', 'v2.101.0', env)
        with patch.object(publication, 'selected', return_value=None), self.assertRaisesRegex(ValueError, 'no registry fallback'):
            publication.workflow_identity('v2.101.0', 'v2.101.0', env)


class RecoveryBackupTests(unittest.TestCase):
    def test_retired_requested_backup_is_allowed_but_staged_or_foreign_base_is_not(self):
        import native_upgrade_input as binder
        from test_native_upgrade import PublicBootstrapTests
        args, proof = PublicBootstrapTests().modern_fixture()
        removed = '1panel-v2.100.0-custom-offline-linux-arm64.tar.gz'
        for suffix, allowed in ((removed + '.backup-0123456789ab', True),
                               (removed + '.staged-0123456789ab', False),
                               ('1panel-v2.99.0-custom-offline-linux-riscv64.tar.gz.backup-0123456789ab', False)):
            changed = copy.deepcopy(args)
            changed[1]['assets'].append({'id': 799998, 'name': suffix, 'state': 'uploaded',
                'url': binder.API_ROOT + '/releases/assets/799998', 'size': 19, 'digest': 'sha256:' + 'c' * 64})
            if allowed:
                self.assertTrue(binder.public_controls_binding(changed[1], changed[2], changed[3], changed[5]))
            else:
                with self.assertRaises(ValueError):
                    binder.public_controls_binding(changed[1], changed[2], changed[3], changed[5])


if __name__ == '__main__':
    unittest.main()
