"""Cancellation and dependency truth tables for every publication pipeline job."""
import itertools
import unittest

from test_workflow_concurrency import JOBS, WRITERS, expression

RESULTS = ['success', 'failure', 'skipped', 'cancelled']


def context(event='workflow_dispatch', operation='repair-existing'):
    return {'github.event_name': event, 'inputs.operation': operation,
            'needs.publication_check.outputs.build_required':'true',
            'needs.publication_check.outputs.checksum_required':'true' if operation == 'build' else 'false',
            'needs.publication_plan.outputs.build_required':'true',
            'needs.publication_prepare.outputs.native_count':'12',
            'needs.publication_prepare.outputs.upgrade_count':'4',
            **{'needs.' + name + '.result': 'success' for name in JOBS}}


class WorkflowCancellationTests(unittest.TestCase):
    def test_cancelled_run_disables_every_conditional_job_for_all_triggers(self):
        cases = [('push', ''), ('pull_request', ''), ('schedule', ''),
                 *[('workflow_dispatch', op) for op in
                   ['build', 'validate-repair', 'repair-existing']]]
        for name, job in JOBS.items():
            self.assertNotIn('always()', job.get('if', ''))
            self.assertIn('!cancelled()', job['if'], name)
            for (event, operation), result in itertools.product(cases, RESULTS):
                values = context(event, operation)
                values.update({'needs.' + n + '.result': result for n in JOBS})
                with self.subTest(job=name, event=event, operation=operation, result=result):
                    self.assertFalse(expression(job['if'], values, cancelled=True))

    def test_each_required_dependency_must_succeed_even_without_workflow_cancellation(self):
        cases = {
            'regression': ('build', ['publication_check']),
            'publication_plan': ('build', ['publication_check', 'regression']),
            'publication_checksums': ('build', ['publication_check']),
            'publication_packages': ('repair-existing', ['publication_plan']),
            'publication_prepare': ('repair-existing', ['publication_plan']),
            'publication_native': ('repair-existing', ['publication_plan', 'publication_prepare']),
            'publication_publish': ('repair-existing', ['publication_plan', 'publication_acceptance']),
        }
        for name, (operation, prerequisites) in cases.items():
            for results in itertools.product(RESULTS, repeat=len(prerequisites)):
                values = context(operation=operation)
                values.update({'needs.' + dep + '.result': result for dep, result in zip(prerequisites, results)})
                with self.subTest(job=name, results=results):
                    self.assertEqual(expression(JOBS[name]['if'], values),
                                     all(result == 'success' for result in results))
        for operation in ('validate-repair', 'repair-existing'):
            values = context(operation=operation)
            for result in RESULTS:
                values['needs.regression.result'] = result
                with self.subTest(operation=operation, regression=result):
                    self.assertEqual(expression(JOBS['publication_plan']['if'], values), result == 'success')

    def test_one_planner_serves_manual_validation_repair_and_normal_noop(self):
        for operation in ['validate-repair', 'repair-existing']:
            for cancelled in (False, True):
                values = context(operation=operation)
                self.assertEqual(expression(JOBS['publication_plan']['if'], values, cancelled),
                                 not cancelled)
        for event, operation in [('schedule', ''), ('workflow_dispatch', 'build')]:
            values = context(event, operation)
            values['needs.publication_check.outputs.build_required'] = 'false'
            values['needs.publication_plan.outputs.build_required'] = 'false'
            self.assertFalse(expression(JOBS['regression']['if'], values))
            self.assertFalse(expression(JOBS['publication_plan']['if'], values))
            self.assertFalse(expression(JOBS['publication_packages']['if'], values))
            self.assertFalse(expression(JOBS['publication_publish']['if'], values))

    def test_branch_failures_allow_independent_acceptance_but_cancellation_does_not(self):
        for result in RESULTS:
            values=context();values['needs.publication_packages.result']=result
            # Expected package failures are records in a successful matrix;
            # a failed matrix means a transport, validation or post-job failure.
            self.assertEqual(expression(JOBS['publication_prepare']['if'],values), result == 'success')
        for native,upgrade in itertools.product(('success','failure','skipped'),repeat=2):
            values=context();values['needs.publication_native.result']=native;values['needs.publication_upgrade.result']=upgrade
            self.assertTrue(expression(JOBS['publication_acceptance']['if'],values))
            self.assertFalse(expression(JOBS['publication_acceptance']['if'],values,cancelled=True))
        values=context();values['needs.publication_native.result']='cancelled'
        self.assertFalse(expression(JOBS['publication_acceptance']['if'],values))
        self.assertIn('publication_native',JOBS['publication_upgrade']['needs'])
        for name in ('publication_native','publication_upgrade'):
            self.assertEqual(JOBS[name]['strategy']['max-parallel'],'2')
            self.assertEqual(JOBS[name]['strategy']['fail-fast'],'false')

    def test_all_writers_and_queued_matrix_rows_require_not_cancelled(self):
        for name in [*WRITERS, 'publication_packages', 'publication_native']:
            operation = 'build' if name == 'publication_checksums' else 'repair-existing'
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
                    retained.append((name, step['id']))
                    self.assertEqual(step['uses'], 'actions/upload-artifact@v4')
                    self.assertIn('/control/', step['with']['path'])
                    self.assertNotIn('run', step)
        self.assertEqual(retained, [('publication_publish', 'journal')])


if __name__ == '__main__':
    unittest.main()
