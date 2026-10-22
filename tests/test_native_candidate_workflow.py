"""Fail-closed same-run repair gate contracts; no live services or downloads."""
import itertools
import hashlib
import json
import subprocess
import tempfile
from unittest.mock import patch
import os
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
                         '${{ fromJSON(needs.publication_prepare.outputs.native_matrix) }}')
        self.assertEqual(self.jobs['publication_plan']['outputs']['native_matrix'],
                         '${{ steps.plan.outputs.native_matrix }}')
        for version, sources in [('v2.2.4', ['custom', 'official']),
                                 ('v2.3.2', ['custom', 'official', 'enterprise-docker'])]:
            with self.subTest(version=version), tempfile.TemporaryDirectory() as td:
                from test_runtime_contract import runtime
                from runtime_fixtures import activate
                activate(self,td,runtime(version=version,enterprise='enterprise-docker' in sources))
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

    def test_all_package_routes_check_exact_prepare_before_native_execution(self):
        job = self.jobs['publication_native']
        self.assertEqual(job['needs'], ['publication_plan', 'publication_prepare'])
        self.assertIn("needs.publication_prepare.result == 'success'",job['if'])
        self.assertNotIn('workflow_dispatch',job['if'])
        self.assertIn('ONEPANEL_RESOLVED_PLAN_SHA256',job['env'])
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

    def test_acceptance_requires_package_and_native_evidence(self):
        acceptance=self.jobs['publication_acceptance']
        self.assertEqual(set(acceptance['needs']),{'publication_plan','publication_prepare','publication_native','publication_upgrade'})
        self.assertEqual(acceptance['permissions'],{'contents':'read','actions':'read'})

    def test_runtime_uses_verified_archive_and_only_small_evidence_export(self):
        job = self.jobs['publication_native']
        commands = '\n'.join(s.get('run', '') for s in job['steps'])
        unpack = next(s for s in job['steps'] if s.get('id') == 'unpack')
        install = next(s for s in job['steps'] if s.get('id') == 'install')
        self.assertLess(job['steps'].index(unpack), job['steps'].index(install))
        self.assertEqual(install['env']['EXPECTED_SHA256'], '${{ steps.candidate.outputs.archive_sha256 }}')
        self.assertIn('--archive-sha256 "$EXPECTED_SHA256"', install['run'])
        self.assertNotIn('GH_TOKEN', install['run'])
        artifact = next(s for s in job['steps'] if s.get('uses') == 'actions/upload-artifact@v4')
        self.assertEqual(artifact['with']['path'].splitlines(), ['${{ runner.temp }}/native-evidence.json'])
        self.assertEqual(artifact['with']['if-no-files-found'], 'error')
        self.assertIn('${{ github.run_attempt }}', artifact['with']['name'])
        for forbidden in ['gh release', 'manual_publication.py publish', 'prepare_offline.sh', 'package_matrix.py shard']:
            self.assertNotIn(forbidden, commands)

    def test_unpack_executes_validation_before_verified_read_and_write(self):
        step = next(s for s in self.jobs['publication_native']['steps'] if s.get('id') == 'unpack')
        log_helper = '''import hashlib,json,os
from pathlib import Path
def record(name, **facts):
    with open(os.environ['TRACE_PATH'], 'a') as log:
        log.write(json.dumps({'call': name, **facts}) + '\\n')
'''
        mocks = {
            'validate_release.py': '''
def validate(root, version, matrix):
    record('validate', version=version, matrix=json.loads(matrix.read_text()))
    packages=sorted(root.glob('*/*.tar.gz'))
    expected=''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\\n' for p in packages)
    assert (root/'checksums.txt').read_text()==expected
    if os.environ['FAIL_VALIDATE']=='1':raise ValueError('mock validation rejected')
''',
            'validate_resolved_custom.py': '''
def archive_bytes(target, pin, archive_root, *, modes):
    record('archive_bytes', target=str(target), pin=pin, archive_root=archive_root)
    assert pin=={'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'bytes':target.stat().st_size}
    modes['member']=0o755
    return b'verified-body'
''',
            'native_upgrade_input.py': '''
def write_verified_archive(body, modes, destination, archive_root):
    record('write_verified_archive', modes=modes, destination=str(destination), archive_root=archive_root)
    assert body==b'verified-body' and modes=={'member':0o755}
    package=destination/archive_root
    package.mkdir(parents=True)
    (package/'ready').write_bytes(body)
    return package
''',
            'release_asset_repair.py': '''
def digest(path):
    return {'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size}
''',
        }
        for source, rejected in itertools.product(('custom', 'enterprise-docker'), (False, True)):
            with self.subTest(source=source, rejected=rejected), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                scripts = root/'scripts'; scripts.mkdir()
                for filename, implementation in mocks.items():
                    (scripts/filename).write_text(log_helper + implementation)
                runner = root/'runner'; inputs = runner/'native-input'/source; inputs.mkdir(parents=True)
                name = '1panel-v2.3.2-'+source+'-offline-linux-amd64.tar.gz'
                archive = inputs/name; archive.write_bytes(b'fixture archive')
                pin = {'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'bytes': archive.stat().st_size}
                (runner/'native-input-provenance.json').write_text(json.dumps({'files': {source+'/'+name: pin}}))
                environment = root/'environment'; environment.write_text('CALLER=retained\n')
                trace = root/'trace'
                result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', step['run']], cwd=root,
                    env=dict(os.environ, RUNNER_TEMP=str(runner), VERSION='v2.3.2', SOURCE=source,
                             ARCH='amd64', GITHUB_ENV=str(environment), TRACE_PATH=str(trace),
                             FAIL_VALIDATE='1' if rejected else '0'), capture_output=True, text=True)
                calls = [json.loads(line) for line in trace.read_text().splitlines()]
                matrix = {source: ['amd64']}
                if source == 'enterprise-docker':matrix['enterprise-original'] = ['amd64']
                self.assertEqual(calls[0], {'call': 'validate', 'version': 'v2.3.2', 'matrix': matrix})
                if rejected:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('mock validation rejected', result.stderr)
                    self.assertEqual([call['call'] for call in calls], ['validate'])
                    self.assertFalse((runner/'native-package').exists())
                    self.assertEqual(environment.read_text(), 'CALLER=retained\n')
                else:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual([call['call'] for call in calls],
                                     ['validate', 'archive_bytes', 'write_verified_archive'])
                    archive_root = '1panel-v2.3.2-linux-amd64' if source == 'enterprise-docker' else name.removesuffix('.tar.gz')
                    self.assertEqual(calls[1], {'call': 'archive_bytes', 'target': str(archive),
                                              'pin': pin, 'archive_root': archive_root})
                    self.assertEqual(calls[2], {'call': 'write_verified_archive', 'modes': {'member': 0o755},
                        'destination': str(runner/'native-package'), 'archive_root': archive_root})
                    package = runner/'native-package'/archive_root
                    self.assertEqual((package/'ready').read_bytes(), b'verified-body')
                    self.assertEqual(environment.read_text(), 'CALLER=retained\nNATIVE_PACKAGE='+str(package)+'\n')



if __name__ == '__main__':
    unittest.main()
