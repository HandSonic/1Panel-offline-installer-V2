"""Synthetic publication bytes and fake GitHub APIs; no downloads or native execution."""
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings
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
            info = name if isinstance(name, zipfile.ZipInfo) else zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            archive.writestr(info, body)
    return output.getvalue()


def replace_artifact(state, identifier, entries):
    artifact = next(a for a in state['artifacts'] if a['id'] == identifier)
    old = artifact['digest'][7:]
    raw = zipped(entries)
    state['blobs'][identifier] = raw
    artifact.update(size_in_bytes=len(raw), digest='sha256:' + facts(raw)['sha256'])
    state['log'] = state['log'].replace(old, artifact['digest'][7:])


def replace_subject(fixture, subject):
    raw = canonical(subject)
    (fixture.output / publication.NATIVE_FILE).write_bytes(raw)
    path = fixture.output / publication.PROOF
    proof = json.loads(path.read_bytes())
    proof['files'][publication.NATIVE_FILE] = facts(raw)
    path.write_bytes(canonical(proof))
    fixture.result.update(receipt_sha256=facts(path.read_bytes())['sha256'],
                          native_acceptance_sha256=facts(raw)['sha256'])


class Fixture:
    def __init__(self, root, matrix=None, *, version='v2.101.0', mode='stable'):
        self.root = root
        self.identity = {'repository': 'HandSonic/1Panel-offline-installer-V2', 'version': version,
            'tag': version, 'run_id': 900001, 'run_attempt': 2, 'head_sha': 'a' * 40, 'event': 'workflow_dispatch'}
        matrix = matrix or {'official': ['amd64'], 'custom': ['arm64', 'riscv64'],
            'enterprise-original': ['amd64'], 'enterprise-docker': ['amd64']}
        self.rows = products(matrix)
        self.plan = {'version': version, 'tag': version, 'repository': self.identity['repository'],
            'workflow_run_id': '900001', 'workflow_commit': 'a' * 40, 'mode': mode,
            'rows': self.rows, 'native_rows': [], 'upstream_input': {'source_kind': 'verified-public-release'},
            'resolved': {'mode': mode, 'source_contract': None,
                         'inventory': {'matrix': matrix, 'rows': self.rows, 'native_rows': []}}}
        self.identity.update(runtime=self.plan['resolved'],runtime_plan_sha256=object_digest(self.plan))
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
        self.companions = {}
        for row in self.rows:
            if row['source'] == 'enterprise-docker':
                name = filename(self.plan['version'], dict(row, source='enterprise-original'))
                raw = self.bodies.get('release/enterprise-original/' + name,
                                     ('synthetic archive: enterprise-original-' + row['arch']).encode())
                self.companions['verification/enterprise-original/' + name] = raw
                self.plan['resolved']['inventory'].setdefault('enterprise', {'archives': {}})['archives'][row['arch']] = facts(raw)
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
        self.identity.update(runtime=self.plan['resolved'],runtime_plan_sha256=object_digest(self.plan))
        self.receipt = (json.dumps(self.proof, indent=2, sort_keys=True) + '\n').encode()
        self.preparation = dict(self.proof, receipt_sha256=facts(self.receipt)['sha256'],
                                controls_artifact_id=700001, _receipt_text=self.receipt.decode())
        raw = zipped(self.prepared_entries())
        self.preparation['_artifact'] = {'id': 700002, 'name': 'verified-publication-900001-1',
            'size_in_bytes': len(raw), 'digest': 'sha256:' + facts(raw)['sha256']}

    def prepared_entries(self):
        return dict(self.bodies, **{
            'release/checksums.txt': (self.release / 'checksums.txt').read_bytes(),
            'control/' + publication.PROOF: self.receipt, 'control/plan.json': canonical(self.plan)})

    def finalize(self, output='final', results=None):
        self.output = self.root / output
        self.controls = self.root / (output + '-admission')
        self.result = publication.finalize(self.preparation, self.plan, self.identity,
            self.results if results is None else results, self.controls,
            {'GITHUB_STEP_SUMMARY': str(self.root / 'summary.md')})
        # Direct writer/legacy-reader tests use the materialized directory the
        # transport creates. The uploaded admission itself remains three files.
        self.output.mkdir()
        for path in self.controls.iterdir():
            (self.output / path.name).write_bytes(path.read_bytes())
        proof = json.loads((self.controls / publication.PROOF).read_bytes())
        for row in proof['outcomes']:
            if row['status'] == 'success':
                name = filename(proof['version'], row)
                (self.output / name).write_bytes((self.release / row['source'] / name).read_bytes())
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
        controls = {'publication-work/control/' + publication.PROOF: self.receipt,
                    'publication-work/release/checksums.txt': (self.release / 'checksums.txt').read_bytes(),
                    'matrix-input/plan.json': canonical(self.plan)}
        add(700001, 'publication-controls-900001-1', controls)
        add(700002, 'verified-publication-900001-1', self.prepared_entries())
        if final:
            add(700003, 'native-admitted-publication-900001-2', {name: (self.output / name).read_bytes()
                for name in (publication.PROOF, publication.NATIVE_FILE, 'checksums.txt')})
            receipt_sha = self.result['receipt_sha256']; job_name = 'publication_acceptance'; attempt = 2
        else:
            receipt_sha = self.preparation['receipt_sha256']; job_name = 'publication_prepare'; attempt = 1
        job = {'id': 600001, 'name': job_name, 'run_id': 900001, 'head_sha': 'a' * 40,
               'run_attempt': attempt, 'status': 'completed', 'conclusion': 'success'}
        log = 'VERIFIED_RELEASE_RECEIPT_SHA256=' + receipt_sha + '\n'
        if final:
            log += 'NATIVE_ACCEPTANCE_SHA256=' + self.result['native_acceptance_sha256'] + '\n'
        for artifact in (artifacts[-1:] if final else artifacts):
            log += 'SHA256 digest of uploaded artifact zip is ' + artifact['digest'][7:] + '\n'
            log += 'Artifact ' + artifact['name'] + '.zip successfully finalized. Artifact ID ' + str(artifact['id']) + '\n'
        state = {'run': run, 'job': job, 'artifacts': artifacts, 'log': log, 'blobs': blobs, 'calls': [], 'downloads': []}
        def api(*args):
            endpoint = args[-1]; state['calls'].append(endpoint)
            if endpoint.endswith('/runs/900001'): return json.dumps(state['run'])
            if '/jobs?filter=all' in endpoint:
                jobs = state.get('jobs', [state['job']])
                return json.dumps({'total_count': len(jobs), 'jobs': jobs})
            if '/artifacts?' in endpoint: return json.dumps({'total_count': len(state['artifacts']), 'artifacts': state['artifacts']})
            if '/actions/artifacts/' in endpoint:
                return json.dumps(next(a for a in state['artifacts'] if a['id'] == int(endpoint.rsplit('/', 1)[1])))
            if endpoint.endswith('/jobs/600001/logs'): return state['log']
            raise AssertionError(endpoint)
        def download(artifact, path):
            state['downloads'].append(artifact['id'])
            path.write_bytes(state['blobs'][artifact['id']])
        client = SimpleNamespace(repo=identity['repository'], tag=identity['tag'], run=api, download_zip=download)
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

    def test_admission_upload_is_only_three_controls_with_original_zip_binding(self):
        fixture = Fixture(self.root); fixture.finalize()
        self.assertEqual({p.name for p in fixture.controls.iterdir()},
                         {publication.PROOF, publication.NATIVE_FILE, 'checksums.txt'})
        self.assertEqual(fixture.result['upload_directory'], str(fixture.controls.resolve()))
        proof, preparation = publication.verify_admission_controls(fixture.controls, fixture.plan, fixture.identity,
            fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'])
        self.assertEqual(preparation['_artifact'], fixture.preparation['_artifact'])
        self.assertEqual(publication.preparation_receipt(preparation), fixture.proof)
        self.assertEqual(preparation['_receipt_text'].encode(), fixture.receipt)
        self.assertEqual(proof['preparation_receipt_sha256'], facts(fixture.receipt)['sha256'])

    def test_production_acceptance_downloads_only_controls_and_never_release_payloads(self):
        fixture = Fixture(self.root, {'official': ['amd64']}); client, state = fixture.transport()
        output = self.root / 'admission'
        args = SimpleNamespace(version=fixture.plan['version'], tag=fixture.plan['tag'], artifact_id='700002',
            controls_artifact_id='700001', receipt_sha256=fixture.preparation['receipt_sha256'], output=str(output))
        with patch.object(publication, 'workflow_identity', return_value=(fixture.identity, fixture.plan)), \
             patch.object(publication.native, 'collect', return_value=fixture.results), \
             patch('sys.stdout', new=io.StringIO()):
            result = publication.acceptance(args, {'RUNNER_TEMP': str(self.root)}, client)
        self.assertEqual(state['downloads'], [700001])
        self.assertEqual({p.name for p in output.iterdir()}, {publication.PROOF, publication.NATIVE_FILE, 'checksums.txt'})
        self.assertTrue(result['all_requested_passed'])
        self.assertFalse(any('/releases' in call for call in state['calls']))

    def test_writer_downloads_original_zip_and_materializes_only_admitted_archives(self):
        fixture = Fixture(self.root); del fixture.results['upgrade:official:amd64']; fixture.finalize()
        client, state = fixture.transport(final=True)
        work = self.root / 'writer'; work.mkdir()
        directory = publication.authenticate_final_artifact(client, fixture.identity, 700003,
            fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], work, plan=fixture.plan)
        proof = publication.verify_final_directory(directory, fixture.plan, fixture.identity,
            fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'])
        self.assertEqual(state['downloads'], [700003, 700002])
        failed = filename(fixture.plan['version'], fixture.rows[0])
        self.assertNotIn(failed, proof['files'])
        self.assertFalse((directory / failed).exists())
        self.assertFalse((work / 'prepared/release/official' / failed).exists())
        self.assertEqual({p.name for p in directory.iterdir()}, set(proof['files']) | {publication.PROOF})

    def test_prepared_zip_accepts_base_or_complete_legacy_companions_without_extracting_them(self):
        for arches in ([], ['amd64'], ['amd64', 'arm64']):
            for legacy in (False, True):
                with self.subTest(arches=arches, legacy=legacy), tempfile.TemporaryDirectory(dir=self.root) as td:
                    matrix = {'official': ['amd64']}
                    if arches: matrix['enterprise-docker'] = arches
                    fixture = Fixture(Path(td), matrix)
                    entries = fixture.prepared_entries()
                    self.assertFalse(any(name.startswith('verification/') for name in entries))
                    if legacy: entries.update(fixture.companions)
                    fixture.prepared_entries = lambda: entries
                    fixture.refresh_preparation(); fixture.finalize()
                    client, state = fixture.transport(final=True)
                    work = Path(td) / 'writer'; work.mkdir()
                    directory = publication.authenticate_final_artifact(client, fixture.identity, 700003,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], work, plan=fixture.plan)
                    proof = publication.verify_final_directory(directory, fixture.plan, fixture.identity,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'])
                    self.assertEqual({p.name for p in directory.iterdir()}, set(proof['files']) | {publication.PROOF})
                    self.assertFalse((work / 'prepared/verification').exists())
                    self.assertEqual(state['downloads'], [700003, 700002])

    def test_prepared_zip_rejects_partial_extra_duplicate_unsafe_or_wrong_size_legacy_companions(self):
        for fault in ('partial', 'unknown', 'duplicate', 'symlink', 'oversize', 'undersize'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.root) as td:
                fixture = Fixture(Path(td), {'official': ['amd64'], 'enterprise-docker': ['amd64', 'arm64']})
                entries = dict(fixture.prepared_entries(), **fixture.companions)
                name = next(iter(fixture.companions)); raw = entries[name]
                if fault == 'partial': del entries[name]
                if fault == 'unknown': entries['verification/enterprise-original/unknown.tar.gz'] = b'bad'
                if fault == 'duplicate': entries[zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))] = raw
                if fault == 'symlink':
                    del entries[name]
                    link = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)); link.external_attr = (stat.S_IFLNK | 0o777) << 16
                    entries[link] = raw
                if fault == 'oversize': entries[name] += b'bad'
                if fault == 'undersize': entries[name] = raw[:-1]
                fixture.prepared_entries = lambda: entries
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    fixture.refresh_preparation(); fixture.finalize(); client, _ = fixture.transport(final=True)
                work = Path(td) / 'writer'; work.mkdir()
                with self.assertRaises(ValueError):
                    publication.authenticate_final_artifact(client, fixture.identity, 700003,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], work, plan=fixture.plan)
                self.assertFalse((work / 'prepared/verification').exists())

    def test_failed_package_row_does_not_authorize_legacy_companion(self):
        for extra_failed_companion in (False, True):
            with self.subTest(extra_failed_companion=extra_failed_companion), tempfile.TemporaryDirectory(dir=self.root) as td:
                fixture = Fixture(Path(td), {'official': ['amd64'], 'enterprise-docker': ['amd64', 'arm64']})
                failed = next(row for row in fixture.proof['outcomes'] if row['key'] == 'enterprise-docker-arm64')
                failed.update(status='failure', reason='Synthetic package failure')
                name = filename(fixture.plan['version'], failed)
                del fixture.files[name]; del fixture.bodies['release/enterprise-docker/' + name]
                sums = ''.join(fixture.files[n]['sha256'] + '  ' + n + '\n'
                               for n in sorted(fixture.files) if n.endswith('.tar.gz')).encode()
                fixture.files['checksums.txt'] = facts(sums); (fixture.release / 'checksums.txt').write_bytes(sums)
                fixture.results = {k: r for k, r in fixture.results.items() if ':enterprise-docker:arm64:' not in k}
                fixture.refresh_preparation()
                for record in fixture.results.values(): record['receipt_sha256'] = fixture.preparation['receipt_sha256']
                entries = fixture.prepared_entries()
                entries.update({n: raw for n, raw in fixture.companions.items()
                                if extra_failed_companion or n.endswith('-amd64.tar.gz')})
                fixture.prepared_entries = lambda: entries
                fixture.refresh_preparation(); fixture.finalize()
                client, _ = fixture.transport(final=True); work = Path(td) / 'writer'; work.mkdir()
                def authenticate():
                    return publication.authenticate_final_artifact(client, fixture.identity, 700003,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], work, plan=fixture.plan)
                if extra_failed_companion:
                    with self.assertRaises(ValueError): authenticate()
                else:
                    directory = authenticate()
                    proof = publication.verify_final_directory(directory, fixture.plan, fixture.identity,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'])
                    self.assertNotIn(name, proof['files'])
                    self.assertFalse((work / 'prepared/verification').exists())

    def test_small_admission_cannot_drop_or_change_prepared_zip_binding(self):
        for fault in ('missing-pin', 'missing-format', 'both-missing', 'id', 'name', 'size', 'digest', 'extra'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.root) as td:
                fixture = Fixture(Path(td), {'official': ['amd64']}); fixture.finalize()
                subject = json.loads((fixture.output / publication.NATIVE_FILE).read_bytes())
                if fault in ('missing-pin', 'both-missing'): subject.pop('preparation_artifact')
                if fault in ('missing-format', 'both-missing'): subject.pop('publication_format')
                if fault == 'id': subject['preparation_artifact']['id'] = 700001
                if fault == 'name': subject['preparation_artifact']['name'] = 'verified-publication-900001-2'
                if fault == 'size': subject['preparation_artifact']['size_in_bytes'] += 1
                if fault == 'digest': subject['preparation_artifact']['digest'] = 'sha256:' + 'd' * 64
                if fault == 'extra': subject['preparation_artifact']['trusted'] = True
                replace_subject(fixture, subject)
                client, state = fixture.transport(final=True)
                work = Path(td) / 'writer'; work.mkdir()
                with self.assertRaises(ValueError):
                    publication.authenticate_final_artifact(client, fixture.identity, 700003,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], work, plan=fixture.plan)
                self.assertNotIn(700002, state['downloads'])

    def test_payload_hash_is_checked_before_any_release_mutation(self):
        fixture = Fixture(self.root, {'official': ['amd64']})
        name = filename(fixture.plan['version'], fixture.rows[0])
        key = 'release/official/' + name
        fixture.bodies[key] = b'X' * len(fixture.bodies[key])
        fixture.refresh_preparation(); fixture.finalize()
        client, _ = fixture.transport(final=True)
        work = self.root / 'writer'; work.mkdir()
        directory = publication.authenticate_final_artifact(client, fixture.identity, 700003,
            fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], work, plan=fixture.plan)
        writer = self.draft_client(fixture)
        with self.assertRaisesRegex(ValueError, 'file differs'):
            publication.publish_draft(writer, directory, fixture.plan, fixture.identity,
                fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], self.root / 'journal.json')
        self.assertEqual(writer.calls, [])

    def test_writer_rejects_changed_prepared_zip_and_original_controls(self):
        for fault in ('zip', 'receipt', 'plan', 'checksums', 'extra-member'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.root) as td:
                fixture = Fixture(Path(td), {'official': ['amd64']})
                entries = fixture.prepared_entries()
                if fault in ('receipt', 'plan', 'checksums'):
                    key = {'receipt': 'control/' + publication.PROOF, 'plan': 'control/plan.json',
                           'checksums': 'release/checksums.txt'}[fault]
                    entries[key] = b'X' + entries[key][1:]
                if fault == 'extra-member': entries['unexpected'] = b'bad'
                fixture.prepared_entries = lambda: entries
                fixture.refresh_preparation(); fixture.finalize()
                client, state = fixture.transport(final=True)
                if fault == 'zip': state['blobs'][700002] += b'changed'
                work = Path(td) / 'writer'; work.mkdir()
                with self.assertRaises(ValueError):
                    publication.authenticate_final_artifact(client, fixture.identity, 700003,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], work, plan=fixture.plan)
                self.assertEqual(state['downloads'], [700003, 700002])

    def test_legacy_full_bundle_is_supported_but_new_format_cannot_use_full_bundle(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory(dir=self.root) as td:
                fixture = Fixture(Path(td), {'official': ['amd64']}); fixture.finalize()
                if legacy:
                    subject = json.loads((fixture.output / publication.NATIVE_FILE).read_bytes())
                    subject.pop('preparation_artifact'); subject.pop('publication_format')
                    replace_subject(fixture, subject)
                client, state = fixture.transport(final=True)
                replace_artifact(state, 700003, {p.name: p.read_bytes() for p in fixture.output.iterdir()})
                work = Path(td) / 'writer'; work.mkdir()
                def authenticate():
                    return publication.authenticate_final_artifact(client, fixture.identity, 700003,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], work, plan=fixture.plan)
                if legacy:
                    directory = authenticate()
                    self.assertTrue(publication.verify_final_directory(directory, fixture.plan, fixture.identity,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256']))
                    self.assertEqual(state['downloads'], [700003])
                else:
                    with self.assertRaisesRegex(ValueError, 'controls only'): authenticate()

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

    def test_preparation_authenticates_controls_and_zip_pin_without_downloading_payloads(self):
        fixture = Fixture(self.root)
        companion = filename(fixture.plan['version'], {'source': 'enterprise-original', 'arch': 'amd64'})
        fixture.plan['resolved']['inventory']['enterprise'] = {'archives': {'amd64': fixture.files[companion]}}
        fixture.proof['plan_sha256'] = object_digest(fixture.plan); fixture.refresh_preparation()
        client, state = fixture.transport()
        work = self.root / 'transport'; work.mkdir()
        preparation = publication.authenticate_preparation(client, fixture.identity, fixture.plan,
            700001, 700002, fixture.preparation['receipt_sha256'], work)
        self.assertEqual(preparation['_receipt_text'].encode(), fixture.receipt)
        self.assertEqual(preparation['_artifact'], fixture.preparation['_artifact'])
        self.assertEqual(state['downloads'], [700001])
        self.assertFalse((work / 'preparation.zip').exists())
        self.assertGreaterEqual(state['calls'].count('repos/' + client.repo + '/actions/artifacts/700002'), 2)

    def test_preparation_cannot_use_wrong_upload_job_run_or_plan(self):
        for fault in ('upload', 'job', 'run', 'plan', 'duplicate','runtime-plan'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.root) as temp:
                fixture = Fixture(Path(temp), {'official': ['amd64']}); client, state = fixture.transport()
                if fault == 'upload': state['log'] = state['log'].replace('Artifact ID 700002', 'Artifact ID 700009')
                if fault == 'job': state['job']['head_sha'] = 'f'*40
                if fault == 'run': state['run']['head_sha'] = 'f' * 40
                if fault == 'plan': fixture.plan['tag'] += '-changed'
                if fault == 'duplicate': state['artifacts'].append(state['artifacts'][0])
                if fault == 'runtime-plan': fixture.identity.pop('runtime_plan_sha256')
                work = Path(temp) / 'transport'; work.mkdir()
                with self.assertRaises(ValueError):
                    publication.authenticate_preparation(client, fixture.identity, fixture.plan,
                        700001, 700002, fixture.preparation['receipt_sha256'], work)

    def test_final_artifact_authenticates_both_exact_markers_and_successful_attempt(self):
        for fault in (None, 'native-marker', 'duplicate-marker', 'receipt-marker', 'attempt', 'job-head', 'zip','runtime-plan'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.root) as temp:
                fixture = Fixture(Path(temp)); fixture.finalize()
                if fault == 'attempt':
                    path = fixture.output / publication.PROOF
                    proof = json.loads(path.read_text()); proof['workflow_run_attempt'] = 1
                    path.write_bytes(canonical(proof))
                    fixture.result['receipt_sha256'] = facts(path.read_bytes())['sha256']
                client, state = fixture.transport(final=True)
                if fault == 'native-marker': state['log'] = state['log'].replace('NATIVE_ACCEPTANCE_SHA256=', 'OTHER=')
                if fault == 'duplicate-marker': state['log'] += 'NATIVE_ACCEPTANCE_SHA256=' + fixture.result['native_acceptance_sha256'] + '\n'
                if fault == 'receipt-marker': state['log'] = state['log'].replace(fixture.result['receipt_sha256'], 'e' * 64)
                if fault == 'job-head': state['job']['head_sha'] = 'f'*40
                if fault == 'zip': state['blobs'][700003] += b'changed'
                if fault == 'runtime-plan': fixture.identity.pop('runtime_plan_sha256')
                work = Path(temp) / 'writer'; work.mkdir()
                def verify():
                    return publication.authenticate_final_artifact(client, fixture.identity, 700003,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], work, plan=fixture.plan)
                if fault:
                    with self.assertRaises(ValueError): verify()
                else:
                    self.assertEqual({p.name for p in verify().iterdir()}, {p.name for p in fixture.output.iterdir()})

    def test_same_run_preparation_and_acceptance_use_finalized_uploads_with_stale_api_status(self):
        for final in (False,True):
            with self.subTest(final=final),tempfile.TemporaryDirectory(dir=self.root) as temp:
                fixture=Fixture(Path(temp),{'official':['amd64']})
                if final:fixture.finalize()
                client,state=fixture.transport(final=final)
                state['job'].update(status='queued',conclusion=None)
                work=Path(temp)/'transport';work.mkdir()
                if final:
                    directory=publication.authenticate_final_artifact(client,fixture.identity,700003,
                        fixture.result['receipt_sha256'],fixture.result['native_acceptance_sha256'],work,plan=fixture.plan)
                    self.assertTrue((directory/publication.PROOF).is_file())
                else:
                    preparation=publication.authenticate_preparation(client,fixture.identity,fixture.plan,
                        700001,700002,fixture.preparation['receipt_sha256'],work)
                    self.assertEqual(preparation['receipt_sha256'],fixture.preparation['receipt_sha256'])

    def test_writer_only_retry_authenticates_original_acceptance_without_using_superseded_results(self):
        for fault in ('copied-success', 'newer-failure'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.root) as temp:
                fixture = Fixture(Path(temp), {'official': ['amd64']})
                fixture.finalize(); client, state = fixture.transport(final=True)
                fixture.identity['run_attempt'] = state['run']['run_attempt'] = 3
                original = state['job']
                original.update(created_at='2026-10-12T02:00:00Z', started_at='2026-10-12T02:00:01Z',
                    completed_at='2026-10-12T02:10:00Z', runner_id=123, runner_name='runner',
                    runner_group_id=0, runner_group_name='GitHub Actions', labels=['ubuntu-24.04'],
                    steps=[{'name': 'Upload', 'conclusion': 'success'}])
                newer = dict(copy.deepcopy(original), id=600002, run_attempt=3,
                    created_at='2026-10-12T07:20:00Z', runner_group_id=None)
                if fault == 'newer-failure':
                    newer.update(started_at='2026-10-12T07:20:01Z', completed_at='2026-10-12T07:21:00Z',
                        conclusion='failure')
                state['jobs'] = [original, newer]
                work = Path(temp) / 'writer'; work.mkdir()
                def authenticate():
                    return publication.authenticate_final_artifact(client, fixture.identity, 700003,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], work, plan=fixture.plan)
                if fault == 'copied-success':
                    self.assertEqual({p.name for p in authenticate().iterdir()}, {p.name for p in fixture.output.iterdir()})
                else:
                    with self.assertRaises(ValueError): authenticate()

    def test_writer_retry_keeps_current_operation_and_uses_authenticated_acceptance_attempt(self):
        fixture = Fixture(self.root, {'official': ['amd64']}); fixture.finalize()
        identity = dict(fixture.identity, run_attempt=3)
        client = self.draft_client(fixture)
        args = SimpleNamespace(version=fixture.plan['version'], tag=fixture.plan['tag'], repository=client.repo,
            artifact_id='700003', receipt_sha256=fixture.result['receipt_sha256'],
            native_acceptance_sha256=fixture.result['native_acceptance_sha256'], journal=str(self.root / 'journal.json'))
        env = {'PUBLICATION_OPERATION': 'build', 'RUNNER_TEMP': str(self.root), 'GITHUB_EVENT_NAME': 'workflow_dispatch'}
        with patch.object(publication, 'workflow_identity', return_value=(identity, fixture.plan)), \
             patch.object(publication, 'authenticate_final_artifact', return_value=fixture.output):
            self.assertEqual(publication.publish(args, env, client)['phase'], 'complete')
        self.assertEqual({call[1] for call in client.calls if call[0] == 'upload'},
            {filename(fixture.plan['version'], fixture.rows[0]) + suffix for suffix in ('', '.sha256')})

    def test_repair_requires_confirmation_for_retired_name_before_any_upload(self):
        from test_release_repair import FakeGitHub
        fixture = Fixture(self.root); del fixture.results['upgrade:official:amd64']; fixture.finalize()
        client = FakeGitHub(); client.repo = fixture.identity['repository']; client.tag = fixture.identity['tag']
        release = client.release
        client.release = lambda: dict(release(), id=800001, tag_name=client.tag, draft=False)
        failed = filename(fixture.plan['version'], fixture.rows[0])
        client.assets = {1: (failed, b'synthetic retired old archive')}
        journal = self.root / 'journal.json'
        with self.assertRaisesRegex(ValueError, 'deletion confirmation'):
            publication.repair_existing(client, fixture.output, fixture.plan, fixture.identity,
                fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], journal)
        state = json.loads(journal.read_text())
        self.assertEqual(state['phase'], 'confirmation_required')
        self.assertEqual(state['conflicts'][0]['asset_id'], 1)
        self.assertEqual(client.assets[1], (failed, b'synthetic retired old archive'))
        self.assertEqual(client.calls, [])
        del client.assets[1]  # External removal after separate action-time confirmation.
        state = publication.repair_existing(client, fixture.output, fixture.plan, fixture.identity,
            fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], self.root / 'approved-rerun.json')
        self.assertEqual(state['phase'], 'complete'); self.assertEqual(client.body, 'old release notes')
        self.assertTrue([c for c in client.calls if c[0] == 'upload'][-1][1].endswith('.tar.gz.sha256'))
        self.assertFalse(any('.backup-' in name or '.staged-' in name for name, _ in client.assets.values()))

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
             patch.object(publication, 'authenticate_preparation', return_value=fixture.preparation), \
             patch.object(native, 'collect', return_value=fixture.results), \
             patch.object(publication, 'verify_admission_controls', side_effect=ValueError('bad final bytes')), \
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
        release = {'id': 800001, 'draft': False, 'assets': [{'name': publication.PROOF}]}
        client = SimpleNamespace(repo=fixture.identity['repository'], release=lambda: release,
            download=lambda name, directory: (directory / name).write_bytes((fixture.output / name).read_bytes()))
        with patch('native_upgrade_input.public_release', return_value=release), \
             patch('native_upgrade_input.verify_public_acceptance', return_value={'receipt': proof}):
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
                self.assets = {}
                self.draft = True; self.release_id = 800001; self.remote_tag = self.tag
                self.prerelease = fixture.plan['mode'] != 'stable'

            def release(self):
                return dict(super().release(), id=self.release_id, tag_name=self.remote_tag,
                            draft=self.draft, prerelease=self.prerelease)

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
        self.assertEqual(state['release_state'], 'public')
        self.assertEqual(json.loads((self.root / 'journal.json').read_text()), state)

    def test_existing_draft_requires_matching_channel_before_any_upload(self):
        for mode, version in (('stable', 'v2.101.0'), ('beta', 'v2.101.0-beta.1')):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=self.root) as temp:
                fixture = Fixture(Path(temp), version=version, mode=mode); fixture.finalize()
                client = self.draft_client(fixture); client.prerelease = not client.prerelease
                with self.assertRaisesRegex(ValueError, 'exact identified draft'):
                    publication.publish_draft(client, fixture.output, fixture.plan, fixture.identity,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], Path(temp) / 'journal.json')
                self.assertEqual(client.calls, [])
                self.assertEqual(client.assets, {})
                self.assertTrue(client.draft)
                client.prerelease = mode != 'stable'
                state = publication.publish_draft(client, fixture.output, fixture.plan, fixture.identity,
                    fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], Path(temp) / 'journal.json')
                self.assertEqual(state['phase'], 'complete')
                self.assertEqual(client.prerelease, mode != 'stable')

    def test_draft_channel_change_during_upload_blocks_publication(self):
        fixture = Fixture(self.root); fixture.finalize(); client = self.draft_client(fixture)
        original_upload = client.upload
        def upload(path):
            result = original_upload(path)
            client.prerelease = True
            return result
        journal = self.root / 'journal.json'
        with patch.object(client, 'upload', side_effect=upload), self.assertRaisesRegex(ValueError, 'Draft identity changed'):
            publication.publish_draft(client, fixture.output, fixture.plan, fixture.identity,
                fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], journal)
        self.assertTrue(client.draft)
        self.assertFalse(any('draft=false' in call for call in client.calls))
        self.assertEqual(json.loads(journal.read_text())['phase'], 'failed')

    def test_final_publication_failure_records_observed_state_without_claiming_success(self):
        for applied, readable in ((False, True), (True, True), (True, False)):
            with self.subTest(applied=applied, readable=readable), tempfile.TemporaryDirectory(dir=self.root) as temp:
                fixture = Fixture(Path(temp)); fixture.finalize(); client = self.draft_client(fixture)
                journal = Path(temp) / 'journal.json'
                original_run, original_release = client.run, client.release
                attempted = False
                def release():
                    if attempted and not readable:
                        raise subprocess.CalledProcessError(1, ['gh', 'api'], stderr='HTTP 404')
                    return original_release()
                def run(*args):
                    nonlocal attempted
                    if 'draft=false' in args:
                        self.assertEqual(json.loads(journal.read_text())['phase'], 'publishing')
                        attempted = True
                        if applied:original_run(*args)
                        raise subprocess.CalledProcessError(1, args, stderr='HTTP 503 private-response-content')
                    return original_run(*args)
                with patch.object(client, 'run', side_effect=run), patch.object(client, 'release', side_effect=release), \
                        self.assertRaises(subprocess.CalledProcessError):
                    publication.publish_draft(client, fixture.output, fixture.plan, fixture.identity,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], journal)
                state = json.loads(journal.read_text())
                self.assertEqual(state['phase'], 'failed')
                self.assertEqual(state['failure_stage'], 'release-publication')
                self.assertEqual(state['failure_code'], 'RELEASE_PUBLISH_FAILED')
                self.assertEqual(state['release_state'], 'unknown' if not readable else 'public' if applied else 'draft')
                self.assertEqual(client.draft, not applied)
                self.assertEqual(len(client.assets), len(fixture.rows) * 2)
                self.assertNotIn('private-response-content', journal.read_text())
                self.assertFalse(any('draft=true' in call or 'DELETE' in call for call in client.calls))

    def test_final_readback_must_verify_public_channel_and_accepted_assets(self):
        for fault in ('unavailable', 'channel', 'missing-asset', 'changed-bytes'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.root) as temp:
                fixture = Fixture(Path(temp)); fixture.finalize(); client = self.draft_client(fixture)
                journal = Path(temp) / 'journal.json'
                original_run, original_release = client.run, client.release
                def run(*args):
                    result = original_run(*args)
                    if 'draft=false' in args:
                        if fault == 'channel':client.prerelease = True
                        if fault in ('missing-asset', 'changed-bytes'):
                            identifier = next(i for i, (n, _) in client.assets.items() if n.endswith('.tar.gz'))
                            if fault == 'missing-asset':del client.assets[identifier]
                            else:client.assets[identifier] = (client.assets[identifier][0], b'changed')
                    return result
                def release():
                    if not client.draft and fault == 'unavailable':raise OSError('unavailable readback')
                    return original_release()
                with patch.object(client, 'run', side_effect=run), patch.object(client, 'release', side_effect=release), \
                        self.assertRaises((OSError, ValueError)):
                    publication.publish_draft(client, fixture.output, fixture.plan, fixture.identity,
                        fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], journal)
                state = json.loads(journal.read_text())
                self.assertEqual(state['phase'], 'failed')
                self.assertEqual(state['failure_stage'], 'public-verification')
                self.assertEqual(state['failure_code'], 'PUBLIC_READBACK_FAILED')
                self.assertEqual(state['release_state'], 'unknown' if fault in ('unavailable', 'channel') else 'public')
                self.assertFalse(client.draft)
                self.assertFalse(any('draft=true' in call or 'DELETE' in call for call in client.calls))

    def test_normal_public_build_publishes_safe_product_and_retains_conflicting_sibling(self):
        for failed_sibling in (False,True):
            with self.subTest(failed_sibling=failed_sibling),tempfile.TemporaryDirectory(dir=self.root) as temporary:
                fixture=Fixture(Path(temporary),{'official':['amd64'],'custom':['arm64']})
                if failed_sibling:
                    for key in list(fixture.results):
                        if ':custom:' in key:del fixture.results[key]
                fixture.finalize();client=self.draft_client(fixture);client.draft=False
                sibling=filename(fixture.plan['version'],{'source':'custom','arch':'arm64'})
                client.assets={1:(sibling,b'old conflicting sibling')}
                args=SimpleNamespace(version=fixture.plan['version'],tag=fixture.plan['tag'],repository=client.repo,
                    artifact_id='700003',receipt_sha256=fixture.result['receipt_sha256'],
                    native_acceptance_sha256=fixture.result['native_acceptance_sha256'],
                    journal=str(Path(temporary)/'journal.json'))
                with patch.object(publication,'workflow_identity',return_value=(fixture.identity,fixture.plan)), \
                     patch.object(publication,'authenticate_final_artifact',return_value=fixture.output), \
                     self.assertRaisesRegex(ValueError,'Conflicting products retained'):
                    publication.publish(args,{'PUBLICATION_OPERATION':'build','RUNNER_TEMP':temporary},client)
                state=json.loads(Path(args.journal).read_text())
                self.assertEqual(state['phase'],'partial');self.assertEqual(client._release_id,800001)
                self.assertEqual(client.assets[1],(sibling,b'old conflicting sibling'))
                safe=filename(fixture.plan['version'],{'source':'official','arch':'amd64'})
                self.assertEqual({call[1] for call in client.calls if call[0]=='upload'},{safe,safe+'.sha256'})
                self.assertEqual(state['blocked_products'],[sibling])

    def test_partial_public_retry_writes_only_requested_pair_and_preserves_existing_ids(self):
        for disappeared in (False, True):
            with self.subTest(disappeared=disappeared), tempfile.TemporaryDirectory(dir=self.root) as temp:
                fixture = Fixture(Path(temp), {'custom': ['arm64']})
                fixture.plan['tag'] = fixture.identity['tag'] = fixture.proof['release_tag'] = 'v2.101.0-offline-r1'
                matrix = {'custom': ['arm64']} if disappeared else {'official': ['amd64'], 'custom': ['arm64']}
                fixture.plan['resolved']['inventory'].update(matrix=matrix, rows=products(matrix))
                fixture.proof['plan_sha256'] = object_digest(fixture.plan)
                fixture.refresh_preparation()
                for result in fixture.results.values():
                    result.update(plan_sha256=fixture.proof['plan_sha256'],
                                  receipt_sha256=fixture.preparation['receipt_sha256'])
                fixture.finalize()
                client = self.draft_client(fixture); client.draft = False
                official = filename(fixture.plan['version'], {
                    'source': 'enterprise-original' if disappeared else 'official', 'arch': 'amd64'})
                old = {70: (official, b'published official'), 71: (official + '.sha256', b'published checksum')}
                client.assets = copy.deepcopy(old)
                result = publication.repair_existing(client, fixture.output, fixture.plan, fixture.identity,
                    fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'],
                    Path(temp) / 'journal.json', isolate_products=True)
                self.assertEqual(result['phase'], 'complete')
                custom = filename(fixture.plan['version'], fixture.rows[0])
                self.assertEqual({call[1] for call in client.calls if call[0] == 'upload'}, {custom, custom + '.sha256'})
                self.assertEqual({identity: client.assets[identity] for identity in old}, old)

    def test_public_retry_preserves_orphan_canonical_backup(self):
        fixture = Fixture(self.root, {'custom': ['arm64']}); fixture.finalize()
        client = self.draft_client(fixture); client.draft = False
        archived = filename(fixture.plan['version'], {'source': 'enterprise-original', 'arch': 'amd64'})
        old = (archived + '.sha256.backup-0123456789ab', b'legacy backup')
        client.assets = {70: old}
        result = publication.repair_existing(client, fixture.output, fixture.plan, fixture.identity,
            fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'],
            self.root / 'journal.json', isolate_products=True)
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(client.assets[70], old)
        custom = filename(fixture.plan['version'], fixture.rows[0])
        self.assertEqual({call[1] for call in client.calls if call[0] == 'upload'}, {custom, custom + '.sha256'})

    def test_conflicted_draft_keeps_safe_uploads_unpublished(self):
        fixture=Fixture(self.root,{'official':['amd64'],'custom':['arm64']});fixture.finalize()
        client=self.draft_client(fixture)
        sibling=filename(fixture.plan['version'],{'source':'custom','arch':'arm64'})
        client.assets={1:(sibling,b'old conflicting sibling')}
        journal=self.root/'journal.json'
        with self.assertRaisesRegex(ValueError,'Conflicting products retained'):
            publication.publish_draft(client,fixture.output,fixture.plan,fixture.identity,
                fixture.result['receipt_sha256'],fixture.result['native_acceptance_sha256'],journal,isolate_products=True)
        self.assertEqual(json.loads(journal.read_text())['phase'],'partial')
        self.assertTrue(client.draft);self.assertFalse(any('draft=false' in call for call in client.calls))
        self.assertEqual(client.assets[1],(sibling,b'old conflicting sibling'))
        safe=filename(fixture.plan['version'],{'source':'official','arch':'amd64'})
        self.assertEqual({call[1] for call in client.calls if call[0]=='upload'},{safe,safe+'.sha256'})

    def test_deleted_bound_draft_cannot_create_a_replacement(self):
        from release_asset_repair import GitHub
        client=GitHub('HandSonic/1Panel-offline-installer-V2','v2.99.0',allow_drafts=True);client.pin_release(9)
        identity={'repository':client.repo,'tag':client.tag,'version':client.tag,'head_sha':'a'*40}
        calls=[]
        def run(*args):
            calls.append(args)
            if '--method' in args:raise AssertionError('Bound 404 must never write')
            raise subprocess.CalledProcessError(1,args,stderr='HTTP 404')
        rows=products({'official':['amd64']})
        plan={'mode':'stable','rows':rows,'native_rows':[],
              'resolved':{'inventory':{'matrix':{'official':['amd64']},'rows':rows,'native_rows':[]}}}
        with patch.object(client,'run',side_effect=run),patch.object(publication,'verify_final_directory',return_value={}):
            with self.assertRaises(publication.PublicationFailure) as caught:
                publication.publish_draft(client,self.root,plan,identity,'a'*64,'b'*64,self.root/'journal.json')
        self.assertEqual(caught.exception.publication_error_code,'BOUND_RELEASE_UNAVAILABLE')
        self.assertEqual(calls,[('api',f'repos/{client.repo}/releases/9')])
        self.assertFalse((self.root/'journal.json').exists())

    def test_failure_diagnostics_include_safe_stage_code_without_original_error_output(self):
        for stage,code in (('final-artifact-authentication','FINAL_ARTIFACT_INVALID'),('draft-creation','DRAFT_CREATE_FAILED')):
            with self.subTest(stage=stage):
                output=io.StringIO()
                def fail():
                    raise subprocess.CalledProcessError(1,['gh','secret-token'],stderr='https://blob.invalid/file?sig=secret-signed-url')
                def status(*args):return publication.stage_call(stage,code,fail)
                with patch.object(publication,'status',side_effect=status),patch('sys.stderr',output):
                    self.assertEqual(publication.main(['status','--version','v2.99.0','--tag','v2.99.0','--repository','owner/repo']),1)
                diagnostic=output.getvalue()
                self.assertIn('stage='+stage,diagnostic);self.assertIn('code='+code,diagnostic)
                self.assertNotIn('secret',diagnostic);self.assertNotIn('blob.invalid',diagnostic)

    def test_new_draft_uses_post_identity_and_partial_success_publishes(self):
        from release_asset_repair import GitHub
        fixture = Fixture(self.root, {'official': ['amd64'], 'custom': ['arm64'],
                                      'enterprise-original': ['amd64']})
        # Custom lacks its required native result; the other two remain admitted.
        for key in list(fixture.results):
            if ':custom:' in key:
                del fixture.results[key]
        fixture.finalize()
        state = self.draft_client(fixture)
        state.exists = False
        calls = []

        class Transport(GitHub):
            def run(self, *args):
                calls.append(args)
                endpoint = next((arg for arg in args if arg.startswith('repos/')), '')
                if endpoint.endswith('/releases/tags/' + self.tag):
                    raise subprocess.CalledProcessError(1, args, stderr='gh: Not Found (HTTP 404)')
                if endpoint.endswith('/releases?per_page=100&page=1'):
                    return '[]'
                if args[:3] == ('api', '--method', 'POST'):
                    self.assert_not_bound = self._release_id is None
                    state.exists = True
                    return json.dumps(dict(state.release(), prerelease=False))
                if endpoint.endswith('/releases/800001/assets?per_page=100&page=1'):
                    return json.dumps(state.release()['assets'])
                if args[:3] == ('api', '--method', 'PATCH'):
                    state.draft = False
                    return '{}'
                if endpoint.endswith('/releases/800001') and state.exists:
                    return json.dumps(dict(state.release(), prerelease=False))
                raise AssertionError(args)

            def upload(self, path):
                return state.upload(path)

            def download(self, name, directory):
                state.download(name, directory)

            def download_id(self, asset_id, destination, size):
                return state.download_id(asset_id, destination, size)

        client = Transport(state.repo, state.tag, allow_drafts=True)
        result = publication.publish_draft(client, fixture.output, fixture.plan, fixture.identity,
            fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], self.root / 'journal.json')
        self.assertEqual(result['phase'], 'complete')
        self.assertFalse(state.draft)
        self.assertTrue(client.assert_not_bound)
        self.assertEqual(client._release_id, 800001)
        self.assertEqual(len([c for c in calls if c[:3] == ('api', '--method', 'POST')]), 1)
        names = {name for name, _ in state.assets.values()}
        self.assertEqual(len([n for n in names if n.endswith('.tar.gz')]), 2)
        self.assertFalse(any('-custom-' in name for name in names))
        # After creation every read uses the immutable ID, even while unpublished.
        created = next(i for i, call in enumerate(calls) if call[:3] == ('api', '--method', 'POST'))
        self.assertFalse(any('/releases/tags/' in str(call) for call in calls[created + 1:]))

    def test_suffixed_draft_requires_old_failed_names_removed_before_publication(self):
        for retain_failed in (False, True):
            with self.subTest(retain_failed=retain_failed), tempfile.TemporaryDirectory(dir=self.root) as temp:
                fixture = Fixture(Path(temp), {'official': ['amd64'], 'enterprise-original': ['amd64']})
                fixture.plan['tag'] = fixture.identity['tag'] = fixture.proof['release_tag'] = 'v2.101.0-review'
                fixture.proof['plan_sha256'] = object_digest(fixture.plan)
                fixture.refresh_preparation()
                fixture.finalize(results={})
                self.assertTrue(fixture.verify())
                client = self.draft_client(fixture)
                failed = filename(fixture.plan['version'], fixture.rows[0])
                if retain_failed:
                    client.assets[2] = (failed, b'old failed product')
                if retain_failed:
                    with self.assertRaisesRegex(ValueError, 'deletion confirmation'):
                        publication.publish_draft(client, fixture.output, fixture.plan, fixture.identity,
                            fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], Path(temp) / 'conflict.json')
                    self.assertEqual(client.calls, [])
                    self.assertTrue(client.draft)
                    self.assertEqual(client.assets[2], (failed, b'old failed product'))
                    del client.assets[2]  # External removal after separate confirmation.
                state = publication.publish_draft(client, fixture.output, fixture.plan, fixture.identity,
                    fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], Path(temp) / 'journal.json')
                self.assertEqual(state['phase'], 'complete')
                self.assertFalse(client.draft)
                self.assertNotIn(failed, [name for name, _ in client.assets.values()])
                accepted = filename(fixture.plan['version'], fixture.rows[1])
                self.assertIn(accepted, [name for name, _ in client.assets.values()])
                self.assertFalse(any('.backup-' in name or '.staged-' in name for name, _ in client.assets.values()))
                self.assertTrue([call for call in client.calls if call[0] == 'upload'][-1][1].endswith('.tar.gz.sha256'))
                self.assertEqual(len([call for call in client.calls if 'draft=false' in call]), 1)
                self.assertEqual(client.body, 'old release notes')

    def test_draft_missing_extra_digest_size_or_identity_race_never_publishes(self):
        original_repair = publication.repair
        for fault in ('missing', 'extra', 'digest', 'zero-size', 'tag', 'id', 'draft', 'channel'):
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
                    if fault == 'channel': client.prerelease = True
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
