"""Synthetic future-version resolution. No real run IDs, receipts or archive pins."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from resolved_inventory import (ARCHES, INSTALLER_REQUIRED, canonical, digest, package_plan, source_contract,
                                vendor_base, vendor_inventory, version_identity)


def facts(text):
    raw = text.encode()
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def contract(version='v2.99.0', mode='stable'):
    files = {p: facts('synthetic source ' + p) for p in
             ['core/config/config.yaml', 'agent/config/config.yaml',
              'frontend/package.json', 'frontend/package-lock.json', 'core/go.mod', 'agent/go.mod']}
    return {'schema': 1, 'kind': '1panel-resolved-build-inputs', 'version': version,
            'mode': mode, 'edition': 'community', 'architectures': list(ARCHES),
            'source': {'repository': '1Panel-dev/1Panel', 'commit': 'a' * 40,
                       'files': files, 'absent': ['frontend/pnpm-lock.yaml']},
            'installer': {'repository': '1Panel-dev/installer', 'commit': 'b' * 40,
                          'original_version': version,
                          'resources': {p: facts('synthetic installer ' + p)
                                        for p in INSTALLER_REQUIRED}},
            'toolchain': {'go': '1.26.1', 'node': '22.22.1', 'npm': '10.9.4'},
            'resources': {'geoip': {'url': 'https://raw.githubusercontent.com/example/data/' + 'c' * 40 + '/GeoIP.mmdb',
                                    **facts('synthetic geoip')}},
            'configuration': {part: {'path': part + '/config/config.yaml',
                                    'source_sha256': files[part + '/config/config.yaml']['sha256'],
                                    'source_bytes': files[part + '/config/config.yaml']['bytes'],
                                    'normalized_sha256': facts('synthetic normalized ' + part)['sha256']}
                              for part in ['core', 'agent']},
            'frontend_lock': {'kind': 'source', 'sha256': files['frontend/package-lock.json']['sha256'],
                              'manifest_sha256': files['frontend/package.json']['sha256']}}


def responses(version, mode, source, available):
    base = vendor_base(version, mode, source)
    available = set(available)
    observations = {arch: {'url': base + f'1panel-{version}-linux-{arch}.tar.gz',
                           'status': 200 if arch in available else 404,
                           'bytes': len(('synthetic archive ' + source + arch).encode()) if arch in available else None}
                    for arch in ARCHES}
    body = ''.join(facts('synthetic archive ' + source + arch)['sha256'] +
                   f'  1panel-{version}-linux-{arch}.tar.gz\n' for arch in ARCHES if arch in available).encode()
    return {'url': base + 'checksums.txt', 'status': 200 if available else 404, 'body': body}, observations


def inventory(version='v2.99.0', mode='stable', source='official', available=('amd64', 'arm64')):
    return vendor_inventory(version, mode, source, *responses(version, mode, source, available))


def dependencies(label):
    return {arch: {'url': f'https://example.invalid/{label}/{arch}', 'version': '1.2.3',
                   'sha256': facts('synthetic dependency ' + label + arch)['sha256']} for arch in ARCHES}


class ResolvedSourceTests(unittest.TestCase):
    def test_previously_unrecorded_versions_resolve_without_repository_files(self):
        # The module has no repository-root parameter or per-version JSON lookup.
        for version, mode in [('v2.99.0', 'stable'), ('v2.100.7-beta.12', 'beta'), ('v2.100.8-dev.1', 'dev')]:
            with self.subTest(version=version):
                value = contract(version, mode)
                self.assertEqual(source_contract(canonical(value), digest(value), version, mode), value)

    def test_canonical_digest_ignores_presentation_but_not_content(self):
        value = contract()
        self.assertEqual(source_contract(json.dumps(value, indent=3).encode(), digest(value), 'v2.99.0', 'stable'), value)
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            source_contract(canonical(value), '0' * 64, 'v2.99.0', 'stable')
        value['source']['commit'] = 'c' * 40
        with self.assertRaises(ValueError):
            source_contract(canonical(value), digest(contract()), 'v2.99.0', 'stable')

    def test_required_identity_and_semantic_contract_reject_substitution(self):
        faults = [
            ('schema', lambda v: v.update(schema=2)),
            ('schema-bool', lambda v: v.update(schema=True)),
            ('version', lambda v: v.update(version='v2.98.0')),
            ('mode', lambda v: v.update(mode='beta')),
            ('edition', lambda v: v.update(edition='enterprise')),
            ('missing-arch', lambda v: v['architectures'].pop()),
            ('duplicate-arch', lambda v: v['architectures'].__setitem__(0, 'arm64')),
            ('repo', lambda v: v['source'].update(repository='untrusted/source')),
            ('commit', lambda v: v['source'].update(commit='main')),
            ('absent-present', lambda v: v['source']['absent'].append('frontend/package.json')),
            ('path', lambda v: v['source']['files'].update({'../escape': facts('x')})),
            ('size-bool', lambda v: v['source']['files']['frontend/package.json'].update(bytes=True)),
            ('installer-missing', lambda v: v['installer']['resources'].pop('1pctl')),
            ('toolchain', lambda v: v['toolchain'].update(node='latest')),
            ('geoip', lambda v: v['resources']['geoip'].update(url='http://github.com/file')),
            ('config', lambda v: v['configuration']['core'].update(source_sha256='d' * 64)),
            ('lock', lambda v: v['frontend_lock'].update(sha256='d' * 64)),
            ('manifest', lambda v: v['frontend_lock'].update(manifest_sha256='d' * 64)),
        ]
        for name, change in faults:
            with self.subTest(fault=name):
                value = contract(); change(value)
                with self.assertRaises(ValueError):
                    source_contract(canonical(value), digest(value), 'v2.99.0', 'stable')

    def test_duplicate_fields_nonfinite_and_channel_mismatch_rejected(self):
        for raw in [b'{"schema":1,"schema":1}', b'{"number":NaN}', b'[]', b'']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                source_contract(raw, '0' * 64, 'v2.99.0', 'stable')
        for version, mode in [('v2.99.0', 'beta'), ('v2.99.0-beta.1', 'stable'), ('v2.99.0-dev.1', 'beta'),
                              ('v3.0.0', 'stable'), ('latest', 'stable'), ('v2.01.0', 'stable')]:
            with self.subTest(version=version, mode=mode), self.assertRaises(ValueError):
                version_identity(version, mode)

    def test_derived_lock_requires_explicit_recipe_digest(self):
        value = contract(); value['frontend_lock'].update(kind='derived', sha256='c' * 64)
        with self.assertRaises(ValueError):
            source_contract(canonical(value), digest(value), 'v2.99.0', 'stable')
        value['frontend_lock']['recipe_sha256'] = facts('synthetic reviewed recipe')['sha256']
        # A generic repair may also correct a present, hash-bound incomplete lock.
        self.assertEqual(source_contract(canonical(value), digest(value), 'v2.99.0', 'stable'), value)
        value['source']['files'].pop('frontend/package-lock.json')
        with self.assertRaisesRegex(ValueError, 'explicit source-lock absence'):
            source_contract(canonical(value), digest(value), 'v2.99.0', 'stable')
        value['source']['absent'].append('frontend/package-lock.json')
        self.assertEqual(source_contract(canonical(value), digest(value), 'v2.99.0', 'stable'), value)
        value['frontend_lock']['manifest_sha256'] = facts('different dependency manifest')['sha256']
        with self.assertRaisesRegex(ValueError, 'manifest is not bound'):
            source_contract(canonical(value), digest(value), 'v2.99.0', 'stable')

    def test_shared_version_rules_apply_to_contract_and_configuration(self):
        from semantic_configuration import production
        raw = b'base:\n  mode: development\n  is_demo: true\nlog:\n  level: debug\n'
        for version, mode in [('v2.99.0', 'stable'), ('v2.99.0-beta.0', 'beta'), ('v2.99.0-dev.10', 'dev')]:
            with self.subTest(version=version):
                value = contract(version, mode)
                source_contract(canonical(value), digest(value), version, mode)
                self.assertIn(('mode: ' + mode).encode(), production(raw, version, 'agent', mode))
        for version, mode in [('v2.099.0', 'stable'), ('v2.99.00', 'stable'),
                              ('v2.99.0-beta.01', 'beta'), ('v2.99.0-dev.01', 'dev'),
                              ('v2.99.0-beta-10', 'beta'), ('v2.99.0-beta.alpha', 'beta'),
                              ('v2.99.0-beta.1.1', 'beta'), ('v2.99.0-beta.1', 'dev')]:
            for validate in (lambda: version_identity(version, mode),
                             lambda: production(raw, version, 'agent', mode)):
                with self.subTest(version=version, mode=mode), self.assertRaises(ValueError):
                    validate()

    def test_archive_backed_geoip_requires_exact_version_origin_and_member(self):
        for mode, version in [('stable', 'v2.99.0'), ('beta', 'v2.99.0-beta.2'), ('dev', 'v2.99.0-dev.2')]:
            value = contract(version, mode)
            value['resources']['geoip'].pop('url')
            value['resources']['geoip']['archive'] = {
                'url': vendor_base(version, mode, 'official') + f'1panel-{version}-linux-amd64.tar.gz',
                'member': f'1panel-{version}-linux-amd64/GeoIP.mmdb', **facts('synthetic official archive')}
            self.assertEqual(source_contract(canonical(value), digest(value), version, mode), value)
            for field, replacement in [('url', 'https://example.invalid/archive'),
                                       ('url', value['resources']['geoip']['archive']['url'].replace('amd64', 'arm64')),
                                       ('url', value['resources']['geoip']['archive']['url'].replace(version, 'v2.98.0')),
                                       ('member', f'1panel-{version}-linux-amd64/../GeoIP.mmdb'),
                                       ('member', f'1panel-{version}-linux-arm64/GeoIP.mmdb'),
                                       ('sha256', 'x' * 64), ('bytes', 512 * 1024 ** 2 + 1), ('bytes', True)]:
                broken = copy.deepcopy(value); broken['resources']['geoip']['archive'][field] = replacement
                with self.subTest(mode=mode, field=field, replacement=replacement), self.assertRaises(ValueError):
                    source_contract(canonical(broken), digest(broken), version, mode)
            for change in ({'url': 'https://github.com/extra'}, {'bytes': 64 * 1024 ** 2 + 1}):
                broken = copy.deepcopy(value); broken['resources']['geoip'].update(change)
                with self.subTest(change=change), self.assertRaises(ValueError):
                    source_contract(canonical(broken), digest(broken), version, mode)

    def test_installer_version_template_is_valid_but_arbitrary_text_is_not(self):
        value = contract(); value['installer']['original_version'] = 'version'
        self.assertEqual(source_contract(canonical(value), digest(value), 'v2.99.0', 'stable'), value)
        value['installer']['original_version'] = 'latest'
        with self.assertRaisesRegex(ValueError, 'original installer version'):
            source_contract(canonical(value), digest(value), 'v2.99.0', 'stable')


class VendorDiscoveryTests(unittest.TestCase):
    def test_complete_supported_inventory_is_derived_from_canonical_responses(self):
        for source, arches in [('official', ARCHES[:-2] + ('riscv64',)), ('enterprise', ('amd64', 'arm64'))]:
            with self.subTest(source=source):
                result = inventory(source=source, available=arches)
                self.assertEqual(set(result['archives']), set(arches))
                self.assertEqual(result['availability'], 'present')

    def test_enterprise_absence_requires_all_endpoints_404(self):
        absent = inventory(source='enterprise', available=())
        self.assertEqual(absent['availability'], 'absent')
        self.assertEqual(absent['archives'], {})
        self.assertTrue(all(row['status'] == 404 for row in absent['evidence']['archives'].values()))
        for status in [200, 401, 403, 429, 500, 503]:
            checksum, probes = responses('v2.99.0', 'stable', 'enterprise', ())
            probes['arm64']['status'] = status
            probes['arm64']['bytes'] = 200 if status == 200 else None
            with self.subTest(status=status), self.assertRaises(ValueError):
                vendor_inventory('v2.99.0', 'stable', 'enterprise', checksum, probes)
        with self.assertRaises(ValueError):
            inventory(source='official', available=())

    def test_incomplete_mismatched_and_unknown_vendor_manifests_fail_closed(self):
        for fault in ['omitted', 'duplicate', 'unknown', 'version', 'sha', 'not-found', 'unadvertised', 'partial-probe', 'redirect', 'no-arm64']:
            checksum, probes = responses('v2.99.0', 'stable', 'official', ('amd64', 'arm64'))
            lines = checksum['body'].splitlines(keepends=True)
            if fault == 'omitted': checksum['body'] = lines[0]
            elif fault == 'duplicate': checksum['body'] += lines[0]
            elif fault == 'unknown': checksum['body'] += lines[0].replace(b'amd64', b'new64')
            elif fault == 'version': checksum['body'] = checksum['body'].replace(b'v2.99.0', b'v2.98.0')
            elif fault == 'sha': checksum['body'] = b'x' + checksum['body'][1:]
            elif fault == 'not-found': probes['amd64'].update(status=404, bytes=None)
            elif fault == 'unadvertised': probes['ppc64le'].update(status=200, bytes=100)
            elif fault == 'partial-probe': probes.pop('loong64')
            elif fault == 'redirect': checksum['url'] = checksum['url'].replace('resource.fit2cloud.com', 'example.invalid')
            elif fault == 'no-arm64': checksum['body'] = lines[0]; probes['arm64'].update(status=404, bytes=None)
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                vendor_inventory('v2.99.0', 'stable', 'official', checksum, probes)

    def test_network_failures_do_not_shrink_matrix(self):
        for status in [301, 401, 403, 408, 429, 500, 503]:
            checksum, probes = responses('v2.99.0', 'stable', 'official', ('amd64', 'arm64'))
            checksum['status'] = status
            with self.subTest(status=status), self.assertRaises(ValueError):
                vendor_inventory('v2.99.0', 'stable', 'official', checksum, probes)


class FuturePlanTests(unittest.TestCase):
    def plan(self, enterprise=True, version='v2.99.0'):
        value = contract(version)
        return package_plan(version, 'stable', value, digest(value),
                            inventory(version=version, available=[a for a in ARCHES if a != 'loong64']),
                            inventory(version=version, source='enterprise', available=('amd64', 'arm64') if enterprise else ()),
                            dependencies('docker'), dependencies('compose'))

    def test_unrecorded_version_gets_independent_17_package_shards_12_native_4_upgrade_plan(self):
        plan, sha = self.plan()
        self.assertEqual(sum(len(a) for a in plan['matrix'].values()), 17)
        self.assertEqual(len(plan['rows']), 17)
        self.assertEqual(len(plan['native_rows']), 12)
        self.assertEqual(len(plan['upgrade_rows']), 4)
        self.assertEqual(sha, digest(plan))
        self.assertEqual(plan['matrix']['enterprise-original'], plan['matrix']['enterprise-docker'])
        self.assertFalse(any(r['source'] == 'enterprise-original' for r in plan['native_rows']))

    def test_explicit_absence_derives_13_packages_and_eight_native_cases(self):
        plan, _ = self.plan(enterprise=False)
        self.assertEqual(sum(len(a) for a in plan['matrix'].values()), 13)
        self.assertEqual(len(plan['rows']), 13)
        self.assertEqual(len(plan['native_rows']), 8)
        self.assertEqual(len(plan['upgrade_rows']), 4)
        self.assertEqual(set(plan['matrix']), {'official', 'custom'})

    def test_version_data_is_local_to_resolved_plan(self):
        first, sha = self.plan(version='v2.99.0')
        other, other_sha = self.plan(version='v2.99.1')
        self.assertNotEqual(sha, other_sha)
        self.assertEqual(digest(first), sha)
        self.assertEqual(len(first['rows']), len(other['rows']))

    def test_missing_or_wrong_dependency_pin_cannot_remove_a_custom_arch(self):
        for fault in ['missing', 'bad-sha', 'floating-url', 'empty-version']:
            docker = dependencies('docker')
            if fault == 'missing': docker.pop('s390x')
            elif fault == 'bad-sha': docker['s390x']['sha256'] = 'latest'
            elif fault == 'floating-url': docker['s390x']['url'] = 'http://example.invalid/file'
            else: docker['s390x']['version'] = ''
            value = contract()
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                package_plan('v2.99.0', 'stable', value, digest(value), inventory(),
                             inventory(source='enterprise'), docker, dependencies('compose'))

    def test_changed_resolved_pin_changes_plan_digest(self):
        plan, sha = self.plan()
        plan['official']['archives']['amd64']['sha256'] = 'c' * 64
        self.assertNotEqual(digest(plan), sha)

    def test_selected_inventory_cannot_disagree_with_saved_discovery(self):
        for fault in ['remove-arch', 'replace-hash', 'hide-enterprise', 'missing-evidence']:
            official = inventory()
            enterprise = inventory(source='enterprise')
            if fault == 'remove-arch': official['archives'].pop('arm64')
            elif fault == 'replace-hash': official['archives']['amd64']['sha256'] = 'c' * 64
            elif fault == 'hide-enterprise': enterprise.update(availability='absent', archives={})
            else: enterprise.pop('evidence')
            value = contract()
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                package_plan('v2.99.0', 'stable', value, digest(value), official, enterprise,
                             dependencies('docker'), dependencies('compose'))


if __name__ == '__main__':
    unittest.main()
