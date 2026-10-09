"""Synthetic identities/results only; no external requests or native execution."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import runtime_native_acceptance as native
from resolved_inventory import canonical


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def fixture():
    rows = [{'source': source, 'arch': arch, 'key': source + '-' + arch}
            for source, arch in [('official', 'amd64'), ('custom', 'arm64'),
                                 ('enterprise-original', 'amd64'), ('enterprise-docker', 'amd64'),
                                 ('custom', 'riscv64')]]
    prep = {'schema': 2, 'contract': 'downstream-matrix', 'repository': 'HandSonic/1Panel-offline-installer-V2',
            'version': 'v2.101.0', 'release_tag': 'v2.101.0', 'workflow_run_id': 900001,
            'workflow_commit': 'a' * 40, 'plan_sha256': sha('synthetic plan'),
            'receipt_sha256': sha('synthetic receipt'), 'controls_artifact_id': 700001,
            'requested_products': rows, 'outcomes': [dict(r, status='success', stage='package', reason='') for r in rows]}
    prep['files'] = {f'1panel-v2.101.0-{r["source"]}-offline-linux-{r["arch"]}.tar.gz':
                     {'sha256': sha('synthetic archive ' + r['key']), 'bytes': 123} for r in rows}
    prep['files']['checksums.txt'] = {'sha256': sha('synthetic sums'), 'bytes': 10}
    results = {}
    for row in rows:
        for key in native.required_results(row):
            results[key] = {'key': key, 'status': 'success', 'repository': prep['repository'],
                'run_id': prep['workflow_run_id'], 'head_sha': prep['workflow_commit'],
                'plan_sha256': prep['plan_sha256'], 'receipt_sha256': prep['receipt_sha256'],
                'controls_artifact_id': prep['controls_artifact_id'], 'job_id': 600001,
                'artifact_id': 700002, 'evidence_sha256': sha('synthetic evidence ' + key),
                'archive_sha256': prep['files'][f'1panel-v2.101.0-{row["source"]}-offline-linux-{row["arch"]}.tar.gz']['sha256']}
    return prep, results


class AdmissionTests(unittest.TestCase):
    def test_complete_success_is_explicit_about_native_and_passthrough(self):
        prep, results = fixture()
        accepted = native.admit(prep, results)
        self.assertTrue(accepted['all_requested_passed'])
        self.assertEqual(accepted['accepted_keys'], [r['key'] for r in prep['requested_products']])
        coverage = {r['key']: r['acceptance'] for r in accepted['outcomes']}
        self.assertEqual(coverage['enterprise-original-amd64'], 'vendor-byte-identity')
        self.assertEqual(coverage['enterprise-docker-amd64'], 'native-install')
        self.assertEqual(coverage['custom-riscv64'], 'payload-validation-only')

    def test_every_native_failure_is_scoped_to_its_exact_product(self):
        prep, results = fixture()
        for key in results:
            for status in ('failure', 'skipped', 'cancelled', 'missing'):
                with self.subTest(key=key, status=status):
                    changed = copy.deepcopy(results)
                    if status == 'missing': del changed[key]
                    else: changed[key]['status'] = status
                    admission = native.admit(prep, changed)
                    product = '-'.join(key.split(':')[1:3])
                    self.assertEqual(set(admission['accepted_keys']),
                                     {r['key'] for r in prep['requested_products']} - {product})
                    self.assertEqual(next(r for r in admission['outcomes'] if r['key'] == product)['stage'], 'native-acceptance')

    def test_community_upgrades_are_mandatory_and_enterprise_original_is_independent(self):
        prep, results = fixture()
        results = {k: v for k, v in results.items() if not k.startswith('upgrade:') and 'enterprise-docker' not in k}
        admission = native.admit(prep, results)
        self.assertEqual(admission['accepted_keys'], ['enterprise-original-amd64', 'custom-riscv64'])

    def test_static_failure_can_never_be_reintroduced_by_native_success(self):
        prep, results = fixture()
        prep['outcomes'][0].update(status='failure', stage='package', reason='synthetic failure')
        del prep['files']['1panel-v2.101.0-official-offline-linux-amd64.tar.gz']
        value = native.admit(prep, results)
        self.assertNotIn('official-amd64', value['accepted_keys'])
        self.assertEqual(value['outcomes'][0]['stage'], 'package')

    def test_shared_identity_controls_and_plan_fail_globally(self):
        prep, results = fixture()
        for field in ('repository', 'run_id', 'head_sha', 'plan_sha256', 'receipt_sha256', 'controls_artifact_id'):
            changed = copy.deepcopy(results)
            next(iter(changed.values()))[field] = 'wrong'
            with self.subTest(field=field), self.assertRaises(ValueError):
                native.admit(prep, changed)
        with self.assertRaises(ValueError): native.admit(prep, results, cancelled=True)

    def test_incomplete_duplicate_or_mismatched_static_coverage_is_global(self):
        for fault in ('missing', 'duplicate', 'extra', 'archive', 'requested'):
            prep, results = fixture()
            if fault == 'missing': prep['outcomes'].pop()
            if fault == 'duplicate': prep['outcomes'].append(prep['outcomes'][0])
            if fault == 'extra': prep['outcomes'][0]['key'] = 'unknown-amd64'
            if fault == 'archive': prep['files'].pop(next(iter(prep['files'])))
            if fault == 'requested': prep['requested_products'].append(prep['requested_products'][0])
            with self.subTest(fault=fault), self.assertRaises(ValueError): native.admit(prep, results)

    def test_unsigned_durable_subject_is_labeled_and_exactly_binds_admission(self):
        prep, results = fixture()
        admission = native.admit(prep, results)
        value = native.durable_subject(prep, results, admission)
        self.assertIn('unsigned', value['authentication'])
        self.assertEqual(value['native_results'], results)
        admission['accepted_keys'].pop()
        with self.assertRaises(ValueError): native.durable_subject(prep, results, admission)


class EvidenceTests(unittest.TestCase):
    def fixture(self):
        prep, _ = fixture()
        name = '1panel-v2.101.0-official-offline-linux-amd64.tar.gz'
        identity = {'repository': prep['repository'], 'run_id': 900001, 'head_sha': 'a' * 40, 'run_attempt': 2}
        candidate = {'repository': prep['repository'], 'workflow_run_id': 900001, 'workflow_commit': 'a' * 40,
                     'workflow_run_attempt': 2, 'version': prep['version'], 'release_tag': prep['release_tag'],
                     'source': 'official', 'arch': 'amd64', 'archive_sha256': prep['files'][name]['sha256'],
                     'receipt_sha256': prep['receipt_sha256'], 'plan_sha256': prep['plan_sha256'],
                     'files': {'official/' + name: prep['files'][name]},
                     'controls': {'artifact_id': prep['controls_artifact_id'], 'producer_job_id': 600010,
                        'producer_attempt': 1, 'zip_bytes': 100, 'zip_sha256': sha('synthetic controls ZIP'),
                        'name': 'publication-controls-900001-1'},
                     'shard': {'artifact_id': 700010, 'producer_job_id': 600011,
                        'producer_attempt': 1, 'zip_bytes': 100, 'zip_sha256': sha('synthetic package ZIP'),
                        'name': 'package-shard-1-official-amd64'}}
        process = {'pid': 12345, 'binary_sha256': sha('synthetic running binary')}
        result = {'schema': 1, 'status': 'passed', 'evidence_level': 'native-install', 'source': 'official',
                  'architecture': 'amd64', 'version': prep['version'], 'archive_sha256': candidate['archive_sha256'],
                  'docker_scenario': 'fresh', 'manifest_sha256': sha('synthetic manifest'), 'installer_mode': 'cli',
                  'panel_processes': {'1panel-core': process, '1panel-agent': process}, 'docker_process': process,
                  '_file_sha256': sha('synthetic result document')}
        job = {'id': 600002, 'run_attempt': 2, 'runner_id': 500001, 'runner_name': 'synthetic hosted runner',
               'labels': ['ubuntu-24.04'], 'runner_group_name': 'GitHub Actions'}
        value = {'schema': 1, 'key': 'install:official:amd64:fresh', 'repository': prep['repository'],
                 'run_id': 900001, 'run_attempt': 2, 'head_sha': 'a' * 40, 'workflow': native.WORKFLOW,
                 'runner': {'name': job['runner_name'], 'arch': 'X64', 'machine': 'x86_64', 'os': 'Linux',
                            'environment': 'github-hosted', 'label': 'ubuntu-24.04'}, 'candidate': candidate, 'result': result}
        return value, identity, job, prep

    def test_same_run_result_runner_architecture_and_scenario_must_agree(self):
        value, identity, job, prep = self.fixture()
        native.verify_evidence(value, value['key'], identity, job, prep)
        for path, changed in [(('runner', 'arch'), 'ARM64'), (('runner', 'name'), 'other'),
                              (('result', 'docker_scenario'), 'existing'), (('result', 'status'), 'failed'),
                              (('candidate', 'archive_sha256'), 'b' * 64)]:
            broken = copy.deepcopy(value); broken[path[0]][path[1]] = changed
            with self.subTest(path=path), self.assertRaises(ValueError):
                native.verify_evidence(broken, value['key'], identity, job, prep)
        for field in ('labels', 'runner_group_name', 'runner_id'):
            changed = dict(job); changed[field] = None
            with self.subTest(field=field), self.assertRaises(ValueError):
                native.verify_evidence(value, value['key'], identity, changed, prep)

    def test_shared_candidate_forgery_is_not_reclassified_as_row_failure(self):
        value, identity, job, prep = self.fixture()
        for field in ('receipt_sha256', 'plan_sha256', 'workflow_run_id', 'workflow_commit'):
            changed = copy.deepcopy(value); changed['candidate'][field] = 'wrong'
            with self.subTest(field=field), self.assertRaises(native.SharedTrustError):
                native.verify_evidence(changed, value['key'], identity, job, prep)

    def test_names_include_kind_attempt_source_architecture_and_scenario(self):
        self.assertEqual(native.artifact_name('install:official:amd64:fresh', 2), 'native-evidence-2-install-official-amd64-fresh')
        self.assertEqual(native.job_name('upgrade:custom:arm64'), 'publication_upgrade (custom, arm64)')



class CollectionTests(EvidenceTests):
    def fixture_transport(self):
        from test_native_candidate_input import zip_bytes
        value, identity, job, prep = self.fixture()
        identity['event'] = 'workflow_dispatch'
        run = {'id': identity['run_id'], 'run_attempt': identity['run_attempt'], 'head_sha': identity['head_sha'],
               'path': native.WORKFLOW, 'event': identity['event'], 'status': 'in_progress', 'conclusion': None,
               'repository': {'id': 600100, 'full_name': identity['repository']},
               'head_repository': {'id': 600100, 'full_name': identity['repository']}}
        job.update(name=native.job_name(value['key']), run_id=identity['run_id'], head_sha=identity['head_sha'],
                   status='completed', conclusion='success')
        body = canonical(value); blob = zip_bytes([(native.EVIDENCE_FILE, body)])
        artifact = {'id': 700100, 'name': native.artifact_name(value['key'], 2), 'expired': False,
                    'size_in_bytes': len(blob), 'digest': 'sha256:' + hashlib.sha256(blob).hexdigest(),
                    'workflow_run': {'id': identity['run_id'], 'head_sha': identity['head_sha'],
                                     'repository_id': 600100, 'head_repository_id': 600100}}
        state = {'run': run, 'job': job, 'artifact': artifact, 'blob': blob,
                 'log': 'VERIFIED_NATIVE_EVIDENCE_SHA256=' + hashlib.sha256(body).hexdigest() + '\n' +
                        'SHA256 digest of uploaded artifact zip is ' + artifact['digest'][7:] + '\n' +
                        'Artifact ' + artifact['name'] + '.zip successfully finalized. Artifact ID 700100\n'}
        def api(*args):
            endpoint = args[-1]
            if endpoint.endswith('/runs/900001'): return json.dumps(state['run'])
            if '/jobs?filter=all' in endpoint: return json.dumps({'total_count': 1, 'jobs': [state['job']]})
            if '/artifacts?' in endpoint: return json.dumps({'total_count': 1, 'artifacts': [state['artifact']]})
            if endpoint.endswith('/artifacts/700100'): return json.dumps(state['artifact'])
            if endpoint.endswith('/jobs/600002/logs'): return state['log']
            raise AssertionError(endpoint)
        client = SimpleNamespace(repo=identity['repository'], run=api,
                                 download_zip=lambda artifact, path: path.write_bytes(state['blob']))
        return state, client, identity, prep, value['key']

    def test_exact_log_zip_result_run_and_runner_form_authenticated_evidence(self):
        state, client, identity, prep, key = self.fixture_transport()
        with tempfile.TemporaryDirectory() as td:
            results = native.collect(client, identity, prep, td)
        self.assertEqual(results[key]['status'], 'success')
        self.assertEqual(results[key]['artifact_id'], 700100)
        self.assertEqual(results[key]['runner_id'], 500001)
        self.assertEqual(results['upgrade:official:amd64']['status'], 'failure')

    def test_tampered_zip_expiry_log_or_failed_job_suppress_only_that_result(self):
        for fault in ('zip', 'expired', 'log', 'failed-job', 'runner'):
            state, client, identity, prep, key = self.fixture_transport()
            if fault == 'zip': state['blob'] += b'X'
            if fault == 'expired': state['artifact']['expired'] = True
            if fault == 'log': state['log'] = state['log'].replace('VERIFIED_NATIVE_EVIDENCE', 'UNVERIFIED_NATIVE_EVIDENCE')
            if fault == 'failed-job': state['job']['conclusion'] = 'failure'
            if fault == 'runner': state['job']['labels'] = ['self-hosted']
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                self.assertEqual(native.collect(client, identity, prep, td)[key]['status'], 'failure')

    def test_global_changed_run_or_candidate_plan_is_not_row_suppression(self):
        state, client, identity, prep, key = self.fixture_transport()
        state['run']['head_sha'] = 'b' * 40
        with tempfile.TemporaryDirectory() as td, self.assertRaises(ValueError):
            native.collect(client, identity, prep, td)


if __name__ == '__main__':
    unittest.main()
