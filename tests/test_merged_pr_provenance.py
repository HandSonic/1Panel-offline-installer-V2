"""Merged-PR recovery uses invented records in real checkout-log shapes."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from test_runtime_outcomes import UpperClient
from upstream_outcomes import PR_FAILURE_REASONS, REPO, safe_pr_failure_reason, verify_jobs


class MergedClient(UpperClient):
    repo = REPO

    def __init__(self):
        super().__init__(pr=True)
        self.state = self.run
        del self.run
        self.state['pull_requests'] = []
        self.pr = {'number': 23, 'state': 'closed', 'merged': True,
                   'head': {'sha': self.head, 'repo': {'full_name': REPO}},
                   'base': {'sha': self.base, 'repo': {'full_name': REPO}}}
        self.build = next(job for job in self.jobs if job['name'] == 'build')
        self.step = {'name': 'Run actions/checkout@v4', 'number': 3, 'status': 'completed',
                     'conclusion': 'success', 'started_at': '2030-01-02T03:04:05Z',
                     'completed_at': '2030-01-02T03:04:06Z'}
        self.build['steps'] = [self.step]
        bodies = [
            '##[group]Run actions/checkout@v4',
            'with:',
            '  repository: ' + REPO,
            '##[endgroup]',
            'Syncing repository: ' + REPO,
            '##[group]Fetching the repository',
            '[command]/usr/bin/git -c protocol.version=2 fetch --no-tags --prune '
            '--no-recurse-submodules --depth=1 origin +' + self.executed + ':refs/remotes/pull/23/merge',
            '##[endgroup]',
            '##[group]Checking out the ref',
            '[command]/usr/bin/git checkout --progress --force refs/remotes/pull/23/merge',
            'HEAD is now at ' + self.executed[:7] + ' Merge ' + self.head + ' into ' + self.base,
            '##[endgroup]',
            '[command]/usr/bin/git log -1 --format=%H',
            self.executed,
        ]
        self.lines = [f'2030-01-02T03:04:06.{index:07d}Z {body}' for index, body in enumerate(bodies)]
        self.calls = []

    def run(self, *args):
        endpoint = args[-1]
        self.calls.append(endpoint)
        base = f'repos/{REPO}'
        responses = {
            base + '/actions/runs/91/attempts/2': self.state,
            base + '/actions/runs/91/attempts/2/jobs?per_page=100&page=1':
                {'total_count': len(self.jobs), 'jobs': self.jobs},
            base + '/pulls/23': self.pr,
            base + '/git/commits/' + self.executed: self.merge,
        }
        if endpoint == base + '/actions/jobs/' + str(self.build['id']) + '/logs':
            return '\n'.join(self.lines) + '\n'
        return json.dumps(responses[endpoint])


class MergedPRProvenanceTests(unittest.TestCase):
    def verify(self, client):
        return verify_jobs(client.manifest, 'v2.99.0', client.executed, client)

    def test_empty_run_pr_array_recovers_exact_merged_checkout(self):
        client = MergedClient()
        self.assertEqual(self.verify(client), [row['architecture'] for row in client.manifest['artifacts']])
        self.assertIn(f'repos/{REPO}/actions/jobs/3/logs', client.calls)
        self.assertIn(f'repos/{REPO}/pulls/23', client.calls)
        self.assertIn(f'repos/{REPO}/git/commits/{client.executed}', client.calls)

    def test_existing_run_pr_binding_does_not_use_fallback(self):
        client = MergedClient()
        client.state['pull_requests'] = [{'head': {'sha': client.head}, 'base': {'sha': client.base}}]
        client.lines = []
        self.verify(client)
        self.assertFalse(any('/logs' in call or '/pulls/' in call for call in client.calls))

    def test_only_explicit_empty_array_can_recover(self):
        for value in (None, {}, '', [{'head': {'sha': 'd' * 40}}], [{}, {}], [None]):
            with self.subTest(value=value):
                client = MergedClient()
                client.state['pull_requests'] = value
                with self.assertRaises(ValueError):
                    self.verify(client)
                self.assertFalse(any('/logs' in call for call in client.calls))
        client = MergedClient()
        del client.state['pull_requests']
        with self.assertRaises(ValueError):
            self.verify(client)

    def test_exact_job_identity_and_success_precede_log_recovery(self):
        for key, value in [('run_id', 92), ('run_attempt', 1), ('head_sha', 'd' * 40),
                           ('status', 'in_progress'), ('conclusion', 'failure'), ('id', 0)]:
            with self.subTest(key=key):
                client = MergedClient()
                client.build[key] = value
                with self.assertRaises(ValueError):
                    self.verify(client)
                self.assertFalse(any('/logs' in call for call in client.calls))
        client = MergedClient()
        client.jobs.append(copy.deepcopy(client.build))
        with self.assertRaises(ValueError):
            self.verify(client)

    def test_nonempty_pr_nested_types_fail_as_validation_errors(self):
        for side in ('base', 'head'):
            for value in (None, [], 'invalid', 17):
                client = MergedClient()
                record = {'base': {'sha': client.base}, 'head': {'sha': client.head}}
                record[side] = value
                client.state['pull_requests'] = [record]
                with self.assertRaisesRegex(ValueError, 'Ambiguous PR producer head'):
                    self.verify(client)
                self.assertFalse(any('/logs' in call for call in client.calls))

    def test_later_step_cannot_supply_checkout_evidence_within_same_second(self):
        for index in (1, 6, 9, 12, 13):
            client = MergedClient()
            client.lines.insert(index, '2030-01-02T03:04:06.0000100Z '
                                '##[group]Run actions/download-artifact@v4')
            with self.assertRaises(ValueError):
                self.verify(client)
        client = MergedClient()
        client.lines.append('2030-01-02T03:04:06.0000100Z ##[group]Run actions/download-artifact@v4')
        self.verify(client)

    def test_checkout_log_parser_budget_is_enforced(self):
        client = MergedClient()
        client.lines.append('x' * (8 * 1024 * 1024))
        with self.assertRaisesRegex(ValueError, 'parser budget'):
            self.verify(client)

    def test_merged_pr_metadata_must_match_both_frozen_parents(self):
        mutations = [lambda c: c.pr.update(number=24), lambda c: c.pr.update(merged=False),
                     lambda c: c.pr.update(merged=1), lambda c: c.pr.update(state='open'),
                     lambda c: c.pr['head'].update(sha='d' * 40),
                     lambda c: c.pr['base'].update(sha='d' * 40),
                     lambda c: c.pr['head']['repo'].update(full_name='other/repository'),
                     lambda c: c.pr['base']['repo'].update(full_name='other/repository')]
        for mutate in mutations:
            client = MergedClient()
            mutate(client)
            with self.assertRaises(ValueError):
                self.verify(client)

    def test_arbitrary_merge_object_and_wrong_parent_order_are_rejected(self):
        for parents in [('b' * 40, 'd' * 40), ('d' * 40, 'a' * 40),
                        ('a' * 40, 'b' * 40), ('a' * 40,), ('b' * 40, 'a' * 40, 'd' * 40)]:
            client = MergedClient()
            client.merge['parents'] = [{'sha': sha} for sha in parents]
            with self.assertRaises(ValueError):
                self.verify(client)
        client = MergedClient()
        client.merge['sha'] = 'd' * 40
        with self.assertRaises(ValueError):
            self.verify(client)

    def test_api_manifest_or_log_head_cannot_be_forged(self):
        for target in ('api', 'manifest', 'log'):
            client = MergedClient()
            if target == 'api':
                client.state['head_sha'] = 'd' * 40
            elif target == 'manifest':
                client.manifest['producer_head_sha'] = 'd' * 40
            else:
                client.lines[10] = client.lines[10].replace(client.head, 'd' * 40)
            with self.assertRaises(ValueError):
                self.verify(client)

    def test_checkout_sequence_requires_exact_full_sha_and_pr_ref(self):
        for index, old, new in [(6, 'c' * 40, 'd' * 40), (6, 'c' * 40, 'c' * 7),
                                (9, '/23/', '/24/'), (10, 'b' * 40, 'd' * 40),
                                (13, 'c' * 40, 'd' * 40), (13, 'c' * 40, 'c' * 7),
                                (4, REPO, 'other/repository')]:
            client = MergedClient()
            client.lines[index] = client.lines[index].replace(old, new)
            with self.assertRaises(ValueError):
                self.verify(client)
        client = MergedClient()
        client.lines[12], client.lines[13] = client.lines[13], client.lines[12]
        with self.assertRaises(ValueError):
            self.verify(client)

    def test_missing_and_later_duplicate_markers_reject(self):
        for index in (0, 4, 6, 9, 10, 12, 13):
            for duplicate in (False, True):
                client = MergedClient()
                if duplicate:
                    client.lines.append(client.lines[index].replace('03:04:06.', '03:04:09.'))
                else:
                    client.lines.pop(index)
                with self.assertRaises(ValueError):
                    self.verify(client)
        client = MergedClient()
        client.lines.append(client.executed)  # A marker without a server timestamp is also ambiguous.
        with self.assertRaises(ValueError):
            self.verify(client)

    def test_step_identity_interval_and_success_are_required(self):
        for field, value in [('name', 'Custom step'), ('status', 'in_progress'),
                             ('conclusion', 'failure'), ('number', 0),
                             ('started_at', '2030-01-02T03:04:08Z'),
                             ('completed_at', '2030-01-02T03:04:05Z'),
                             ('started_at', 'invalid')]:
            client = MergedClient()
            client.step[field] = value
            with self.assertRaises(ValueError):
                self.verify(client)
        client = MergedClient()
        client.build['steps'].append(copy.deepcopy(client.step))
        with self.assertRaises(ValueError):
            self.verify(client)

    def test_existing_ansi_safe_reader_prevents_token_reassembly(self):
        client = MergedClient()
        client.lines[13] = client.lines[13].replace(client.executed, '\x1b[32m' + client.executed + '\x1b[0m')
        self.verify(client)
        for escape in ('\x1b[0m', '\x1b[2K', '\x00'):
            client = MergedClient()
            client.lines[13] = client.lines[13].replace(client.executed, client.executed[:20] + escape + client.executed[20:])
            with self.assertRaises(ValueError):
                self.verify(client)


class ProvenanceDiagnosticTests(unittest.TestCase):
    def test_only_exact_literal_valueerror_arguments_are_exposed(self):
        for reason in PR_FAILURE_REASONS:
            self.assertEqual(safe_pr_failure_reason(ValueError(reason)), reason)
            self.assertLessEqual(len(reason), 96)
        class ExternalError(ValueError):
            def __str__(self):
                raise AssertionError('External exception text must not be inspected')
        for error in (ValueError(), ValueError('Ambiguous PR producer head', 'extra'),
                      ValueError('Ambiguous PR producer head\nsecret'), ValueError(object()),
                      ExternalError('Ambiguous PR producer head'), OSError('Ambiguous PR producer head')):
            self.assertIsNone(safe_pr_failure_reason(error))

    def test_planner_and_failed_shard_reveal_safe_reason_but_no_external_text(self):
        import package_matrix
        import resolved_transport
        from test_automatic_source_consumption import SyntheticUpstream, VERSION, REPOSITORY
        secret = 'synthetic-sensitive-token'
        url = 'https://example.invalid/private?signature=' + secret
        errors = [
            (ValueError('Ambiguous PR producer head'), ': Ambiguous PR producer head'),
            (ValueError('Upper checkout does not bind the executed PR merge'),
             ': Upper checkout does not bind the executed PR merge'),
            (ValueError(url), ''),
            (ValueError('Ambiguous PR producer head: ' + url), ''),
            (OSError(url), ''),
            (HTTPError(url, 403, secret, {}, None), ''),
            (subprocess.CalledProcessError(1, ['gh', secret], output=url, stderr=secret), ''),
        ]
        for error, suffix in errors:
            with self.subTest(error=type(error).__name__, suffix=suffix), tempfile.TemporaryDirectory() as temporary:
                upper = SyntheticUpstream()
                args = SimpleNamespace(version=VERSION, tag=VERSION, repository=REPOSITORY,
                                       mode='stable', work=str(Path(temporary) / 'input'), upstream_source='verified-ci',
                                       run_id='91', artifact_id='92', artifact_sha256='d' * 64, build_commit='a' * 40,
                                       source='custom', arch='amd64')
                output, errors_output = io.StringIO(), io.StringIO()
                environment = {'GITHUB_SHA': 'a' * 40, 'GITHUB_RUN_ID': '99', 'GITHUB_OUTPUT': ''}
                with patch.dict(os.environ, environment), \
                     patch.object(package_matrix, 'fetch_ci_bundle', side_effect=error), \
                     patch.object(resolved_transport, 'urlopen', side_effect=upper.urlopen), \
                     redirect_stdout(output), redirect_stderr(errors_output):
                    package_matrix.plan(args)
                    _, plan = package_matrix.load_plan(args)
                    expected = ('Custom input authentication failed: Verified CI input authentication failed: ' +
                                type(error).__name__ + suffix)
                    self.assertEqual(plan['resolved']['custom_failure'], expected)
                    self.assertIsNone(plan['resolved']['source_contract'])
                    with patch.object(package_matrix.subprocess, 'run') as execute, self.assertRaises(ValueError) as failure:
                        package_matrix.shard(args)
                    execute.assert_not_called()
                    self.assertEqual(str(failure.exception), expected)
                visible = json.dumps(plan) + output.getvalue() + errors_output.getvalue() + str(failure.exception)
                self.assertNotIn(secret, visible)
                self.assertNotIn(url, visible)


if __name__ == '__main__':
    unittest.main()
