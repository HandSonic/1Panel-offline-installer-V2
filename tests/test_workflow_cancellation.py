"""Cancellation and dependency truth tables for every publication pipeline job."""
import itertools
import unittest

from test_workflow_concurrency import JOBS, WRITERS, expression

RESULTS = ['success', 'failure', 'skipped', 'cancelled']


def context(event='workflow_dispatch', operation='repair-existing'):
    return {'github.event_name': event, 'inputs.operation': operation,
            'needs.build_plan.outputs.build_required': 'true',
            **{'needs.' + name + '.result': 'success' for name in JOBS}}


class WorkflowCancellationTests(unittest.TestCase):
    def test_cancelled_run_disables_every_conditional_job_for_all_triggers(self):
        cases = [('push', ''), ('pull_request', ''), ('schedule', ''),
                 *[('workflow_dispatch', op) for op in
                   ['build', 'validate-repair', 'repair-existing', 'validate-receipt', 'refresh-receipt']]]
        for name, job in JOBS.items():
            self.assertNotIn('always()', job.get('if', ''))
            if name == 'regression':
                # No override: GitHub's default success() cancellation behavior.
                self.assertNotIn('if', job)
                continue
            self.assertTrue(job['if'].startswith('${{ !cancelled() && '), name)
            for (event, operation), result in itertools.product(cases, RESULTS):
                values = context(event, operation)
                values.update({'needs.' + n + '.result': result for n in JOBS})
                with self.subTest(job=name, event=event, operation=operation, result=result):
                    self.assertFalse(expression(job['if'], values, cancelled=True))

    def test_each_required_dependency_must_succeed_even_without_workflow_cancellation(self):
        cases = {
            'build_plan': ('build', ['regression']),
            'build': ('build', ['build_plan', 'publication_prepare']),
            'publication_plan': ('build', ['regression', 'build_plan']),
            'publication_packages': ('repair-existing', ['publication_plan']),
            'publication_prepare': ('repair-existing', ['publication_plan', 'publication_packages']),
            'publication_native': ('repair-existing', ['publication_plan', 'publication_prepare']),
            'publication_repair': ('repair-existing', ['publication_plan', 'publication_prepare', 'publication_native']),
            'publication_revalidate': ('refresh-receipt', ['regression']),
            'publication_receipt_refresh': ('refresh-receipt', ['publication_revalidate']),
        }
        for name, (operation, prerequisites) in cases.items():
            for results in itertools.product(RESULTS, repeat=len(prerequisites)):
                values = context(operation=operation)
                values.update({'needs.' + dep + '.result': result for dep, result in zip(prerequisites, results)})
                with self.subTest(job=name, results=results):
                    self.assertEqual(expression(JOBS[name]['if'], values),
                                     all(result == 'success' for result in results))

    def test_skipped_normal_plan_still_allows_manual_validation_and_repair(self):
        for operation in ['validate-repair', 'repair-existing']:
            for result, cancelled in itertools.product(RESULTS, [False, True]):
                values = context(operation=operation)
                values['needs.build_plan.result'] = 'skipped'
                values['needs.build_plan.outputs.build_required'] = ''
                values['needs.regression.result'] = result
                self.assertEqual(expression(JOBS['publication_plan']['if'], values, cancelled),
                                 result == 'success' and not cancelled)
        for event, operation in [('push', ''), ('schedule', ''), ('workflow_dispatch', 'build')]:
            values = context(event, operation)
            values['needs.build_plan.outputs.build_required'] = 'false'
            self.assertFalse(expression(JOBS['publication_plan']['if'], values))
            self.assertFalse(expression(JOBS['build']['if'], values))

    def test_all_writers_and_queued_matrix_rows_require_not_cancelled(self):
        for name in [*WRITERS, 'publication_packages', 'publication_native']:
            operation = {'build': 'build', 'publication_receipt_refresh': 'refresh-receipt'}.get(name, 'repair-existing')
            values = context(operation=operation)
            self.assertTrue(expression(JOBS[name]['if'], values))
            self.assertFalse(expression(JOBS[name]['if'], values, cancelled=True))
            self.assertNotIn('continue-on-error', JOBS[name])
        self.assertEqual(JOBS['publication_native']['strategy']['fail-fast'], 'false')
        self.assertEqual(JOBS['publication_native']['strategy']['max-parallel'], '2')

    def test_always_is_reserved_for_existing_publication_journal_exports(self):
        retained = []
        for name, job in JOBS.items():
            for step in job['steps']:
                if 'always()' in step.get('if', ''):
                    retained.append((name, step['name']))
                    self.assertEqual(step['uses'], 'actions/upload-artifact@v4')
                    self.assertIn('/control/', step['with']['path'])
                    self.assertNotIn('run', step)
        self.assertEqual(retained, [
            ('publication_repair', 'Preserve publication journal on success or failure'),
            ('publication_receipt_refresh', 'Preserve receipt-refresh journal on success or failure')])


if __name__ == '__main__':
    unittest.main()
