"""Partial-rerun API copies must not become immutable artifact producers."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import native_candidate_input as candidate
import runtime_native_acceptance as native
import publication_outcomes as outcomes

IDENTITY = {'run_id': 42, 'run_attempt': 3, 'head_sha': 'a' * 40, 'repository': 'owner/repo'}


def original(name='publication_prepare'):
    return dict(id=101, run_id=42, run_attempt=1, head_sha='a' * 40, name=name,
        status='completed', conclusion='success', created_at='2026-10-12T02:00:00Z',
        started_at='2026-10-12T02:00:01Z', completed_at='2026-10-12T02:10:00Z',
        runner_id=123, runner_name='runner', runner_group_id=0, runner_group_name='GitHub Actions',
        labels=['ubuntu-24.04'], steps=[{'name': 'Upload', 'conclusion': 'success',
            'started_at': '2026-10-12T02:09:00Z', 'completed_at': '2026-10-12T02:10:00Z'}])


def clone(job, attempt=2):
    return dict(copy.deepcopy(job), id=100 + attempt, run_attempt=attempt,
                created_at='2026-10-12T07:20:00Z', runner_group_id=None)


def executed(job, conclusion, attempt=2):
    return dict(clone(job, attempt), conclusion=conclusion,
                started_at='2026-10-12T07:20:01Z', completed_at='2026-10-12T07:21:00Z')


class CloneTests(unittest.TestCase):
    def prepare(self, jobs, attempt=1):
        control = {'name': f'publication-controls-42-{attempt}'}
        with patch.object(candidate, 'listed', return_value=jobs):
            return candidate.select_prepare_attempt(SimpleNamespace(repo='owner/repo'), control, [control], IDENTITY)

    def test_prepare_chain_preserves_original_attempt(self):
        first = original()
        self.assertEqual(self.prepare([clone(first, 3), first, clone(first)]), 1)
        self.assertIs(candidate.latest_executed_job([first, clone(first)], IDENTITY), first)

    def test_prepare_real_newer_failure_cancel_or_success_supersedes(self):
        first = original()
        for status in ('failure', 'cancelled', 'success'):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.prepare([first, executed(first, status)])
        self.assertEqual(self.prepare([first, executed(first, 'success')], 2), 2)

    def test_real_failure_wins_even_after_later_copy_of_old_success(self):
        first = original()
        with self.assertRaises(ValueError):
            self.prepare([first, executed(first, 'failure'), clone(first, 3)])

    def test_unproven_copies_and_ambiguous_identity_fail_closed(self):
        first = original()
        for key, value in [('runner_id', 999), ('steps', []), ('runner_group_id', 99), ('head_sha', 'b'*40),
                           ('started_at', '2026-10-12T02:00:02Z'), ('conclusion', 'failure')]:
            bad = clone(first); bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.prepare([first, bad])
        for jobs in ([clone(first)], [first, dict(first, id=999)],
                     [dict(first, created_at='bad'), clone(first)]):
            with self.assertRaises(ValueError):
                self.prepare(jobs)

    def package_outcome(self, jobs):
        jobs = copy.deepcopy(jobs)
        for job in jobs:
            job['html_url'] = f'https://github.com/{outcomes.REPOSITORY}/actions/runs/42/job/{job["id"]}'
        return outcomes.collect(jobs, outcomes.products({'official': ['amd64']}), 42, 3, 'a' * 40)[0]

    def test_package_copies_keep_original_shard_producer_identity(self):
        first = original('publication_packages (official, amd64)')
        result = self.package_outcome([clone(first, 3), first, clone(first)])
        self.assertEqual((result['status'], result['producer_run_attempt'], result['job_id']),
                         ('success', 1, first['id']))

    def test_package_real_new_execution_wins_over_later_old_success_copy(self):
        first = original('publication_packages (official, amd64)')
        for conclusion in ('success', 'failure'):
            with self.subTest(conclusion=conclusion):
                newer = executed(first, conclusion)
                result = self.package_outcome([first, newer, clone(first, 3)])
                self.assertEqual((result['status'], result['producer_run_attempt'], result['job_id']),
                                 (conclusion, 2, newer['id']))

    def test_package_unproven_copy_ambiguity_and_cancellation_cannot_reuse_old_success(self):
        first = original('publication_packages (official, amd64)')
        malformed = clone(first); malformed['runner_id'] = 999
        for jobs in ([clone(first)], [first, malformed], [first, dict(first, id=999)],
                     [first, executed(first, 'cancelled')]):
            with self.subTest(jobs=jobs), self.assertRaises(ValueError):
                self.package_outcome(jobs)

    def collect(self, jobs, bad_log=False):
        key = 'install-official-amd64'  # Transport selection only; row semantics mocked below.
        artifact = {'name': native.artifact_name(key, 1), 'id': 501,
                    'digest': 'sha256:' + 'b'*64, 'size_in_bytes': 100}
        prep = {'repository': 'owner/repo', 'workflow_run_id': 42, 'workflow_commit': 'a'*40,
                'version': 'v2.2.1', 'receipt_sha256': 'c'*64, 'plan_sha256': 'd'*64,
                'controls_artifact_id': 600}
        evidence = {'runner': {}, 'result': {'_file_sha256': 'e'*64},
                    'candidate': {'archive_sha256': 'f'*64}}
        raw = json.dumps(evidence).encode()
        import hashlib
        sha = hashlib.sha256(raw).hexdigest()
        log = f'SHA256 digest of uploaded artifact zip is {"b"*64}\nArtifact {artifact["name"]}.zip successfully finalized. Artifact ID 501\nVERIFIED_NATIVE_EVIDENCE_SHA256={sha}\n'
        if bad_log:
            log = log.replace('Artifact ID 501', 'Artifact ID 999')
        def extract(archive, destination, *args):
            destination.mkdir(); (destination / native.EVIDENCE_FILE).write_bytes(raw)
        with tempfile.TemporaryDirectory() as work, \
             patch.object(native, 'preparation_rows', return_value=({'row': {'key': 'row'}}, {'row': {'status': 'success'}})), \
             patch.object(native, 'authenticated_context', return_value={}), \
             patch.object(native, 'required_results', return_value=[key]), \
             patch.object(native, 'job_name', return_value='native-test'), \
             patch.object(native, 'listed', side_effect=[jobs, [artifact]]), \
             patch.object(native, 'verify_artifact') as verify_artifact, \
             patch.object(native, 'api', return_value=artifact), \
             patch.object(native, 'read_job_log', return_value=log) as read_log, \
             patch.object(native, 'download_verified'), \
             patch.object(native, 'extract_zip', side_effect=extract), \
             patch.object(native, 'verify_evidence') as verify_evidence:
            result = native.collect(SimpleNamespace(repo='owner/repo'), IDENTITY, prep, work,
                                    run_verifier=lambda *args: {})[key]
            return result, read_log.call_args_list, verify_evidence.call_args_list

    def test_native_uses_original_artifact_job_attempt_and_evidence_binding(self):
        first = original('native-test')
        result, logs, evidence = self.collect([first, clone(first), clone(first, 3)])
        self.assertEqual(result['status'], 'success')
        self.assertEqual((result['job_id'], result['run_attempt'], result['artifact_id']), (101, 1, 501))
        self.assertEqual(logs[0].args[1], 101)
        self.assertEqual(evidence[0].args[3], first)

    def test_native_newer_failure_cancel_active_never_falls_back(self):
        first = original('native-test')
        for conclusion in ('failure', 'cancelled', None):
            result, logs, evidence = self.collect([first, executed(first, conclusion)])
            self.assertEqual(result['status'], 'failure')
            self.assertEqual(logs, [])

    def test_native_original_upload_log_still_required(self):
        first = original('native-test')
        result, logs, evidence = self.collect([first, clone(first)], bad_log=True)
        self.assertEqual(result['status'], 'failure')
        self.assertEqual(evidence, [])

    def test_native_real_new_success_requires_its_own_artifact(self):
        first = original('native-test')
        result, logs, evidence = self.collect([first, executed(first, 'success')])
        self.assertEqual(result['status'], 'failure')
        self.assertEqual(logs, [])


if __name__ == '__main__':
    unittest.main()
