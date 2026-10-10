"""Raw custom AppStore hooks remain subject to the complete producer contract."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from resolved_inventory import ARCHES, digest
from validate_upstream_package import validate
from test_validate_resolved_custom import Fixture, byte_facts


class UpstreamPackageCapabilityTests(unittest.TestCase):
    def fixture(self, arch='amd64'):
        fixture = Fixture(arch)
        samples = ROOT / 'tests/fixtures/historical-installers'
        row = next(row for row in json.loads((samples / 'index.json').read_text())['fixtures']
                   if row['appstore_install'])
        fixture.files['install.sh'] = (samples / row['file']).read_bytes()
        fixture.contract['installer']['resources']['install.sh'] = byte_facts(fixture.files['install.sh'])
        return fixture

    def validate(self, fixture, path, pin):
        row = {'architecture': fixture.arch,
               'file': f'1panel-{fixture.version}-linux-{fixture.arch}.tar.gz',
               'sha256': pin['sha256'], 'size': pin['bytes'],
               'source_commit': fixture.contract['source']['commit'],
               'installer_commit': fixture.contract['installer']['commit'],
               'build_repository_commit': fixture.producer,
               'resolved_contract_sha256': digest(fixture.contract)}
        runtime = {'mode': 'stable', 'source_contract': fixture.contract,
                   'source_contract_sha256': digest(fixture.contract),
                   'upstream': {'records': {fixture.arch: row}, 'producer_commit': fixture.producer},
                   'configuration_sources': {key: raw.decode() for key, raw in fixture.config.items()}}
        with patch('runtime_contract.require_selected', return_value=runtime), \
                patch('resolved_transport.acquire_origin', side_effect=AssertionError('No network in fixture')):
            return validate(path, fixture.version, fixture.arch)

    def test_all_raw_custom_architectures_accept_guarded_hook_without_payload(self):
        for arch in ARCHES:
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as td:
                fixture = self.fixture(arch); path = Path(td) / 'source.tar.gz'
                fixture.files.update({'LICENSE': b'synthetic license', 'README.md': b'synthetic documentation'})
                result = self.validate(fixture, path, fixture.archive(path))
                self.assertEqual(result['architecture'], arch)
                self.assertEqual(result['contract_sha256'], digest(fixture.contract))

    def test_guard_does_not_bypass_pins_manifest_or_required_resources(self):
        for fault, message in [
            ('required-hook', 'requires an archive payload'),
            ('unpinned-installer', 'installer resource mismatch'),
            ('unmanifested-installer', 'manifest does not enumerate'),
            ('unbound-payload', 'file set differs'),
            ('missing-bound-payload', 'file set differs'),
            ('changed-bound-payload', 'installer resource mismatch'),
        ]:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = self.fixture(); path = Path(td) / 'source.tar.gz'; change_manifest = None
                if fault == 'required-hook':
                    fixture.files['install.sh'] = fixture.files['install.sh'].replace(b'        return\n', b'        :\n')
                    fixture.contract['installer']['resources']['install.sh'] = byte_facts(fixture.files['install.sh'])
                if fault == 'unpinned-installer': fixture.files['install.sh'] += b'\n# changed source\n'
                if fault == 'unmanifested-installer': change_manifest = lambda value: value['files'].pop('install.sh')
                if fault == 'unbound-payload': fixture.files['appstore.tar.gz'] = b'unbound appstore'
                if fault in ('missing-bound-payload', 'changed-bound-payload'):
                    fixture.contract['installer']['resources']['appstore.tar.gz'] = byte_facts(b'bound appstore')
                if fault == 'changed-bound-payload': fixture.files['appstore.tar.gz'] = b'changed appstore'
                with self.assertRaisesRegex(ValueError, message):
                    self.validate(fixture, path, fixture.archive(path, change_manifest))

    def test_custom_payload_is_accepted_only_when_bound_to_producer_contract(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = self.fixture(); path = Path(td) / 'source.tar.gz'
            fixture.files['appstore.tar.gz'] = b'synthetic pinned AppStore'
            fixture.contract['installer']['resources']['appstore.tar.gz'] = byte_facts(fixture.files['appstore.tar.gz'])
            self.assertEqual(self.validate(fixture, path, fixture.archive(path))['version'], fixture.version)


if __name__ == '__main__':
    unittest.main()
