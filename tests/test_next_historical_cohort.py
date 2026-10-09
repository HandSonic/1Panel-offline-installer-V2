"""Version-complete historical inventories; local fixtures, no native-pass claim."""
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
from package_matrix import load_plan, matrix_rows, plan
from publication_contract import expected_names, policy_fingerprint
from release_inventory import native_rows, resolved_matrix, unavailable_enterprise
from upstream_validation_contract import validator_root

VERSIONS = ['v2.2.4', 'v2.2.3', 'v2.2.2', 'v2.2.1']


def copied_root(temporary):
    root = Path(temporary) / 'repo'
    shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns('build', '__pycache__', '.git', '*.pyc'))
    return root


class NextHistoricalCohort(unittest.TestCase):
    def test_exact_complete_packages_shards_and_native_rows(self):
        for version in VERSIONS:
            with self.subTest(version=version):
                rows = matrix_rows(version)
                absent = version == 'v2.2.4'
                self.assertEqual(len(rows), 13 if absent else 15)
                self.assertEqual(len(expected_names('downstream17', version)) - 1, 13 if absent else 17)
                self.assertEqual(len(native_rows(version)), 8 if absent else 12)
                self.assertEqual({r['arch'] for r in rows if r['source'] == 'custom'},
                                 {'amd64', 'arm64', 'armv7', 'ppc64le', 's390x', 'riscv64', 'loong64'})
                self.assertNotIn('loong64', resolved_matrix(version)['official'])
                self.assertNotIn('enterprise-original', {r['source'] for r in native_rows(version)})

    def test_unavailable_enterprise_is_explicit_and_not_a_payload_contract(self):
        evidence = unavailable_enterprise('v2.2.4')
        self.assertEqual({r['http_status'] for r in evidence['observations']}, {404})
        self.assertEqual(len(evidence['observations']), 3)
        self.assertFalse(any('enterprise' in name for name in expected_names('downstream17', 'v2.2.4')))
        with self.assertRaisesRegex(ValueError, 'Unreviewed enterprise'):
            contract('v2.2.4')

    def test_unavailable_is_not_inferred_from_missing_or_partial_evidence(self):
        for fault in ['missing', 'incomplete', 'server-error', 'success', 'wrong-version', 'wrong-url', 'duplicate', 'no-timezone']:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                root = copied_root(td)
                path = root / 'config/source-availability/v2.2.4.json'
                data = json.loads(path.read_text())
                if fault == 'missing':
                    path.unlink()
                else:
                    if fault == 'incomplete': data['observations'].pop()
                    elif fault == 'server-error': data['observations'][0]['http_status'] = 503
                    elif fault == 'success': data['observations'][0]['http_status'] = 200
                    elif fault == 'wrong-version': data['version'] = 'v2.2.3'
                    elif fault == 'wrong-url': data['observations'][1]['url'] = data['observations'][1]['url'].replace('v2.2.4', 'v2.2.3')
                    elif fault == 'duplicate': data['observations'][1] = data['observations'][2]
                    elif fault == 'no-timezone': data['observations'][0]['observed_at_utc'] = '2026-10-09T05:07:00'
                    path.write_text(json.dumps(data))
                with self.assertRaises(ValueError): matrix_rows('v2.2.4', root)

    def test_available_sources_cannot_disappear_from_matrix(self):
        for version in VERSIONS + ['v2.3.2']:
            for fault in ['official', 'custom', 'enterprise', 'unpaired', 'duplicate', 'unknown', 'empty', 'empty-source', 'unsupported-arch']:
                with self.subTest(version=version, fault=fault), tempfile.TemporaryDirectory() as td:
                    root = copied_root(td)
                    path = root / f'release-matrix-{version}.json'
                    data = json.loads(path.read_text())
                    if fault in ('official', 'custom'): data[fault].pop()
                    elif fault == 'enterprise':
                        if version == 'v2.2.4': data['enterprise-original'] = data['enterprise-docker'] = ['amd64', 'arm64']
                        else:
                            data.pop('enterprise-original'); data.pop('enterprise-docker')
                    elif fault == 'unpaired': data['enterprise-docker'] = ['amd64']
                    elif fault == 'duplicate': data['official'].append(data['official'][0])
                    elif fault == 'unknown': data['unreviewed'] = ['amd64']
                    elif fault == 'empty': data = {}
                    elif fault == 'empty-source': data['custom'] = []
                    elif fault == 'unsupported-arch': data['official'].append('unknown64')
                    path.write_text(json.dumps(data))
                    with self.assertRaises(ValueError): matrix_rows(version, root)
                    with self.assertRaises(ValueError): expected_names('downstream17', version, root)

    def test_missing_source_locks_and_duplicate_matrix_fields_fail_closed(self):
        for fault in ['official-lock', 'enterprise-lock', 'duplicate-source']:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                root = copied_root(td)
                if fault.endswith('-lock'):
                    (root / (fault.removesuffix('-lock') + '-sources-v2.2.4.json')).unlink()
                else:
                    path = root / 'release-matrix-v2.2.4.json'
                    path.write_text(path.read_text().replace('{', '{"custom":["amd64"],', 1))
                with self.assertRaises(ValueError): matrix_rows('v2.2.4', root)

    def test_new_snapshots_pin_exact_producer_entries_and_configuration(self):
        fixtures = json.loads((ROOT / 'tests/fixtures/next-historical-inputs.json').read_text())
        code = 'import sys,hashlib;sys.path.insert(0,sys.argv[1]);from embedded_configuration import expected_bytes;print(hashlib.sha256(expected_bytes(sys.argv[2],sys.argv[3])[1]).hexdigest())'
        for version, expected in fixtures.items():
            with self.subTest(version=version):
                selected = validator_root(version)
                self.assertEqual(json.loads((selected / 'SOURCE.json').read_text())['commit'], expected['producer_commit'])
                source = json.loads((selected / 'config/sources.json').read_text())[version]
                self.assertEqual(source, expected['source_entry'])
                for component in ['core', 'agent']:
                    output = subprocess.check_output([sys.executable, '-c', code, str(selected / 'scripts'), version, component], text=True, timeout=5).strip()
                    self.assertEqual(output, hashlib.sha256(expected_bytes(version, component)[1]).hexdigest())
                self.assertEqual(source['go_version'], '1.26.1' if version in ['v2.2.4', 'v2.2.3'] else '1.25.10')

    def test_selected_installer_cohorts_preserve_real_cli_and_interactive_boundary(self):
        fixtures = json.loads((ROOT / 'tests/fixtures/next-historical-inputs.json').read_text())
        hashes = []
        for version, expected in fixtures.items():
            source = expected['source_entry']
            installer = (ROOT / 'tests/fixtures/historical-installers' / (source['installer_commit'] + '.sh')).read_bytes()
            self.assertEqual(hashlib.sha256(installer).hexdigest(), source['installer_sha256']['install.sh'])
            self.assertEqual(b'NON_INTERACTIVE=' in installer, version in ['v2.2.4', 'v2.2.3'])
            self.assertNotIn(b'function Install_AppStore', installer)
            hashes.append(hashlib.sha256(installer).hexdigest())
        self.assertEqual(len(set(hashes)), 3)
        self.assertEqual(hashes[-1], hashes[-2])

    def test_enterprise_uses_observed_source_specific_installer_cohort(self):
        from enterprise_contract import validate_layout
        from validate_payload import APP_REQUIRED
        import tarfile
        layouts = json.loads((ROOT / 'tests/fixtures/next-enterprise-layouts.json').read_text())
        sources = json.loads((ROOT / 'tests/fixtures/next-historical-inputs.json').read_text())
        for version, row in layouts.items():
            with self.subTest(version=version):
                self.assertEqual(contract(version), {k: row[k] for k in ['installer_sha256', 'appstore_required']})
                fixture = ROOT / 'tests/fixtures/historical-installers' / (row['immutable_installer_cohort_commit'] + '.sh')
                installer = fixture.read_bytes()
                self.assertEqual(hashlib.sha256(installer).hexdigest(), row['installer_sha256'])
                self.assertEqual(b'NON_INTERACTIVE=' in installer, row['non_interactive_cli'])
                self.assertFalse(row['appstore_required'])
                prefix = f'1panel-{version}-linux-amd64/'
                entries = {}
                for name in APP_REQUIRED + ['install.sh', 'upgrade.sh']:
                    member = tarfile.TarInfo(prefix + name); member.size = 1; entries[member.name] = member
                validate_layout(entries, prefix, installer, version)
                for wrong in [installer + b'\n# changed', b'unsupported cohort']:
                    with self.assertRaises(ValueError): validate_layout(entries, prefix, wrong, version)
                member = tarfile.TarInfo(prefix + 'appstore.tar.gz'); member.size = 1; entries[member.name] = member
                with self.assertRaisesRegex(ValueError, 'AppStore'): validate_layout(entries, prefix, installer, version)
                if version == 'v2.2.3':
                    self.assertNotEqual(row['installer_sha256'], sources[version]['source_entry']['installer_sha256']['install.sh'])
                else:
                    self.assertEqual(row['installer_sha256'], sources[version]['source_entry']['installer_sha256']['install.sh'])

    def test_old_routes_files_and_semantic_version_fingerprints_are_unchanged(self):
        fixtures = json.loads((ROOT / 'tests/fixtures/protected-historical-contracts.json').read_text())
        routes = json.loads((ROOT / 'config/upstream-validation.json').read_text())
        for version, previous in fixtures.items():
            with self.subTest(version=version):
                self.assertEqual(routes[version], previous['route'])
                selected = validator_root(version)
                self.assertEqual(hashlib.sha256((selected / 'SOURCE.json').read_bytes()).hexdigest(), previous['source_manifest_actual_sha256'])
                self.assertEqual(policy_fingerprint('downstream17', version), previous['downstream_policy_fingerprint'])
                self.assertEqual(len(matrix_rows(version)), 15)
                self.assertEqual(len(native_rows(version)), 12)

    def test_absence_evidence_is_bound_to_only_its_version_policy(self):
        with tempfile.TemporaryDirectory() as td:
            root = copied_root(td)
            before = {v: policy_fingerprint('downstream17', v, root) for v in ['v2.2.4', 'v2.3.2']}
            path = root / 'config/source-availability/v2.2.4.json'; data = json.loads(path.read_text())
            data['observations'][0]['observed_at_utc'] = '2026-10-09T05:08:00+00:00'
            path.write_text(json.dumps(data))
            self.assertNotEqual(before['v2.2.4'], policy_fingerprint('downstream17', 'v2.2.4', root))
            self.assertEqual(before['v2.3.2'], policy_fingerprint('downstream17', 'v2.3.2', root))

    def test_plan_exports_and_binds_exact_native_expectations(self):
        for version in VERSIONS:
            with self.subTest(version=version), tempfile.TemporaryDirectory() as td, patch.dict(os.environ, GITHUB_SHA='a' * 40, GITHUB_RUN_ID='123', GITHUB_OUTPUT=str(Path(td) / 'outputs')):
                args = SimpleNamespace(version=version, repository='HandSonic/1Panel-offline-installer-V2', tag=version,
                                       work=str(Path(td) / 'plan'), upstream_source='release')
                plan(args)
                path = Path(args.work) / 'plan.json'; facts = json.loads(path.read_text())
                outputs = dict(line.split('=', 1) for line in (Path(td) / 'outputs').read_text().splitlines())
                self.assertEqual(json.loads(outputs['native_matrix']), {'include': native_rows(version)})
                self.assertEqual(facts['native_rows'], native_rows(version))
                for fault in ['missing', 'duplicate', 'extra', 'scenario', 'empty', 'unsupported']:
                    altered = copy.deepcopy(facts)
                    if fault == 'missing': altered['native_rows'].pop()
                    elif fault == 'duplicate': altered['native_rows'].append(altered['native_rows'][0])
                    elif fault == 'extra': altered['native_rows'].append({'source': 'enterprise-original', 'arch': 'amd64', 'scenario': 'fresh'})
                    elif fault == 'scenario': altered['native_rows'][0]['scenario'] = 'optional'
                    elif fault == 'empty': altered['native_rows'] = []
                    elif fault == 'unsupported': altered['native_rows'][0]['arch'] = 'unknown64'
                    path.write_text(json.dumps(altered))
                    with self.assertRaisesRegex(ValueError, 'plan identity'): load_plan(args)


if __name__ == '__main__':
    unittest.main()
