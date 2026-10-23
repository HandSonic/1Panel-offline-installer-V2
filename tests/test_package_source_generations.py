"""A release may append an architecture built from a later immutable source pin."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
from embedded_configuration import expected_bytes, validate_archive as validate_configuration_archive
from installer_source_fixtures import ORIGINAL
from package_retry import expected_installer, validate_inputs
from resolved_inventory import ARCHES, canonical, object_bytes
from runtime_contract import (PLAN_PATH, PLAN_SHA, custom_inputs, manifest_contract,
                              product_policy, validate)
from semantic_configuration import production
from test_package_public_source import Publisher
from test_runtime_contract import runtime as legacy_runtime
from test_validate_resolved_custom import Fixture
from validate_resolved_custom import archive_bytes, byte_facts
from validate_upstream_package import validate as validate_package


def generation(arch, *, later=False, different_configuration=False):
    fixture = Fixture(arch)
    if later:
        fixture.contract['installer']['commit'] = 'd' * 40
    installer = ORIGINAL + (b'\n# later installer resources\n' if different_configuration else b'')
    fixture.files['install.sh'] = installer
    fixture.contract['installer']['resources']['install.sh'] = byte_facts(installer)
    if different_configuration:
        fixture.contract['source']['commit'] = 'e' * 40
        for part, previous in list(fixture.config.items()):
            raw = previous.replace(b'extra_optional: keep', b'extra_optional: later-source')
            fixture.config[part] = raw
            profile = fixture.contract['configuration'][part]
            fixture.contract['source']['files'][profile['path']] = byte_facts(raw)
            normalized = production(raw, fixture.version, part, 'stable')
            profile.update(source_sha256=byte_facts(raw)['sha256'], source_bytes=len(raw),
                           normalized_sha256=byte_facts(normalized)['sha256'])
            fixture.files['1panel-' + part] = fixture.files['1panel-' + part][:64] + normalized
    return fixture


def publisher_with_append(*, different_configuration=False):
    publisher = Publisher()
    for arch in ARCHES:
        publisher.publish(generation(arch))
    later = generation('arm64', later=True, different_configuration=different_configuration)
    publisher.publish(later)
    return publisher, later


class PackageGenerationTests(unittest.TestCase):
    def bind(self, work, runtime):
        path = Path(work) / 'plan.json'
        raw = canonical({'resolved': runtime})
        path.write_bytes(raw)
        return {PLAN_PATH: str(path), PLAN_SHA: byte_facts(raw)['sha256']}

    def archive(self, work, publisher, arch):
        name = f'1panel-{publisher.tag}-linux-{arch}.tar.gz'
        path = Path(work) / name
        path.write_bytes(publisher.bodies[name])
        return path

    def test_later_documentation_commit_keeps_each_archives_own_contract(self):
        publisher, later = publisher_with_append()
        runtime = publisher.runtime()
        old, old_sha, _ = custom_inputs(runtime, 'amd64')
        current, current_sha, _ = custom_inputs(runtime, 'arm64')
        self.assertEqual(old['installer']['resources'], current['installer']['resources'])
        self.assertNotEqual(old['installer']['commit'], current['installer']['commit'])
        self.assertNotEqual(old_sha, current_sha)
        self.assertEqual(current, later.contract)
        with tempfile.TemporaryDirectory() as work, patch.dict(os.environ, self.bind(work, runtime)), \
                patch('installer_capabilities.inspect_installer'):
            for arch in ('amd64', 'arm64'):
                result = validate_package(self.archive(work, publisher, arch), publisher.tag, arch)
                self.assertEqual(result['contract_sha256'], custom_inputs(runtime, arch)[1])

    def test_source_configuration_manifest_recipe_and_reuse_select_the_same_architecture(self):
        publisher, later = publisher_with_append(different_configuration=True)
        runtime = publisher.runtime()
        contract, sha, sources = custom_inputs(runtime, 'arm64')
        self.assertEqual(contract, later.contract)
        self.assertNotEqual(contract['source']['commit'], runtime['source_contract']['source']['commit'])
        self.assertEqual(sources, {part: raw.decode() for part, raw in later.config.items()})
        with tempfile.TemporaryDirectory() as work, patch.dict(os.environ, self.bind(work, runtime)), \
                patch('installer_capabilities.inspect_installer'):
            path = self.archive(work, publisher, 'arm64')
            result = validate_package(path, publisher.tag, 'arm64')
            self.assertEqual(result['source_commit'], contract['source']['commit'])
            validate_configuration_archive(path, publisher.tag, 'arm64')
            original, normalized, commit = expected_bytes(publisher.tag, 'core', arch='arm64')
            self.assertEqual(original, later.config['core'])
            self.assertIn(b'later-source', normalized)
            self.assertEqual(commit, contract['source']['commit'])
            members = archive_bytes(path, byte_facts(path.read_bytes()), path.name[:-7])
            manifest = object_bytes(members['manifest.json'])
            manifest_contract(manifest, members['1pctl'], publisher.tag, 'arm64')
            with self.assertRaises(ValueError):
                manifest_contract(manifest, members['1pctl'], publisher.tag, 'amd64')
            from patch_installer import render
            with patch('resolved_transport.canonical_read', side_effect=lambda url, **kw: publisher.read(url)) as read:
                rendered = expected_installer(runtime, publisher.tag, 'custom', 'arm64', work, {})
            self.assertEqual(rendered, render(later.files['install.sh'].decode()).encode())
            self.assertIn('/' + contract['installer']['commit'] + '/install.sh', read.call_args.args[0])
            row = runtime['upstream']['records']['arm64']
            offline = {'inputs': {key: runtime['inventory'][key]['arm64'] for key in ('docker', 'compose')},
                       'upstream_provenance': manifest,
                       'payloads': {name: byte_facts((ROOT / path).read_bytes()) for name, path in
                                    (('docker.service', 'docker.service'), ('upgrade.sh', 'upgrade_offline.sh'))}}
            offline['inputs']['app'] = {'bytes': row['size'], 'sha256': row['sha256']}
            validate_inputs(offline, runtime, 'custom', 'arm64')
            offline['upstream_provenance'] = dict(manifest, resolved_contract_sha256=runtime['source_contract_sha256'])
            with self.assertRaisesRegex(ValueError, 'source contract changed'):
                validate_inputs(offline, runtime, 'custom', 'arm64')
        payloads = {'install.sh': byte_facts(b'generated installer')}
        policy = product_policy(runtime, 'custom', 'arm64', payloads)
        # Choosing a different shared compatibility anchor cannot stale arm64.
        alternate = copy.deepcopy(runtime)
        alternate.update(source_contract=contract, source_contract_sha256=sha, configuration_sources=sources)
        alternate['inventory']['source_contract_sha256'] = sha
        validate(alternate, publisher.tag)
        self.assertEqual(product_policy(alternate, 'custom', 'arm64', payloads), policy)
        altered = copy.deepcopy(runtime)
        altered['upstream']['source_inputs']['arm64']['configuration_sources']['core'] += '\n'
        self.assertNotEqual(product_policy(altered, 'custom', 'arm64', payloads), policy)

    def test_cross_architecture_contract_or_configuration_substitution_is_rejected(self):
        publisher, _ = publisher_with_append(different_configuration=True)
        runtime = publisher.runtime()
        for key in ('source_contract', 'configuration_sources'):
            with self.subTest(key=key):
                changed = copy.deepcopy(runtime)
                changed['upstream']['source_inputs']['arm64'][key] = copy.deepcopy(
                    changed['upstream']['source_inputs']['amd64'][key])
                with self.assertRaises(ValueError):
                    validate(changed, publisher.tag)

    def test_candidate_native_source_uses_its_architecture_contract_and_proof(self):
        from native_upgrade_input import source_archive_from_candidate, bind_candidate_source
        publisher, _ = publisher_with_append(different_configuration=True)
        runtime = publisher.runtime()
        plan = {'resolved': runtime, 'version': publisher.tag, 'mode': 'stable',
                'upstream_input': {'source_kind': 'verified-public-release'}}
        with patch('native_upgrade_input.ControlGitHub', return_value=publisher), \
                patch('native_upgrade_input.PredecessorGitHub', return_value=publisher), \
                patch('native_upgrade_input.array_pages', return_value=publisher.assets), \
                patch('native_upgrade_input.public_release', side_effect=lambda client, identifier: publisher.release()), \
                patch('native_upgrade_input.canonical_read', side_effect=publisher.read):
            for arch in ('amd64', 'arm64'):
                with self.subTest(arch=arch), tempfile.TemporaryDirectory() as work:
                    body, proof = source_archive_from_candidate(plan, runtime, 'custom', arch, work)
                    contract, sha, _ = custom_inputs(runtime, arch)
                    self.assertEqual(proof['contract_sha256'], sha)
                    self.assertEqual(object_bytes(body['manifest.json'])['source_commit'], contract['source']['commit'])
                    wrong = dict(proof, contract_sha256=custom_inputs(runtime, 'arm64' if arch == 'amd64' else 'amd64')[1])
                    with self.assertRaisesRegex(ValueError, 'differs from its authenticated candidate plan'):
                        bind_candidate_source(runtime, 'custom', arch, wrong)

    def test_wrong_embedded_configuration_still_fails_full_archive_validation(self):
        publisher, later = publisher_with_append(different_configuration=True)
        later.files['1panel-core'] = generation('arm64').files['1panel-core']
        publisher.publish(later)  # Manifest and API hashes honestly describe the bad bytes.
        runtime = publisher.runtime()
        with tempfile.TemporaryDirectory() as work, patch.dict(os.environ, self.bind(work, runtime)), \
                patch('installer_capabilities.inspect_installer'), \
                self.assertRaisesRegex(ValueError, 'wrong production configuration'):
            validate_package(self.archive(work, publisher, 'arm64'), publisher.tag, 'arm64')

    def test_unavailable_or_invalid_later_source_is_isolated_to_its_architecture(self):
        for fault in ('source-404', 'installer-404', 'normalized-pin'):
            with self.subTest(fault=fault):
                publisher, later = publisher_with_append(different_configuration=True)
                if fault == 'normalized-pin':
                    later.contract['configuration']['core']['normalized_sha256'] = 'f' * 64
                    publisher.publish(later)
                else:
                    commit = later.contract['source' if fault == 'source-404' else 'installer']['commit']
                    url = next(url for url in publisher.http if '/' + commit + '/' in url)
                    publisher.http[url] = {'status': 404, 'body': b''}
                runtime = publisher.runtime()
                self.assertEqual(set(runtime['upstream']['records']), set(ARCHES) - {'arm64'})
                self.assertEqual(set(runtime['upstream']['source_inputs']), set(ARCHES) - {'arm64'})
                self.assertEqual(runtime['inventory']['matrix']['custom'], list(ARCHES))

    def test_legacy_publications_still_require_the_single_shared_contract(self):
        runtime = legacy_runtime()
        validate(runtime, runtime['version'])
        for field, replacement in (('source_commit', 'd' * 40), ('installer_commit', 'e' * 40),
                                   ('resolved_contract_sha256', 'f' * 64)):
            with self.subTest(field=field):
                changed = copy.deepcopy(runtime)
                changed['upstream']['records']['arm64'][field] = replacement
                with self.assertRaisesRegex(ValueError, 'differs from authenticated source contract'):
                    validate(changed, changed['version'])


if __name__ == '__main__':
    unittest.main()
