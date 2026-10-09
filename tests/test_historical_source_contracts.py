"""Reviewed historical inputs and source-specific requirements; no network/runtime."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from enterprise_contract import contract
from official_source import source, verify
from package_matrix import matrix_rows
from publication_contract import policy_fingerprint
from upstream_validation_contract import validator_root

VERSIONS = ['v2.3.1', 'v2.3.0', 'v2.2.5']


class HistoricalSourceContracts(unittest.TestCase):
    def test_actual_source_matrices_and_appstore_boundary(self):
        for version in VERSIONS:
            with self.subTest(version=version):
                rows = matrix_rows(version)
                self.assertEqual(len(rows), 15)
                self.assertEqual(sum(2 if r['source'] == 'enterprise-docker' else 1 for r in rows), 17)
                self.assertEqual({r['arch'] for r in rows if r['source'] == 'official'},
                                 {'amd64', 'arm64', 'armv7', 'ppc64le', 's390x', 'riscv64'})
                self.assertEqual({r['arch'] for r in rows if r['source'] == 'enterprise-docker'},
                                 {'amd64', 'arm64'})
                self.assertEqual(contract(version)['appstore_required'], version != 'v2.2.5')
                for row in rows:
                    if row['source'] == 'official':
                        self.assertEqual(source(version, row['arch'])['version'], version)

    def test_frozen_current_and_corrected_historical_producer_contracts(self):
        current = validator_root('v2.3.2')
        self.assertEqual(current.name, 'upstream-validation')
        self.assertEqual(json.loads((current / 'config/sources.json').read_text())['v2.3.2']['node_version'], '22.14.0')
        for version in VERSIONS:
            selected = validator_root(version)
            self.assertEqual(selected.name, 'upstream-validation-historical')
            self.assertEqual(json.loads((selected / 'SOURCE.json').read_text())['commit'],
                             'b6502c789507eaa6759c70dd853ace23a36a9012')
            self.assertEqual(json.loads((selected / 'config/sources.json').read_text())[version]['node_version'], '22.22.1')
            result = subprocess.run([sys.executable, str(selected / 'scripts/resolve_inputs.py'), version],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_historical_configuration_checks_match_downstream_exact_bytes(self):
        from embedded_configuration import expected_bytes
        selected = validator_root('v2.3.1')
        code = "import sys;sys.path.insert(0,sys.argv[1]);from embedded_configuration import expected_bytes;import hashlib;print(hashlib.sha256(expected_bytes(sys.argv[2],sys.argv[3])[1]).hexdigest())"
        for version in VERSIONS:
            for component in ['core', 'agent']:
                actual = subprocess.check_output([sys.executable, '-c', code, str(selected / 'scripts'),
                                                  version, component], text=True, timeout=5).strip()
                self.assertEqual(actual, hashlib.sha256(expected_bytes(version, component)[1]).hexdigest())

    def test_unknown_version_or_architecture_is_not_guessed(self):
        for version in ['v2.1.13', 'v9.9.9', '../../escape']:
            with self.subTest(version=version), self.assertRaises(ValueError):
                validator_root(version)
        with self.assertRaises(ValueError):
            source('v2.3.1', 'loong64')
        with self.assertRaises(ValueError):
            contract('v2.2.4')

    def test_vendor_tamper_extra_import_and_route_mismatch_fail(self):
        for change in ['script', 'extra', 'manifest', 'route']:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                shutil.copytree(ROOT / 'vendor', root / 'vendor')
                (root / 'config').mkdir()
                shutil.copyfile(ROOT / 'config/upstream-validation.json', root / 'config/upstream-validation.json')
                selected = root / 'vendor/upstream-validation-historical'
                if change == 'script':
                    (selected / 'scripts/validate_artifacts.py').write_text('changed')
                elif change == 'extra':
                    (selected / 'scripts/gzip.py').write_text('unexpected import shadow')
                elif change == 'manifest':
                    (selected / 'SOURCE.json').write_text('{}')
                else:
                    path = root / 'config/upstream-validation.json';data = json.loads(path.read_text())
                    data['v2.3.1']['commit'] = '0' * 40;path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    validator_root('v2.3.1', root)

    def test_official_bytes_size_url_and_version_are_enforced(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td);package = root / 'app.tgz';package.write_bytes(b'reviewed input fixture')
            url = 'https://resource.fit2cloud.com/1panel/package/v2/stable/v2.3.1/release/1panel-v2.3.1-linux-amd64.tar.gz'
            pin = {'version': 'v2.3.1', 'url': url, 'sha256': hashlib.sha256(package.read_bytes()).hexdigest(),
                   'bytes': package.stat().st_size}
            lock = root / 'official-sources-v2.3.1.json';lock.write_text(json.dumps({'amd64': pin}))
            verify(package, 'v2.3.1', 'amd64', url, root)
            with self.assertRaises(ValueError):
                verify(package, 'v2.3.1', 'amd64', url.replace('stable', 'beta'), root)
            package.write_bytes(b'corrupt input')
            with self.assertRaises(ValueError):
                verify(package, 'v2.3.1', 'amd64', url, root)
            for update in [{'bytes': -1}, {'version': 'v2.3.0'}, {'url': 'https://unreviewed.invalid/archive'}]:
                lock.write_text(json.dumps({'amd64': dict(pin, **update)}))
                with self.assertRaises(ValueError):
                    source('v2.3.1', 'amd64', root)

    def test_unrelated_enterprise_history_does_not_change_version_fingerprint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'repo'
            shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'build'))
            before = policy_fingerprint('downstream17', 'v2.3.2', root)
            path = root / 'config/enterprise-contracts.json';data = json.loads(path.read_text())
            data['v2.2.5']['appstore_required'] = True;path.write_text(json.dumps(data))
            self.assertEqual(before, policy_fingerprint('downstream17', 'v2.3.2', root))
            data['v2.3.2']['appstore_required'] = False;path.write_text(json.dumps(data))
            self.assertNotEqual(before, policy_fingerprint('downstream17', 'v2.3.2', root))

    def test_ci_pins_semantic_validation_dependency(self):
        self.assertEqual((ROOT / 'requirements-validation.txt').read_text().strip(), 'PyYAML==6.0.3')
        workflow = (ROOT / '.github/workflows/build-offline-v2.yml').read_text()
        for job, end in [('regression', 'build_plan'), ('publication_plan', 'publication_packages')]:
            section = workflow.split('  ' + job + ':', 1)[1].split('  ' + end + ':', 1)[0]
            self.assertIn('python3 -m venv', section)
            self.assertIn('-r requirements-validation.txt', section)
            self.assertIn('GITHUB_PATH', section)

    def test_both_custom_input_routes_pass_producer_gate_before_extraction(self):
        script=(ROOT/'prepare_offline.sh').read_text()
        gate=script.index('scripts/validate_upstream_package.py')
        self.assertLess(script.index('scripts/import_ci_artifact.py'),gate)
        self.assertLess(script.index('scripts/validate_upstream.py" checksum'),gate)
        self.assertLess(gate,script.index('tar -xf "${app_tar}"'))
        self.assertIn('if [[ "${source}" == "custom" ]]; then\n        # Both canonical-release',script)
        workflow=(ROOT/'.github/workflows/build-offline-v2.yml').read_text()
        packages=workflow.split('  publication_packages:',1)[1].split('  publication_prepare:',1)[0]
        self.assertIn('-r requirements-validation.txt',packages)

    def test_archive_gate_uses_selected_snapshot_without_module_collision(self):
        import validate_upstream_package as gate
        chosen=ROOT/'vendor/upstream-validation-historical'
        with patch.object(gate,'validator_root',return_value=chosen) as selection, \
                patch.object(gate.subprocess,'run') as command:
            gate.validate('/tmp/fixture.tgz','v2.3.1','arm64')
        selection.assert_called_once_with('v2.3.1')
        argv=command.call_args.args[0]
        self.assertEqual(argv[-4:],[str(chosen/'scripts'),'/tmp/fixture.tgz','v2.3.1','arm64'])
        self.assertIn('validate_package',argv[2])
        self.assertTrue(command.call_args.kwargs['check'])

    def test_current_toolchain_cannot_replace_historical_producer_contract(self):
        import upstream_validation_contract as contracts
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'config').mkdir()
            control=b'ORIGINAL_VERSION=v2.3.1\n'
            entry={'source_commit':'a'*40,'installer_commit':'b'*40,'mode':'stable',
                   'go_version':'1.26.1','node_version':'22.22.1','npm_version':'10.9.4',
                   'geoip_sha256':'c'*64,'installer_original_version':'version',
                   'installer_sha256':{'install.sh':'d'*64,'1pctl':hashlib.sha256(b'ORIGINAL_VERSION=version\n').hexdigest()}}
            (root/'config/sources.json').write_text(json.dumps({'v2.3.1':entry}))
            manifest={k:entry[k] for k in ['source_commit','installer_commit','mode','go_version','node_version','npm_version']}
            manifest.update(schema_version=1,edition='community',version='v2.3.1',architecture='amd64',
                            files={'GeoIP.mmdb':{'sha256':'c'*64},'install.sh':{'sha256':'d'*64}})
            with patch.object(contracts,'validator_root',return_value=root):
                contracts.validate_manifest_contract(manifest,'v2.3.1','amd64',control)
                with self.assertRaisesRegex(ValueError,'pinned producer contract'):
                    contracts.validate_manifest_contract(dict(manifest,node_version='22.14.0'),'v2.3.1','amd64',control)


if __name__ == '__main__':
    unittest.main()
