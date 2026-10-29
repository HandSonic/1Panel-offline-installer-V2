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
WRITERS = {'publication_publish': 'publication_plan', 'publication_checksums': 'publication_check'}


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
                            env=dict(os.environ, GITHUB_OUTPUT=str(output), GITHUB_ENV=str(directory / 'environment'), **environment),
                            text=True, capture_output=True)
    outputs = dict(line.split('=', 1) for line in output.read_text().splitlines()) if output.exists() else {}
    return result, outputs


def trigger_graph(event, operation, *, build=True, checksums=False, issues=False,
                  plan_build=None, plan_issues=False, checksum_issues=False, check_result='success'):
    context = {'github.event_name': event, 'inputs.operation': operation}
    remaining, results, selected = dict(JOBS), {}, set()
    while remaining:
        ready = []
        for name, job in remaining.items():
            needs = job.get('needs', [])
            needs = [needs] if isinstance(needs, str) else needs
            if set(needs) <= results.keys():
                ready.append(name)
        if not ready:
            raise AssertionError('Workflow dependencies must be resolvable')
        for name in ready:
            job = remaining.pop(name)
            active = expression(job['if'], context)
            result = (check_result if name == 'publication_check' else 'success') if active else 'skipped'
            if active and name == 'publication_status' and any(
                    context.get('needs.publication_' + stage + '.outputs.issues_present') == 'true'
                    for stage in ('check', 'plan', 'checksums')):
                result = 'failure'
            results[name] = result
            context['needs.' + name + '.result'] = result
            if active:
                selected.add(name)
            if result == 'success':
                for output in job.get('outputs', {}):
                    context['needs.' + name + '.outputs.' + output] = {
                        'build_required': str(plan_build if name == 'publication_plan' and plan_build is not None else build).lower(),
                        'checksum_required': str(checksums).lower(),
                        'issues_present': str({'publication_check': issues, 'publication_plan': plan_issues,
                                               'publication_checksums': checksum_issues}.get(name, False)).lower(),
                        'native_count': '12', 'upgrade_count': '4'}.get(output, 'fixture')
    return selected, results


class WorkflowConcurrencyTests(unittest.TestCase):
    def test_all_writers_share_resolved_tag_lock_across_refs_and_operations(self):
        actual = {name for name, job in JOBS.items() if job.get('permissions', {}).get('contents') == 'write'}
        self.assertEqual(actual, set(WRITERS))
        keys = {}
        for tag in ['v2.2.1', 'v2.2.2', 'v2.3.2-offline-r1']:
            keys[tag] = set()
            for (writer, planner), ref, operation in itertools.product(
                    WRITERS.items(), ['refs/heads/master', 'refs/heads/repair'], ['build', 'repair-existing']):
                job = JOBS[writer]
                needs = job['needs'] if isinstance(job['needs'], list) else [job['needs']]
                self.assertIn(planner, needs)
                self.assertEqual(job['concurrency']['queue'], 'max')
                self.assertEqual(job['concurrency']['cancel-in-progress'], 'false')
                self.assertIn('release_tag', JOBS[planner]['outputs'])
                lock = job['concurrency']['group']
                self.assertEqual(lock, 'offline-release-${{ needs.' + planner + '.outputs.release_tag }}')
                for supplied in ['', 'unresolved-latest', 'v9.9.9']:
                    context = {'github.ref': ref, 'inputs.operation': operation,
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
        # One failed architecture must not cancel independent package/native rows.
        for name in ('publication_packages', 'publication_native'):
            self.assertEqual(JOBS[name]['strategy']['fail-fast'], 'false')

    def test_trigger_routes_preserve_write_and_native_gates(self):
        cases = [('pull_request','',set()),('push','',set()),('schedule','',{'publication_publish'}),
                 ('workflow_dispatch','build',{'publication_publish'}),('workflow_dispatch','validate-repair',set()),
                 ('workflow_dispatch','repair-existing',{'publication_publish'})]
        for event, operation, expected in cases:
            normal = event == 'schedule' or event == 'workflow_dispatch' and operation == 'build'
            package = normal or operation in ('validate-repair','repair-existing')
            context = {'github.event_name':event,'inputs.operation':operation,
                       'needs.publication_plan.outputs.build_required':'true' if package else '',
                       'needs.publication_check.outputs.checksum_required':'false',
                       **{'needs.'+n+'.result':'success' for n in JOBS}}
            if not package:
                context['needs.publication_plan.result']='skipped'
                context['needs.publication_acceptance.result']='skipped'
            selected={name for name in WRITERS if expression(JOBS[name]['if'],context)}
            self.assertEqual(selected,expected,(event,operation))
            for writer in selected:
                for result in ('failure','cancelled','skipped'):
                    self.assertFalse(expression(JOBS[writer]['if'],dict(context,**{'needs.publication_acceptance.result':result})))

    def test_trigger_graph_separates_code_regression_from_package_admission(self):
        admission = {'publication_plan', 'publication_packages', 'publication_prepare',
                     'publication_native', 'publication_upgrade', 'publication_acceptance', 'publication_status'}
        cases = [('push', '', {'regression'}), ('pull_request', '', {'regression'}),
                 ('schedule', '', admission | {'publication_check', 'regression', 'publication_publish'}),
                 ('workflow_dispatch', 'build', admission | {'publication_check', 'regression', 'publication_publish'}),
                 ('workflow_dispatch', 'validate-repair', admission | {'publication_check', 'regression'}),
                 ('workflow_dispatch', 'repair-existing', admission | {'publication_check', 'regression', 'publication_publish'})]
        for event, operation, expected in cases:
            with self.subTest(event=event, operation=operation):
                selected, results = trigger_graph(event, operation)
                self.assertEqual(selected, expected)
                if event in ('push', 'pull_request'):
                    self.assertEqual(results['publication_check'], 'skipped')
                    self.assertEqual(results['regression'], 'success')
                if event in ('schedule', 'workflow_dispatch'):
                    self.assertEqual(set(JOBS['publication_plan']['needs']), {'publication_check', 'regression'})
                    self.assertIn("needs.regression.result == 'success'", JOBS['publication_plan']['if'])

    def test_complete_releases_stop_before_regression_and_checksum_only_skips_builds(self):
        for event, operation in [('schedule', ''), ('workflow_dispatch', 'build')]:
            for checksums in (False, True):
                with self.subTest(event=event, checksums=checksums):
                    selected, results = trigger_graph(event, operation, build=False, checksums=checksums)
                    self.assertEqual(selected, {'publication_check'} |
                                     ({'publication_checksums'} if checksums else set()))
                    for name in ('regression', 'publication_plan', 'publication_packages', 'publication_publish'):
                        self.assertEqual(results[name], 'skipped')
            selected, _ = trigger_graph(event, operation, build=True, checksums=True)
            self.assertEqual(selected, set(JOBS))

    def test_failed_metadata_check_does_not_run_regression_or_publication(self):
        for event, operation in [('schedule', ''), ('workflow_dispatch', 'build')]:
            selected, results = trigger_graph(event, operation, check_result='failure')
            self.assertEqual(selected, {'publication_check'})
            self.assertEqual(results['publication_check'], 'failure')
            self.assertTrue(all(result == 'skipped' for name, result in results.items() if name != 'publication_check'))

    def test_product_check_issues_report_failure_without_blocking_independent_work(self):
        for event, operation in [('schedule', ''), ('workflow_dispatch', 'build')]:
            for build, checksums in itertools.product((False, True), repeat=2):
                with self.subTest(event=event, build=build, checksums=checksums):
                    expected, _ = trigger_graph(event, operation, build=build, checksums=checksums)
                    selected, results = trigger_graph(event, operation, build=build,
                                                      checksums=checksums, issues=True)
                    self.assertEqual(selected, expected | {'publication_status'})
                    self.assertEqual(results['publication_check'], 'success')
                    self.assertEqual(results['publication_status'], 'failure')
                    if build:
                        self.assertEqual(results['publication_publish'], 'success')
                    if checksums:
                        self.assertEqual(results['publication_checksums'], 'success')
        for name, job in JOBS.items():
            self.assertNotIn('publication_status', job.get('needs', []), name)
        values = {'needs.publication_check.result': 'success',
                  'needs.publication_check.outputs.issues_present': 'true'}
        self.assertFalse(expression(JOBS['publication_status']['if'], values, cancelled=True))

    def test_later_plan_issues_are_red_even_when_no_healthy_build_rows_remain(self):
        for event, operation in [('schedule', ''), ('workflow_dispatch', 'build')]:
            selected, results = trigger_graph(event, operation, build=True,
                                              plan_build=False, plan_issues=True)
            self.assertEqual(selected, {'publication_check', 'regression',
                                        'publication_plan', 'publication_status'})
            self.assertEqual(results['publication_status'], 'failure')
            self.assertEqual(results['publication_acceptance'], 'skipped')
            self.assertEqual(results['publication_publish'], 'skipped')
        values = {'needs.publication_plan.result': 'success',
                  'needs.publication_plan.outputs.issues_present': 'true'}
        self.assertFalse(expression(JOBS['publication_status']['if'], values, cancelled=True))

    def test_later_checksum_conflict_is_red_without_starting_package_jobs(self):
        selected, results = trigger_graph('workflow_dispatch', 'build', build=False,
                                          checksums=True, checksum_issues=True)
        self.assertEqual(selected, {'publication_check', 'publication_checksums', 'publication_status'})
        self.assertEqual(results['publication_checksums'], 'success')
        self.assertEqual(results['publication_status'], 'failure')
        self.assertEqual(results['publication_plan'], 'skipped')
        self.assertEqual(results['publication_acceptance'], 'skipped')
        self.assertEqual(results['publication_publish'], 'skipped')

    def test_documentation_filter_keeps_runtime_transfer_pins_in_regression_scope(self):
        def matches(path, pattern):
            regex = re.escape(pattern).replace(r'\*\*/', r'(?:.*/)?').replace(r'\*\*', '.*').replace(r'\*', '[^/]*')
            return re.fullmatch(regex, path) is not None
        for event in ('push', 'pull_request'):
            patterns = WORKFLOW['on'][event]['paths-ignore']
            for path, ignored in (
                    ('README.md', True), ('docs/workflow-concurrency.md', True),
                    ('docs/recovery/notes.md', True),
                    ('docs/recovery/2026-10-artifact-transfer.json', False),
                    ('docs/recovery/helper.py', False),
                    ('.github/workflows/build-offline-v2.yml', False)):
                with self.subTest(event=event, path=path):
                    self.assertEqual(any(matches(path, pattern) for pattern in patterns), ignored)

    def test_normal_resolution_preserves_explicit_tag_and_empty_latest(self):
        script=next(s['run'] for s in JOBS['publication_check']['steps'] if s.get('id')=='identity')
        for supplied,tag,mode,latest in [('v2.99.0','','stable','v2.99.1'),
                ('v2.99.0','v2.99.0-offline-r1','stable','v2.99.1'),('','','stable','v2.100.0'),
                ('','','dev','v2.100.0-dev.1')]:
            with tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/'scripts').symlink_to(ROOT/'scripts',target_is_directory=True)
                (root/'curl').write_text('#!/bin/bash\ntouch "$MOCK_CURL_CALLED"\nprintf "%s" "$MOCK_LATEST"\n')
                (root/'curl').chmod(0o755)
                result,output=run_step(script,root,INPUT_VERSION=supplied,INPUT_RELEASE_TAG=tag,
                    INPUT_MODE=mode,PUBLICATION_OPERATION='build',MOCK_LATEST=latest,MOCK_CURL_CALLED=str(root/'latest-called'),PATH=str(root)+':'+os.environ['PATH'],PYTHONDONTWRITEBYTECODE='1')
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(output['version'],supplied or latest)
                self.assertEqual(output['release_tag'],tag or supplied or latest)
                self.assertEqual(output['mode'],mode)
                self.assertEqual((root/'latest-called').exists(),not bool(supplied))
                self.assertFalse(list(root.glob('*sources-v*.json')))

    def test_repairs_require_explicit_valid_identity_without_latest_discovery(self):
        job = JOBS['publication_check']
        steps = job['steps']
        identity = next(s for s in steps if s.get('id') == 'identity')
        check = next(s for s in steps if s.get('id') == 'check')
        self.assertLess(steps.index(identity), steps.index(check))
        self.assertEqual(job['outputs']['release_tag'], '${{ steps.identity.outputs.release_tag }}')
        for operation, version, tag in itertools.product(
                ('validate-repair', 'repair-existing'), ('', 'v2.2.1'), ('', 'v2.2.1-offline-r1')):
            with self.subTest(operation=operation, version=version, tag=tag), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / 'scripts').symlink_to(ROOT / 'scripts', target_is_directory=True)
                (root / 'curl').write_text('#!/bin/bash\nexit 99\n')
                (root / 'curl').chmod(0o755)
                result, output = run_step(identity['run'], root, PUBLICATION_OPERATION=operation,
                    INPUT_VERSION=version, INPUT_RELEASE_TAG=tag, INPUT_MODE='stable',
                    PATH=str(root) + ':' + os.environ['PATH'])
                self.assertEqual(result.returncode == 0, bool(version), result.stderr)
                if version:
                    self.assertEqual(output, {'version': version, 'release_tag': tag or version, 'mode': 'stable'})
                    self.assertEqual((root / 'environment').read_text().splitlines(),
                        ['VERSION=' + version, 'RELEASE_TAG=' + (tag or version), 'MODE=stable'])
                    (root / 'outputs').unlink()
                    result, output = run_step(check['run'], root, PUBLICATION_OPERATION=operation)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(output, {'build_required': 'true', 'checksum_required': 'false',
                                              'issues_present': 'false', 'issues_json': '[]'})
                else:
                    self.assertEqual(output, {})
                    self.assertFalse((root / 'environment').exists())
        self.assertEqual(JOBS['publication_publish']['env']['RELEASE_TAG'],
                         '${{ needs.publication_plan.outputs.release_tag }}')
        for key in ('version', 'release_tag', 'mode'):
            value = '${{ needs.publication_check.outputs.' + key + ' }}'
            self.assertEqual(JOBS['publication_plan']['outputs'][key], value)
            self.assertEqual(JOBS['publication_plan']['env'][key.upper()], value)
            self.assertEqual(JOBS['publication_checksums']['env'][key.upper()], value)
        self.assertFalse(any(step.get('id') == 'identity' for step in JOBS['publication_plan']['steps']))


if __name__ == '__main__':
    unittest.main()
