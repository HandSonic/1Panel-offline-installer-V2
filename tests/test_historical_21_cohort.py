"""Reviewed 2.1 inputs and fail-closed plans; no native installation is executed."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from embedded_configuration import expected_bytes
from enterprise_contract import contract
from official_source import source
from package_matrix import load_plan, matrix_rows, plan
from publication_contract import expected_names, policy_fingerprint
from release_inventory import native_rows, resolved_matrix, unavailable_enterprise
from upstream_validation_contract import validate_manifest_contract, validator_root

VERSIONS = ['v2.1.13', 'v2.1.12', 'v2.1.11']
FIXTURES = ROOT / 'tests/fixtures/historical-21'


def copied_root(temporary):
    root = Path(temporary) / 'repo'
    shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns('build', '__pycache__', '.git', '*.pyc'))
    return root


class Historical21Cohort(unittest.TestCase):
    def test_complete_source_architecture_and_native_scenario_sets(self):
        official = {'amd64', 'arm64', 'armv7', 'ppc64le', 's390x', 'riscv64'}
        for version in VERSIONS:
            with self.subTest(version=version):
                self.assertEqual(resolved_matrix(version), {
                    'official': ['amd64', 'arm64', 'armv7', 'ppc64le', 's390x', 'riscv64'],
                    'custom': ['amd64', 'arm64', 'armv7', 'ppc64le', 's390x', 'riscv64', 'loong64']})
                self.assertEqual(len(matrix_rows(version)), 13)
                self.assertEqual(len(expected_names('downstream17', version)), 14)
                self.assertEqual({(r['source'], r['arch'], r['scenario']) for r in native_rows(version)},
                                 {(s, a, d) for s in ('official', 'custom')
                                  for a in ('amd64', 'arm64') for d in ('existing', 'fresh')})
                for arch in official:
                    self.assertEqual(source(version, arch)['version'], version)
                with self.assertRaises(ValueError): source(version, 'loong64')
                self.assertEqual(json.loads((ROOT / f'enterprise-sources-{version}.json').read_text()), {})
                self.assertEqual(len(unavailable_enterprise(version)['observations']), 3)
                with self.assertRaises(ValueError): contract(version)

    def test_missing_absence_evidence_and_fabricated_enterprise_fail_closed(self):
        for version in VERSIONS:
            for fault in ['missing', 'server-error', 'wrong-version', 'enterprise', 'empty-enterprise']:
                with self.subTest(version=version, fault=fault), tempfile.TemporaryDirectory() as td:
                    root = copied_root(td)
                    path = root / 'config/source-availability' / (version + '.json')
                    if fault == 'missing': path.unlink()
                    elif fault in ['server-error', 'wrong-version']:
                        data = json.loads(path.read_text())
                        if fault == 'server-error': data['observations'][0]['http_status'] = 503
                        else: data['version'] = 'v9.9.9'
                        path.write_text(json.dumps(data))
                    else:
                        path = root / f'release-matrix-{version}.json'
                        data = json.loads(path.read_text())
                        data['enterprise-original'] = data['enterprise-docker'] = [] if fault == 'empty-enterprise' else ['amd64', 'arm64']
                        path.write_text(json.dumps(data))
                    with self.assertRaises(ValueError): matrix_rows(version, root)

    def test_exact_producer_source_and_historical_toolchain(self):
        expected = json.loads((FIXTURES / 'inputs.json').read_text())
        for version in VERSIONS:
            with self.subTest(version=version):
                selected = validator_root(version)
                self.assertEqual(selected.name, 'upstream-validation-historical-21-interactive')
                self.assertEqual(json.loads((selected / 'SOURCE.json').read_text())['commit'],
                                 '8e8ffd07a00c2660b559e999846b6bf6d82fc152')
                entry = json.loads((selected / 'config/sources.json').read_text())[version]
                self.assertEqual(entry, expected[version])
                self.assertEqual((entry['go_version'], entry['node_version'], entry['npm_version']),
                                 ('1.25.7', '22.22.1', '10.9.4'))
                result = subprocess.run([sys.executable, str(selected / 'scripts/resolve_inputs.py'), version],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                installer = ROOT / 'tests/fixtures/historical-installers' / (entry['installer_commit'] + '.sh')
                self.assertEqual(hashlib.sha256(installer.read_bytes()).hexdigest(), entry['installer_sha256']['install.sh'])
                self.assertNotIn(b'NON_INTERACTIVE=', installer.read_bytes())
                self.assertNotIn(b'function Install_AppStore', installer.read_bytes())

    def test_old_configuration_has_exact_fields_and_matches_producer(self):
        registry = json.loads((ROOT / 'config/embedded-configs.json').read_text())
        code = 'import sys,hashlib;sys.path.insert(0,sys.argv[1]);from embedded_configuration import expected_bytes;print(hashlib.sha256(expected_bytes(sys.argv[2],sys.argv[3])[1]).hexdigest())'
        for version in VERSIONS:
            selected = validator_root(version)
            self.assertEqual(registry[version], json.loads((selected / 'config/embedded-configs.json').read_text())[version])
            for component in ('core', 'agent'):
                with self.subTest(version=version, component=component):
                    original, normalized, _ = expected_bytes(version, component)
                    self.assertNotIn(b'is_enterprise', original)
                    self.assertNotIn(b'is_enterprise', normalized)
                    actual = subprocess.check_output([sys.executable, '-c', code, str(selected / 'scripts'), version, component], text=True, timeout=5).strip()
                    self.assertEqual(actual, hashlib.sha256(normalized).hexdigest())

    def test_v212_official_control_is_not_substituted_for_custom(self):
        version = 'v2.1.12'
        entry = json.loads((FIXTURES / 'inputs.json').read_text())[version]
        manifest = {key: entry[key] for key in ('source_commit', 'installer_commit', 'mode', 'go_version', 'node_version', 'npm_version')}
        manifest.update(schema_version=1, edition='community', version=version, architecture='amd64',
                        files={name: {'sha256': value} for name, value in entry['installer_sha256'].items()})
        manifest['files']['GeoIP.mmdb'] = {'sha256': entry['geoip_sha256']}
        custom = (FIXTURES / 'v2.1.12-custom-1pctl').read_bytes()
        official = (FIXTURES / 'v2.1.12-official-1pctl').read_bytes()
        self.assertEqual(hashlib.sha256(custom).hexdigest(), '4c31165ea5c4e45a81846aaff5a0d6cee4b095e0d333d0ef8b78d11ec80bd637')
        self.assertEqual(hashlib.sha256(official).hexdigest(), '403b88e31bb753d6702b856b2f6c665edfae5a56a0a89bff103a6b1c476204c7')
        validate_manifest_contract(manifest, version, 'amd64', custom)
        with self.assertRaisesRegex(ValueError, '1pctl'):
            validate_manifest_contract(manifest, version, 'amd64', official)
        for toolchain in ('go_version', 'node_version', 'npm_version'):
            with self.subTest(toolchain=toolchain), self.assertRaisesRegex(ValueError, 'producer contract'):
                validate_manifest_contract(dict(manifest, **{toolchain: '0.0.1'}), version, 'amd64', custom)

    def test_all_eight_old_routes_and_policy_fingerprints_remain_unchanged(self):
        protected = json.loads((ROOT / 'tests/fixtures/protected-21-cohort-contracts.json').read_text())
        self.assertEqual(len(protected), 8)
        routes = json.loads((ROOT / 'config/upstream-validation.json').read_text())
        for version, facts in protected.items():
            with self.subTest(version=version):
                self.assertEqual(routes[version], facts['route'])
                self.assertEqual(policy_fingerprint('downstream17', version), facts['policy_fingerprint'])

    def test_background_v2110_snapshot_record_does_not_activate_version(self):
        self.assertIn('v2.1.10', json.loads((validator_root('v2.1.11') / 'config/embedded-configs.json').read_text()))
        with self.assertRaises(ValueError): validator_root('v2.1.10')
        with self.assertRaises(ValueError): resolved_matrix('v2.1.10')
        with self.assertRaises(ValueError): source('v2.1.10', 'amd64')
        with self.assertRaises(ValueError): expected_bytes('v2.1.10', 'core')
        self.assertFalse((ROOT / 'enterprise-sources-v2.1.10.json').exists())

    def test_plan_binds_every_native_candidate_row(self):
        for version in VERSIONS:
            with self.subTest(version=version), tempfile.TemporaryDirectory() as td, patch.dict(os.environ, GITHUB_SHA='a' * 40, GITHUB_RUN_ID='123', GITHUB_OUTPUT=str(Path(td) / 'outputs')):
                args = SimpleNamespace(version=version, repository='HandSonic/1Panel-offline-installer-V2', tag=version,
                                       work=str(Path(td) / 'plan'), upstream_source='release')
                plan(args)
                load_plan(args)
                path = Path(args.work) / 'plan.json'
                original = json.loads(path.read_text())
                self.assertEqual(original['native_rows'], native_rows(version))
                for fault in ('missing', 'duplicate', 'unreviewed'):
                    altered = copy.deepcopy(original)
                    if fault == 'missing': altered['native_rows'].pop()
                    elif fault == 'duplicate': altered['native_rows'].append(altered['native_rows'][0])
                    else: altered['native_rows'][0]['source'] = 'enterprise-docker'
                    path.write_text(json.dumps(altered))
                    with self.assertRaisesRegex(ValueError, 'plan identity'): load_plan(args)


if __name__ == '__main__':
    unittest.main()
