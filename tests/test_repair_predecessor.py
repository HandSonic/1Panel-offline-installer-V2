"""Explicit repair admission with synthetic API/ZIPs; never execute packages."""
import copy
import hashlib
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
import runtime_native_acceptance as native
from test_candidate_predecessor import CandidateFixture
from test_native_candidate_input import encoded, zip_bytes
import test_native_upgrade as archive_tests


SELECTOR = {'version': 'v2.100.0', 'run_id': 123, 'run_attempt': 1,
            'head_sha': 'a' * 40, 'controls_artifact_id': 91,
            'receipt_sha256': 'b' * 64, 'plan_sha256': 'c' * 64}


def repair_env(request, operation='validate-repair'):
    return {'CANDIDATE_PREDECESSOR': '', 'REPAIR_PREDECESSOR': json.dumps(request),
            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'PUBLICATION_OPERATION': operation,
            'GITHUB_RUN_ID': '999'}


class RepairFixture(CandidateFixture):
    def __init__(self, root, operation='validate-repair', **kwargs):
        super().__init__(root, **kwargs)
        self.target_env.update(repair_env(self.request, operation))
        self.calls.clear()

    @property
    def context(self):
        return {'repair_predecessor': self.request,
                'repair_operation': self.target_env['PUBLICATION_OPERATION']}

    def materialize_predecessor(self):
        self.target_env['REPAIR_PREDECESSOR'] = json.dumps(self.request)
        work = self.root / 'repair-work'
        work.mkdir()
        return upgrade.candidate_predecessor(self.request, self.target,
            self.target_env, self.client, work, context=self.context)


class RepairSelectorTests(unittest.TestCase):
    def test_both_manual_operations_have_explicit_typed_context(self):
        for operation in ('validate-repair', 'repair-existing'):
            with self.subTest(operation=operation):
                context = candidate.predecessor_context('v2.101.0', repair_env(SELECTOR, operation))
                self.assertEqual(context, {'repair_predecessor': SELECTOR, 'repair_operation': operation})
                self.assertEqual(candidate.recorded_context(context, 'v2.101.0'), context)
                self.assertEqual(candidate.predecessor_selector(json.dumps(SELECTOR), 'v2.101.0'), SELECTOR)

    def test_absent_selectors_do_not_infer_repair_from_operation(self):
        self.assertIsNone(candidate.predecessor_selector('', 'v2.101.0'))
        for operation in ('validate-repair', 'repair-existing'):
            env = repair_env(SELECTOR, operation)
            env['REPAIR_PREDECESSOR'] = ''
            self.assertEqual(candidate.predecessor_context('v2.101.0', env), {})
        self.assertEqual(candidate.recorded_context({'unrelated': 'value'}, 'v2.101.0'), {})

    def test_selectors_are_mutually_exclusive_even_when_one_is_malformed(self):
        for raw in (json.dumps(SELECTOR), 'not-json'):
            env = dict(repair_env(SELECTOR), CANDIDATE_PREDECESSOR=raw)
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, 'mutually exclusive'):
                candidate.predecessor_context('v2.101.0', env)

    def test_repair_requires_manual_event_and_repair_operation(self):
        for field, value in [('GITHUB_EVENT_NAME', 'push'), ('GITHUB_EVENT_NAME', 'schedule'),
                             ('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_EVENT_NAME', ''),
                             ('PUBLICATION_OPERATION', 'build'), ('PUBLICATION_OPERATION', ''),
                             ('PUBLICATION_OPERATION', 'repair')]:
            env = repair_env(SELECTOR)
            env[field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, 'manual repair'):
                candidate.predecessor_context('v2.101.0', env)

    def test_read_only_selector_cannot_request_repair_existing(self):
        env = dict(repair_env(SELECTOR, 'repair-existing'),
                   REPAIR_PREDECESSOR='', CANDIDATE_PREDECESSOR=json.dumps(SELECTOR))
        with self.assertRaisesRegex(ValueError, 'read-only'):
            candidate.predecessor_context('v2.101.0', env)
        env['PUBLICATION_OPERATION'] = 'validate-repair'
        self.assertEqual(candidate.predecessor_context('v2.101.0', env), {'read_only_recovery': SELECTOR})

    def test_seed_must_be_from_an_independent_run_for_both_operations(self):
        for operation in ('validate-repair', 'repair-existing'):
            env = dict(repair_env(SELECTOR, operation), GITHUB_RUN_ID=str(SELECTOR['run_id']))
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, 'independent'):
                candidate.predecessor_context('v2.101.0', env)

    def test_semver_is_strict_numeric_earlier_and_same_channel(self):
        for earlier, target in [('v2.9.9', 'v2.10.0'), ('v2.10.0-beta.9', 'v2.10.0-beta.10'),
                                ('v2.10.0-dev.9', 'v2.10.0-dev.10')]:
            request = dict(SELECTOR, version=earlier)
            with self.subTest(earlier=earlier, target=target):
                self.assertEqual(candidate.predecessor_selector(json.dumps(request), target), request)
        for earlier, target in [('v2.10.0', 'v2.9.9'), ('v2.100.0', 'v2.100.0'),
                                ('v2.101.0', 'v2.100.0'), ('v2.99.0-beta.1', 'v2.100.0'),
                                ('v2.99.0-dev.1', 'v2.100.0-beta.1'), ('v2.010.0', 'v2.100.0'),
                                ('v2.10.0-beta.01', 'v2.10.0-beta.2'),
                                ('v2.100.0', 'v2.0101.0'), ('v2.100.0+build', 'v2.101.0')]:
            with self.subTest(earlier=earlier, target=target), self.assertRaises(ValueError):
                candidate.predecessor_selector(json.dumps(dict(SELECTOR, version=earlier)), target)

    def test_exact_seven_typed_pins_and_bounded_unique_json_are_required(self):
        malformed = ['[]', 'null', '{', 'x' * 4097,
                     json.dumps(SELECTOR)[:-1] + ', "run_id": 124}']
        for field in SELECTOR:
            request = dict(SELECTOR)
            del request[field]
            malformed.append(json.dumps(request))
        for field, value in [('run_id', True), ('run_id', '123'), ('run_attempt', 0),
                             ('controls_artifact_id', -1), ('head_sha', 'A' * 40),
                             ('receipt_sha256', 'x' * 64), ('plan_sha256', 'c' * 63),
                             ('version', None), ('extra', 'unexpected')]:
            malformed.append(json.dumps(dict(SELECTOR, **{field: value})))
        for raw in malformed:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                candidate.predecessor_selector(raw, 'v2.101.0')

    def test_recorded_context_rejects_orphan_mixed_and_malformed_modes(self):
        valid = {'repair_predecessor': SELECTOR, 'repair_operation': 'repair-existing'}
        malformed = [{'repair_predecessor': SELECTOR}, {'repair_operation': 'repair-existing'},
                     dict(valid, repair_operation='build'), dict(valid, repair_operation=None),
                     dict(valid, read_only_recovery=SELECTOR),
                     {'read_only_recovery': SELECTOR, 'repair_operation': 'validate-repair'},
                     dict(valid, repair_predecessor=None), dict(valid, repair_predecessor=json.dumps(SELECTOR)),
                     dict(valid, repair_predecessor=dict(SELECTOR, run_attempt=True)),
                     {'read_only_recovery': None}]
        for record in malformed:
            with self.subTest(record=record), self.assertRaises(ValueError):
                candidate.recorded_context(record, 'v2.101.0')


class RepairTransportTests(unittest.TestCase):
    def setUp(self):
        # Every API response and artifact body comes from the synthetic fixture.
        # Stub only the archive fixture's syntax-only check; execute no shell.
        def syntax_only(argv, **kwargs):
            if argv != ['bash', '-n']:
                raise AssertionError('No subprocesses in synthetic repair transport')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        for name, effect in [('run', syntax_only),
                             ('Popen', AssertionError('No subprocesses in synthetic repair transport'))]:
            blocker = patch.object(candidate.subprocess, name, side_effect=effect)
            blocker.start()
            self.addCleanup(blocker.stop)

    def test_both_operations_authenticate_only_exact_candidate_and_fresh_evidence(self):
        for operation in ('validate-repair', 'repair-existing'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as td:
                fixture = RepairFixture(Path(td), operation)
                binding, path, runtime, identity = fixture.materialize_predecessor()
                self.assertEqual(binding['kind'], 'current-run-repair-predecessor-bootstrap')
                self.assertEqual(candidate.recorded_context(binding, fixture.target['version']), fixture.context)
                self.assertEqual(binding['selection']['method'], 'explicit-repair')
                self.assertEqual(binding['historical_native_acceptance'], 'not-claimed')
                self.assertNotIn('publication_eligible', binding)
                self.assertEqual(binding['fresh_acceptance']['status'], 'success')
                self.assertEqual(path.read_bytes(), next(iter(fixture.files.values())))
                self.assertEqual(runtime, fixture.runtime)
                self.assertEqual(identity['run_id'], fixture.request['run_id'])
                self.assertEqual(fixture.downloads, [91, 91, fixture.shard['id'], 93])
                self.assertEqual(upgrade.candidate_binding(binding, fixture.target['version'],
                    'official', 'amd64', context=fixture.context), fixture.request)
                self.assertFalse(any('/releases' in call[-1] for call in fixture.calls))

    def test_read_only_seed_is_reused_only_as_predecessor_with_its_own_context(self):
        for operation in ('validate-repair', 'repair-existing'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as td:
                fixture = RepairFixture(Path(td), operation, prior_recovery=True)
                binding, _, _, identity = fixture.materialize_predecessor()
                prior = fixture.plan['read_only_recovery']
                self.assertEqual(identity['read_only_recovery'], prior)
                self.assertEqual(identity['source_plan']['read_only_recovery'], prior)
                self.assertEqual(fixture.proof['read_only_recovery'], prior)
                self.assertNotIn('repair_predecessor', binding['candidate'])
                self.assertEqual(binding['repair_predecessor'], fixture.request)
                self.assertNotIn('read_only_recovery', binding)
                with self.assertRaisesRegex(ValueError, 'not active or successful'):
                    candidate.verify_run(fixture.client, identity)
                with self.assertRaisesRegex(ValueError, 'typed candidate binding'):
                    upgrade.candidate_binding(binding, fixture.target['version'], 'official', 'amd64')

    def test_seed_read_only_marker_must_remain_bound_to_both_authenticated_controls(self):
        for field in ('proof', 'plan'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as td:
                fixture = RepairFixture(Path(td), prior_recovery=True)
                getattr(fixture, field).pop('read_only_recovery')
                fixture.proof['plan_sha256'] = candidate.digest_bytes(encoded(fixture.plan))['sha256']
                fixture.record['plan_sha256'] = fixture.proof['plan_sha256']
                fixture.request['plan_sha256'] = fixture.proof['plan_sha256']
                fixture.refresh_controls()
                fixture.refresh_shard()
                fixture.request['receipt_sha256'] = fixture.args.receipt_sha256
                with self.assertRaisesRegex(ValueError, 'recovery mode differs'):
                    fixture.materialize_predecessor()

    def test_same_reviewed_head_is_required_before_any_transport(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = RepairFixture(Path(td))
            fixture.target['head_sha'] = 'f' * 40
            with self.assertRaisesRegex(ValueError, 'same reviewed workflow commit'):
                fixture.materialize_predecessor()
            self.assertEqual(fixture.calls, [])
            self.assertEqual(fixture.downloads, [])

    def test_wrong_run_head_attempt_receipt_plan_and_cancelled_producers_fail_closed(self):
        for fault in ('run', 'head', 'attempt', 'controls', 'receipt', 'plan', 'cancelled-run',
                      'active-run', 'cancelled-prepare', 'cancelled-package', 'expired-controls', 'expired-shard'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = RepairFixture(Path(td))
                if fault == 'run': fixture.run['id'] += 1
                elif fault == 'head': fixture.run['head_sha'] = 'd' * 40
                elif fault == 'attempt': fixture.run['run_attempt'] += 1
                elif fault == 'controls': fixture.controls['id'] += 1
                elif fault == 'receipt': fixture.request['receipt_sha256'] = 'd' * 64
                elif fault == 'plan': fixture.request['plan_sha256'] = 'e' * 64
                elif fault == 'cancelled-run': fixture.run['conclusion'] = 'cancelled'
                elif fault == 'active-run': fixture.run.update(status='in_progress', conclusion=None)
                elif fault == 'cancelled-prepare': fixture.prepare['conclusion'] = 'cancelled'
                elif fault == 'cancelled-package': fixture.producer['conclusion'] = 'cancelled'
                elif fault == 'expired-controls': fixture.controls['expired'] = True
                elif fault == 'expired-shard': fixture.shard['expired'] = True
                with self.assertRaises(ValueError):
                    fixture.materialize_predecessor()

    def test_package_archive_and_source_cannot_be_relabelled(self):
        for fault in ('archive-bytes', 'archive-pin', 'source', 'arch', 'receipt', 'plan', 'controls', 'shard'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = RepairFixture(Path(td))
                if fault == 'archive-bytes':
                    name = next(iter(fixture.files))
                    fixture.files[name] += b'changed candidate bytes'
                    fixture.refresh_shard()
                else:
                    provenance = fixture.evidence['candidate']
                    if fault == 'archive-pin': provenance['archive_sha256'] = 'd' * 64
                    elif fault == 'source': provenance['source'] = 'custom'
                    elif fault == 'arch': provenance['arch'] = 'arm64'
                    elif fault == 'receipt': provenance['receipt_sha256'] = 'd' * 64
                    elif fault == 'plan': provenance['plan_sha256'] = 'e' * 64
                    elif fault == 'controls': provenance['controls']['artifact_id'] += 1
                    elif fault == 'shard': provenance['shard']['artifact_id'] += 1
                    fixture.refresh_native()
                with self.assertRaises(ValueError):
                    fixture.materialize_predecessor()

    def test_missing_malformed_failed_or_unbound_fresh_evidence_is_rejected(self):
        for fault in ('missing-artifact', 'missing-job', 'malformed-json', 'malformed-result',
                      'failed', 'cancelled', 'foreign-head', 'wrong-attempt', 'runner',
                      'result-archive', 'existing-scenario', 'missing-log'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = RepairFixture(Path(td))
                if fault == 'missing-artifact': fixture.artifacts.remove(fixture.native_artifact)
                elif fault == 'missing-job': fixture.jobs[1].remove(fixture.fresh_job)
                elif fault == 'malformed-result': fixture.evidence['result'] = {}
                elif fault == 'failed': fixture.fresh_job['conclusion'] = 'failure'
                elif fault == 'cancelled': fixture.fresh_job['conclusion'] = 'cancelled'
                elif fault == 'foreign-head': fixture.evidence['head_sha'] = 'd' * 40
                elif fault == 'wrong-attempt': fixture.evidence['run_attempt'] = 2
                elif fault == 'runner': fixture.fresh_job['runner_group_name'] = 'self-hosted'
                elif fault == 'result-archive': fixture.evidence['result']['archive_sha256'] = 'd' * 64
                elif fault == 'existing-scenario': fixture.evidence['result']['docker_scenario'] = 'existing'
                fixture.refresh_native()
                if fault == 'malformed-json':
                    raw = b'{"schema":'
                    data = zip_bytes([(native.EVIDENCE_FILE, raw)])
                    fixture.native_artifact.update(fixture.metadata(93, fixture.native_artifact['name'], data))
                    fixture.blobs[93] = data
                    fixture.logs[1003] = ('VERIFIED_NATIVE_EVIDENCE_SHA256=' + hashlib.sha256(raw).hexdigest() +
                                          '\n' + fixture.upload_log(fixture.native_artifact))
                elif fault == 'missing-log': fixture.logs[1003] = ''
                with self.assertRaises(ValueError):
                    fixture.materialize_predecessor()

    def test_binding_context_and_operation_cannot_be_changed_at_consumer(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = RepairFixture(Path(td), 'repair-existing')
            binding, _, _, _ = fixture.materialize_predecessor()
            for fault in ('kind', 'method', 'operation', 'request', 'orphan', 'mixed', 'publication-flag'):
                changed = copy.deepcopy(binding)
                if fault == 'kind': changed['kind'] = 'current-run-candidate-predecessor-bootstrap'
                elif fault == 'method': changed['selection']['method'] = 'explicit-candidate'
                elif fault == 'operation': changed['repair_operation'] = 'validate-repair'
                elif fault == 'request': changed['repair_predecessor']['run_id'] += 1
                elif fault == 'orphan': del changed['repair_operation']
                elif fault == 'mixed': changed['read_only_recovery'] = fixture.request
                elif fault == 'publication-flag': changed['publication_eligible'] = True
                with self.subTest(fault=fault), self.assertRaises(ValueError):
                    upgrade.candidate_binding(changed, fixture.target['version'], 'official', 'amd64', context=fixture.context)

    def test_latest_native_attempt_never_reuses_an_older_success(self):
        for state in ('no-new-job', 'failure', 'cancelled', 'in_progress', 'success-without-evidence'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as td:
                fixture = RepairFixture(Path(td))
                fixture.run['run_attempt'] = fixture.request['run_attempt'] = 2
                if state != 'no-new-job':
                    latest = dict(fixture.fresh_job, id=1004, run_attempt=2)
                    if state == 'in_progress': latest.update(status='in_progress', conclusion=None)
                    elif state != 'success-without-evidence': latest['conclusion'] = state
                    fixture.jobs[2] = [latest]
                with self.assertRaises(ValueError):
                    fixture.materialize_predecessor()
                if state != 'no-new-job':
                    self.assertNotIn(93, fixture.downloads)

    def test_superseded_controls_and_run_changes_do_not_reuse_selected_attempt(self):
        for fault in ('new-controls', 'new-preparation', 'run-changes-during-authentication'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = RepairFixture(Path(td))
                if fault in ('new-controls', 'new-preparation'):
                    fixture.run['run_attempt'] = fixture.request['run_attempt'] = 2
                    if fault == 'new-controls':
                        newer = dict(fixture.controls, id=900, name='publication-controls-123-2')
                        fixture.artifacts.append(newer)
                    else:
                        fixture.jobs[2] = [fixture.job(1004, 'publication_prepare', 2)]
                else:
                    original = fixture.client.run
                    reads = 0
                    def advancing_run(*args):
                        nonlocal reads
                        if args[-1].endswith('/runs/123'):
                            reads += 1
                            if reads > 1: fixture.run['run_attempt'] = 2
                        return original(*args)
                    fixture.client.run = advancing_run
                with self.assertRaises(ValueError):
                    fixture.materialize_predecessor()
                self.assertNotIn(900, fixture.downloads)

    def test_invalid_explicit_transport_never_enters_public_fallback(self):
        for operation in ('validate-repair', 'repair-existing'):
            for fault in ('wrong-run', 'missing-fresh'):
                with self.subTest(operation=operation, fault=fault), tempfile.TemporaryDirectory() as td:
                    temp = Path(td)
                    fixture = RepairFixture(temp, operation)
                    if fault == 'wrong-run': fixture.run['id'] += 1
                    else: fixture.artifacts.remove(fixture.native_artifact)
                    plan = temp / 'target-plan.json'
                    plan.write_bytes(encoded(fixture.context))
                    fixture.target_env['ONEPANEL_RESOLVED_PLAN'] = str(plan)
                    args = SimpleNamespace(version=fixture.target['version'], tag='', source='official', arch='amd64',
                        output=str(temp / 'upgrade-output'), provenance=str(temp / 'upgrade-output.json'),
                        target_input=str(temp / 'target-input'), target_provenance=str(temp / 'target.json'))
                    with patch.object(upgrade, 'current_identity', return_value=fixture.target), \
                            patch.object(upgrade, 'predecessor_candidates', side_effect=AssertionError('No public fallback')) as select, \
                            patch.object(upgrade, 'array_pages', side_effect=AssertionError('No release catalogue')) as catalogue:
                        with self.assertRaises(ValueError):
                            upgrade.materialize(args, fixture.target_env, fixture.client)
                    select.assert_not_called()
                    catalogue.assert_not_called()
                    self.assertFalse(Path(args.output).exists())
                    self.assertFalse(Path(args.provenance).exists())

    def test_predecessor_keeps_its_own_authenticated_dependency_plan(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = RepairFixture(Path(td), changed_dependencies=True)
            _, _, runtime, identity = fixture.materialize_predecessor()
            self.assertEqual(runtime['inventory']['docker']['amd64']['version'], '99.0.1')
            self.assertEqual(runtime, identity['source_plan']['resolved'])

    def test_independent_official_and_custom_source_pins_must_match_seed_plan(self):
        for source in ('official', 'custom'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as td:
                fixture = RepairFixture(Path(td), source=source)
                _, _, runtime, _ = fixture.materialize_predecessor()
                if source == 'official':
                    proof = {'kind': 'canonical-vendor', 'pin': runtime['inventory']['official']['archives']['amd64']}
                else:
                    record = runtime['upstream']['records']['amd64']
                    proof = {'kind': 'resolved-custom-source', 'pin': {'bytes': record['size'], 'sha256': record['sha256']},
                             'contract_sha256': runtime['source_contract_sha256'], 'upstream': runtime['upstream']}
                upgrade.bind_candidate_source(runtime, source, 'amd64', proof)
                for field in ('sha256', 'bytes'):
                    changed = copy.deepcopy(proof)
                    changed['pin'][field] = 'f' * 64 if field == 'sha256' else changed['pin'][field] + 1
                    with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'authenticated candidate plan'):
                        upgrade.bind_candidate_source(runtime, source, 'amd64', changed)

    def test_authenticated_source_and_dependency_pins_cannot_be_overridden_by_package(self):
        for component in ('app', 'docker', 'compose'):
            fields = ('sha256', 'bytes') if component == 'app' else ('sha256', 'bytes', 'url', 'version')
            for field in fields:
                with self.subTest(component=component, field=field), tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    archive, binding, source, proof, _ = archive_tests.IndependentArchiveTests().fixture(root)
                    dependencies = {name: json.loads((root / (name + '-sources.json')).read_text())
                                    for name in ('docker', 'compose')}
                    pin = proof['pin'] if component == 'app' else dependencies[component]['amd64']
                    if field == 'bytes': pin[field] += 1
                    elif field == 'sha256': pin[field] = 'f' * 64
                    else: pin[field] += '-changed'
                    with self.assertRaisesRegex(ValueError, 'source archive pin|dependency source|dependency size'):
                        upgrade.unpack_predecessor(archive, root / 'blocked', binding, 'official', 'amd64',
                            source, proof, root, dependency_pins=dependencies)
                    self.assertFalse((root / 'blocked').exists())


if __name__ == '__main__':
    unittest.main()
