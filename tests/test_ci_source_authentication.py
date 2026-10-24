"""Exact CI availability classification and original upload/run authentication."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import manual_publication as manual
from resolved_inventory import ARCHES, canonical
from test_candidate_source_acquisition import SourceFixture
from test_public_predecessor_source import RetainedUpper
from test_merged_pr_provenance import MergedClient


class CITransportFixture(SourceFixture):
    def __init__(self, directory, merged_pr=False):
        super().__init__(directory)
        if merged_pr:
            self.upper = MergedClient()
            self.upper.state['repository']['id'] = self.upper.state['head_repository']['id'] = 11
            self.upper.manifest['artifacts'] = [self.runtime['upstream']['records'][row['architecture']]
                                               for row in self.upper.manifest['artifacts']]
            self.matrix = self.upper.manifest
            for name in (self.raws / self.runtime['upstream']['records']['arm64']['file'],):
                name.unlink(); Path(str(name) + '.sha256').unlink()
            (self.raws / 'checksums.txt').write_text(''.join(row['sha256'] + '  ' + row['file'] + '\n'
                                                           for row in self.matrix['artifacts']))
        else:
            self.upper = RetainedUpper(self)
            self.matrix = self.upper.matrix
        self.run = self.upper.state
        self.run_id = self.run['id']
        self.job = next(job for job in self.upper.jobs if job['name'] == 'build')
        (self.raws / 'build-manifest.json').write_bytes(canonical(self.matrix))
        archive = directory / 'input.zip'
        with zipfile.ZipFile(archive, 'w') as stream:
            for path in self.raws.iterdir(): stream.write(path, path.name)
        self.zip = archive.read_bytes(); self.zip_sha = hashlib.sha256(self.zip).hexdigest()
        self.name = f'verified-1panel-{self.version}-{self.producer}'
        self.upload_log = (f'Artifact {self.name}.zip successfully finalized. Artifact ID 666\n'
            f'SHA256 digest of uploaded artifact zip is {self.zip_sha}\n')
        self.artifact = {'id': 666, 'expired': False, 'workflow_run': {'id': self.run_id, 'head_sha': self.run['head_sha']},
                         'name': self.name, 'digest': 'sha256:' + self.zip_sha, 'size_in_bytes': len(self.zip)}
        self.artifacts = [self.artifact]
        self.calls = []
        self.catalogue_total = None
        self.latest = self.run
        self.evidence = {}

    def api(self, endpoint):
        self.calls.append(endpoint)
        base = f'repos/{manual.UPSTREAM}/actions'
        if endpoint == f'{base}/runs/{self.run_id}': return copy.deepcopy(self.latest)
        if endpoint == f'{base}/runs/{self.run_id}/attempts/{self.job["run_attempt"]}': return copy.deepcopy(self.run)
        if endpoint.startswith(f'{base}/runs/{self.run_id}/jobs?'):
            return {'total_count': len(self.upper.jobs), 'jobs': copy.deepcopy(self.upper.jobs)}
        if endpoint.startswith(f'{base}/runs/{self.run_id}/artifacts?'):
            return {'total_count': len(self.artifacts) if self.catalogue_total is None else self.catalogue_total,
                    'artifacts': copy.deepcopy(self.artifacts)}
        if endpoint == f'{base}/artifacts/666': return copy.deepcopy(self.artifact)
        raise AssertionError(endpoint)

    def download(self, argv, **kwargs):
        if argv != ['gh', 'api', f'repos/{manual.UPSTREAM}/actions/artifacts/666/zip']:
            raise AssertionError('Unexpected subprocess: ' + repr(argv))
        kwargs['stdout'].write(self.zip); return SimpleNamespace(returncode=0)

    def execute(self):
        with patch.object(manual, 'github_json', side_effect=self.api), \
                patch.object(manual, 'read_job_log', return_value=self.upload_log), \
                patch.object(manual.subprocess, 'run', side_effect=self.download), \
                patch('resolved_transport.ControlGitHub', return_value=self.upper):
            return manual.fetch_ci_bundle(self.directory / 'output', self.version, self.run_id, 666,
                                           self.zip_sha, self.producer, 'downstream17', evidence=self.evidence)


class CIAuthenticationTests(unittest.TestCase):
    def test_available_ci_authenticates_original_upload_and_all_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = CITransportFixture(Path(td)); proof = fixture.execute()
            self.assertEqual(proof['artifact_sha256'], fixture.zip_sha)
            self.assertEqual(proof['run_id'], fixture.run_id)
            self.assertEqual(fixture.evidence['run_attempt'], 2)
            self.assertEqual(fixture.evidence['availability'], 'available')
            self.assertTrue((Path(td) / 'output' / 'resolved-source.json').is_file())

    def test_old_merged_pr_binds_branch_head_and_executed_merge_independently(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = CITransportFixture(Path(td), merged_pr=True)
            proof = fixture.execute()
            self.assertNotEqual(fixture.run['head_sha'], fixture.producer)
            self.assertEqual(proof['build_repository_commit'], fixture.producer)
            self.assertIn('repos/HandSonic/1Panel-Build-v2/pulls/23', fixture.upper.calls)

    def test_rerun_uses_original_upload_attempt(self):
        for status, conclusion in (('completed', 'success'), ('completed', 'failure'),
                                   ('completed', 'cancelled'), ('in_progress', None), ('queued', None)):
            with self.subTest(status=status, conclusion=conclusion), tempfile.TemporaryDirectory() as td:
                fixture = CITransportFixture(Path(td))
                fixture.latest = dict(fixture.run, run_attempt=3, status=status, conclusion=conclusion)
                fixture.execute()
                self.assertIn(f'repos/{manual.UPSTREAM}/actions/runs/{fixture.run_id}/attempts/2', fixture.calls)
                self.assertEqual(fixture.evidence['run_attempt'], 2)
                self.assertEqual(fixture.evidence['latest_run']['run_attempt'], 3)

    def test_later_success_does_not_admit_cancelled_or_running_original_producer(self):
        for status, conclusion in (('completed', 'cancelled'), ('in_progress', None)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as td:
                fixture = CITransportFixture(Path(td))
                fixture.latest = dict(fixture.run, run_attempt=3)
                fixture.run.update(status=status, conclusion=conclusion)
                with self.assertRaisesRegex(ValueError, 'completed noncancelled'):
                    fixture.execute()
                self.assertFalse((Path(td) / 'output').exists())

    def test_later_attempt_status_change_during_download_does_not_change_original_producer(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = CITransportFixture(Path(td))
            fixture.latest = dict(fixture.run, run_attempt=3, status='in_progress', conclusion=None)
            original_download = fixture.download
            def completed_download(argv, **kwargs):
                result = original_download(argv, **kwargs)
                fixture.latest.update(status='completed', conclusion='cancelled')
                return result
            fixture.download = completed_download
            fixture.execute()
            self.assertEqual(fixture.evidence['run']['conclusion'], 'success')
            self.assertEqual(fixture.evidence['run_attempt'], 2)

    def test_attempt_suffixed_artifact_binds_original_upload_name_and_attempt(self):
        for suffix in ('2', '1', '3', '02', '2-extra'):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as td:
                fixture = CITransportFixture(Path(td))
                actual = fixture.name + '-' + suffix
                fixture.upload_log = fixture.upload_log.replace(fixture.name, actual)
                fixture.artifact['name'] = actual
                fixture.latest = dict(fixture.run, run_attempt=3)
                if suffix == '2':
                    fixture.execute()
                    self.assertEqual(fixture.evidence['artifact_name'], actual)
                    self.assertEqual(fixture.evidence['run_attempt'], 2)
                else:
                    with self.assertRaises(ValueError): fixture.execute()

    def test_copied_upload_job_does_not_replace_or_duplicate_the_original_log(self):
        for fault in ('valid', 'different-steps', 'missing-original'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = CITransportFixture(Path(td))
                fixture.job.update(created_at='2030-01-02T03:00:00Z',started_at='2030-01-02T03:00:01Z',
                    completed_at='2030-01-02T03:00:02Z',runner_id=71,runner_name='hosted-runner',
                    runner_group_id=0,runner_group_name='GitHub Actions',labels=['ubuntu-latest'],
                    steps=[{'name':'Upload','status':'completed','conclusion':'success'}])
                copied = copy.deepcopy(fixture.job)
                copied.update(id=999,run_attempt=3,created_at='2030-01-02T04:00:00Z',runner_group_id=None)
                fixture.upper.jobs.append(copied)
                fixture.latest = dict(fixture.run,run_attempt=3)
                if fault == 'different-steps': copied['steps'][0]['name']='Different upload'
                if fault == 'missing-original': fixture.upper.jobs.remove(fixture.job)
                if fault == 'valid':
                    fixture.execute()
                    self.assertEqual(fixture.evidence['job_id'],fixture.job['id'])
                    self.assertEqual(fixture.evidence['run_attempt'],2)
                else:
                    with self.assertRaises(ValueError): fixture.execute()

    def test_subset_bundle_preserves_requested_records_and_missing_custom_shard_fails_alone(self):
        import package_matrix
        from resolved_transport import ci_controls
        from runtime_contract import validate
        with tempfile.TemporaryDirectory() as td:
            fixture = CITransportFixture(Path(td)); selected = ['riscv64', 'amd64']
            outcomes = {row['architecture']: row for row in fixture.matrix['outcomes']}
            records = {row['architecture']: row for row in fixture.matrix['artifacts']}
            fixture.matrix.update(outcome_source='package-step', requested_architectures=selected,
                outcomes=[outcomes[arch] for arch in selected], artifacts=[records[arch] for arch in selected])
            fixture.upper.jobs[:] = [job for job in fixture.upper.jobs if not job['name'].startswith('compile') or
                                     job['name'] in [f'compile ({arch})' for arch in selected]]
            fixture.upper.jobs.append(dict(fixture.job, id=999, name='matrix_status'))
            for arch, row in records.items():
                if arch not in selected:
                    (fixture.raws / row['file']).unlink(); (fixture.raws / (row['file'] + '.sha256')).unlink()
            (fixture.raws / 'build-manifest.json').write_bytes(canonical(fixture.matrix))
            (fixture.raws / 'checksums.txt').write_text(''.join(records[arch]['sha256'] + '  ' + records[arch]['file'] + '\n'
                                                             for arch in selected))
            archive = Path(td) / 'subset.zip'
            with zipfile.ZipFile(archive, 'w') as stream:
                for path in fixture.raws.iterdir(): stream.write(path, path.name)
            fixture.zip = archive.read_bytes(); old_sha = fixture.zip_sha
            fixture.zip_sha = hashlib.sha256(fixture.zip).hexdigest()
            actual_name = fixture.name + '-2'
            fixture.upload_log = fixture.upload_log.replace(fixture.name, actual_name).replace(old_sha, fixture.zip_sha)
            fixture.artifact.update(name=actual_name, digest='sha256:' + fixture.zip_sha, size_in_bytes=len(fixture.zip))
            provenance = fixture.execute()
            with patch('resolved_transport.ControlGitHub', return_value=fixture.upper):
                _, _, upstream = ci_controls(Path(td) / 'output', fixture.version, 'stable', provenance)
            self.assertEqual(list(upstream['records']), selected)
            value = copy.deepcopy(fixture.runtime); value['upstream'] = upstream
            self.assertIs(validate(value, fixture.version), value)
            self.assertEqual(value['inventory']['matrix']['custom'], list(ARCHES))
            args = SimpleNamespace(source='custom', arch='arm64')
            facts = {'resolved': value, 'rows': value['inventory']['rows']}
            with patch.object(package_matrix, 'load_plan', return_value=(Path(td), facts)), \
                    patch.object(package_matrix.subprocess, 'run') as execute:
                with self.assertRaisesRegex(ValueError, 'Authenticated upstream package unavailable for arm64'):
                    package_matrix.shard(args)
                execute.assert_not_called()

    def test_latest_run_change_during_available_acquisition_is_rejected(self):
        for change in ('cancelled', 'in_progress', 'head', 'attempt'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as td:
                fixture = CITransportFixture(Path(td)); original_download = fixture.download
                def changed_download(argv, **kwargs):
                    result = original_download(argv, **kwargs)
                    fixture.latest = copy.deepcopy(fixture.run)
                    if change == 'cancelled': fixture.latest.update(run_attempt=3, conclusion='cancelled')
                    if change == 'in_progress': fixture.latest.update(run_attempt=3, status='in_progress', conclusion=None)
                    if change == 'head': fixture.latest['head_sha'] = '7' * 40
                    if change == 'attempt': fixture.latest['run_attempt'] = 3
                    return result
                fixture.download = changed_download
                with self.assertRaises(ValueError): fixture.execute()

    def test_only_authenticated_expired_or_catalogue_absent_are_unavailable(self):
        for availability in ('expired', 'missing'):
            with self.subTest(availability=availability), tempfile.TemporaryDirectory() as td:
                fixture = CITransportFixture(Path(td))
                if availability == 'expired': fixture.artifact['expired'] = True
                else: fixture.artifacts = []
                with self.assertRaises(manual.OriginalCIUnavailable) as raised: fixture.execute()
                self.assertEqual(raised.exception.authentication['availability'], availability)
                self.assertEqual(raised.exception.authentication['run_attempt'], 2)
                self.assertFalse((Path(td) / 'output').exists())

    def test_cancellation_bad_metadata_missing_logs_and_tamper_are_not_expiry(self):
        faults = ('cancelled', 'repo', 'head-repo', 'run-id', 'artifact-id', 'artifact-run',
                  'artifact-head', 'artifact-digest', 'artifact-size', 'expired-type', 'upload-job-run',
                  'upload-job-head', 'upload-job-attempt', 'upload-job-status', 'upload-marker',
                  'zip-tamper', 'truncated-catalogue', 'missing-without-log', 'matrix-attempt',
                  'non-pr-built-commit')
        for fault in faults:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = CITransportFixture(Path(td))
                if fault == 'cancelled': fixture.run['conclusion'] = 'cancelled'; fixture.artifact['expired'] = True
                if fault == 'repo': fixture.run['repository']['full_name'] = 'other/repo'
                if fault == 'head-repo': fixture.run['head_repository']['id'] = 12
                if fault == 'run-id': fixture.run['id'] += 1
                if fault == 'artifact-id': fixture.artifact['id'] += 1
                if fault == 'artifact-run': fixture.artifact['workflow_run']['id'] += 1
                if fault == 'artifact-head': fixture.artifact['workflow_run']['head_sha'] = '7' * 40
                if fault == 'artifact-digest': fixture.artifact['digest'] = 'sha256:' + '0' * 64; fixture.artifact['expired'] = True
                if fault == 'artifact-size': fixture.artifact['size_in_bytes'] = 0
                if fault == 'expired-type': fixture.artifact['expired'] = 'true'
                if fault == 'upload-job-run': fixture.job['run_id'] += 1
                if fault == 'upload-job-head': fixture.job['head_sha'] = '7' * 40
                if fault == 'upload-job-attempt': fixture.job['run_attempt'] += 1
                if fault == 'upload-job-status': fixture.job['status'] = 'in_progress'
                if fault == 'upload-marker': fixture.upload_log = fixture.upload_log.replace('Artifact ID 666', 'Artifact ID 6660')
                if fault == 'zip-tamper': fixture.zip += b'changed'
                if fault == 'truncated-catalogue': fixture.catalogue_total = 2
                if fault == 'missing-without-log': fixture.artifacts = []; fixture.upload_log = ''
                if fault == 'matrix-attempt': fixture.run['run_attempt'] = 1; fixture.job['run_attempt'] = 1
                if fault == 'non-pr-built-commit': fixture.run['head_sha'] = '7' * 40; fixture.job['head_sha'] = '7' * 40
                if fault == 'artifact-id':
                    # A complete catalogue really lacking the expected ID is
                    # absence; a malformed direct record is not.
                    fixture.artifacts = [dict(fixture.artifact, id=666)]
                with self.assertRaises(ValueError) as raised: fixture.execute()
                self.assertNotIsInstance(raised.exception, manual.OriginalCIUnavailable)


if __name__ == '__main__': unittest.main()
