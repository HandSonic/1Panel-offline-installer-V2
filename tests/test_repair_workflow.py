"""Exercise explicit repair selector routing without executing package payloads."""
import itertools
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

import yaml

from test_workflow_concurrency import expression


ROOT = Path(__file__).resolve().parents[1]
SELECTORS = {
    'CANDIDATE_PREDECESSOR': 'candidate_predecessor',
    'REPAIR_PREDECESSOR': 'repair_predecessor',
}
PINS = json.dumps({
    'version': 'v2.1.13', 'run_id': 101, 'run_attempt': 1,
    'head_sha': 'a' * 40, 'controls_artifact_id': 102,
    'receipt_sha256': 'b' * 64, 'plan_sha256': 'c' * 64,
})


class RepairWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.load(
            (ROOT / '.github/workflows/build-offline-v2.yml').read_text(),
            Loader=yaml.BaseLoader)
        cls.jobs = cls.workflow['jobs']

    def writer_context(self, **changes):
        return {
            'github.event_name': 'workflow_dispatch',
            'inputs.operation': 'repair-existing',
            'inputs.candidate_predecessor': '',
            'inputs.repair_predecessor': '',
            'needs.build_plan.result': 'success',
            'needs.publication_plan.result': 'success',
            'needs.publication_acceptance.result': 'success',
            'needs.publication_plan.outputs.build_required': 'true',
            **changes,
        }

    def test_repair_selector_is_an_optional_explicit_dispatch_string(self):
        inputs = self.workflow['on']['workflow_dispatch']['inputs']
        repair = inputs['repair_predecessor']
        self.assertEqual(repair['type'], 'string')
        self.assertEqual(repair['default'], '')
        self.assertNotEqual(repair.get('required'), 'true')
        self.assertIn('repair-existing', repair['description'])
        self.assertIn('JSON', repair['description'])
        self.assertIn('Read-only', inputs['candidate_predecessor']['description'])

    def test_both_selectors_reach_only_the_explicit_consuming_steps(self):
        expected = {
            'build': {'Authenticate admitted artifacts and publish recoverably'},
            'publication_plan': {'Resolve the exact package matrix and shared upstream input'},
            'publication_native': {
                'Fetch exact same-run candidate shard and bind producer evidence',
                'Bind real installation result to exact run and hosted runner'},
            'publication_upgrade': {
                'Authenticate target and explicit candidate or canonical public predecessor',
                'Verify real upgrade and rollback or proven initial installation',
                'Bind native result and its explicit applicability'},
            'publication_acceptance': {'Authenticate package and native results and admit each product'},
            'publication_repair': {'Authenticate admitted artifacts and repair recoverably'},
        }
        self.assertNotIn('inputs.', json.dumps(self.workflow.get('env', {})))
        for variable, input_name in SELECTORS.items():
            with self.subTest(variable=variable):
                self.assertNotIn(variable, self.workflow.get('env', {}))
                found = {}
                for name, job in self.jobs.items():
                    self.assertNotIn(variable, job.get('env', {}))
                    for step in job['steps']:
                        value = step.get('env', {}).get(variable)
                        if value:
                            self.assertEqual(value, '${{ inputs.' + input_name + ' }}')
                            found.setdefault(name, set()).add(step['name'])
                self.assertEqual(found, expected)

    def test_regression_clears_either_or_both_inherited_selectors(self):
        job = self.jobs['regression']
        step = next(s for s in job['steps']
                    if s.get('name') == 'Run isolated regression and syntax checks')
        self.assertIn('python3 -m unittest discover -s tests -v', step['run'])
        self.assertNotIn('inputs.', json.dumps(job.get('env', {})))
        self.assertNotIn('inputs.', json.dumps(step.get('env', {})))
        for variable in SELECTORS:
            self.assertEqual(step['env'][variable], '')
        for candidate, repair in itertools.product(('', PINS), repeat=2):
            with self.subTest(candidate=bool(candidate), repair=bool(repair)):
                env = dict(os.environ, CANDIDATE_PREDECESSOR=candidate,
                           REPAIR_PREDECESSOR=repair)
                env.update(self.workflow.get('env', {}))
                env.update(job.get('env', {}))
                env.update(step['env'])
                result = subprocess.run([
                    sys.executable, '-B', '-c',
                    'import os; assert os.environ["CANDIDATE_PREDECESSOR"] == ""; '
                    'assert os.environ["REPAIR_PREDECESSOR"] == ""'],
                    env=env, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_upgrade_preserves_both_selectors_across_sudo(self):
        command = next(s['run'] for s in self.jobs['publication_upgrade']['steps']
                       if 'sudo --preserve-env' in s.get('run', ''))
        preserved = set(re.search(r'--preserve-env=([^\s]+)', command)[1].split(','))
        self.assertTrue(set(SELECTORS).issubset(preserved))
        self.assertTrue({'GITHUB_EVENT_NAME', 'PUBLICATION_OPERATION',
                         'ONEPANEL_RESOLVED_PLAN', 'ONEPANEL_RESOLVED_PLAN_SHA256'} <= preserved)
        self.assertNotIn('GH_TOKEN', preserved)

    def test_ordinary_writer_rejects_either_nonempty_selector(self):
        for event, candidate, repair in itertools.product(
                ('push', 'schedule', 'workflow_dispatch'), ('', PINS, ' '), ('', PINS, ' ')):
            with self.subTest(event=event, candidate=bool(candidate), repair=bool(repair)):
                context = self.writer_context(**{
                    'github.event_name': event, 'inputs.operation': 'build',
                    'inputs.candidate_predecessor': candidate,
                    'inputs.repair_predecessor': repair})
                self.assertEqual(bool(expression(self.jobs['build']['if'], context)),
                                 candidate == '' and repair == '')

    def test_repair_writer_keeps_manual_operation_and_readonly_exclusion(self):
        for event, operation, candidate, repair in itertools.product(
                ('push', 'schedule', 'pull_request', 'workflow_dispatch'),
                ('build', 'validate-repair', 'repair-existing'), ('', PINS), ('', PINS)):
            with self.subTest(event=event, operation=operation,
                              candidate=bool(candidate), repair=bool(repair)):
                context = self.writer_context(**{
                    'github.event_name': event, 'inputs.operation': operation,
                    'inputs.candidate_predecessor': candidate,
                    'inputs.repair_predecessor': repair})
                allowed = event == 'workflow_dispatch' and operation == 'repair-existing' and candidate == ''
                self.assertEqual(bool(expression(self.jobs['publication_repair']['if'], context)), allowed)
                self.assertFalse(expression(self.jobs['publication_repair']['if'], context, cancelled=True))
        for dependency, result in itertools.product(
                ('publication_plan', 'publication_acceptance'), ('failure', 'cancelled', 'skipped')):
            context = self.writer_context(**{
                'inputs.repair_predecessor': PINS, 'needs.' + dependency + '.result': result})
            self.assertFalse(expression(self.jobs['publication_repair']['if'], context))

    def test_acceptance_finalizes_repairs_but_readonly_reports_never_publish(self):
        job = self.jobs['publication_acceptance']
        self.assertEqual(job['permissions'], {'contents': 'read', 'actions': 'read'})
        artifacts = [s for s in job['steps'] if s.get('uses') == 'actions/upload-artifact@v4']
        final = [s for s in artifacts if s['with']['name'].startswith('native-admitted-')]
        reports = [s for s in artifacts if s['with']['name'].startswith('read-only-native-recovery-')]
        self.assertEqual(len(final), 2)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]['with']['path'],
                         '${{ runner.temp }}/final-publication/read-only-native-recovery.json')
        for candidate, repair in itertools.product(('', PINS), repeat=2):
            context = self.writer_context(**{
                'inputs.candidate_predecessor': candidate, 'inputs.repair_predecessor': repair})
            for step in final:
                self.assertEqual(bool(expression(step['if'], context)), candidate == '')
            self.assertEqual(bool(expression(reports[0]['if'], context)), candidate != '')
            if candidate:
                for writer in ('build', 'publication_repair'):
                    self.assertFalse(expression(self.jobs[writer]['if'], context))


if __name__ == '__main__':
    unittest.main()
