"""Synthetic official API and target bytes; no package execution or downloads."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import initial_install_input as initial
import native_upgrade_input as binder
from test_initial_release_applicability import COMMIT, VERSION, GitHub, release


class InitialInputMaterializationTests(unittest.TestCase):
    def fixture(self, root, source):
        client = GitHub()
        client.responses[f'repos/{binder.REPOSITORY}/releases?per_page=100&page=1'] = []
        identity = {'runtime': {'source_contract': {'source': {'commit': COMMIT}},
                                'source_contract_sha256': 'e' * 64,
                                'configuration_sources': {}}}
        candidate = {'version': VERSION, 'source': source, 'arch': 'amd64', 'archive_sha256': 'a' * 64}
        provenance = root / 'candidate.json'; provenance.write_text(json.dumps(candidate))
        args = SimpleNamespace(version=VERSION, source=source, arch='amd64', target_receipt_sha256='b' * 64,
                               target_controls_id=701)
        def unpack(directory, proof, destination, actual_identity, receipt, controls, checkout):
            self.assertEqual((proof, actual_identity, receipt, controls), (candidate, identity, 'b' * 64, 701))
            destination.mkdir()
            manifest = {'upstream_provenance': {'source_commit': COMMIT}}
            (destination / 'offline-manifest.json').write_text(json.dumps(manifest))
            return destination
        def invoke():
            return binder.materialize_initial_install(args, {}, identity, client, [], root,
                root / 'output', root / 'input.json', root / 'target-input', provenance, ROOT)
        return client, identity, candidate, unpack, invoke

    def test_real_applicability_resolution_binds_independently_validated_target(self):
        for source in ('official', 'custom'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as td:
                root = Path(td); client, _, candidate, unpack, invoke = self.fixture(root, source)
                with patch.object(binder, 'unpack_target', side_effect=unpack), \
                        patch.object(binder, 'verify_run') as current:
                    result = invoke()
                proof = json.loads((root / 'input.json').read_bytes())
                initial.validate_input(proof, candidate)
                self.assertEqual(result['status'], 'initial-applicability-verified-native-install-pending')
                self.assertNotIn('predecessor', proof)
                self.assertTrue((Path(proof['target_package_path']) / 'offline-manifest.json').is_file())
                self.assertEqual(proof['target_source_commit'], COMMIT if source == 'custom' else None)
                current.assert_called_once()

    def test_changed_lower_catalogue_or_current_run_cannot_expose_initial_inputs(self):
        for fault in ('catalogue', 'current-run'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                root = Path(td); client, _, _, unpack, invoke = self.fixture(root, 'official')
                if fault == 'catalogue':
                    client.responses[f'repos/{binder.REPOSITORY}/releases?per_page=100&page=1'] = [
                        release('v2.72.0', repository=binder.REPOSITORY)]
                with patch.object(binder, 'unpack_target', side_effect=unpack), \
                        patch.object(binder, 'verify_run', side_effect=ValueError('changed current run')
                                     if fault == 'current-run' else None), self.assertRaises(ValueError):
                    invoke()
                self.assertFalse((root / 'input.json').exists())
                self.assertFalse((root / 'output').exists())

    def test_custom_target_source_and_actual_manifest_must_match_official_tag(self):
        for fault in ('plan', 'payload'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                root = Path(td); _, identity, _, unpack, invoke = self.fixture(root, 'custom')
                if fault == 'plan': identity['runtime']['source_contract']['source']['commit'] = 'c' * 40
                def changed(*args):
                    package = unpack(*args)
                    if fault == 'payload':
                        (package / 'offline-manifest.json').write_text(json.dumps(
                            {'upstream_provenance': {'source_commit': 'd' * 40}}))
                    return package
                with patch.object(binder, 'unpack_target', side_effect=changed), \
                        patch.object(binder, 'verify_run'), self.assertRaises(ValueError): invoke()
                self.assertFalse((root / 'input.json').exists())

    def test_initial_custom_uses_selected_arch_contract_instead_of_shared_anchor(self):
        for wrong_selected_commit in (False, True):
            with self.subTest(wrong_selected_commit=wrong_selected_commit), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                _, identity, candidate, unpack, invoke = self.fixture(root, 'custom')
                runtime = identity['runtime']
                runtime['source_contract']['source']['commit'] = 'c' * 40
                selected_contract = {'source': {'commit': 'd' * 40 if wrong_selected_commit else COMMIT}}
                runtime['upstream'] = {
                    'source_kind': 'trusted-package-release',
                    'source_inputs': {'amd64': {'source_contract': selected_contract,
                                                'configuration_sources': {}}},
                    'records': {'amd64': {'resolved_contract_sha256': 'f' * 64}},
                }
                with patch.object(binder, 'unpack_target', side_effect=unpack) as target, \
                        patch.object(binder, 'verify_run') as current:
                    if wrong_selected_commit:
                        with self.assertRaises(ValueError):
                            invoke()
                        target.assert_not_called()
                        current.assert_not_called()
                        self.assertFalse((root / 'input.json').exists())
                        self.assertFalse((root / 'output').exists())
                    else:
                        result = invoke()
                        proof = json.loads((root / 'input.json').read_bytes())
                        initial.validate_input(proof, candidate)
                        self.assertEqual(result['status'], 'initial-applicability-verified-native-install-pending')
                        self.assertEqual(proof['target_source_commit'], COMMIT)
                        target.assert_called_once()
                        current.assert_called_once()

    def test_failed_independent_target_validation_never_becomes_initial_acceptance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); _, _, _, _, invoke = self.fixture(root, 'official')
            with patch.object(binder, 'unpack_target', side_effect=ValueError('bad target archive')), \
                    self.assertRaisesRegex(ValueError, 'bad target archive'): invoke()
            self.assertFalse((root / 'input.json').exists())


if __name__ == '__main__':
    unittest.main()
