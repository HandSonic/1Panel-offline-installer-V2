import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/release.py'
spec = importlib.util.spec_from_file_location('offline_release', SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {'fingerprint': 'f', 'complete': True,
            'requested': {'sources': ['official', 'custom'], 'architectures': ['amd64']},
            'packages': [{'source': source, 'arch': 'amd64', 'status': 'built', 'asset': source+'.tar.gz', 'sha256': '0'*64}
                         for source in ['official', 'custom']]}
        self.metadata = {'assets': [{'name': p['asset'], 'size': 99, 'digest': 'sha256:'+'0'*64} for p in self.manifest['packages']]
                         + [{'name': 'manifest.json'}, {'name': 'checksums.txt'}]}

    def test_only_complete_matching_assets_skip(self):
        self.assertTrue(release.manifest_complete(self.manifest, self.metadata, 'f'))
        self.assertFalse(release.manifest_complete(self.manifest, self.metadata, 'new-code'))

    def test_one_amd64_does_not_hide_late_custom_build(self):
        self.metadata['assets'].pop(1)
        self.assertFalse(release.manifest_complete(self.manifest, self.metadata, 'f'))

    def test_partial_build_and_changed_digest_retry(self):
        self.manifest['complete'] = False
        self.assertFalse(release.manifest_complete(self.manifest, self.metadata, 'f'))
        self.manifest['complete'] = True
        self.metadata['assets'][0]['digest'] = 'sha256:changed'
        self.assertFalse(release.manifest_complete(self.manifest, self.metadata, 'f'))

    def test_empty_manifest_cannot_claim_complete(self):
        self.manifest['packages'] = []
        self.assertFalse(release.manifest_complete(self.manifest, self.metadata, 'f'))

    def test_staging_uses_manifest_not_stale_globs(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(release, 'ROOT', Path(directory)):
            root = Path(directory)
            version = root / 'build/v2.2.5'
            (version / 'official').mkdir(parents=True)
            package = version / 'official/current.tar.gz'
            package.write_bytes(b'current-archive')
            (version / 'official/stale.tar.gz').write_bytes(b'previous-run')
            (root / 'build/ci-plan.json').write_text(json.dumps({'version': 'v2.2.5', 'fingerprint': 'f'}))
            manifest = {'version': 'v2.2.5', 'fingerprint': 'f', 'packages': [{'source': 'official', 'arch': 'amd64', 'status': 'built', 'asset': package.name,
                'path': 'official/'+package.name, 'sha256': hashlib.sha256(package.read_bytes()).hexdigest()}]}
            (version / 'manifest.json').write_text(json.dumps(manifest))
            release.stage_release('github')
            stage = root / 'build/release-assets'
            self.assertFalse((stage / 'stale.tar.gz').exists())
            self.assertEqual((stage / 'checksums.txt').read_text().split()[1], package.name)
            package.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                release.stage_release('github')

    def test_unavailable_version_defers_without_historical_downgrade(self):
        with patch.dict(os.environ, {'INPUT_VERSION': '', 'MODE': 'stable'}, clear=True), patch.object(release, 'fetch', return_value=None), patch.object(release, 'outputs') as output:
            release.plan_release('github')
            self.assertEqual(output.call_args.args[0]['build'], '0')

    def test_zero_packages_never_calls_publish(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(release, 'ROOT', Path(directory)), patch.object(release, 'gh') as gh:
            root = Path(directory)
            version = root / 'build/v2.2.5'
            version.mkdir(parents=True)
            (root / 'build/ci-plan.json').write_text(json.dumps({'version': 'v2.2.5', 'fingerprint': 'f'}))
            (version / 'manifest.json').write_text(json.dumps({'version': 'v2.2.5', 'fingerprint': 'f', 'packages': []}))
            release.publish_release()
            gh.assert_not_called()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'build/v2.2.5'
        (self.directory / 'official').mkdir(parents=True)
        self.name = '1panel-v2.2.5-official-offline-linux-amd64.tar.gz'
        package = self.directory / 'official' / self.name
        package.write_bytes(b'usable-new-package')
        self.plan = {'version': 'v2.2.5', 'fingerprint': 'f', 'mode': 'stable'}
        self.manifest = {'version': 'v2.2.5', 'fingerprint': 'f', 'complete': False,
            'requested': {'sources': ['official', 'custom'], 'architectures': ['amd64']},
            'packages': [{'source': 'official', 'arch': 'amd64', 'status': 'built', 'asset': self.name,
                'path': 'official/'+self.name, 'sha256': hashlib.sha256(package.read_bytes()).hexdigest(), 'size': package.stat().st_size},
                {'source': 'custom', 'arch': 'amd64', 'status': 'skipped', 'reason': 'temporarily unavailable'}]}
        (self.root / 'build/ci-plan.json').write_text(json.dumps(self.plan))
        (self.directory / 'manifest.json').write_text(json.dumps(self.manifest))
        self.files = {}
        self.exists = False
        self.draft = True
        self.calls = []
        self.fail_name = ''
        self.corrupt_size = False
        for mocked in [patch.object(release, 'ROOT', self.root), patch.object(release, 'gh', self.gh),
                       patch.object(release, 'release_metadata', self.metadata),
                       patch.object(release, 'fetch', return_value={'tag_name': 'v2.3.0'}),
                       patch.dict(os.environ, {'GITHUB_REPOSITORY': 'test/repo'})]:
            mocked.start()
            self.addCleanup(mocked.stop)

    def metadata(self, *args):
        if not self.exists:
            return None
        return {'draft': self.draft, 'assets': [{'name': name,
            'size': len(data) + (1 if self.corrupt_size and name == self.name else 0),
            'digest': 'sha256:'+hashlib.sha256(data).hexdigest()} for name, data in self.files.items()]}

    def gh(self, *args, check=True):
        self.calls.append(args)
        operation = args[1]
        if operation == 'view':
            return subprocess.CompletedProcess(args, 0 if self.exists else 1, '{}', '')
        if operation == 'create':
            self.assertIn('--draft', args)
            self.exists = True
        elif operation == 'upload':
            for filename in args[3:args.index('-R')]:
                file = Path(filename)
                if file.name == self.fail_name:
                    raise subprocess.CalledProcessError(1, args)
                self.files[file.name] = file.read_bytes()
        elif operation == 'download':
            name = args[args.index('-p')+1]
            if name not in self.files:
                return subprocess.CompletedProcess(args, 1, '', 'missing')
            Path(args[args.index('-O')+1]).write_bytes(self.files[name])
        elif operation == 'delete-asset':
            self.files.pop(args[3], None)
        elif operation == 'edit':
            self.assertIn('manifest.json', self.files)
            self.draft = False
        else:
            self.fail(f'Unexpected operation {args}')
        return subprocess.CompletedProcess(args, 0, '', '')

    def test_partial_release_publishes_after_verification_without_latest_downgrade(self):
        release.publish_release()
        self.assertFalse(self.draft)
        self.assertFalse(json.loads(self.files['manifest.json'])['complete'])
        edit = [call for call in self.calls if call[1] == 'edit'][-1]
        self.assertIn('--latest=false', edit)

    def test_interrupted_update_invalidates_old_completion_marker(self):
        self.exists = True
        self.draft = False
        self.files['manifest.json'] = json.dumps(self.manifest).encode()
        self.fail_name = self.name
        with self.assertRaises(subprocess.CalledProcessError):
            release.publish_release()
        self.assertNotIn('manifest.json', self.files)
        self.assertFalse(any(call[1] == 'edit' for call in self.calls))
        self.fail_name = ''
        release.publish_release()
        self.assertIn('manifest.json', self.files)

    def test_incorrect_remote_size_does_not_finish_publication(self):
        self.corrupt_size = True
        with self.assertRaises(RuntimeError):
            release.publish_release()
        self.assertTrue(self.draft)
        self.assertNotIn('manifest.json', self.files)

    def test_same_input_retry_preserves_a_verified_remote_architecture(self):
        self.exists = True
        self.draft = False
        previous = copy.deepcopy(self.manifest)
        name = '1panel-v2.2.5-custom-offline-linux-amd64.tar.gz'
        self.files[name] = b'previous-working-package'
        previous['packages'][1] = {'source': 'custom', 'arch': 'amd64', 'status': 'built',
            'asset': name, 'path': 'custom/'+name, 'sha256': hashlib.sha256(self.files[name]).hexdigest(), 'size': len(self.files[name])}
        self.files['manifest.json'] = json.dumps(previous).encode()
        release.publish_release()
        self.assertTrue(json.loads(self.files['manifest.json'])['complete'])
        self.assertIn(name.encode(), self.files['checksums.txt'])
        self.assertIn(name, self.files)

    def test_stale_build_fingerprint_is_rejected_before_remote_writes(self):
        self.manifest['fingerprint'] = 'previous-build'
        (self.directory / 'manifest.json').write_text(json.dumps(self.manifest))
        with self.assertRaises(ValueError):
            release.publish_release()
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
