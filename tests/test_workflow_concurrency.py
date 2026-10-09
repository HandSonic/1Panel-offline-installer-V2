"""Exercise checked-in admission, resolved writer keys and trigger routing offline."""
import itertools
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.load((ROOT / '.github/workflows/build-offline-v2.yml').read_text(), Loader=yaml.BaseLoader)
JOBS = WORKFLOW['jobs']
WRITERS = {'build': 'build_plan', 'publication_repair': 'publication_plan',
           'publication_receipt_refresh': 'publication_revalidate'}


def expression(text, context, cancelled=False):
    text = text.removeprefix('${{').removesuffix('}}').strip()
    text = text.replace('!cancelled()', repr(not cancelled)).replace('always()', 'True').replace('&&', ' and ').replace('||', ' or ')
    text = re.sub(r'\b(?:github|inputs|needs)\.[a-zA-Z_][a-zA-Z_0-9.]*',
                  lambda m: repr(context.get(m[0], '')), text)
    return eval(text, {'__builtins__': {}}, {})


def group(text, context):
    return re.sub(r'\$\{\{(.*?)\}\}', lambda m: str(expression(m[1], context)), text).lower()


def run_step(script, directory, **environment):
    output = directory / 'outputs'
    result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=directory,
                            env=dict(os.environ, GITHUB_OUTPUT=str(output), **environment),
                            text=True, capture_output=True)
    outputs = dict(line.split('=', 1) for line in output.read_text().splitlines()) if output.exists() else {}
    return result, outputs


class WorkflowConcurrencyTests(unittest.TestCase):
    def test_independent_runs_and_attempts_never_replace_each_other(self):
        self.assertEqual(set(WORKFLOW['on']), {'push', 'pull_request', 'schedule', 'workflow_dispatch'})
        contexts = []
        for run_id, (event, operation) in enumerate([
                ('push', ''), ('pull_request', ''), ('schedule', ''),
                *[('workflow_dispatch', op) for op in
                  ['build', 'validate-repair', 'repair-existing', 'validate-receipt', 'refresh-receipt']]], 100):
            for attempt in [1, 2]:
                contexts.append({'github.event_name': event, 'inputs.operation': operation,
                                 'github.ref': 'refs/heads/master', 'github.run_id': run_id,
                                 'github.run_attempt': attempt})
        keys = [group(WORKFLOW['concurrency']['group'], c) for c in contexts]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(WORKFLOW['concurrency']['cancel-in-progress'], 'false')

    def test_all_writers_share_resolved_tag_lock_across_refs_and_operations(self):
        actual = {name for name, job in JOBS.items() if job.get('permissions', {}).get('contents') == 'write'}
        self.assertEqual(actual, set(WRITERS))
        keys = {}
        for tag in ['v2.2.1', 'v2.2.2', 'v2.3.2-offline-r1']:
            keys[tag] = set()
            for (writer, planner), ref in itertools.product(WRITERS.items(), ['refs/heads/master', 'refs/heads/repair']):
                job = JOBS[writer]
                needs = job['needs'] if isinstance(job['needs'], list) else [job['needs']]
                self.assertIn(planner, needs)
                self.assertEqual(job['concurrency']['queue'], 'max')
                self.assertEqual(job['concurrency']['cancel-in-progress'], 'false')
                self.assertIn('release_tag', JOBS[planner]['outputs'])
                lock = job['concurrency']['group']
                self.assertEqual(lock, 'offline-release-${{ needs.' + planner + '.outputs.release_tag }}')
                for supplied in ['', 'unresolved-latest', 'v9.9.9']:
                    context = {'github.ref': ref, 'inputs.operation': writer,
                               'inputs.version': supplied, 'inputs.release_tag': supplied,
                               'needs.' + planner + '.outputs.release_tag': tag}
                    keys[tag].add(group(lock, context))
                self.assertEqual(group(lock, {'needs.' + planner + '.outputs.release_tag': tag.upper()}),
                                 group(lock, {'needs.' + planner + '.outputs.release_tag': tag}))
            self.assertEqual(len(keys[tag]), 1)
        self.assertEqual(len(set.union(*keys.values())), 3)

    def test_readonly_work_never_holds_writer_lock(self):
        for name in set(JOBS) - set(WRITERS):
            with self.subTest(job=name):
                self.assertNotIn('concurrency', JOBS[name])
                self.assertEqual(JOBS[name]['permissions']['contents'], 'read')
        self.assertEqual(JOBS['publication_packages']['strategy']['max-parallel'], '3')
        self.assertEqual(JOBS['publication_native']['strategy']['max-parallel'], '2')

    def test_trigger_routes_preserve_write_and_native_gates(self):
        stages = ['build_plan', 'publication_plan', 'publication_revalidate', 'publication_native', *WRITERS]
        cases = [
            ('pull_request', '', set()),
            ('push', '', {'build_plan', 'publication_plan', 'build'}),
            ('schedule', '', {'build_plan', 'publication_plan', 'build'}),
            ('workflow_dispatch', 'build', {'build_plan', 'publication_plan', 'build'}),
            ('workflow_dispatch', 'validate-repair', {'publication_plan', 'publication_native'}),
            ('workflow_dispatch', 'repair-existing', {'publication_plan', 'publication_native', 'publication_repair'}),
            ('workflow_dispatch', 'validate-receipt', {'publication_revalidate'}),
            ('workflow_dispatch', 'refresh-receipt', {'publication_revalidate', 'publication_receipt_refresh'}),
        ]
        for event, operation, expected in cases:
            with self.subTest(event=event, operation=operation):
                normal = event in ['push', 'schedule'] or operation == 'build'
                context = {'github.event_name': event, 'inputs.operation': operation,
                           'needs.build_plan.outputs.build_required': 'true' if normal else '',
                           **{'needs.' + n + '.result': 'success' for n in JOBS}}
                if not normal:
                    context['needs.build_plan.result'] = 'skipped'
                selected = {name for name in stages if expression(JOBS[name]['if'], context)}
                self.assertEqual(selected, expected)
        context = {'github.event_name': 'workflow_dispatch', 'inputs.operation': 'repair-existing',
                   'needs.publication_prepare.result': 'success'}
        for outcome in ['failure', 'cancelled', 'skipped', '']:
            self.assertFalse(expression(JOBS['publication_repair']['if'],
                                        dict(context, **{'needs.publication_native.result': outcome})))

    def test_normal_resolution_preserves_explicit_tag_and_empty_latest(self):
        script = next(s['run'] for s in JOBS['build_plan']['steps'] if s.get('id') == 'version')
        for supplied, tag, mode, state in [
                ('v2.2.1', '', '', 'absent'), ('v2.2.1', 'v2.2.1-offline-r1', 'stable', 'draft'),
                ('', '', '', 'absent'), ('', 'v2.3.2-offline-r1', 'dev', 'absent'),
                ('v2.2.1', '', 'stable', 'verified')]:
            with self.subTest(version=supplied, tag=tag, mode=mode, state=state), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                version = supplied or 'v2.3.2'
                (root / ('release-matrix-' + version + '.json')).write_text('{}')
                (root / 'curl').write_text('#!/bin/bash\nprintf "%s\\n" "$*" > curl-call\nprintf v2.3.2\n')
                (root / 'python3').write_text('#!/bin/bash\nprintf "%s\\n" "$*" > state-call\nprintf "%s" "$MOCK_STATE"\n')
                for executable in ['curl', 'python3']:
                    (root / executable).chmod(0o755)
                result, output = run_step(script, root, INPUT_VERSION=supplied, INPUT_RELEASE_TAG=tag,
                                          INPUT_MODE=mode, GITHUB_REPOSITORY='example/repo', MOCK_STATE=state,
                                          PATH=str(root) + ':' + os.environ['PATH'])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output['version'], version)
                self.assertEqual(output['release_tag'], tag or version)
                self.assertEqual(output['mode'], mode or 'stable')
                self.assertEqual(output['build_required'], 'false' if state == 'verified' else 'true')
                self.assertEqual((root / 'curl-call').exists(), not bool(supplied))
                if not supplied:
                    self.assertIn('/' + (mode or 'stable') + '/latest', (root / 'curl-call').read_text())
                self.assertIn('--tag ' + (tag or version), (root / 'state-call').read_text())

    def test_manual_plans_export_validated_identity_to_writers(self):
        for name, step_id in [('publication_plan', 'identity'), ('publication_revalidate', 'receipt')]:
            job = JOBS[name]
            steps = job['steps']
            position = next(i for i, s in enumerate(steps) if s.get('id') == step_id)
            validation = 'package_matrix.py plan' if name == 'publication_plan' else 'package_matrix.py revalidate'
            self.assertTrue(any(validation in s.get('run', '') for s in steps[:position]))
            self.assertEqual(job['outputs']['release_tag'], '${{ steps.' + step_id + '.outputs.release_tag }}')
            for tag in ['', 'v2.2.1']:
                with self.subTest(job=name, tag=tag), tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    (root / 'receipt-work/control').mkdir(parents=True)
                    (root / 'receipt-work/control/release-validation.json').write_text('{}')
                    result, output = run_step(steps[position]['run'], root, VERSION='v2.2.1', RELEASE_TAG=tag, MODE='stable')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(output['version'], 'v2.2.1')
                    self.assertEqual(output['release_tag'], 'v2.2.1')
        for writer, planner in list(WRITERS.items())[1:]:
            self.assertEqual(JOBS[writer]['env']['RELEASE_TAG'], '${{ needs.' + planner + '.outputs.release_tag }}')


if __name__ == '__main__':
    unittest.main()
