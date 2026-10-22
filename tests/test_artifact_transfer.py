"""Finite read-only transfer: original acceptance and exact bytes remain mandatory."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import warnings
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import transfer_admitted_artifact as transfer


def facts(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.name = '1panel-v2.3.0-enterprise-docker-offline-linux-amd64.tar.gz'
        self.raw = b'Original tar bytes: never execute or recompress'
        self.row = {'filename': self.name, 'tar': facts(self.raw)}

    def archive(self, entries):
        path = self.root / 'shard.zip'
        with warnings.catch_warnings(), zipfile.ZipFile(path, 'w') as z:
            warnings.simplefilter('ignore', UserWarning)
            for name, body in entries:
                z.writestr(name, body)
        return path

    def test_only_target_and_canonical_sidecar_leave_original_bytes_unchanged(self):
        archive = self.archive([('enterprise-docker/' + self.name, self.raw), ('enterprise-original/redundant.tar.gz', b'redundant')])
        output = self.root / 'output'
        transfer.selected_tar(archive, output, self.row)
        self.assertEqual({p.name for p in output.iterdir()}, {self.name, self.name + '.sha256'})
        self.assertEqual((output / self.name).read_bytes(), self.raw)
        self.assertEqual((output / (self.name + '.sha256')).read_bytes(),
                         (self.row['tar']['sha256'] + '  ' + self.name + '\n').encode())

    def test_changed_digest_size_missing_or_duplicate_target_rejected(self):
        for entries in [ [(self.name, b'wrong')], [('other', self.raw)],
                         [(self.name, self.raw), (self.name, self.raw)],
                         [('a/' + self.name, self.raw), ('b/' + self.name, self.raw)],
                         [(self.name, b'x' * len(self.raw))] ]:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as out, self.assertRaises(ValueError):
                transfer.selected_tar(self.archive(entries), Path(out) / 'output', self.row)

    def test_unsafe_members_rejected_even_when_target_is_valid(self):
        for name in ('../outside', '/absolute', 'a\\b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                transfer.selected_tar(self.archive([(self.name, self.raw), (name, b'bad')]), self.root / 'output', self.row)
        special = zipfile.ZipInfo('symlink'); special.external_attr = 0o120777 << 16
        with self.assertRaises(ValueError):
            transfer.selected_tar(self.archive([(self.name, self.raw), (special, b'target')]), self.root / 'output', self.row)

    def test_oversized_target_rejected_before_output(self):
        row = dict(self.row, tar={'bytes': transfer.LIMIT, 'sha256': 'a'*64})
        with self.assertRaises(ValueError):
            transfer.selected_tar(self.archive([(self.name, self.raw)]), self.root / 'output', row)
        self.assertFalse((self.root / 'output').exists())

    def controls(self):
        identity = {'repository': transfer.REPOSITORY, 'workflow_run_id': 42, 'workflow_run_attempt': 1,
                    'workflow_commit': 'a'*40, 'version': 'v2.3.0'}
        receipt = dict(identity, files={self.name: self.row['tar']}, outcomes=[{'key': 'enterprise-docker-amd64', 'status': 'success'}])
        native = dict(identity, admission={'accepted_keys': ['enterprise-docker-amd64']})
        rb, nb = json.dumps(receipt).encode(), json.dumps(native).encode()
        (self.root / 'release-validation.json').write_bytes(rb)
        (self.root / 'native-acceptance.json').write_bytes(nb)
        row = dict(self.row, run_id=42, run_attempt=1, head_sha='a'*40, version='v2.3.0', product_key='enterprise-docker-amd64',
                   receipt_sha256=facts(rb)['sha256'], native_sha256=facts(nb)['sha256'])
        log = f'VERIFIED_RELEASE_RECEIPT_SHA256={row["receipt_sha256"]}\nNATIVE_ACCEPTANCE_SHA256={row["native_sha256"]}\n'
        return row, log

    def test_exact_original_controls_and_log_required(self):
        row, log = self.controls()
        transfer.authenticate_controls(self.root, row, log)
        for bad in ('', log + log, log.replace(row['native_sha256'], 'f'*64)):
            with self.assertRaises(ValueError):
                transfer.authenticate_controls(self.root, row, bad)
        for field, value in [('head_sha', 'b'*40), ('run_id', 99), ('run_attempt', 2),
                             ('product_key', 'unadmitted'), ('receipt_sha256', 'c'*64), ('native_sha256', 'd'*64)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                transfer.authenticate_controls(self.root, dict(row, **{field: value}), log)

    def test_all_six_frozen_targets_fit_with_zip_overhead_margin(self):
        rows = json.loads(transfer.MANIFEST.read_text())
        self.assertEqual(len(rows), 6)
        self.assertEqual(len({r['key'] for r in rows}), 6)
        self.assertEqual({r['head_sha'] for r in rows}, {'d7881002c4a26f0ae56e2254e3508f8b2ae3d5b8'})
        for row in rows:
            self.assertLess(row['tar']['bytes'] + 1024*1024, transfer.LIMIT)
            self.assertGreater(row['shard']['size_in_bytes'], transfer.LIMIT)

    def test_original_job_and_artifact_identity_fail_before_any_download(self):
        row = json.loads(transfer.MANIFEST.read_text())[0]
        repo = {'id': 5, 'full_name': transfer.REPOSITORY}
        run = {'id': row['run_id'], 'head_sha': row['head_sha'], 'path': transfer.WORKFLOW,
               'repository': repo, 'head_repository': repo}
        job = {'id': row['acceptance_job_id'], 'run_id': row['run_id'], 'run_attempt': 1,
               'head_sha': row['head_sha'], 'name': 'publication_acceptance',
               'status': 'completed', 'conclusion': 'success'}
        artifact = dict(row['controls'], expired=False, workflow_run={
            'id': row['run_id'], 'head_sha': row['head_sha'], 'repository_id': 5, 'head_repository_id': 5})
        for bad in [dict(job, conclusion='failure'), dict(job, run_attempt=2), dict(job, head_sha='f'*40)]:
            with patch.object(transfer, 'api', side_effect=[run, bad]), patch.object(transfer, 'download_verified') as download:
                with self.assertRaises(ValueError):
                    transfer.transfer(SimpleNamespace(repo=transfer.REPOSITORY), row, self.root / 'out')
                download.assert_not_called()
        for bad in [dict(artifact, expired=True), dict(artifact, id=1), dict(artifact, digest='sha256:'+'f'*64),
                    dict(artifact, workflow_run=dict(artifact['workflow_run'], head_sha='f'*40))]:
            with patch.object(transfer, 'api', side_effect=[run, job, bad]), patch.object(transfer, 'download_verified') as download:
                with self.assertRaises(ValueError):
                    transfer.transfer(SimpleNamespace(repo=transfer.REPOSITORY), row, self.root / 'out')
                download.assert_not_called()

    def test_recovery_workflow_is_separate_manual_and_read_only(self):
        import yaml
        from test_workflow_concurrency import expression
        root = Path(__file__).resolve().parents[1]
        main = yaml.load((root / '.github/workflows/build-offline-v2.yml').read_text(), Loader=yaml.BaseLoader)
        recovery = yaml.load((root / '.github/workflows/recover-admitted-artifacts.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertNotIn('artifact_transfer', main['jobs'])
        self.assertNotIn('transport-only', main['on']['workflow_dispatch']['inputs']['operation']['options'])
        self.assertEqual(set(recovery['on']), {'workflow_dispatch'})
        self.assertFalse(recovery['on']['workflow_dispatch'])
        self.assertEqual(set(recovery['jobs']), {'artifact_transfer'})
        job = recovery['jobs']['artifact_transfer']
        self.assertEqual(job['permissions'], {'contents': 'read', 'actions': 'read'})
        for event in ('push', 'pull_request', 'schedule', 'workflow_dispatch'):
            for cancelled in (False, True):
                self.assertEqual(expression(job['if'], {'github.event_name': event}, cancelled),
                                 event == 'workflow_dispatch' and not cancelled)
        self.assertEqual(job['strategy']['max-parallel'], '1')
        manifest = json.loads((root / 'docs/recovery/2026-10-artifact-transfer.json').read_text())
        self.assertEqual(set(job['strategy']['matrix']['key']), {row['key'] for row in manifest})
        self.assertEqual(len(manifest), 6)
        self.assertEqual(job['steps'][0]['with']['persist-credentials'], 'false')
        upload = next(s for s in job['steps'] if s.get('uses') == 'actions/upload-artifact@v4')
        self.assertEqual(upload['with']['retention-days'], '7')
        self.assertEqual(upload['with']['compression-level'], '0')


if __name__ == '__main__':
    unittest.main()
