"""Read-only source authentication must not leak runner tokens to packaging."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import package_matrix


class ShardCredentialTests(unittest.TestCase):
    def test_workflow_scopes_read_token_to_one_trusted_shard_step(self):
        workflow = yaml.load((ROOT / '.github/workflows/build-offline-v2.yml').read_text(), Loader=yaml.BaseLoader)
        job = workflow['jobs']['publication_packages']
        self.assertEqual(job['permissions'], {'contents': 'read', 'actions': 'read'})
        self.assertNotIn('GH_TOKEN', job.get('env', {}))
        self.assertNotIn('GITHUB_TOKEN', job.get('env', {}))
        scoped = [s for s in job['steps'] if 'GH_TOKEN' in s.get('env', {})]
        self.assertEqual(len(scoped), 1)
        self.assertEqual(scoped[0]['name'], 'Build and validate one package shard')
        self.assertEqual(scoped[0]['env'], {'GH_TOKEN': '${{ secrets.GITHUB_TOKEN }}'})
        self.assertIn('scripts/package_matrix.py shard', scoped[0]['run'])

    def test_read_token_is_available_only_before_packaging(self):
        for source_kind in ('verified-public-release', 'verified-ci-artifact'):
            with self.subTest(source_kind=source_kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                args = SimpleNamespace(version='v2.3.2', source='custom', arch='amd64')
                row = {'source': 'custom', 'arch': 'amd64', 'key': 'custom-amd64'}
                runtime = {'source_contract': {}, 'upstream': {'source_kind': source_kind, 'records': {'amd64': {}}}}
                facts = {'rows': [row], 'resolved': runtime, 'mode': 'stable', 'upstream_input': {}}
                def authenticate(value, arch):
                    self.assertEqual(os.environ['GH_TOKEN'], 'synthetic-read-token')
                    self.assertEqual(os.environ['GITHUB_TOKEN'], 'synthetic-other-token')
                def producer(*args, **kwargs):
                    self.assertNotIn('GH_TOKEN', os.environ)
                    self.assertNotIn('GITHUB_TOKEN', os.environ)
                    raise RuntimeError('synthetic producer boundary reached')
                with patch.dict(os.environ, GH_TOKEN='synthetic-read-token', GITHUB_TOKEN='synthetic-other-token'), \
                        patch.object(package_matrix, 'ROOT', root), \
                        patch.object(package_matrix, 'load_plan', return_value=(root, facts)), \
                        patch('resolved_transport.verify_public_payload', side_effect=authenticate) as verify, \
                        patch.object(package_matrix.subprocess, 'run', side_effect=producer), \
                        self.assertRaisesRegex(RuntimeError, 'producer boundary'):
                    package_matrix.shard(args)
                self.assertEqual(verify.call_count, int(source_kind == 'verified-public-release'))


if __name__ == '__main__': unittest.main()
