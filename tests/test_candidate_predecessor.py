"""Synthetic transport and control tests only; never claim or run native services."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import native_candidate_input as candidate
import native_upgrade_input as upgrade
import native_upgrade_smoke as smoke
import runtime_native_acceptance as native
import runtime_publication as publication
from test_native_candidate_input import Fixture, encoded, zip_bytes
import test_native_upgrade as archive_tests


class CandidateFixture(Fixture):
    def __init__(self, root, source='official', arch='amd64', changed_dependencies=False, prior_recovery=False):
        super().__init__(root, source, arch, version='v2.100.0')
        if changed_dependencies:
            self.runtime['inventory']['docker'][arch]['sha256'] = '4' * 64
            self.runtime['inventory']['docker'][arch]['version'] = '99.0.1'
            with patch('runtime_contract.selected', return_value=self.runtime):
                self.expected_policy = candidate.policy_fingerprint('downstream17', self.version)
            self.plan['resolved'] = self.runtime
            self.proof['policy_fingerprint'] = self.expected_policy
            self.proof['plan_sha256'] = candidate.digest_bytes(encoded(self.plan))['sha256']
            self.env['ONEPANEL_RESOLVED_PLAN_SHA256'] = self.proof['plan_sha256']
            self.record['plan_sha256'] = self.proof['plan_sha256']
            self.refresh_controls(); self.refresh_shard()
        if prior_recovery:
            earlier = {'version': 'v2.99.0', 'run_id': 77, 'run_attempt': 1, 'head_sha': 'a' * 40,
                'controls_artifact_id': 88, 'receipt_sha256': '7' * 64, 'plan_sha256': '8' * 64}
            self.plan['read_only_recovery'] = self.proof['read_only_recovery'] = earlier
            self.env['CANDIDATE_PREDECESSOR'] = json.dumps(earlier)
            self.proof['plan_sha256'] = candidate.digest_bytes(encoded(self.plan))['sha256']
            self.env['ONEPANEL_RESOLVED_PLAN_SHA256'] = self.proof['plan_sha256']
            self.record['plan_sha256'] = self.proof['plan_sha256']
            self.refresh_controls(); self.refresh_shard()
        self.execute()
        self.provenance = json.loads(Path(self.args.provenance).read_text())
        self.downloads.clear()
        self.run.update(status='completed', conclusion='failure')
        self.request = {'version': self.version, 'run_id': 123, 'run_attempt': 1, 'head_sha': 'a' * 40,
            'controls_artifact_id': 91, 'receipt_sha256': self.args.receipt_sha256,
            'plan_sha256': self.proof['plan_sha256']}
        self.target = {'version': 'v2.101.0', 'row': self.identity['row'], 'head_sha': self.identity['head_sha']}
        self.target_env = dict(self.env, GITHUB_RUN_ID='999', CANDIDATE_PREDECESSOR=json.dumps(self.request))
        self.fresh_job = self.job(1003, f'publication_native ({source}, {arch}, fresh)', 1)
        architecture, machine, label = native.ARCH_MAPPING[arch]
        self.fresh_job.update(runner_name='synthetic-hosted-runner', runner_id=888,
            runner_group_name='GitHub Actions', labels=[label])
        self.jobs.setdefault(1, []).append(self.fresh_job)
        result = {'schema': 1, 'status': 'passed', 'evidence_level': 'native-install', 'source': source,
            'architecture': arch, 'version': self.version, 'archive_sha256': self.provenance['archive_sha256'],
            'docker_scenario': 'fresh', 'manifest_sha256': 'b' * 64, 'installer_mode': 'cli',
            'panel_processes': {name: {'pid': n, 'binary_sha256': 'c' * 64}
                                for n, name in enumerate(('1panel-core', '1panel-agent'), 101)},
            'docker_process': {'pid': 99, 'binary_sha256': 'd' * 64}, '_file_sha256': 'e' * 64}
        self.evidence = {'schema': 1, 'key': f'install:{source}:{arch}:fresh', 'repository': self.repo,
            'run_id': 123, 'run_attempt': 1, 'head_sha': 'a' * 40, 'workflow': candidate.WORKFLOW,
            'runner': {'name': self.fresh_job['runner_name'], 'arch': architecture, 'machine': machine,
                'os': 'Linux', 'environment': 'github-hosted', 'label': label},
            'candidate': self.provenance, 'result': result}
        self.native_artifact = self.metadata(93, native.artifact_name(self.evidence['key'], 1), b'')
        self.artifacts.append(self.native_artifact)
        self.refresh_native()
        self.client = SimpleNamespace(repo=self.repo, run=self.client_run, download_zip=self.download_zip)

    def refresh_native(self):
        raw = encoded(self.evidence)
        data = zip_bytes([(native.EVIDENCE_FILE, raw)])
        self.native_artifact.update(self.metadata(93, self.native_artifact['name'], data))
        self.blobs[93] = data
        self.logs[1003] = ('VERIFIED_NATIVE_EVIDENCE_SHA256=' + hashlib.sha256(raw).hexdigest() + '\n' +
                           self.upload_log(self.native_artifact))

    def client_run(self, *args):
        endpoint = args[-1]
        if endpoint == f'repos/{self.repo}/actions/artifacts/93':
            return json.dumps(self.native_artifact)
        return super().client_run(*args)

    def materialize_predecessor(self):
        work = self.root / 'candidate-work'; work.mkdir()
        return upgrade.candidate_predecessor(self.request, self.target, self.target_env, self.client, work)


class CandidatePredecessorTests(unittest.TestCase):
    def test_read_only_target_can_be_the_next_independently_qualified_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = CandidateFixture(Path(td), prior_recovery=True)
            binding, _, _, identity = fixture.materialize_predecessor()
            self.assertEqual(identity['read_only_recovery']['version'], 'v2.99.0')
            self.assertEqual(binding['version'], 'v2.100.0')
            self.assertEqual(binding['selection']['target_version'], 'v2.101.0')
            self.assertEqual(binding['fresh_acceptance']['status'], 'success')

    def test_completed_failed_seed_needs_exact_build_and_real_fresh_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = CandidateFixture(Path(td))
            binding, path, runtime, identity = fixture.materialize_predecessor()
            self.assertEqual(binding['kind'], 'current-run-candidate-predecessor-bootstrap')
            self.assertFalse(binding['publication_eligible'])
            self.assertEqual(path.read_bytes(), next(iter(fixture.files.values())))
            self.assertEqual(fixture.downloads, [91, 91, fixture.shard['id'], 93])
            self.assertFalse(any('upgrade' in job['name'] for jobs in fixture.jobs.values() for job in jobs))
            with self.assertRaisesRegex(ValueError, 'not active or successful'):
                candidate.verify_run(fixture.client, identity)

    def test_other_native_sources_and_architectures_remain_independent(self):
        for source, arch in [('official', 'arm64'), ('custom', 'amd64'), ('custom', 'arm64')]:
            with self.subTest(source=source, arch=arch), tempfile.TemporaryDirectory() as td:
                fixture = CandidateFixture(Path(td), source, arch)
                binding, _, _, _ = fixture.materialize_predecessor()
                self.assertEqual(binding['fresh_acceptance']['key'], f'install:{source}:{arch}:fresh')

    def test_same_code_candidate_may_have_its_own_authenticated_dependency_pins(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = CandidateFixture(Path(td), changed_dependencies=True)
            _, _, runtime, _ = fixture.materialize_predecessor()
            self.assertEqual(runtime['inventory']['docker']['amd64']['version'], '99.0.1')

    def test_wrong_run_head_attempt_cancel_expiry_or_plan_fails_closed(self):
        for fault in ('run', 'head', 'other-reviewed-head', 'attempt', 'cancelled', 'active', 'expired', 'receipt', 'plan', 'build', 'policy'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = CandidateFixture(Path(td))
                if fault == 'run': fixture.run['id'] += 1
                if fault == 'head': fixture.run['head_sha'] = 'b' * 40
                if fault == 'other-reviewed-head': fixture.target['head_sha'] = 'c' * 40
                if fault == 'attempt': fixture.run['run_attempt'] += 1
                if fault == 'cancelled': fixture.run['conclusion'] = 'cancelled'
                if fault == 'active': fixture.run.update(status='in_progress', conclusion=None)
                if fault == 'expired': fixture.shard['expired'] = True
                if fault == 'receipt': fixture.request['receipt_sha256'] = '1' * 64
                if fault == 'plan': fixture.request['plan_sha256'] = '2' * 64
                if fault == 'build': fixture.producer['conclusion'] = 'failure'
                if fault == 'policy':
                    fixture.proof['policy_fingerprint'] = 'f' * 64
                    fixture.refresh_controls(); fixture.request['receipt_sha256'] = fixture.args.receipt_sha256
                with self.assertRaises(ValueError): fixture.materialize_predecessor()

    def test_missing_failed_foreign_or_unbound_fresh_evidence_blocks_candidate(self):
        for fault in ('missing', 'failed', 'attempt', 'head', 'runner', 'archive', 'plan', 'shard', 'log', 'existing'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = CandidateFixture(Path(td))
                if fault == 'missing': fixture.artifacts.remove(fixture.native_artifact)
                if fault == 'failed': fixture.fresh_job['conclusion'] = 'failure'
                if fault == 'attempt': fixture.fresh_job['run_attempt'] = 2
                if fault == 'head': fixture.fresh_job['head_sha'] = 'c' * 40
                if fault == 'runner': fixture.fresh_job['runner_group_name'] = 'self-hosted'
                if fault == 'archive': fixture.evidence['result']['archive_sha256'] = '7' * 64
                if fault == 'plan': fixture.evidence['candidate']['plan_sha256'] = '8' * 64
                if fault == 'shard': fixture.evidence['candidate']['shard']['artifact_id'] = 999
                if fault == 'existing': fixture.evidence['result']['docker_scenario'] = 'existing'
                fixture.refresh_native()
                if fault == 'log': fixture.logs[1003] = ''
                with self.assertRaises(ValueError): fixture.materialize_predecessor()

    def test_numeric_same_channel_explicit_selection_never_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = CandidateFixture(Path(td)); request = fixture.request
            self.assertEqual(candidate.recovery_request(json.dumps(request), 'v2.101.0', fixture.target_env), request)
            self.assertIsNone(candidate.recovery_request('', 'v2.101.0'))
            for version in ('v2.101.0', 'v2.102.0', 'v2.100.0-beta.1', 'v2.010.0'):
                with self.subTest(version=version), self.assertRaises(ValueError):
                    candidate.recovery_request(json.dumps(dict(request, version=version)), 'v2.101.0', fixture.target_env)
            for operation in ('build', 'repair-existing'):
                with self.assertRaisesRegex(ValueError, 'read-only'):
                    candidate.recovery_request(json.dumps(request), 'v2.101.0', dict(fixture.target_env, PUBLICATION_OPERATION=operation))
            for value in (dict(request, run_id=True), dict(request, extra='no'), dict(request, head_sha='x')):
                with self.assertRaises(ValueError): candidate.recovery_request(json.dumps(value), 'v2.101.0')

    def test_each_predecessor_source_must_match_its_own_plan(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = CandidateFixture(Path(td))
            pin = fixture.runtime['inventory']['official']['archives']['amd64']
            upgrade.bind_candidate_source(fixture.runtime, 'official', 'amd64', {'kind': 'canonical-vendor', 'pin': pin})
            with self.assertRaises(ValueError):
                upgrade.bind_candidate_source(fixture.runtime, 'official', 'amd64',
                    {'kind': 'canonical-vendor', 'pin': dict(pin, sha256='9' * 64)})
            with self.assertRaises(ValueError):
                upgrade.bind_candidate_source(fixture.runtime, 'custom', 'amd64', {'kind': 'canonical-vendor', 'pin': pin})

    def test_bad_explicit_candidate_does_not_try_any_public_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = CandidateFixture(Path(td)); temp = Path(td)
            args = SimpleNamespace(version='v2.101.0', tag='', source='official', arch='amd64',
                output=str(temp/'out'), provenance=str(temp/'out.json'),
                target_input=str(temp/'target'), target_provenance=str(temp/'target.json'))
            with patch.object(upgrade, 'current_identity', return_value=fixture.target), \
                    patch.object(upgrade, 'materialize_candidate_upgrade', side_effect=ValueError('Invalid selected candidate')), \
                    patch.object(upgrade, 'select_predecessor', side_effect=AssertionError('No public fallback')):
                with self.assertRaisesRegex(ValueError, 'Invalid selected candidate'):
                    upgrade.materialize(args, fixture.target_env, fixture.client)
            self.assertFalse((temp/'out').exists())

    def test_read_only_receipt_marker_is_bound_to_plan_and_candidate_transport(self):
        for field in ('proof', 'plan'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as td:
                fixture = CandidateFixture(Path(td), prior_recovery=True)
                getattr(fixture, field).pop('read_only_recovery')
                fixture.refresh_controls(); fixture.request['receipt_sha256'] = fixture.args.receipt_sha256
                with self.assertRaises(ValueError): fixture.materialize_predecessor()

    def test_unpack_uses_authenticated_candidate_dependency_not_current_target(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            helper = archive_tests.IndependentArchiveTests()
            archive, binding, source, provenance, _ = helper.fixture(work)
            historical = {name: json.loads((work / (name + '-sources.json')).read_text()) for name in ('docker', 'compose')}
            for name in historical:
                (work / (name + '-sources.json')).write_text(json.dumps({'amd64': dict(historical[name]['amd64'], version='different')}))
            with self.assertRaisesRegex(ValueError, 'dependency source'):
                upgrade.unpack_predecessor(archive, work / 'blocked', binding, 'official', 'amd64', source, provenance, work)
            package, _ = upgrade.unpack_predecessor(archive, work / 'accepted', binding, 'official', 'amd64',
                source, provenance, work, dependency_pins=historical)
            self.assertTrue((package / 'offline-manifest.json').is_file())

    def test_synthetic_input_handoff_validates_and_stamps_without_executing_services(self):
        # Transport authentication and source acquisition have separate tests.
        # This checks their handoff through the unchanged pure harness validators;
        # no synthetic result produced here is evidence of native execution.
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td); fixture = CandidateFixture(temp)
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
            plan_file = temp/'plan.json'; plan_file.write_text(json.dumps({'read_only_recovery': fixture.request}))
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
            installed = dict(fresh['evidence']['result'], regional_edition='intl')
            installed_path = temp/'synthetic-install-result.json'; installed_path.write_text(json.dumps(installed))
            result = {'schema': 1, 'status': 'passed', 'evidence_level': 'native-upgrade', 'source': args.source,
                'architecture': args.arch, 'version': args.version, 'regional_edition': 'intl',
                'target_archive_sha256': target['archive_sha256'], 'target_receipt_sha256': target['receipt_sha256'],
                'target_run_id': 999, 'target_run_attempt': 1, 'target_commit': target['workflow_commit'],
                'rollback': 'passed; one-shot real systemd synthetic unit-test field',
                'upgrade': 'passed; unchanged fixed target upgrade.sh', 'database_before': {}, 'database_after_rollback': {},
                'input_provenance_sha256': candidate.digest(proof_path)['sha256'],
                'predecessor_install_result_sha256': candidate.digest(installed_path)['sha256'],
                'predecessor_archive_sha256': provenance['archive_sha256'], 'read_only_recovery': fixture.request,
                'publication_eligible': False, 'predecessor_candidate': provenance,
                'docker_process': {'pid': 100, 'binary_sha256': '1'*64},
                **{flag: True for flag in ('database_user_rows_preserved', 'durable_settings_preserved',
                    'user_configuration_preserved', 'persistent_user_file_preserved', 'docker_unchanged')}}
            result_path = temp/'synthetic-upgrade-result.json'; result_path.write_text(json.dumps(result))
            stamp_args = SimpleNamespace(version=args.version, tag='', source=args.source, arch=args.arch,
                kind='upgrade', scenario='', candidate=str(target_file), result=str(result_path),
                upgrade_input=str(proof_path), predecessor_install=str(installed_path), output=str(temp/'synthetic-evidence.json'))
            with patch.object(native, 'current_identity', return_value=identity), patch.object(native.platform, 'machine', return_value='x86_64'):
                stamped = native.stamp(stamp_args, env, source_root)
            self.assertEqual(stamped['upgrade_input']['read_only_recovery'], fixture.request)
            self.assertFalse(stamped['result']['publication_eligible'])
            with self.assertRaisesRegex(ValueError, 'Candidate predecessor evidence'):
                native.admit(fixture.proof, {'upgrade:official:amd64': {'evidence': stamped}})

    def test_reports_survive_all_failed_native_outcomes_and_writers_reject_mode(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = CandidateFixture(Path(td))
            proof = dict(fixture.proof, read_only_recovery=fixture.request, receipt_sha256=fixture.args.receipt_sha256,
                         controls_artifact_id=91)
            plan = dict(fixture.plan, version='v2.101.0', read_only_recovery=fixture.request)
            identity = dict(fixture.identity, version='v2.101.0', run_id=999)
            result = publication.recovery_report(proof, plan, identity, {}, Path(td) / 'report', fixture.target_env)
            self.assertFalse(result['publication_eligible'])
            report = json.loads((Path(td) / 'report/read-only-native-recovery.json').read_text())
            self.assertEqual(report['native_results'], {})
            self.assertTrue(all(not row['publication_eligible'] for row in report['outcomes']))
            self.assertFalse((Path(td) / 'report/release-validation.json').exists())
            with self.assertRaisesRegex(ValueError, 'Read-only recovery'):
                native.admit(proof, {})
            with self.assertRaisesRegex(ValueError, 'Read-only recovery'):
                publication.verify_final_directory(Path(td) / 'report', plan, identity, 'a'*64, 'b'*64)
            with patch.object(publication, 'workflow_identity', side_effect=AssertionError('Writer must stop before API/input work')):
                with self.assertRaisesRegex(ValueError, 'cannot invoke a writer'):
                    publication.publish(SimpleNamespace(), fixture.target_env)
            with self.assertRaisesRegex(ValueError, 'Candidate predecessor evidence'):
                native.admit(fixture.proof, {'x': {'evidence': {'upgrade_input': {'predecessor':
                    {'kind': 'current-run-candidate-predecessor-bootstrap'}}}}})

    def test_workflow_keeps_candidate_recovery_read_only_with_native_report(self):
        workflow = yaml.load((ROOT / '.github/workflows/build-offline-v2.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow['env']['CANDIDATE_PREDECESSOR'], '${{ inputs.candidate_predecessor }}')
        from test_workflow_concurrency import expression
        jobs = workflow['jobs']
        for writer in ('build', 'publication_repair'):
            context = {'inputs.candidate_predecessor': '{"candidate":"explicit"}', 'inputs.operation': 'repair-existing',
                'github.event_name': 'workflow_dispatch', 'needs.build_plan.result': 'success',
                'needs.publication_plan.result': 'success', 'needs.publication_acceptance.result': 'success',
                'needs.publication_plan.outputs.build_required': 'true'}
            self.assertFalse(expression(jobs[writer]['if'], context))
        command = next(s['run'] for s in jobs['publication_upgrade']['steps'] if 'sudo --preserve-env' in s.get('run', ''))
        self.assertIn('CANDIDATE_PREDECESSOR', command)
        artifacts = [s for s in jobs['publication_acceptance']['steps'] if s.get('uses') == 'actions/upload-artifact@v4']
        self.assertTrue(any(s['with']['name'].startswith('read-only-native-recovery-') for s in artifacts))
        for step in artifacts:
            if step['with']['name'].startswith('native-admitted-'):
                self.assertEqual(step['if'], "inputs.candidate_predecessor == ''")


if __name__ == '__main__': unittest.main()
