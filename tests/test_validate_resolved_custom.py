"""Independent bytes/configuration/architecture checks for an unrecorded version."""
import copy
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from resolved_inventory import ARCHES, canonical, digest
from semantic_configuration import production
from validate_payload import ARCHES as ELF_ARCHES
from validate_resolved_custom import byte_facts, verify_archive
from test_resolved_inventory import contract


class Fixture:
    def __init__(self, arch='amd64'):
        self.version, self.arch = 'v2.99.0', arch
        self.contract = contract(self.version)
        self.producer = 'c' * 40
        self.config = {part: ('base:\n  mode: development\n' +
                              ('  version: development\n' if part == 'core' else '') +
                              '  is_demo: true\n  extra_optional: keep\nlog:\n  level: debug\n').encode()
                       for part in ('core', 'agent')}
        self.files = {name: ('synthetic original ' + name).encode()
                      for name in self.contract['installer']['resources']}
        self.files['1pctl'] = b'#!/bin/bash\nORIGINAL_VERSION=version\nPANEL_EDITION=cn\n'
        self.contract['installer']['original_version'] = 'version'
        self.contract['installer']['resources'] = {name: byte_facts(raw) for name, raw in self.files.items()}
        self.files['1pctl'] = self.files['1pctl'].replace(b'ORIGINAL_VERSION=version', b'ORIGINAL_VERSION=v2.99.0')
        self.files['GeoIP.mmdb'] = b'synthetic geoip'
        cls, endian, machine = ELF_ARCHES[arch]
        header = bytearray(64); header[:4] = b'\x7fELF'; header[4:6] = bytes([cls, endian])
        header[18:20] = struct.pack('<H' if endian == 1 else '>H', machine)
        for part, raw in self.config.items():
            path = f'{part}/config/config.yaml'
            self.contract['source']['files'][path] = byte_facts(raw)
            normalized = production(raw, self.version, part, 'stable')
            self.contract['configuration'][part] = {'path': path, 'source_sha256': byte_facts(raw)['sha256'],
                                                   'source_bytes': len(raw), 'normalized_sha256': byte_facts(normalized)['sha256']}
            self.files['1panel-' + part] = bytes(header) + normalized
            self.files['1panel-' + part + '.service'] = self.files['initscript/1panel-' + part + '.service']

    def root_layout(self):
        from test_service_layout import original_files
        resources = {name: raw for name, raw in self.files.items() if name.startswith('lang/')}
        resources.update(original_files())
        for name in self.contract['installer']['resources']:
            del self.files[name]
        self.contract['installer']['original_version'] = re.search(
            rb'(?m)^ORIGINAL_VERSION=([^\n]+)$', resources['1pctl'])[1].decode()
        self.contract['installer']['resources'] = {name: byte_facts(raw) for name, raw in resources.items()}
        self.files.update(resources)
        self.files['1pctl'] = re.sub(rb'(?m)^ORIGINAL_VERSION=[^\n]+$',
                                   b'ORIGINAL_VERSION=' + self.version.encode(), self.files['1pctl'])
        return self

    def archive(self, path, change_manifest=None, extra=None, modes=None):
        manifest = {'schema_version': 1, 'version': self.version, 'architecture': self.arch, 'edition': 'community',
                    'source_commit': self.contract['source']['commit'], 'installer_commit': self.contract['installer']['commit'],
                    'build_repository_commit': self.producer, 'mode': 'stable',
                    'resolved_contract_sha256': digest(self.contract),
                    **{k + '_version': value for k, value in self.contract['toolchain'].items()},
                    'files': {name: {'size': len(raw), 'sha256': byte_facts(raw)['sha256']} for name, raw in self.files.items()}}
        if change_manifest:
            change_manifest(manifest)
        root = f'1panel-{self.version}-linux-{self.arch}/'
        with tarfile.open(path, 'w:gz') as archive:
            for name, raw in {**self.files, 'manifest.json': canonical(manifest)}.items():
                item = tarfile.TarInfo(root + name); item.size = len(raw)
                item.mode = (modes or {}).get(name, 0o755)
                archive.addfile(item, io.BytesIO(raw))
            if extra:
                archive.addfile(extra, io.BytesIO(b'x') if extra.isfile() else None)
        return byte_facts(path.read_bytes())

    def geoip_archive(self, path, fault=None):
        root = f'1panel-{self.version}-linux-amd64/'
        with tarfile.open(path, 'w:gz') as archive:
            members = {'GeoIP.mmdb' if fault != 'missing-member' else 'different.mmdb':
                       b'wrong geoip' if fault == 'member-bytes' else self.files['GeoIP.mmdb'],
                       'unselected.bin': b'opaque other official payload'}
            for name, raw in members.items():
                item = tarfile.TarInfo(root + name); item.size = len(raw)
                archive.addfile(item, io.BytesIO(raw))
        if fault == 'truncated':
            path.write_bytes(path.read_bytes()[:-4])
        self.contract['resources']['geoip'].pop('url', None)
        self.contract['resources']['geoip']['archive'] = {
            'url': 'https://resource.fit2cloud.com/1panel/package/v2/stable/' + self.version +
                   '/release/' + root[:-1] + '.tar.gz', 'member': root + 'GeoIP.mmdb',
            **byte_facts(path.read_bytes())}

    def verify(self, path, pin, aggregate_record=None, geoip_origin=None):
        if aggregate_record is None:
            aggregate_record = {'architecture': self.arch, 'file': f'1panel-{self.version}-linux-{self.arch}.tar.gz',
                                'sha256': pin['sha256'], 'size': pin['bytes'],
                                'source_commit': self.contract['source']['commit'],
                                'installer_commit': self.contract['installer']['commit'],
                                'build_repository_commit': self.producer,
                                'resolved_contract_sha256': digest(self.contract)}
        return verify_archive(path, self.version, 'stable', self.arch, self.contract,
                              digest(self.contract), pin, self.producer, self.config, aggregate_record, geoip_origin)


class ResolvedArchiveTests(unittest.TestCase):
    def test_root_services_are_pinned_directly_without_generated_init_files(self):
        for arch in ('amd64', 'arm64'):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(arch).root_layout(); path = Path(td) / 'input.tar.gz'
                self.assertFalse(any(name.startswith('initscript/') for name in fixture.files))
                result = fixture.verify(path, fixture.archive(path))
                self.assertEqual(result['architecture'], arch)

    def test_root_resource_mutations_fail_even_with_complete_archive_manifest(self):
        for fault in ('wrong-service-bytes', 'missing-service', 'nested-alias', 'missing-copy', 'modern-interface'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = Fixture().root_layout(); path = Path(td) / 'input.tar.gz'
                if fault == 'wrong-service-bytes':
                    fixture.files['1panel-core.service'] += b'changed independently of the source pin'
                elif fault == 'missing-service':
                    del fixture.files['1panel-agent.service']
                elif fault == 'nested-alias':
                    fixture.files['initscript/1panel-core.service'] = fixture.files['1panel-core.service']
                else:
                    if fault == 'missing-copy':
                        fixture.files['install.sh'] = fixture.files['install.sh'].replace(
                            b'    cp ./1panel-core.service /etc/systemd/system\n', b'')
                    else:
                        sample = ROOT / 'tests/fixtures/historical-installers/3faa744fd158283470b48b3a971dc98cc390c7f291d4287b170416ae862dd28f.sh'
                        fixture.files['install.sh'] = sample.read_bytes()
                    # Bind the changed script to the contract, so rejection must
                    # come from its actual interface, without a callback mock.
                    fixture.contract['installer']['resources']['install.sh'] = byte_facts(fixture.files['install.sh'])
                with self.assertRaises(ValueError):
                    fixture.verify(path, fixture.archive(path))

    def test_producer_optional_documentation_still_requires_complete_manifest_hashes(self):
        for documents in ((), ('LICENSE',), ('README.md',), ('LICENSE', 'README.md')):
            with self.subTest(documents=documents), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(); path = Path(td) / 'input.tar.gz'
                fixture.files.update({name: ('synthetic source ' + name).encode() for name in documents})
                self.assertEqual(fixture.verify(path, fixture.archive(path))['version'], fixture.version)
        for fault in ('missing-entry', 'wrong-hash'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(); path = Path(td) / 'input.tar.gz'
                fixture.files['README.md'] = b'synthetic source documentation'
                def corrupt(manifest):
                    if fault == 'missing-entry':
                        del manifest['files']['README.md']
                    else:
                        manifest['files']['README.md']['sha256'] = 'f' * 64
                pin = fixture.archive(path, change_manifest=corrupt)
                with self.assertRaisesRegex(ValueError, 'manifest'):
                    fixture.verify(path, pin)

    def test_optional_documentation_does_not_allow_other_members_or_missing_payloads(self):
        for name in ('README.sh', 'docs/README.md', 'extra.bin'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(); path = Path(td) / 'input.tar.gz'
                fixture.files[name] = b'synthetic unexpected member'
                with self.assertRaisesRegex(ValueError, 'unexpected='):
                    fixture.verify(path, fixture.archive(path))
        with tempfile.TemporaryDirectory() as td:
            fixture = Fixture(); path = Path(td) / 'input.tar.gz'
            del fixture.files['1panel-core']
            fixture.files['LICENSE'] = b'synthetic source license'
            with self.assertRaisesRegex(ValueError, 'missing='):
                fixture.verify(path, fixture.archive(path))

    def test_archive_backed_geoip_authenticates_complete_stream_and_selected_member(self):
        for arch in ('amd64', 'arm64'):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(arch); path = Path(td) / 'input.tar.gz'; origin = Path(td) / 'official.tar.gz'
                fixture.geoip_archive(origin); pin = fixture.archive(path)
                extracted = []
                original = tarfile.TarFile.extractfile
                def extract(archive, member):
                    extracted.append(member.name)
                    return original(archive, member)
                with patch.object(tarfile.TarFile, 'extractfile', new=extract):
                    result = fixture.verify(path, pin, geoip_origin=origin)
                self.assertEqual(result['architecture'], arch)
                self.assertFalse(any(name.endswith('/unselected.bin') for name in extracted))

    def test_archive_backed_geoip_rejects_missing_origin_bad_member_pin_and_truncation(self):
        for fault in ('missing-origin', 'missing-member', 'member-bytes', 'size', 'sha', 'truncated'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(); path = Path(td) / 'input.tar.gz'; origin = Path(td) / 'official.tar.gz'
                fixture.geoip_archive(origin, fault)
                if fault == 'size': fixture.contract['resources']['geoip']['archive']['bytes'] += 1
                if fault == 'sha': fixture.contract['resources']['geoip']['archive']['sha256'] = 'f' * 64
                pin = fixture.archive(path)
                with self.assertRaises((ValueError, EOFError, gzip.BadGzipFile)):
                    fixture.verify(path, pin, geoip_origin=None if fault == 'missing-origin' else origin)

    def test_path_replacement_and_in_place_mutation_between_read_passes_fail(self):
        for phase in ('gzip', 'tar', 'in-place'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(); path = Path(td) / 'input.tar.gz'; pin = fixture.archive(path)
                replacement = Path(td) / 'replacement.tar.gz'
                fixture.files['1panel-core'] += b'different binary bytes'
                fixture.archive(replacement)
                original = tarfile.open if phase == 'tar' else gzip.GzipFile
                changed = False
                def replace_then_open(*args, **kwargs):
                    nonlocal changed
                    if not changed:
                        changed = True
                        if phase == 'in-place':
                            before = path.stat()
                            with path.open('r+b') as writer:
                                writer.seek(4); writer.write(b'abcd')  # Valid different gzip header.
                            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))
                        else:
                            os.replace(replacement, path)
                    return original(*args, **kwargs)
                target = 'validate_resolved_custom.tarfile.open' if phase == 'tar' else 'validate_resolved_custom.gzip.GzipFile'
                with patch(target, side_effect=replace_then_open), self.assertRaisesRegex(ValueError, 'changed during validation'):
                    fixture.verify(path, pin)
                self.assertTrue(changed)

    def test_symlink_path_is_never_followed(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = Fixture(); path = Path(td) / 'input.tar.gz'; pin = fixture.archive(path)
            link = Path(td) / 'link.tar.gz'; link.symlink_to(path.name)
            with self.assertRaisesRegex(ValueError, 'stable regular'):
                fixture.verify(link, pin)

    def test_fifo_input_is_rejected_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'input.fifo'; os.mkfifo(path)
            script = ('import sys; from validate_resolved_custom import archive_bytes; '
                      'archive_bytes(sys.argv[1], {"sha256":"0"*64,"bytes":1}, "root")')
            result = subprocess.run([sys.executable, '-B', '-c', script, str(path)],
                                    env={**os.environ, 'PYTHONPATH': str(ROOT / 'scripts'),
                                         'PYTHONDONTWRITEBYTECODE': '1'},
                                    capture_output=True, text=True, timeout=2)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('archive size/type mismatch', result.stderr)

    def test_real_gnu_and_pax_sparse_members_are_rejected_before_extraction(self):
        for kind in ('gnu', 'pax'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(); path = Path(td) / 'input.tar.gz'
                item = tarfile.TarInfo('1panel-v2.99.0-linux-amd64/sparse'); item.size = 1
                if kind == 'gnu': item.type = tarfile.GNUTYPE_SPARSE
                else: item.pax_headers = {'GNU.sparse.map': '0,1', 'GNU.sparse.size': '32768'}
                pin = fixture.archive(path, extra=item)
                original = tarfile.TarFile.extractfile
                extracted = []
                def extract(archive, member):
                    extracted.append(member.name)
                    return original(archive, member)
                with patch.object(tarfile.TarFile, 'extractfile', new=extract), self.assertRaisesRegex(ValueError, 'Sparse'):
                    fixture.verify(path, pin)
                self.assertFalse(any(name.endswith('/sparse') for name in extracted))

    def test_cumulative_logical_limit_is_checked_before_member_allocation(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = Fixture(); path = Path(td) / 'input.tar.gz'; pin = fixture.archive(path)
            original = tarfile.TarFile.extractfile
            extracted = []
            def extract(archive, member):
                extracted.append(member.name)
                return original(archive, member)
            with patch('validate_resolved_custom.LOGICAL_LIMIT', sum(map(len, fixture.files.values()))), \
                    patch.object(tarfile.TarFile, 'extractfile', new=extract), \
                    self.assertRaisesRegex(ValueError, 'cumulative logical'):
                fixture.verify(path, pin)
            self.assertFalse(any(name.endswith('/manifest.json') for name in extracted))

    def test_unrecorded_version_all_seven_architectures_and_optional_config_field(self):
        for arch in ARCHES:
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(arch); path = Path(td) / 'input.tar.gz'
                result = fixture.verify(path, fixture.archive(path))
                self.assertEqual(result['version'], 'v2.99.0')
                self.assertEqual(result['architecture'], arch)
                self.assertEqual(result['binaries'], {name: byte_facts(fixture.files[name])['sha256']
                                                     for name in ['1panel-core', '1panel-agent']})

    def test_self_consistent_changed_payload_cannot_bypass_pinned_source_contract(self):
        for fault in ['installer', 'geoip', 'wrong-elf', 'dev-config', 'wrong-service', 'extra-file', 'wrong-control-version']:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(); path = Path(td) / 'input.tar.gz'
                if fault == 'installer': fixture.files['install.sh'] += b' changed'
                elif fault == 'geoip': fixture.files['GeoIP.mmdb'] += b' changed'
                elif fault == 'wrong-elf': fixture.files['1panel-core'] = fixture.files['1panel-core'][:18] + b'\xb7\x00' + fixture.files['1panel-core'][20:]
                elif fault == 'dev-config': fixture.files['1panel-core'] = fixture.files['1panel-core'][:64] + fixture.config['core']
                elif fault == 'wrong-service': fixture.files['1panel-core.service'] += b' changed'
                elif fault == 'extra-file': fixture.files['unrequested.sh'] = b'surprise'
                else: fixture.files['1pctl'] = fixture.files['1pctl'].replace(b'v2.99.0', b'v2.98.0')
                pin = fixture.archive(path)  # Recompute both whole-archive and manifest SHA, deliberately.
                with self.assertRaises(ValueError): fixture.verify(path, pin)

    def test_exact_producer_and_toolchain_identity_required(self):
        for key, value in [('build_repository_commit', 'd' * 40), ('node_version', '22.14.0'),
                           ('npm_version', '10.0.0'), ('source_commit', 'd' * 40), ('schema_version', True),
                           ('resolved_contract_sha256', 'd' * 64)]:
            with self.subTest(field=key), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(); path = Path(td) / 'input.tar.gz'
                pin = fixture.archive(path, lambda m: m.update({key: value}))
                with self.assertRaisesRegex(ValueError, 'producer contract'): fixture.verify(path, pin)

    def test_outer_manifest_must_bind_exact_contract_archive_and_producer(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = Fixture(); path = Path(td) / 'input.tar.gz'; pin = fixture.archive(path)
            valid = {'architecture': fixture.arch, 'file': '1panel-v2.99.0-linux-amd64.tar.gz',
                     'sha256': pin['sha256'], 'size': pin['bytes'],
                     'source_commit': fixture.contract['source']['commit'],
                     'installer_commit': fixture.contract['installer']['commit'],
                     'build_repository_commit': fixture.producer,
                     'resolved_contract_sha256': digest(fixture.contract)}
            for field in valid:
                for mode in ['missing', 'substituted']:
                    broken = dict(valid)
                    if mode == 'missing': broken.pop(field)
                    else: broken[field] = 1 if field == 'size' else 'wrong'
                    with self.subTest(field=field, mode=mode), self.assertRaisesRegex(ValueError, 'Aggregate record'):
                        fixture.verify(path, pin, broken)

    def test_wrong_source_or_normalization_hash_is_rejected(self):
        for fault in ['source', 'normalized', 'missing-component']:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(); path = Path(td) / 'input.tar.gz'; pin = fixture.archive(path)
                if fault == 'source': fixture.config['agent'] += b'changed'
                elif fault == 'normalized': fixture.contract['configuration']['agent']['normalized_sha256'] = 'd' * 64
                else: fixture.config.pop('agent')
                with self.assertRaises(ValueError): fixture.verify(path, pin)

    def test_links_traversal_duplicate_members_and_corrupt_gzip_fail(self):
        for fault in ['link', 'traversal', 'duplicate', 'gzip']:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = Fixture(); path = Path(td) / 'input.tar.gz'
                item = tarfile.TarInfo('1panel-v2.99.0-linux-amd64/extra')
                item.size = 1
                if fault == 'link': item.type = tarfile.SYMTYPE; item.linkname = '/etc/passwd'
                elif fault == 'traversal': item.name = '1panel-v2.99.0-linux-amd64/../escape'
                elif fault == 'duplicate': item.name = '1panel-v2.99.0-linux-amd64/install.sh'
                pin = fixture.archive(path, extra=item if fault != 'gzip' else None)
                if fault == 'gzip':
                    path.write_bytes(path.read_bytes()[:-4]); pin = byte_facts(path.read_bytes())
                with self.assertRaises((ValueError, EOFError, gzip.BadGzipFile)):
                    fixture.verify(path, pin)


class PackageOnlyFixture(Fixture):
    def __init__(self, arch='amd64', lock_kind='source'):
        super().__init__(arch)
        from resolved_frontend_lock import GENERATOR, SIDECAR
        if lock_kind == 'derived':
            self.contract['frontend_lock'].update(kind='derived', recipe_sha256='e' * 64)
        elif lock_kind == 'resolved':
            manifest = {'name': 'synthetic-panel', 'version': '0.0.0',
                        'dependencies': {'synthetic': '1.0.0'}}
            manifest_raw = canonical(manifest)
            lock = {'name': manifest['name'], 'version': manifest['version'], 'lockfileVersion': 3,
                    'packages': {'': dict(manifest), 'node_modules/synthetic': {
                        'version': '1.0.0', 'resolved': 'https://registry.npmjs.org/synthetic/-/synthetic-1.0.0.tgz',
                        'integrity': 'sha512-' + 'A' * 86 + '=='}}}
            raw = canonical(lock)
            self.contract['source']['files']['frontend/package.json'] = byte_facts(manifest_raw)
            self.contract['source']['files'].pop('frontend/package-lock.json')
            self.contract['source']['absent'] = ['frontend/package-lock.json']
            self.contract['frontend_lock'] = {'kind': 'resolved', **byte_facts(raw),
                'manifest_sha256': byte_facts(manifest_raw)['sha256'], 'manifest': manifest_raw.decode(),
                'generator': dict(GENERATOR)}
            self.files['build-metadata/' + SIDECAR] = raw
        self.refresh_metadata()

    def refresh_metadata(self):
        self.files['build-metadata/resolved-source.json'] = canonical(self.contract)

    def archive(self, path, change_manifest=None, extra=None, modes=None):
        def decorate(manifest):
            manifest['publication_format'] = 'package-only-v1'
            if change_manifest:
                change_manifest(manifest)
        permissions = {name: 0o644 for name in self.files if name.startswith('build-metadata/')}
        permissions.update(modes or {})
        return super().archive(path, decorate, extra, permissions)

    def record(self, pin, producer_commit=None):
        return {'architecture': self.arch, 'file': f'1panel-{self.version}-linux-{self.arch}.tar.gz',
                'sha256': pin['sha256'], 'size': pin['bytes'],
                'source_commit': self.contract['source']['commit'],
                'installer_commit': self.contract['installer']['commit'],
                'build_repository_commit': producer_commit,
                'resolved_contract_sha256': digest(self.contract)}

    def verify(self, path, pin, producer_commit=None, aggregate_record=None, **kwargs):
        if aggregate_record is None:
            aggregate_record = self.record(pin, producer_commit)
        return verify_archive(path, self.version, 'stable', self.arch, self.contract,
                              digest(self.contract), pin, producer_commit, self.config, aggregate_record, **kwargs)


class PackageOnlyArchiveTests(unittest.TestCase):
    def test_all_architectures_lock_origins_and_optional_producer_pin(self):
        for arch in ARCHES:
            for kind in ('source', 'derived', 'resolved'):
                with self.subTest(arch=arch, lock_kind=kind), tempfile.TemporaryDirectory() as td:
                    fixture = PackageOnlyFixture(arch, kind)
                    fixture.producer = hashlib.sha1(arch.encode()).hexdigest()
                    path = Path(td) / 'input.tar.gz'; pin = fixture.archive(path)
                    for producer in (None, fixture.producer):
                        result = fixture.verify(path, pin, producer_commit=producer)
                        self.assertEqual(result['producer_commit'], fixture.producer)
                        self.assertEqual(result['contract_sha256'], digest(fixture.contract))
                        self.assertEqual(result['architecture'], arch)

    def test_embedded_metadata_accepts_root_services_and_read_only_modes(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = PackageOnlyFixture('arm64', 'resolved').root_layout()
            fixture.refresh_metadata()
            path = Path(td) / 'input.tar.gz'
            modes = {name: 0o444 for name in fixture.files if name.startswith('build-metadata/')}
            self.assertEqual(fixture.verify(path, fixture.archive(path, modes=modes))['architecture'], 'arm64')

    def test_unknown_or_removed_marker_never_downgrades(self):
        for marker in (None, False, '', 'package-only-v2', 'missing'):
            for pinned in (False, True):
                with self.subTest(marker=marker, pinned=pinned), tempfile.TemporaryDirectory() as td:
                    fixture = PackageOnlyFixture(); path = Path(td) / 'input.tar.gz'
                    def corrupt(manifest):
                        if marker == 'missing': manifest.pop('publication_format')
                        else: manifest['publication_format'] = marker
                    pin = fixture.archive(path, corrupt)
                    with self.assertRaises(ValueError):
                        fixture.verify(path, pin, producer_commit=fixture.producer if pinned else None)

    def test_legacy_format_requires_pinned_producer_even_with_explicit_null_record(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = PackageOnlyFixture(); path = Path(td) / 'input.tar.gz'
            del fixture.files['build-metadata/resolved-source.json']
            pin = fixture.archive(path, lambda manifest: manifest.pop('publication_format'))
            with self.assertRaisesRegex(ValueError, 'pinned producer'):
                fixture.verify(path, pin)
            self.assertEqual(fixture.verify(path, pin, producer_commit=fixture.producer)['producer_commit'], fixture.producer)

    def test_missing_extra_or_noncanonical_metadata_rejects_with_rebound_hashes(self):
        for fault in ('missing-contract', 'missing-lock', 'unexpected-lock', 'unexpected-metadata',
                      'noncanonical', 'different-contract', 'changed-lock'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = PackageOnlyFixture(lock_kind='source' if fault == 'unexpected-lock' else 'resolved')
                path = Path(td) / 'input.tar.gz'
                contract_name = 'build-metadata/resolved-source.json'
                lock_name = 'build-metadata/resolved-frontend-package-lock.json'
                if fault == 'missing-contract': del fixture.files[contract_name]
                elif fault == 'missing-lock': del fixture.files[lock_name]
                elif fault == 'unexpected-lock': fixture.files[lock_name] = b'{}'
                elif fault == 'unexpected-metadata': fixture.files['build-metadata/unknown.json'] = b'{}'
                elif fault == 'noncanonical': fixture.files[contract_name] += b'\n'
                elif fault == 'different-contract':
                    changed = copy.deepcopy(fixture.contract); changed['source']['commit'] = 'd' * 40
                    fixture.files[contract_name] = canonical(changed)
                else: fixture.files[lock_name] += b' '
                # Recompute manifest and archive hashes, so independent embedded
                # contract/lock verification must still reject corrupt bytes.
                with self.assertRaises(ValueError): fixture.verify(path, fixture.archive(path))

    def test_manifest_must_enumerate_and_hash_each_metadata_member_exactly(self):
        for name in ('build-metadata/resolved-source.json', 'build-metadata/resolved-frontend-package-lock.json'):
            for fault in ('missing', 'wrong-size', 'boolean-size', 'wrong-sha', 'extra-field'):
                with self.subTest(name=name, fault=fault), tempfile.TemporaryDirectory() as td:
                    fixture = PackageOnlyFixture(lock_kind='resolved'); path = Path(td) / 'input.tar.gz'
                    def corrupt(manifest):
                        row = manifest['files'][name]
                        if fault == 'missing': del manifest['files'][name]
                        elif fault == 'wrong-size': row['size'] += 1
                        elif fault == 'boolean-size': row['size'] = True
                        elif fault == 'wrong-sha': row['sha256'] = 'd' * 64
                        else: row['unknown'] = 'field'
                    with self.assertRaisesRegex(ValueError, 'manifest'):
                        fixture.verify(path, fixture.archive(path, corrupt))

    def test_metadata_cannot_be_executable_or_have_special_permission_bits(self):
        for name in ('build-metadata/resolved-source.json', 'build-metadata/resolved-frontend-package-lock.json'):
            for mode in (0o744, 0o654, 0o645, 0o4644, 0o2644, 0o1644):
                with self.subTest(name=name, mode=oct(mode)), tempfile.TemporaryDirectory() as td:
                    fixture = PackageOnlyFixture(lock_kind='resolved'); path = Path(td) / 'input.tar.gz'
                    with self.assertRaisesRegex(ValueError, 'metadata'):
                        fixture.verify(path, fixture.archive(path, modes={name: mode}))

    def test_metadata_links_special_entries_and_directories_reject(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE,
                     tarfile.CONTTYPE, tarfile.GNUTYPE_SPARSE, tarfile.DIRTYPE):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                fixture = PackageOnlyFixture(); path = Path(td) / 'input.tar.gz'
                name = 'build-metadata/resolved-source.json'; del fixture.files[name]
                item = tarfile.TarInfo(f'1panel-{fixture.version}-linux-{fixture.arch}/' + name)
                item.type = kind; item.size = 1; item.mode = 0o644; item.linkname = '../install.sh'
                with self.assertRaises(ValueError): fixture.verify(path, fixture.archive(path, extra=item))

    def test_frontend_lock_semantics_checked_even_when_contract_rebinds_bytes(self):
        for fault in ('schema', 'root', 'origin', 'integrity'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = PackageOnlyFixture(lock_kind='resolved'); path = Path(td) / 'input.tar.gz'
                name = 'build-metadata/resolved-frontend-package-lock.json'; lock = json.loads(fixture.files[name])
                if fault == 'schema': lock['lockfileVersion'] = 2
                elif fault == 'root': lock['packages']['']['dependencies'] = {}
                elif fault == 'origin': lock['packages']['node_modules/synthetic']['resolved'] = 'https://untrusted.invalid/a.tgz'
                else: del lock['packages']['node_modules/synthetic']['integrity']
                fixture.files[name] = canonical(lock)
                fixture.contract['frontend_lock'].update(byte_facts(fixture.files[name]))
                fixture.refresh_metadata()
                with self.assertRaises(ValueError): fixture.verify(path, fixture.archive(path))

    def test_unpinned_producer_is_exact_commit_and_null_record_is_explicit(self):
        for producer in (None, True, '', 'main', 'a' * 39, 'A' * 40, 'z' * 40):
            with self.subTest(producer=producer), tempfile.TemporaryDirectory() as td:
                fixture = PackageOnlyFixture(); path = Path(td) / 'input.tar.gz'
                pin = fixture.archive(path, lambda manifest: manifest.update(build_repository_commit=producer))
                with self.assertRaisesRegex(ValueError, 'Immutable commit'): fixture.verify(path, pin)
        with tempfile.TemporaryDirectory() as td:
            fixture = PackageOnlyFixture(); path = Path(td) / 'input.tar.gz'; pin = fixture.archive(path)
            for fault in ('missing', 'pinned', 'wrong'):
                record = fixture.record(pin)
                if fault == 'missing': del record['build_repository_commit']
                else: record['build_repository_commit'] = fixture.producer if fault == 'pinned' else 'd' * 40
                with self.subTest(fault=fault), self.assertRaisesRegex(ValueError, 'Aggregate record'):
                    fixture.verify(path, pin, aggregate_record=record)
            with self.assertRaisesRegex(ValueError, 'producer contract'):
                fixture.verify(path, pin, producer_commit='d' * 40)

    def test_package_only_keeps_independent_payload_and_archive_checks(self):
        for fault in ('installer', 'geoip', 'elf', 'configuration', 'archive-sha', 'archive-size'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = PackageOnlyFixture(); path = Path(td) / 'input.tar.gz'
                if fault == 'installer': fixture.files['install.sh'] += b' changed'
                elif fault == 'geoip': fixture.files['GeoIP.mmdb'] += b' changed'
                elif fault == 'elf': fixture.files['1panel-core'] = b'bad!' + fixture.files['1panel-core'][4:]
                elif fault == 'configuration': fixture.files['1panel-core'] = fixture.files['1panel-core'][:64] + fixture.config['core']
                pin = fixture.archive(path)
                if fault == 'archive-sha': pin['sha256'] = 'f' * 64
                elif fault == 'archive-size': pin['bytes'] += 1
                with self.assertRaises(ValueError): fixture.verify(path, pin)


if __name__ == '__main__':
    unittest.main()
