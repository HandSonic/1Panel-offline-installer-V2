"""Synthetic repair contracts only; none of these results claim native execution."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import native_candidate_input as candidate
import native_upgrade_input as upgrade
import native_upgrade_smoke as smoke
import test_native_upgrade as archive_tests
import test_repair_predecessor as repair_transport
from test_native_candidate_input import encoded
import runtime_native_acceptance as native
import runtime_publication as publication
from publication_outcomes import validate_preparation
from resolved_inventory import canonical, digest as object_digest
from test_candidate_predecessor import CandidateFixture
from test_runtime_publication import Fixture, facts
import test_runtime_native_acceptance as native_tests


class RepairFixture(Fixture):
    def __init__(self, root, operation='repair-existing'):
        super().__init__(root, {'official': ['amd64'], 'enterprise-original': ['amd64']})
        seed_root = root / 'seed'; seed_root.mkdir()
        seed = CandidateFixture(seed_root)
        self.context = {'repair_predecessor': seed.request, 'repair_operation': operation}
        self.plan.update(self.context); self.identity.update(self.context); self.proof.update(self.context)
        self.proof['plan_sha256'] = object_digest(self.plan)
        self.refresh_preparation()
        work = seed_root / 'repair-seed'; work.mkdir()
        repair_env = dict(seed.target_env, CANDIDATE_PREDECESSOR='', REPAIR_PREDECESSOR=json.dumps(seed.request),
                          PUBLICATION_OPERATION=operation)
        binding, _, _, _ = upgrade.candidate_predecessor(seed.request, seed.target, repair_env,
            seed.client, work, context=self.context)
        old = binding['candidate']
        name = '1panel-v2.101.0-official-offline-linux-amd64.tar.gz'
        target = dict(old, version=self.plan['version'], release_tag=self.plan['tag'],
            workflow_run_id=self.identity['run_id'], workflow_run_attempt=self.identity['run_attempt'],
            receipt_sha256=self.preparation['receipt_sha256'], plan_sha256=self.proof['plan_sha256'],
            archive_sha256=self.files[name]['sha256'], files={'official/' + name: self.files[name]})
        target['controls'] = dict(old['controls'], artifact_id=700001, name='publication-controls-900001-1')
        template = binding['fresh_acceptance']['evidence']['result']
        installed = dict(template, regional_edition='intl', docker_scenario='existing')
        proof = {'schema': 2, **self.context, 'target': target, 'predecessor': binding, '_file_sha256': '8' * 64}
        result = {'schema': 1, 'status': 'passed', 'evidence_level': 'native-upgrade',
            'source': 'official', 'architecture': 'amd64', 'version': self.plan['version'], 'regional_edition': 'intl',
            'target_archive_sha256': target['archive_sha256'], 'target_receipt_sha256': target['receipt_sha256'],
            'target_run_id': target['workflow_run_id'], 'target_run_attempt': target['workflow_run_attempt'],
            'target_commit': target['workflow_commit'], 'predecessor_archive_sha256': old['archive_sha256'],
            'rollback': 'passed; one-shot real systemd synthetic unit-test field',
            'upgrade': 'passed; unchanged fixed target upgrade.sh', 'database_before': {}, 'database_after_rollback': {},
            'input_provenance_sha256': proof['_file_sha256'],
            'predecessor_install_result_sha256': installed['_file_sha256'],
            'predecessor_candidate': old, **self.context, 'docker_process': template['docker_process'],
            **{flag: True for flag in ('database_user_rows_preserved', 'durable_settings_preserved',
                'user_configuration_preserved', 'persistent_user_file_preserved', 'docker_unchanged')}}
        for key, record in self.results.items():
            record.update(plan_sha256=self.proof['plan_sha256'], receipt_sha256=self.preparation['receipt_sha256'],
                          candidate=target)
            if key.startswith('upgrade:'):
                evidence = {'candidate': target, 'result': result, 'upgrade_input': proof, 'predecessor_install': installed}
            else:
                evidence = {'candidate': target, 'result': dict(template, version=self.plan['version'],
                    archive_sha256=target['archive_sha256'], docker_scenario=key.split(':')[-1])}
            record['evidence'] = evidence
        self.env = dict(seed.env, CANDIDATE_PREDECESSOR='', REPAIR_PREDECESSOR=json.dumps(seed.request),
            PUBLICATION_OPERATION=operation, GITHUB_RUN_ID=str(self.identity['run_id']),
            GITHUB_RUN_ATTEMPT=str(self.identity['run_attempt']), RUNNER_TEMP=str(root))
        self.plan_path = root / 'plan.json'; self.plan_path.write_bytes(canonical(self.plan))
        self.env.update(ONEPANEL_RESOLVED_PLAN=str(self.plan_path), ONEPANEL_RESOLVED_PLAN_SHA256=object_digest(self.plan))

    def admission(self, results=None, identity=None):
        return native.admit(self.preparation, self.results if results is None else results,
                            identity=self.identity if identity is None else identity)


class RepairAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        policy = patch.object(publication, 'policy', return_value='b' * 64)
        policy.start(); self.addCleanup(policy.stop)

    def test_both_manual_operations_finalize_exact_context_and_enterprise_passthrough(self):
        for operation in ('validate-repair', 'repair-existing'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory(dir=self.root) as td:
                fixture = RepairFixture(Path(td), operation)
                fixture.finalize(); proof = fixture.verify()
                self.assertEqual(candidate.recorded_context(proof, fixture.plan['version']), fixture.context)
                subject = json.loads((fixture.output / publication.NATIVE_FILE).read_bytes())
                self.assertEqual(candidate.recorded_context(subject, fixture.plan['version']), fixture.context)
                self.assertEqual(subject['admission']['accepted_keys'], ['official-amd64', 'enterprise-original-amd64'])
                self.assertEqual(subject['admission']['outcomes'][1]['acceptance'], 'vendor-byte-identity')
                with patch.object(publication, 'selected', return_value=fixture.plan['resolved']):
                    identity, plan = publication.workflow_identity(fixture.plan['version'], fixture.plan['tag'], fixture.env)
                self.assertEqual(identity, fixture.identity); self.assertEqual(plan, fixture.plan)

    def test_admission_requires_current_manual_identity_and_exact_operation(self):
        fixture = RepairFixture(self.root)
        for identity in (None, dict(fixture.identity, event='push'), dict(fixture.identity, event='schedule'),
                dict(fixture.identity, repair_operation='validate-repair'), dict(fixture.identity, run_id=123),
                dict(fixture.identity, head_sha='9' * 40)):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                native.admit(fixture.preparation, fixture.results, identity=identity)
        with self.assertRaises(ValueError): native.admit(fixture.preparation, fixture.results, identity=fixture.identity, cancelled=True)

    def test_supplied_repair_identity_cannot_enter_stripped_ordinary_admission(self):
        fixture = RepairFixture(self.root)
        stripped = {key: value for key, value in fixture.preparation.items() if key not in fixture.context}
        # No native results: the ordinary branch could otherwise still admit the
        # independent enterprise-original row and disguise the lost repair context.
        self.assertEqual(native.admit(stripped, {})['accepted_keys'], ['enterprise-original-amd64'])
        with self.assertRaisesRegex(ValueError, 'identity context differs'):
            native.repair_admission_context(stripped, fixture.identity)
        with self.assertRaisesRegex(ValueError, 'identity context differs'):
            native.admit(stripped, {}, identity=fixture.identity)

    def test_missing_or_failed_native_evidence_suppresses_only_its_product(self):
        fixture = RepairFixture(self.root)
        for key in fixture.results:
            for missing in (True, False):
                broken = copy.deepcopy(fixture.results)
                if missing: del broken[key]
                else: broken[key]['status'] = 'failure'
                with self.subTest(key=key, missing=missing):
                    admission = fixture.admission(broken)
                    self.assertEqual(admission['accepted_keys'], ['enterprise-original-amd64'])
                    self.assertFalse(admission['all_requested_passed'])
        fixture.results.pop('upgrade:official:amd64')
        fixture.finalize(); proof = fixture.verify()
        self.assertNotIn('1panel-v2.101.0-official-offline-linux-amd64.tar.gz', proof['files'])

    def test_actual_install_upgrade_rollback_and_current_target_cannot_be_claim_only(self):
        fixture = RepairFixture(self.root)
        for fault in ('fresh-target', 'old-target', 'missing-install', 'install-process', 'install-edition',
                'install-archive', 'fresh-predecessor', 'upgrade', 'rollback', 'database', 'wrong-receipt',
                'wrong-attempt', 'wrong-source', 'wrong-head', 'wrong-archive'):
            broken = copy.deepcopy(fixture.results)
            evidence = broken['upgrade:official:amd64']['evidence']
            if fault == 'fresh-target': broken['install:official:amd64:fresh'].pop('evidence')
            if fault == 'old-target': evidence['candidate']['workflow_run_id'] = 123
            if fault == 'missing-install': evidence.pop('predecessor_install')
            if fault == 'install-process': evidence['predecessor_install']['panel_processes'] = {}
            if fault == 'install-edition': evidence['predecessor_install']['regional_edition'] = 'cn'
            if fault == 'install-archive': evidence['predecessor_install']['archive_sha256'] = '6' * 64
            if fault == 'fresh-predecessor': evidence['upgrade_input']['predecessor']['fresh_acceptance']['status'] = 'failure'
            if fault == 'upgrade': evidence['result']['upgrade'] = 'claimed'
            if fault == 'rollback': evidence['result']['rollback'] = 'not executed'
            if fault == 'database': evidence['result']['database_after_rollback'] = {'changed': True}
            if fault == 'wrong-receipt': evidence['result']['target_receipt_sha256'] = '7' * 64
            if fault == 'wrong-attempt': evidence['result']['target_run_attempt'] = 3
            if fault == 'wrong-source': evidence['result']['source'] = 'custom'
            if fault == 'wrong-head': evidence['result']['target_commit'] = '3' * 40
            if fault == 'wrong-archive': evidence['result']['target_archive_sha256'] = '2' * 64
            with self.subTest(fault=fault), self.assertRaises(ValueError): fixture.admission(broken)

    def test_readonly_report_or_mode_laundering_never_admits(self):
        fixture = RepairFixture(self.root)
        for location in ('preparation', 'input', 'result', 'binding'):
            prep, results = copy.deepcopy((fixture.preparation, fixture.results))
            evidence = results['upgrade:official:amd64']['evidence']
            target = {'preparation': prep, 'input': evidence['upgrade_input'], 'result': evidence['result'],
                      'binding': evidence['upgrade_input']['predecessor']}[location]
            target['read_only_recovery'] = fixture.context['repair_predecessor']
            target['publication_eligible'] = False
            with self.subTest(location=location), self.assertRaises(ValueError):
                native.admit(prep, results, identity=fixture.identity)
        prep = dict(fixture.preparation)
        for field in fixture.context: prep.pop(field)
        with self.assertRaises(ValueError): native.admit(prep, fixture.results)

    def test_fields_or_operation_cannot_be_removed_or_invented_in_final_controls(self):
        fixture = RepairFixture(self.root); fixture.finalize()
        original = (fixture.output / publication.PROOF).read_bytes()
        for fault in ('request-removed', 'operation-removed', 'extra-field', 'operation-mismatch', 'readonly-added'):
            raw = json.loads((fixture.output / publication.PROOF).read_bytes())
            if fault == 'request-removed': raw.pop('repair_predecessor')
            if fault == 'operation-removed': raw.pop('repair_operation')
            if fault == 'extra-field': raw['allow_repair'] = True
            if fault == 'operation-mismatch': raw['repair_operation'] = 'validate-repair'
            if fault == 'readonly-added': raw['read_only_recovery'] = fixture.context['repair_predecessor']
            body = canonical(raw); (fixture.output / publication.PROOF).write_bytes(body)
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                publication.verify_final_directory(fixture.output, fixture.plan, fixture.identity,
                    facts(body)['sha256'], fixture.result['native_acceptance_sha256'])
            (fixture.output / publication.PROOF).write_bytes(original)
        fixture.verify()

    def test_input_plan_and_receipt_operations_must_match_before_acceptance(self):
        fixture = RepairFixture(self.root)
        for fault in ('no-input', 'wrong-operation', 'push', 'schedule', 'both-inputs'):
            env = dict(fixture.env)
            if fault == 'no-input': env['REPAIR_PREDECESSOR'] = ''
            if fault == 'wrong-operation': env['PUBLICATION_OPERATION'] = 'validate-repair'
            if fault == 'push': env.update(GITHUB_EVENT_NAME='push', PUBLICATION_OPERATION='build')
            if fault == 'schedule': env.update(GITHUB_EVENT_NAME='schedule', PUBLICATION_OPERATION='build')
            if fault == 'both-inputs': env['CANDIDATE_PREDECESSOR'] = env['REPAIR_PREDECESSOR']
            with self.subTest(fault=fault), patch.object(publication, 'selected', return_value=fixture.plan['resolved']), self.assertRaises(ValueError):
                publication.workflow_identity(fixture.plan['version'], fixture.plan['tag'], env)
        for field in fixture.context:
            proof = dict(fixture.proof); proof.pop(field)
            with self.assertRaises(ValueError): validate_preparation(proof, fixture.plan)

    def test_validate_artifact_and_ordinary_build_cannot_reach_any_writer(self):
        fixture = RepairFixture(self.root, 'validate-repair'); fixture.finalize()
        with self.assertRaises(ValueError): publication.repair_existing(None, fixture.output, fixture.plan, fixture.identity,
            fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], self.root / 'journal.json')
        with self.assertRaises(ValueError): publication.publish_draft(None, fixture.output, fixture.plan, fixture.identity,
            fixture.result['receipt_sha256'], fixture.result['native_acceptance_sha256'], self.root / 'journal.json')
        args = SimpleNamespace(version=fixture.plan['version'], tag=fixture.plan['tag'], repository=fixture.identity['repository'])
        for operation in ('build', 'validate-repair'):
            env = dict(fixture.env, PUBLICATION_OPERATION=operation)
            with self.subTest(operation=operation), patch.object(publication, 'selected', return_value=fixture.plan['resolved']), \
                    patch.object(publication, 'authenticate_final_artifact', side_effect=AssertionError('No writer transport')), self.assertRaises(ValueError):
                publication.publish(args, env)

    def test_writer_reauthenticates_exact_final_artifact_attempt_log_and_context(self):
        fixture = RepairFixture(self.root); fixture.finalize()
        client, state = fixture.transport(final=True)
        args = SimpleNamespace(version=fixture.plan['version'], tag=fixture.plan['tag'], repository=fixture.identity['repository'],
            artifact_id='700003', receipt_sha256=fixture.result['receipt_sha256'],
            native_acceptance_sha256=fixture.result['native_acceptance_sha256'], journal=str(self.root / 'journal.json'))
        with patch.object(publication, 'selected', return_value=fixture.plan['resolved']), \
                patch.object(publication, 'repair_existing', return_value={'phase': 'synthetic-writer-authorized'}) as writer:
            self.assertEqual(publication.publish(args, fixture.env, client)['phase'], 'synthetic-writer-authorized')
            self.assertEqual(writer.call_args.args[3], fixture.identity)
        for fault in ('cancelled', 'attempt', 'head', 'zip', 'log'):
            client, state = fixture.transport(final=True)
            if fault == 'cancelled': state['run'].update(status='completed', conclusion='cancelled')
            if fault == 'attempt': state['run']['run_attempt'] += 1
            if fault == 'head': state['run']['head_sha'] = '0' * 40
            if fault == 'zip': state['blobs'][700003] += b'tampered'
            if fault == 'log': state['log'] = ''
            with self.subTest(fault=fault), patch.object(publication, 'selected', return_value=fixture.plan['resolved']), \
                    patch.object(publication, 'repair_existing', side_effect=AssertionError('No writer')), self.assertRaises(ValueError):
                publication.publish(args, fixture.env, client)

    def test_repaired_public_receipt_is_a_normal_predecessor_with_cancelled_run_rejected(self):
        fixture = RepairFixture(self.root); fixture.finalize()
        receipt = (fixture.output / publication.PROOF).read_bytes()
        sums = (fixture.output / 'checksums.txt').read_bytes()
        release = {'id': 800001, 'tag_name': fixture.plan['version'], 'draft': False, 'prerelease': False,
            'url': upgrade.API_ROOT + '/releases/800001', 'assets': []}
        for number, path in enumerate(sorted(fixture.output.iterdir()), 700010):
            pin = facts(path.read_bytes())
            release['assets'].append({'id': number, 'name': path.name, 'state': 'uploaded',
                'url': upgrade.API_ROOT + '/releases/assets/' + str(number), 'size': pin['bytes'], 'digest': 'sha256:' + pin['sha256']})
        client, state = fixture.transport(final=True)
        state['run'].update(status='completed', conclusion='success')
        with patch.object(upgrade, 'public_release', return_value=release):
            authenticated = upgrade.verify_public_acceptance(client, release, receipt, sums)
        self.assertEqual(authenticated['receipt']['repair_predecessor'], fixture.context['repair_predecessor'])
        selection = upgrade.predecessor_candidates('v2.102.0', 'stable', [release], catalogue_complete=True)[0]
        bound = upgrade.bind_public(selection, release, receipt, sums, state['run'], 'official', 'amd64')
        self.assertEqual(bound['kind'], 'current-run-public-predecessor-bootstrap')
        for fault in ('cancelled', 'event', 'log', 'foreign-head'):
            client, state = fixture.transport(final=True); state['run'].update(status='completed', conclusion='success')
            if fault == 'cancelled': state['run']['conclusion'] = 'cancelled'
            if fault == 'event': state['run']['event'] = 'push'
            if fault == 'log': state['log'] = ''
            if fault == 'foreign-head': state['run']['head_sha'] = 'f' * 40
            with self.subTest(fault=fault), patch.object(upgrade, 'public_release', return_value=release), self.assertRaises(ValueError):
                upgrade.verify_public_acceptance(client, release, receipt, sums)

    def test_repair_collection_hash_or_cancel_failure_is_shared_but_native_failure_is_local(self):
        for fault in ('zip', 'log', 'cancelled-job', 'failed-job', 'runner'):
            state, client, identity, prep, key = native_tests.CollectionTests().fixture_transport()
            context = {'repair_predecessor': {'version': 'v2.100.0', 'run_id': 123, 'run_attempt': 1,
                'head_sha': 'a' * 40, 'controls_artifact_id': 91, 'receipt_sha256': 'b' * 64, 'plan_sha256': 'c' * 64},
                'repair_operation': 'repair-existing'}
            prep.update(context); identity.update(context)
            if fault == 'zip': state['blob'] += b'changed'
            if fault == 'log': state['log'] = state['log'].replace('VERIFIED_NATIVE_EVIDENCE_SHA256=', 'UNTRUSTED=')
            if fault == 'cancelled-job': state['job']['conclusion'] = 'cancelled'
            if fault == 'failed-job': state['job']['conclusion'] = 'failure'
            if fault == 'runner': state['job']['labels'] = ['self-hosted']
            with self.subTest(fault=fault):
                if fault in ('zip', 'log', 'cancelled-job'):
                    with self.assertRaises(native.SharedTrustError): native.collect(client, identity, prep, self.root)
                else:
                    self.assertEqual(native.collect(client, identity, prep, self.root)[key]['status'], 'failure')

    def test_repair_handoff_materializes_validates_and_stamps_the_exact_context(self):
        # Transport authentication and source acquisition have separate tests.
        # This checks their handoff through the unchanged pure harness validators;
        # no synthetic result produced here is evidence of native execution.
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td); fixture = repair_transport.RepairFixture(temp)
            binding, _, _, _ = fixture.materialize_predecessor()
            source_root = temp / 'source'; source_root.mkdir()
            archive, archive_binding, body, source_proof, packed = archive_tests.IndependentArchiveTests().fixture(source_root)
            binding['archive'] = archive_binding['archive']
            name = binding['archive']['name']
            provenance = binding['candidate']
            provenance['archive_sha256'] = binding['archive']['sha256']
            provenance['files'] = {'official/' + name: {k: binding['archive'][k] for k in ('bytes', 'sha256')}}
            fresh = binding['fresh_acceptance']
            fresh['candidate'] = fresh['evidence']['candidate'] = provenance
            fresh['evidence']['result']['archive_sha256'] = provenance['archive_sha256']
            runtime = {'inventory': {n: json.loads((source_root/(n+'-sources.json')).read_text())
                                     for n in ('docker', 'compose')}}
            (source_root/'upgrade_offline.sh').write_bytes((ROOT/'upgrade_offline.sh').read_bytes())
            identity = dict(fixture.identity, version='v2.101.0', tag='v2.101.0', run_id=999)
            target = dict(provenance, version='v2.101.0', release_tag='v2.101.0', workflow_run_id=999,
                          receipt_sha256='6'*64, archive_sha256='5'*64)
            target_file = temp/'target.json'; target_file.write_text(json.dumps(target))
            plan_file = temp/'plan.json'; plan_file.write_text(json.dumps(fixture.context))
            env = dict(fixture.target_env, ONEPANEL_RESOLVED_PLAN=str(plan_file),
                       RUNNER_ARCH='X64', RUNNER_NAME='synthetic-hosted-runner', RUNNER_OS='Linux')
            target_body = dict(packed)
            target_body['upgrade.sh'] = (source_root/'upgrade_offline.sh').read_bytes()
            manifest = json.loads(target_body['offline-manifest.json']); manifest['app_version'] = 'v2.101.0'
            manifest['payloads']['upgrade.sh'] = archive_tests.facts(target_body['upgrade.sh'])
            target_body['offline-manifest.json'] = encoded(manifest)
            def unpack(*args):
                return upgrade.write_verified_archive(target_body, {n: 0o755 for n in target_body}, args[2], 'target-package')
            args = SimpleNamespace(version='v2.101.0', source='official', arch='amd64',
                                   target_receipt_sha256=target['receipt_sha256'], target_controls_id=91)
            proof_path = temp/'handoff.json'
            with patch.object(upgrade, 'candidate_predecessor', return_value=(binding, archive, runtime, dict(fixture.identity, source_plan=fixture.plan))), \
                    patch.object(upgrade, 'source_archive_from_candidate', return_value=(body, source_proof)), \
                    patch.object(upgrade, 'bind_candidate_source'), patch.object(upgrade, 'unpack_target', side_effect=unpack), \
                    patch.object(upgrade, 'verify_completed_candidate'), patch.object(upgrade, 'verify_run'):
                upgrade.materialize_candidate_upgrade(args, env, identity, fixture.request, fixture.client,
                    temp, temp/'handoff', proof_path, temp/'target-input', target_file, source_root)
            with patch.object(smoke, 'disposable_guard', return_value='amd64'), \
                    patch.object(smoke, 'current_identity', return_value=identity), \
                    patch.object(smoke, 'run', side_effect=AssertionError('No native execution in unit tests')):
                proof, _, _, old, new = smoke.validate_inputs(proof_path, args.version, args.source, args.arch, env, source_root)
            self.assertNotEqual(old, new)
            installed = dict(fresh['evidence']['result'], regional_edition='intl', docker_scenario='existing')
            installed_path = temp/'synthetic-install-result.json'; installed_path.write_text(json.dumps(installed))
            result = {'schema': 1, 'status': 'passed', 'evidence_level': 'native-upgrade', 'source': args.source,
                'architecture': args.arch, 'version': args.version, 'regional_edition': 'intl',
                'target_archive_sha256': target['archive_sha256'], 'target_receipt_sha256': target['receipt_sha256'],
                'target_run_id': 999, 'target_run_attempt': 1, 'target_commit': target['workflow_commit'],
                'rollback': 'passed; one-shot real systemd synthetic unit-test field',
                'upgrade': 'passed; unchanged fixed target upgrade.sh', 'database_before': {}, 'database_after_rollback': {},
                'input_provenance_sha256': candidate.digest(proof_path)['sha256'],
                'predecessor_install_result_sha256': candidate.digest(installed_path)['sha256'],
                'predecessor_archive_sha256': provenance['archive_sha256'], **fixture.context, 'predecessor_candidate': provenance,
                'docker_process': {'pid': 100, 'binary_sha256': '1'*64},
                **{flag: True for flag in ('database_user_rows_preserved', 'durable_settings_preserved',
                    'user_configuration_preserved', 'persistent_user_file_preserved', 'docker_unchanged')}}
            result_path = temp/'synthetic-upgrade-result.json'; result_path.write_text(json.dumps(result))
            stamp_args = SimpleNamespace(version=args.version, tag='', source=args.source, arch=args.arch,
                kind='upgrade', scenario='', candidate=str(target_file), result=str(result_path),
                upgrade_input=str(proof_path), predecessor_install=str(installed_path), output=str(temp/'synthetic-evidence.json'))
            with patch.object(native, 'current_identity', return_value=identity), patch.object(native.platform, 'machine', return_value='x86_64'):
                stamped = native.stamp(stamp_args, env, source_root)
            self.assertEqual(candidate.recorded_context(stamped['upgrade_input'], args.version), fixture.context)
            self.assertEqual(candidate.recorded_context(stamped['result'], args.version), fixture.context)
            self.assertNotIn('publication_eligible', stamped['result'])
            with self.assertRaises(ValueError):
                native.admit(fixture.proof, {'upgrade:official:amd64': {'evidence': stamped}})


if __name__ == '__main__': unittest.main()
