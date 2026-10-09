"""Fail-closed same-run repair gate contracts; no live services or downloads."""
import hashlib
import itertools
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class NativeCandidateWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.load((ROOT / '.github/workflows/build-offline-v2.yml').read_text(), Loader=yaml.BaseLoader)
        cls.jobs = cls.workflow['jobs']

    def test_complete_version_matrix_is_mandatory_and_native(self):
        import sys
        sys.path.insert(0, str(ROOT / 'scripts'))
        from release_inventory import native_rows
        job = self.jobs['publication_native']
        self.assertEqual(job['strategy']['matrix'],
                         '${{ fromJSON(needs.publication_plan.outputs.native_matrix) }}')
        self.assertEqual(self.jobs['publication_plan']['outputs']['native_matrix'],
                         '${{ steps.plan.outputs.native_matrix }}')
        for version, sources in [('v2.2.4', ['custom', 'official']),
                                 ('v2.2.3', ['custom', 'official', 'enterprise-docker']),
                                 ('v2.2.2', ['custom', 'official', 'enterprise-docker']),
                                 ('v2.2.1', ['custom', 'official', 'enterprise-docker']),
                                 ('v2.3.2', ['custom', 'official', 'enterprise-docker'])]:
            with self.subTest(version=version):
                rows = native_rows(version)
                self.assertEqual({(r['source'], r['arch'], r['scenario']) for r in rows},
                                 set(itertools.product(sources, ['amd64', 'arm64'], ['existing', 'fresh'])))
                self.assertEqual(len(rows), 4 * len(sources))
        self.assertEqual(job['strategy']['max-parallel'], '2')
        self.assertEqual(job['strategy']['fail-fast'], 'false')
        self.assertNotIn('continue-on-error', job)
        self.assertTrue(all('continue-on-error' not in s for s in job['steps']))
        self.assertEqual(job['runs-on'], "${{ matrix.arch == 'arm64' && 'ubuntu-24.04-arm' || 'ubuntu-24.04' }}")
        self.assertEqual(job['permissions'], {'contents': 'read', 'actions': 'read'})

    def test_native_is_manual_candidate_only_and_checks_exact_prepare(self):
        job = self.jobs['publication_native']
        self.assertEqual(job['needs'], ['publication_plan', 'publication_prepare'])
        self.assertEqual(job['if'], "${{ !cancelled() && github.event_name == 'workflow_dispatch' && (inputs.operation == 'validate-repair' || inputs.operation == 'repair-existing') && needs.publication_plan.result == 'success' && needs.publication_prepare.result == 'success' }}")
        prepare = self.jobs['publication_prepare']
        self.assertEqual(prepare['outputs']['controls_artifact_id'], '${{ steps.controls.outputs.artifact-id }}')
        controls = next(s for s in prepare['steps'] if s.get('id') == 'controls')
        self.assertEqual(controls['with']['name'], 'publication-controls-${{ github.run_id }}-${{ github.run_attempt }}')
        candidate = next(s for s in job['steps'] if s.get('id') == 'candidate')
        self.assertEqual(candidate['env']['CONTROLS_ARTIFACT_ID'], '${{ needs.publication_prepare.outputs.controls_artifact_id }}')
        self.assertEqual(candidate['env']['EXPECTED_RECEIPT_SHA256'], '${{ needs.publication_prepare.outputs.receipt_sha256 }}')
        self.assertIn('--tag "$RELEASE_TAG"', candidate['run'])
        self.assertIn('--controls-artifact-id "$CONTROLS_ARTIFACT_ID"', candidate['run'])
        self.assertEqual(job['env']['VERSION'], '${{ needs.publication_plan.outputs.version }}')
        self.assertEqual(job['env']['RELEASE_TAG'], '${{ needs.publication_plan.outputs.release_tag }}')
        checkout = next(s for s in job['steps'] if s.get('uses') == 'actions/checkout@v4')
        self.assertEqual(checkout['with'], {'ref': '${{ github.sha }}', 'persist-credentials': 'false'})

    def test_any_non_success_native_matrix_blocks_writer(self):
        job = self.jobs['publication_repair']
        self.assertEqual(job['needs'], ['publication_plan', 'publication_prepare', 'publication_native'])
        # Exact conjunction is intentional: no any-success, skipped fallback,
        # OR, or continue-on-error can turn a partial matrix into authorization.
        expected = "${{ !cancelled() && github.event_name == 'workflow_dispatch' && inputs.operation == 'repair-existing' && needs.publication_plan.result == 'success' && needs.publication_prepare.result == 'success' && needs.publication_native.result == 'success' }}"
        self.assertEqual(job['if'], expected)
        for native in ['failure', 'cancelled', 'skipped', '']:
            expression = expected.removeprefix('${{').removesuffix('}}').strip().replace('!cancelled()', 'True').replace('&&', 'and')
            expression = expression.replace('github.event_name', repr('workflow_dispatch')).replace('inputs.operation', repr('repair-existing'))
            expression = expression.replace('needs.publication_plan.result', repr('success'))
            expression = expression.replace('needs.publication_prepare.result', repr('success')).replace('needs.publication_native.result', repr(native))
            self.assertFalse(eval(expression, {'__builtins__': {}}, {}), native)
        self.assertNotIn('continue-on-error', job)
        self.assertTrue(all('continue-on-error' not in step for step in job['steps']))
        self.assertEqual(job['env']['EXPECTED_VALIDATION_RECEIPT_SHA256'], '${{ needs.publication_prepare.outputs.receipt_sha256 }}')
        self.assertTrue(any(s.get('with', {}).get('artifact-ids') == '${{ needs.publication_prepare.outputs.artifact_id }}' for s in job['steps']))

    def test_runtime_uses_verified_archive_and_only_small_evidence_export(self):
        job = self.jobs['publication_native']
        commands = '\n'.join(s.get('run', '') for s in job['steps'])
        self.assertIn('validate(root,version,root/\'matrix.json\')', commands)
        self.assertLess(commands.index('validate(root,version'), commands.index('archive.extractall'))
        self.assertIn("archive.extractall(destination,filter='data')", commands)
        install = next(s for s in job['steps'] if 'native_install_smoke.py' in s.get('run', ''))
        self.assertEqual(install['env']['EXPECTED_SHA256'], '${{ steps.candidate.outputs.archive_sha256 }}')
        self.assertIn('--archive-sha256 "$EXPECTED_SHA256"', install['run'])
        self.assertNotIn('GH_TOKEN', install['run'])
        artifact = next(s for s in job['steps'] if s.get('uses') == 'actions/upload-artifact@v4')
        self.assertEqual(artifact['with']['path'].splitlines(), ['${{ runner.temp }}/native-input-provenance.json', '${{ runner.temp }}/native-result.json'])
        self.assertEqual(artifact['with']['if-no-files-found'], 'error')
        self.assertIn('${{ github.run_attempt }}', artifact['with']['name'])
        for forbidden in ['gh release', 'manual_publication.py publish', 'prepare_offline.sh', 'package_matrix.py shard']:
            self.assertNotIn(forbidden, commands)

    def test_reviewed_native_harness_remains_byte_identical(self):
        self.assertEqual(hashlib.sha256((ROOT / 'scripts/native_install_smoke.py').read_bytes()).hexdigest(),
                         'f996d7818ba104ad253d5e46c0bfddb2823c45eae90809a6620f4213fd1d0316')


if __name__ == '__main__':
    unittest.main()
