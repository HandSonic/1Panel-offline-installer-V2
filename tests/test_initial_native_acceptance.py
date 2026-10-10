"""Synthetic initial installation integration; never execute native services."""
import copy
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import initial_install_input as initial_input
import native_upgrade_input as binder
import native_upgrade_smoke as smoke
import runtime_native_acceptance as native
import test_runtime_native_acceptance as existing_native
from resolved_inventory import canonical, digest as object_digest
from test_initial_release_applicability import COMMIT, VERSION, release, resolve


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def raw(value):
    return canonical({key: data for key, data in value.items() if key != '_file_sha256'})


def pin(value):
    value['_file_sha256'] = sha(raw(value))
    return value


def repair_context():
    return {'repair_predecessor': {'version': 'v2.72.0', 'run_id': 900000, 'run_attempt': 1,
        'head_sha': 'a' * 40, 'controls_artifact_id': 700000,
        'receipt_sha256': 'b' * 64, 'plan_sha256': 'c' * 64}, 'repair_operation': 'repair-existing'}


def fixture():
    value, identity, job, prep = existing_native.EvidenceTests().fixture()
    value, identity, job, prep = json.loads(json.dumps([value, identity, job, prep]).replace('v2.101.0', VERSION))
    candidate = value['candidate']
    installed = value['result']
    installed.update(docker_scenario='existing', regional_edition='intl',
                     upgrade='not-tested', rollback='not-tested')
    pin(installed)
    applicability = resolve()
    proof = {'schema': 3, 'kind': initial_input.KIND, 'target': copy.deepcopy(candidate),
             'target_manifest_sha256': installed['manifest_sha256'], 'target_source_commit': None,
             'applicability': applicability, 'applicability_sha256': object_digest(applicability),
             'target_package_path': '/synthetic/target', 'required_acceptance': initial_input.REQUIRED}
    pin(proof)
    result = {'schema': 1, 'status': 'passed', 'evidence_level': 'native-initial-install',
        'version': VERSION, 'source': candidate['source'], 'architecture': candidate['arch'],
        'target_archive_sha256': candidate['archive_sha256'],
        'target_receipt_sha256': candidate['receipt_sha256'],
        'target_run_id': candidate['workflow_run_id'], 'target_run_attempt': candidate['workflow_run_attempt'],
        'target_commit': candidate['workflow_commit'], 'input_provenance_sha256': proof['_file_sha256'],
        'applicability_sha256': proof['applicability_sha256'],
        'initial_install_result_sha256': installed['_file_sha256'],
        'upgrade': initial_input.NOT_APPLICABLE, 'rollback': initial_input.NOT_APPLICABLE,
        'docker_process': copy.deepcopy(installed['docker_process']),
        'panel_processes': copy.deepcopy(installed['panel_processes']), 'regional_edition': 'intl',
        'docker_scenario': 'existing'}
    pin(result)
    value.update(key='upgrade:official:amd64', result=result, upgrade_input=proof, initial_install=installed)
    return value, identity, job, prep


def check(value):
    native.check_result(value['result'], value['candidate'], 'upgrade', 'official', 'amd64', None,
                        value.get('upgrade_input'), value.get('predecessor_install'), value.get('initial_install'))


class InitialInputAndResultTests(unittest.TestCase):
    def test_typed_input_and_result_preserve_actual_install_without_upgrade_claim(self):
        value, identity, job, prep = fixture()
        initial_input.validate_input(value['upgrade_input'], value['candidate'])
        check(value)
        native.verify_evidence(value, value['key'], identity, job, prep)
        self.assertEqual(value['result']['upgrade'], initial_input.NOT_APPLICABLE)
        self.assertEqual(value['result']['rollback'], initial_input.NOT_APPLICABLE)
        self.assertFalse(any(field.startswith('predecessor_') for field in value['result']))

    def test_input_rejects_forged_applicability_commit_and_explicit_repair_context(self):
        for fault in ('schema', 'kind', 'target', 'manifest', 'source-commit', 'applicability',
                      'applicability-hash', 'repair', 'recovery', 'relative-path', 'required'):
            value, _, _, _ = fixture(); proof = value['upgrade_input']
            if fault == 'schema': proof['schema'] = 2
            elif fault == 'kind': proof['kind'] = 'upgrade'
            elif fault == 'target': proof['target']['workflow_run_id'] = 123
            elif fault == 'manifest': proof['target_manifest_sha256'] = 'unverified'
            elif fault == 'source-commit': proof['target_source_commit'] = COMMIT
            elif fault == 'applicability':
                proof['applicability']['official_tags']['entries'] = []
                proof['applicability']['official_tags']['sha256'] = object_digest([])
                proof['applicability']['official_tags']['page_lengths'] = [0]
                proof['applicability_sha256'] = object_digest(proof['applicability'])
            elif fault == 'applicability-hash': proof['applicability_sha256'] = 'f' * 64
            elif fault == 'repair': proof.update(repair_context())
            elif fault == 'recovery': proof['read_only_recovery'] = repair_context()['repair_predecessor']
            elif fault == 'relative-path': proof['target_package_path'] = 'target'
            elif fault == 'required': proof['required_acceptance'] = 'not needed'
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                initial_input.validate_input(proof, value['candidate'])

    def test_custom_source_requires_exact_official_tag_commit(self):
        value, _, _, _ = fixture()
        candidate = value['candidate']; candidate['source'] = 'custom'
        proof = value['upgrade_input']; proof['target'] = copy.deepcopy(candidate)
        proof['target_source_commit'] = COMMIT
        initial_input.validate_input(proof, candidate)
        for commit in (None, True, 'b' * 40):
            proof['target_source_commit'] = commit
            with self.subTest(commit=commit), self.assertRaises(ValueError):
                initial_input.validate_input(proof, candidate)

    def test_initial_result_requires_actual_install_bytes_processes_and_manifest(self):
        for fault in ('missing-install', 'missing-input', 'install-status', 'install-level', 'install-archive',
                      'install-scenario', 'install-manifest', 'install-mode', 'install-byte-pin', 'input-byte-pin',
                      'panel-process', 'docker-process', 'process-mismatch', 'edition-mismatch'):
            value, _, _, _ = fixture()
            result = value['result']; installed = value['initial_install']; proof = value['upgrade_input']
            if fault == 'missing-install': value.pop('initial_install')
            elif fault == 'missing-input': value.pop('upgrade_input')
            elif fault == 'install-status': installed['status'] = 'failed'
            elif fault == 'install-level': installed['evidence_level'] = 'native-upgrade'
            elif fault == 'install-archive': installed['archive_sha256'] = 'b' * 64
            elif fault == 'install-scenario': installed['docker_scenario'] = 'fresh'
            elif fault == 'install-manifest': installed['manifest_sha256'] = 'b' * 64
            elif fault == 'install-mode': installed['installer_mode'] = 'not-run'
            elif fault == 'install-byte-pin': installed['_file_sha256'] = 'b' * 64
            elif fault == 'input-byte-pin': proof['_file_sha256'] = 'b' * 64
            elif fault == 'panel-process':
                installed['panel_processes'] = {}; result['panel_processes'] = {}
            elif fault == 'docker-process':
                installed['docker_process'] = {}; result['docker_process'] = {}
            elif fault == 'process-mismatch': result['panel_processes']['1panel-core']['pid'] += 1
            elif fault == 'edition-mismatch': result['regional_edition'] = 'cn'
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                check(value)

    def test_initial_result_rejects_target_tampering_and_false_upgrade_claims(self):
        for field, replacement in (
            ('target_archive_sha256', 'b' * 64), ('target_receipt_sha256', 'b' * 64),
            ('target_run_id', 123), ('target_run_attempt', 123), ('target_commit', 'b' * 40),
            ('applicability_sha256', 'b' * 64), ('upgrade', 'passed; unchanged fixed target upgrade.sh'),
            ('rollback', 'passed; one-shot real systemd'), ('predecessor_version', 'v2.72.0'),
            ('read_only_recovery', repair_context()['repair_predecessor']), ('architecture', 'arm64')):
            value, _, _, _ = fixture(); value['result'][field] = replacement
            with self.subTest(field=field), self.assertRaises(ValueError):
                check(value)
        value, _, _, _ = fixture(); value['predecessor_install'] = value['initial_install']
        with self.assertRaisesRegex(ValueError, 'reuse predecessor'):
            check(value)

    def test_ordinary_upgrade_cannot_be_satisfied_by_initial_install_evidence(self):
        value, _, _, _ = fixture()
        value['result']['evidence_level'] = 'native-upgrade'
        with self.assertRaisesRegex(ValueError, 'cannot replace actual upgrade'):
            check(value)
        value.pop('initial_install')
        with self.assertRaisesRegex(ValueError, 'requires actual predecessor'):
            check(value)
        value['predecessor_install'] = {'status': 'passed'}
        value['result'].update(regional_edition='intl', **{flag: True for flag in (
            'database_user_rows_preserved', 'durable_settings_preserved', 'user_configuration_preserved',
            'persistent_user_file_preserved', 'docker_unchanged')})
        with self.assertRaisesRegex(ValueError, 'Actual rollback/upgrade'):
            check(value)

    def test_evidence_binds_hosted_runner_current_archive_and_explicit_context(self):
        for fault in ('runner-name', 'runner-arch', 'self-hosted', 'runner-id', 'current-archive',
                      'candidate-attempt', 'candidate-head', 'candidate-receipt', 'repair-preparation'):
            value, identity, job, prep = fixture()
            if fault == 'runner-name': value['runner']['name'] = 'another runner'
            elif fault == 'runner-arch': value['runner']['arch'] = 'ARM64'
            elif fault == 'self-hosted': job['runner_group_name'] = 'Self-hosted'
            elif fault == 'runner-id': job['runner_id'] = 0
            elif fault == 'current-archive': value['candidate']['archive_sha256'] = 'b' * 64
            elif fault == 'candidate-attempt': value['candidate']['workflow_run_attempt'] = 1
            elif fault == 'candidate-head': value['candidate']['workflow_commit'] = 'b' * 40
            elif fault == 'candidate-receipt': value['candidate']['receipt_sha256'] = 'b' * 64
            elif fault == 'repair-preparation': prep.update(repair_context())
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                native.verify_evidence(value, value['key'], identity, job, prep)


class InitialAdmissionTests(unittest.TestCase):
    def fixture(self):
        value, identity, job, _ = fixture()
        prep, results = existing_native.fixture()
        prep, results = json.loads(json.dumps([prep, results]).replace('v2.101.0', VERSION))
        native.verify_evidence(value, value['key'], identity, job, prep)
        results[value['key']].update(evidence=value, candidate=value['candidate'], run_attempt=identity['run_attempt'])
        return prep, results

    def test_initial_acceptance_is_explicit_and_fresh_and_existing_still_required(self):
        prep, results = self.fixture()
        admitted = native.admit(prep, results)
        row = next(row for row in admitted['outcomes'] if row['key'] == 'official-amd64')
        self.assertEqual(row['acceptance'], 'native-initial-install; upgrade-and-rollback-not-applicable')
        self.assertEqual(native.required_results({'source': 'official', 'arch': 'amd64'}),
            ['install:official:amd64:fresh', 'install:official:amd64:existing', 'upgrade:official:amd64'])
        for key in ('install:official:amd64:fresh', 'install:official:amd64:existing', 'upgrade:official:amd64'):
            changed = copy.deepcopy(results); changed.pop(key)
            with self.subTest(key=key):
                self.assertNotIn('official-amd64', native.admit(prep, changed)['accepted_keys'])

    def test_admission_revalidates_typed_initial_install_and_applicability(self):
        for fault in ('missing-install', 'missing-input', 'forged-applicability', 'missing-process', 'archive'):
            prep, results = self.fixture(); value = results['upgrade:official:amd64']['evidence']
            if fault == 'missing-install': value.pop('initial_install')
            elif fault == 'missing-input': value.pop('upgrade_input')
            elif fault == 'forged-applicability': value['upgrade_input']['applicability_sha256'] = 'b' * 64
            elif fault == 'missing-process': value['initial_install']['panel_processes'] = {}
            elif fault == 'archive': value['result']['target_archive_sha256'] = 'b' * 64
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                native.admit(prep, results)

    def test_each_forged_initial_marker_requires_complete_typed_evidence(self):
        for marker in ('result', 'input', 'install'):
            prep, results = self.fixture(); record = results['upgrade:official:amd64']
            record['evidence'] = {'candidate': record['candidate'], 'result': {'evidence_level': 'native-upgrade'}}
            if marker == 'result': record['evidence']['result']['evidence_level'] = 'native-initial-install'
            elif marker == 'input': record['evidence']['upgrade_input'] = {'kind': initial_input.KIND}
            else: record['evidence']['initial_install'] = {}
            with self.subTest(marker=marker), self.assertRaises(ValueError):
                native.admit(prep, results)


class InitialLocalHarnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.value, self.identity, self.job, self.prep = fixture()
        self.target = self.root / 'target'; self.target.mkdir()
        payload = b'synthetic executable bytes, never executed'
        (self.target / '1panel-core').write_bytes(payload)
        manifest = {'app_version': VERSION, 'source': 'official', 'architecture': 'amd64',
                    'payloads': {'1panel-core': {'bytes': len(payload), 'sha256': sha(payload)}}}
        manifest_raw = canonical(manifest); (self.target / 'offline-manifest.json').write_bytes(manifest_raw)
        proof = self.value['upgrade_input']; installed = self.value['initial_install']
        proof.update(target_package_path=str(self.target), target_manifest_sha256=sha(manifest_raw))
        installed['manifest_sha256'] = sha(manifest_raw)
        pin(proof); pin(installed)
        self.value['result'].update(input_provenance_sha256=proof['_file_sha256'],
                                    initial_install_result_sha256=installed['_file_sha256'])
        pin(self.value['result'])
        self.paths = {key: self.root / (key + '.json') for key in
                      ('candidate', 'result', 'upgrade_input', 'initial_install')}
        self.write_documents()
        self.env = {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted',
                    'RUNNER_TEMP': str(self.root), 'RUNNER_ARCH': 'X64', 'RUNNER_OS': 'Linux',
                    'RUNNER_NAME': self.job['runner_name']}
        self.args = SimpleNamespace(kind='upgrade', version=VERSION, tag=VERSION, source='official',
            arch='amd64', scenario='', output=str(self.root / 'native-evidence.json'),
            candidate=str(self.paths['candidate']), result=str(self.paths['result']),
            upgrade_input=str(self.paths['upgrade_input']), initial_install=str(self.paths['initial_install']),
            predecessor_install=str(self.root / 'predecessor-must-not-be-read.json'))
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        for module, name, replacement in ((native, 'current_identity', self.identity),
                                           (smoke, 'current_identity', self.identity)):
            self.stack.enter_context(patch.object(module, name, return_value=replacement))
        self.stack.enter_context(patch('native_install_smoke.os.geteuid', return_value=0))
        self.stack.enter_context(patch('native_install_smoke.platform.machine', return_value='x86_64'))
        self.stack.enter_context(patch.object(smoke, 'run', side_effect=AssertionError('No service command permitted')))
        self.stack.enter_context(patch.object(smoke, 'run_upgrade', side_effect=AssertionError('No upgrade permitted')))
        self.stack.enter_context(patch.object(smoke, 'failure_probe', side_effect=AssertionError('No rollback permitted')))

    def write_documents(self):
        for key, path in self.paths.items():
            path.write_bytes(raw(self.value[key]))

    def test_stamp_reads_real_result_bytes_and_preserves_separate_install_evidence(self):
        with redirect_stdout(io.StringIO()):
            value = native.stamp(self.args, self.env)
        native.verify_evidence(value, value['key'], self.identity, self.job, self.prep)
        self.assertIn('initial_install', value)
        self.assertNotIn('predecessor_install', value)
        self.assertEqual(value['initial_install']['_file_sha256'], sha(self.paths['initial_install'].read_bytes()))
        self.assertEqual(value['upgrade_input']['_file_sha256'], sha(self.paths['upgrade_input'].read_bytes()))
        self.assertEqual(json.loads(Path(self.args.output).read_bytes()), value)

    def test_stamp_missing_install_or_changed_bytes_cannot_produce_evidence(self):
        original = self.paths['initial_install'].read_bytes()
        for fault in ('missing', 'changed-whitespace', 'symlink', 'wrong-run', 'wrong-runner', 'repair'):
            self.paths['initial_install'].unlink(missing_ok=True)
            self.paths['initial_install'].write_bytes(original)
            environment = dict(self.env)
            if fault == 'missing': self.paths['initial_install'].unlink()
            elif fault == 'changed-whitespace': self.paths['initial_install'].write_bytes(original + b'\n')
            elif fault == 'symlink':
                backup = self.root / 'install-copy.json'; backup.write_bytes(original)
                self.paths['initial_install'].unlink(); self.paths['initial_install'].symlink_to(backup)
            elif fault == 'wrong-run':
                data = json.loads(self.paths['candidate'].read_bytes()); data['workflow_run_id'] = 123
                self.paths['candidate'].write_bytes(canonical(data))
            elif fault == 'wrong-runner': environment['RUNNER_ARCH'] = 'ARM64'
            elif fault == 'repair': environment.update(REPAIR_PREDECESSOR=json.dumps(repair_context()['repair_predecessor']),
                    GITHUB_EVENT_NAME='workflow_dispatch', PUBLICATION_OPERATION='repair-existing')
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                native.stamp(self.args, environment)
            self.assertFalse(Path(self.args.output).exists())
            self.paths['candidate'].write_bytes(raw(self.value['candidate']))

    def test_input_validation_checks_package_and_current_candidate_before_initial_route(self):
        result = smoke.validate_inputs(self.paths['upgrade_input'], VERSION, 'official', 'amd64', self.env)
        self.assertEqual(result[1:4], (None, None, None))
        self.assertEqual(result[-1], self.target)
        (self.target / '1panel-core').write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'payload changed'):
            smoke.validate_inputs(self.paths['upgrade_input'], VERSION, 'official', 'amd64', self.env)

    def test_input_validation_rejects_current_target_context_and_runner_changes(self):
        original = self.paths['upgrade_input'].read_bytes()
        for fault in ('current-run', 'repair', 'runner-architecture', 'manifest'):
            proof = json.loads(original); environment = dict(self.env)
            if fault == 'current-run': proof['target']['workflow_run_id'] = 123
            elif fault == 'repair': environment.update(REPAIR_PREDECESSOR=json.dumps(repair_context()['repair_predecessor']),
                    GITHUB_EVENT_NAME='workflow_dispatch', PUBLICATION_OPERATION='repair-existing')
            elif fault == 'runner-architecture':
                environment['RUNNER_ARCH'] = 'ARM64'
            elif fault == 'manifest': proof['target_manifest_sha256'] = 'b' * 64
            self.paths['upgrade_input'].write_bytes(canonical(proof))
            expected_arch = 'arm64' if fault == 'runner-architecture' else 'amd64'
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                smoke.validate_inputs(self.paths['upgrade_input'], VERSION, 'official', expected_arch, environment)

    def test_non_hosted_guard_runs_before_initial_branch_reads_or_mutations(self):
        output = self.root / 'never-written.json'
        with patch.object(smoke, 'validate_inputs', side_effect=AssertionError('Must not read inputs')) as inputs, \
                patch.object(smoke, 'initial_install', side_effect=AssertionError('Must not reach initial branch')) as initial:
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'disposable GitHub-hosted'):
                smoke.native_upgrade(self.paths['upgrade_input'], VERSION, 'official', 'amd64', output, env={})
        inputs.assert_not_called(); initial.assert_not_called()
        self.assertFalse(output.exists())

    def test_initial_harness_calls_actual_installer_interface_and_never_upgrade_or_rollback(self):
        output = self.root / 'initial-harness-result.json'
        def installed(package, version, scenario, path, archive_sha):
            self.assertEqual((package, version, scenario, archive_sha),
                (self.target, VERSION, 'existing', self.value['candidate']['archive_sha256']))
            path.write_bytes(raw(self.value['initial_install']))
        with patch.object(smoke, 'native_install', side_effect=installed) as installer, redirect_stdout(io.StringIO()):
            smoke.native_upgrade(self.paths['upgrade_input'], VERSION, 'official', 'amd64', output, self.env)
        installer.assert_called_once()
        result = json.loads(output.read_bytes())
        self.assertEqual(result['evidence_level'], 'native-initial-install')
        self.assertEqual(result['upgrade'], initial_input.NOT_APPLICABLE)
        self.assertEqual(result['rollback'], initial_input.NOT_APPLICABLE)
        self.assertFalse(any(key.startswith('predecessor_') for key in result))
        self.assertEqual(result['initial_install_result_sha256'], sha((self.root / 'initial-install-result.json').read_bytes()))
        value = copy.deepcopy(self.value); value['result'] = result
        check(value)

    def test_initial_harness_cannot_write_success_without_passing_actual_install_result(self):
        for fault in ('missing', 'failed', 'manifest', 'process'):
            output = self.root / (fault + '-result.json')
            install_path = self.root / 'initial-install-result.json'; install_path.unlink(missing_ok=True)
            def install(package, version, scenario, path, archive_sha):
                if fault == 'missing': return
                installed = copy.deepcopy(self.value['initial_install'])
                if fault == 'failed': installed['status'] = 'failed'
                elif fault == 'manifest': installed['manifest_sha256'] = 'b' * 64
                elif fault == 'process': installed['panel_processes'] = {}
                path.write_bytes(raw(installed))
            with self.subTest(fault=fault), patch.object(smoke, 'native_install', side_effect=install), \
                    redirect_stdout(io.StringIO()), self.assertRaises((ValueError, FileNotFoundError)):
                smoke.native_upgrade(self.paths['upgrade_input'], VERSION, 'official', 'amd64', output, self.env)
            self.assertFalse(output.exists())


class InitialMaterializeRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.args = SimpleNamespace(version=VERSION, tag=VERSION, source='official', arch='amd64',
            output=str(self.root / 'verified'), provenance=str(self.root / 'input.json'),
            target_input=str(self.root / 'target-input'), target_provenance=str(self.root / 'candidate.json'),
            target_receipt_sha256='b' * 64, target_controls_id=700001)
        self.env = {'RUNNER_TEMP': str(self.root)}
        self.client = SimpleNamespace(repo=binder.REPOSITORY)
        identity = patch.object(binder, 'current_identity', return_value={})
        identity.start(); self.addCleanup(identity.stop)

    def materialize(self):
        with redirect_stdout(io.StringIO()):
            return binder.materialize(self.args, self.env, self.client)

    def test_successful_empty_complete_lower_catalogue_routes_to_initial_applicability(self):
        expected = {'status': 'initial-applicability-verified-native-install-pending'}
        with patch.object(binder, 'array_pages', return_value=[]) as catalogue, \
                patch.object(binder, 'materialize_initial_install', return_value=expected) as initial, \
                patch('public_predecessor_selection.select_public_product', side_effect=AssertionError('No predecessor')) as selector:
            self.assertEqual(self.materialize(), expected)
        catalogue.assert_called_once_with(self.client, f'repos/{binder.REPOSITORY}/releases')
        initial.assert_called_once(); self.assertEqual(initial.call_args.args[4], [])
        selector.assert_not_called()

    def test_failed_or_malformed_catalogue_never_routes_to_initial_applicability(self):
        for response in (RuntimeError('authenticated API failed'), None, [{'message': 'API failed'}]):
            kwargs = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
            with self.subTest(response=response), patch.object(binder, 'array_pages', **kwargs), \
                    patch.object(binder, 'materialize_initial_install') as initial, \
                    self.assertRaises((ValueError, RuntimeError)):
                self.materialize()
            initial.assert_not_called()

    def test_existing_older_candidate_routes_to_product_selection_without_failure_fallback(self):
        older = release('v2.72.0', repository=binder.REPOSITORY)
        for problem in ('missing receipt', 'cancelled validator', 'product absence exhausted', 'source mismatch'):
            with self.subTest(problem=problem), patch.object(binder, 'array_pages', return_value=[older]), \
                    patch.object(binder, 'materialize_initial_install') as initial, \
                    patch('public_predecessor_selection.select_public_product', side_effect=ValueError(problem)) as selector, \
                    self.assertRaisesRegex(ValueError, problem):
                self.materialize()
            selector.assert_called_once()
            self.assertEqual(selector.call_args.args[1][0]['release']['tag_name'], 'v2.72.0')
            initial.assert_not_called()

    def test_explicit_repair_predecessor_never_enters_initial_catalogue_route(self):
        context = repair_context()
        self.env.update(REPAIR_PREDECESSOR=json.dumps(context['repair_predecessor']),
            GITHUB_EVENT_NAME='workflow_dispatch', PUBLICATION_OPERATION='repair-existing', GITHUB_RUN_ID='900001')
        expected = {'status': 'repair-inputs-verified-native-acceptance-pending'}
        with patch.object(binder, 'materialize_candidate_upgrade', return_value=expected) as repair, \
                patch.object(binder, 'array_pages') as catalogue, \
                patch.object(binder, 'materialize_initial_install') as initial:
            self.assertEqual(self.materialize(), expected)
        repair.assert_called_once(); catalogue.assert_not_called(); initial.assert_not_called()


if __name__ == '__main__':
    unittest.main()
