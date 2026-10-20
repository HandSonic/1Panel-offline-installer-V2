"""Synthetic unregistered versions run through real package-consumer adapters."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from resolved_inventory import ARCHES, canonical, digest, package_plan
from runtime_contract import PLAN_PATH, PLAN_SHA, selected, validate
from test_resolved_inventory import inventory
from test_validate_resolved_custom import Fixture


def runtime(root=ROOT, version='v2.99.0', mode='stable', enterprise=True):
    fixture = Fixture()
    contract = fixture.contract
    contract['version'], contract['mode'] = version, mode
    from semantic_configuration import production
    for part, raw in fixture.config.items():
        contract['configuration'][part]['normalized_sha256'] = hashlib.sha256(production(raw, version, part, mode)).hexdigest()
    deps = {name: json.loads((root / (name + '-sources.json')).read_text()) for name in ('docker', 'compose')}
    plan, _ = package_plan(version, mode, contract, digest(contract),
                           inventory(version, mode, available=[a for a in ARCHES if a != 'loong64']),
                           inventory(version, mode, source='enterprise', available=('amd64', 'arm64') if enterprise else ()),
                           deps['docker'], deps['compose'])
    rows = {arch: {'architecture': arch, 'file': f'1panel-{version}-linux-{arch}.tar.gz',
                   'sha256': hashlib.sha256(('synthetic archive ' + arch).encode()).hexdigest(), 'size': 123,
                   'source_commit': contract['source']['commit'], 'installer_commit': contract['installer']['commit'],
                   'build_repository_commit': fixture.producer, 'resolved_contract_sha256': digest(contract)}
            for arch in ARCHES}
    value = {'schema': 1, 'kind': '1panel-resolved-offline-inputs', 'version': version, 'mode': mode,
             'source_contract': contract, 'source_contract_sha256': digest(contract), 'inventory': plan,
             'upstream': {'repository': 'HandSonic/1Panel-Build-v2', 'source_kind': 'verified-public-release',
                          'validation_sha256': 'd' * 64, 'validation_run_id': 900001,
                          'validation_commit': 'e' * 40, 'producer_commit': fixture.producer, 'records': rows,
                          'matrix_manifest': None},
             'configuration_sources': {part: raw.decode() for part, raw in fixture.config.items()}}
    return value


class RuntimeContractTests(unittest.TestCase):
    def save(self, directory, value):
        path = Path(directory) / 'plan.json'; raw = canonical({'resolved': value}); path.write_bytes(raw)
        return {PLAN_PATH: str(path), PLAN_SHA: hashlib.sha256(raw).hexdigest()}

    def test_public_predecessor_source_validator_is_policy_bound(self):
        from runtime_contract import policy
        value = runtime()
        before = policy(value)
        read_bytes = Path.read_bytes
        def changed(path):
            raw = read_bytes(path)
            return raw + b'\n# synthetic validator change\n' if path == ROOT / 'scripts/public_predecessor_source.py' else raw
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(policy(value), before)

    def test_upgrade_configuration_profile_is_policy_bound(self):
        from runtime_contract import policy
        value = runtime(); before = policy(value); read_bytes = Path.read_bytes
        def changed(path):
            raw = read_bytes(path)
            return raw + b'\n# synthetic profile change\n' if path == ROOT / 'scripts/upgrade_configuration.py' else raw
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(policy(value), before)

    def test_unregistered_plan_uses_only_generic_dependencies(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ('docker', 'compose'):
                (root / (name + '-sources.json')).write_bytes((ROOT / (name + '-sources.json')).read_bytes())
            value = runtime(root)
            self.assertEqual(validate(value, 'v2.99.0', root), value)
            env = self.save(root, value)
            with patch.dict(os.environ, env):
                from release_inventory import resolved_matrix, native_rows
                from official_source import source
                from enterprise_contract import source as enterprise_source
                from embedded_configuration import expected_bytes
                self.assertEqual(resolved_matrix('v2.99.0', root), value['inventory']['matrix'])
                self.assertEqual(native_rows('v2.99.0', root), value['inventory']['native_rows'])
                self.assertEqual(source('v2.99.0', 'amd64', root), value['inventory']['official']['archives']['amd64'])
                self.assertEqual(enterprise_source('v2.99.0', 'arm64', root), value['inventory']['enterprise']['archives']['arm64'])
                original, normalized, commit = expected_bytes('v2.99.0', 'core', root)
                self.assertIn(b'extra_optional: keep', normalized)
                self.assertIn(b'mode: stable', normalized)
                self.assertEqual(commit, value['source_contract']['source']['commit'])
            self.assertEqual({p.name for p in root.iterdir()}, {'docker-sources.json', 'compose-sources.json', 'plan.json'})

    def test_no_implicit_runtime_activation(self):
        with tempfile.TemporaryDirectory() as td:
            self.save(td, runtime())
            self.assertIsNone(selected('v2.99.0', env={}))
            for env in ({PLAN_PATH: str(Path(td) / 'plan.json')}, {PLAN_SHA: 'a' * 64}):
                with self.assertRaisesRegex(ValueError, 'both required'):
                    selected('v2.99.0', env=env)

    def test_all_production_consumers_reject_absent_runtime(self):
        from embedded_configuration import expected_bytes
        from enterprise_contract import source as enterprise_source
        from official_source import source
        from package_matrix import matrix_rows
        from publication_contract import policy_fingerprint
        from release_inventory import native_rows, resolved_matrix
        consumers = [lambda: expected_bytes('v2.3.2','core'),
                     lambda: enterprise_source('v2.3.2','amd64'),
                     lambda: source('v2.3.2','amd64'),
                     lambda: matrix_rows('v2.3.2'),lambda: native_rows('v2.3.2'),
                     lambda: resolved_matrix('v2.3.2'),
                     lambda: policy_fingerprint('downstream17','v2.3.2')]
        with patch.dict(os.environ,{},clear=True):
            for index,consume in enumerate(consumers):
                with self.subTest(index=index),self.assertRaisesRegex(ValueError,'Authenticated runtime plan required'):
                    consume()
        for pattern in ('release-matrix-v*.json','official-sources-v*.json','enterprise-sources-v*.json'):
            self.assertEqual(list(ROOT.glob(pattern)),[])
        self.assertFalse(any((ROOT/'config').rglob('*.json')))
        self.assertFalse(any((ROOT/'vendor').rglob('SOURCE.json')))

    def test_plan_sha_version_and_symlink_are_enforced(self):
        with tempfile.TemporaryDirectory() as td:
            env = self.save(td, runtime())
            self.assertIsNotNone(selected('v2.99.0', env=env))
            with self.assertRaises(ValueError): selected('v2.99.1', env=env)
            with self.assertRaisesRegex(ValueError, 'bytes changed'):
                selected('v2.99.0', env={**env, PLAN_SHA: 'a' * 64})
            alias = Path(td) / 'alias.json'; alias.symlink_to('plan.json')
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                selected('v2.99.0', env={**env, PLAN_PATH: str(alias)})

    def test_self_consistent_plan_cannot_relax_matrix_pins_or_configuration(self):
        faults = [lambda v: v['inventory']['matrix']['custom'].pop(),
                  lambda v: v['inventory']['rows'].pop(),
                  lambda v: v['inventory']['docker']['amd64'].update(sha256='f' * 64),
                  lambda v: v['configuration_sources'].update(core='different'),
                  lambda v: v['upstream']['records'].pop('arm64'),
                  lambda v: v['upstream']['records']['arm64'].update(build_repository_commit='f' * 40),
                  lambda v: v['upstream']['records']['arm64'].update(resolved_contract_sha256='f' * 64),
                  lambda v: v['upstream'].update(validation_run_id=True)]
        for index, mutate in enumerate(faults):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as td:
                value = runtime(); mutate(value); env = self.save(td, value)
                with self.assertRaises(ValueError): selected('v2.99.0', env=env)

    def test_runtime_manifest_recheck_binds_actual_control_and_producer(self):
        from runtime_contract import manifest_contract
        value = runtime(); contract = value['source_contract']; fixture = Fixture()
        data = {'schema_version': 1, 'edition': 'community', 'version': fixture.version,
                'architecture': 'amd64', 'source_commit': contract['source']['commit'],
                'installer_commit': contract['installer']['commit'], 'mode': 'stable',
                'build_repository_commit': fixture.producer, 'resolved_contract_sha256': digest(contract),
                **{key + '_version': v for key, v in contract['toolchain'].items()},
                'files': {name: {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                          for name, raw in fixture.files.items()}}
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, self.save(td, value)):
            self.assertEqual(manifest_contract(data, fixture.files['1pctl'], fixture.version, 'amd64'), data)
            for field in ('source_commit', 'resolved_contract_sha256', 'build_repository_commit', 'node_version'):
                wrong = copy.deepcopy(data); wrong[field] = 'untrusted'
                with self.subTest(field=field), self.assertRaises(ValueError):
                    manifest_contract(wrong, fixture.files['1pctl'], fixture.version, 'amd64')
            with self.assertRaises(ValueError):
                manifest_contract(data, fixture.files['1pctl'] + b'changed', fixture.version, 'amd64')


class ProductPolicyTests(unittest.TestCase):
    def setUp(self):
        # Fingerprint boundaries are independent of the checkout's file bytes;
        # RuntimeContractTests separately check that validator code is hashed.
        self.code = patch('runtime_contract.validator_code', return_value={'scripts/validator.py': 'a' * 64})
        self.validator_code = self.code.start()
        self.addCleanup(self.code.stop)

    def test_official_product_survives_custom_recovery_and_sibling_input_changes(self):
        from runtime_contract import policy, product_policy
        value = runtime(); before = product_policy(value, 'official', 'amd64'); whole = policy(value)
        def unavailable(v):
            v.update(source_contract=None, source_contract_sha256=None, upstream=None,
                     configuration_sources={}, custom_failure='Custom input authentication failed')
            v['inventory']['source_contract_sha256'] = None
        changes = [unavailable,
            lambda v: v['inventory']['official']['archives']['arm64'].update(sha256='b' * 64),
            lambda v: v['inventory']['enterprise']['archives']['amd64'].update(sha256='b' * 64),
            lambda v: v['inventory']['docker']['arm64'].update(sha256='b' * 64),
            lambda v: v['inventory']['compose']['arm64'].update(sha256='b' * 64)]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                changed = copy.deepcopy(value); change(changed)
                self.assertEqual(product_policy(changed, 'official', 'amd64'), before)
                self.assertNotEqual(policy(changed), whole)
        absent = copy.deepcopy(value); unavailable(absent)
        self.assertEqual(product_policy(absent, 'official', 'amd64'), product_policy(value, 'official', 'amd64'))

    def test_own_vendor_archive_and_bundled_dependencies_change_product_policy(self):
        from runtime_contract import product_policy
        value = runtime()
        for source in ('official', 'enterprise-docker'):
            vendor = 'official' if source == 'official' else 'enterprise'
            before = product_policy(value, source, 'amd64')
            changes = [lambda v: v['inventory'][vendor]['archives']['amd64'].update(sha256='b' * 64),
                       lambda v: v['inventory'][vendor]['archives']['amd64'].update(bytes=999),
                       lambda v: v['inventory']['docker']['amd64'].update(sha256='b' * 64),
                       lambda v: v['inventory']['compose']['amd64'].update(sha256='b' * 64)]
            for index, change in enumerate(changes):
                with self.subTest(source=source, change=index):
                    changed = copy.deepcopy(value); change(changed)
                    self.assertNotEqual(product_policy(changed, source, 'amd64'), before)

    def test_validator_code_changes_invalidate_every_product_policy(self):
        from runtime_contract import product_policy
        value = runtime()
        sources = ('official', 'custom', 'enterprise-original', 'enterprise-docker')
        before = {source: product_policy(value, source, 'amd64') for source in sources}
        self.validator_code.return_value = {'scripts/validator.py': 'b' * 64}
        for source in sources:
            with self.subTest(source=source):
                self.assertNotEqual(product_policy(value, source, 'amd64'), before[source])

    def test_custom_policy_binds_own_archive_contract_configuration_and_dependencies(self):
        from runtime_contract import product_policy
        value = runtime(); before = product_policy(value, 'custom', 'amd64')
        changes = [lambda v: v['upstream']['records']['amd64'].update(sha256='b' * 64),
                   lambda v: v['upstream']['records']['amd64'].update(size=999),
                   lambda v: v['source_contract']['source'].update(commit='b' * 40),
                   lambda v: v['configuration_sources'].update(core='changed configuration'),
                   lambda v: v['inventory']['docker']['amd64'].update(sha256='b' * 64),
                   lambda v: v['inventory']['compose']['amd64'].update(sha256='b' * 64)]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                changed = copy.deepcopy(value); change(changed)
                self.assertNotEqual(product_policy(changed, 'custom', 'amd64'), before)

    def test_custom_policy_ignores_other_archives_and_vendor_origins(self):
        from runtime_contract import product_policy
        value = runtime(); before = product_policy(value, 'custom', 'amd64')
        changes = [lambda v: v['upstream']['records']['arm64'].update(sha256='b' * 64),
                   lambda v: v['inventory']['official']['archives']['amd64'].update(sha256='b' * 64),
                   lambda v: v['inventory']['enterprise']['archives']['amd64'].update(sha256='b' * 64),
                   lambda v: v['inventory']['docker']['arm64'].update(sha256='b' * 64),
                   lambda v: v['inventory']['compose']['arm64'].update(sha256='b' * 64)]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                changed = copy.deepcopy(value); change(changed)
                self.assertEqual(product_policy(changed, 'custom', 'amd64'), before)

    def test_enterprise_original_ignores_docker_and_compose_but_binds_vendor_bytes(self):
        from runtime_contract import product_policy
        value = runtime(); before = product_policy(value, 'enterprise-original', 'amd64')
        for dependency in ('docker', 'compose'):
            changed = copy.deepcopy(value)
            for arch in ARCHES:changed['inventory'][dependency][arch]['sha256'] = 'b' * 64
            with self.subTest(dependency=dependency):
                self.assertEqual(product_policy(changed, 'enterprise-original', 'amd64'), before)
        changed = copy.deepcopy(value)
        changed['inventory']['enterprise']['archives']['amd64']['sha256'] = 'b' * 64
        self.assertNotEqual(product_policy(changed, 'enterprise-original', 'amd64'), before)

    def test_unavailable_or_unrequested_products_cannot_claim_a_policy(self):
        from runtime_contract import product_policy
        value = runtime()
        with self.assertRaisesRegex(ValueError, 'Unrequested'):product_policy(value, 'official', 'loong64')
        del value['upstream']['records']['amd64']
        with self.assertRaisesRegex(ValueError, 'unavailable'):product_policy(value, 'custom', 'amd64')


if __name__ == '__main__':
    unittest.main()
