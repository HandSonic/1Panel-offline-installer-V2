"""Synthetic missing-lock contracts; no npm execution or dependency downloads."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from resolved_frontend_lock import (GENERATOR, MAX_LOCK, SIDECAR, read_payload,
                                    required_sidecars, validate_descriptor, validate_payload)
from resolved_inventory import canonical, digest, source_contract
from resolved_transport import public_controls, ci_controls
from test_automatic_source_consumption import SyntheticUpstream, VERSION


def resolved(contract):
    value = copy.deepcopy(contract)
    manifest = {'name': 'synthetic-panel', 'version': '0.0.0', 'dependencies': {'synthetic': '1.0.0'}}
    text = canonical(manifest).decode()
    lock = {'name': manifest['name'], 'version': manifest['version'], 'lockfileVersion': 3,
            'packages': {'': dict(manifest), 'node_modules/synthetic': {
                'version': '1.0.0', 'resolved': 'https://registry.npmjs.org/synthetic/-/synthetic-1.0.0.tgz',
                'integrity': 'sha512-' + 'A' * 86 + '=='}}}
    raw = canonical(lock)
    value['source']['files']['frontend/package.json'] = {'sha256': hashlib.sha256(text.encode()).hexdigest(), 'bytes': len(text.encode())}
    value['source']['files'].pop('frontend/package-lock.json', None)
    value['source']['absent'] = ['frontend/package-lock.json']
    value['frontend_lock'] = {'kind': 'resolved', 'sha256': hashlib.sha256(raw).hexdigest(),
        'bytes': len(raw), 'manifest_sha256': hashlib.sha256(text.encode()).hexdigest(),
        'manifest': text, 'generator': dict(GENERATOR)}
    return value, raw


class ResolvedPublic(SyntheticUpstream):
    def __init__(self):
        super().__init__()
        self.contract, payload = resolved(self.contract)
        self.bodies['resolved-source.json'] = canonical(self.contract)
        self.bodies[SIDECAR] = payload
        for row in self.manifest['artifacts']: row['resolved_contract_sha256'] = digest(self.contract)
        self.bodies['build-manifest.json'] = canonical(self.manifest)
        self.rebind()

    def rebind(self):
        proof = json.loads(self.bodies['release-validation.json'])
        proof['files'] = {name: {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                          for name, raw in self.bodies.items() if name != 'release-validation.json'}
        self.bodies['release-validation.json'] = canonical(proof)
        self.assets = [dict(id=920000 + index, name=name, size=len(raw),
                           digest='sha256:' + hashlib.sha256(raw).hexdigest())
                       for index, (name, raw) in enumerate(self.bodies.items())]
        self.log = 'VERIFIED_RELEASE_RECEIPT_SHA256=' + hashlib.sha256(self.bodies['release-validation.json']).hexdigest() + '\n'

    def asset_bytes(self, asset, limit=None):
        if asset['name'] == SIDECAR:
            assert limit == MAX_LOCK
        return self.bodies[asset['name']]


class ResolvedLockTests(unittest.TestCase):
    def setUp(self):
        self.contract, self.raw = resolved(SyntheticUpstream().contract)

    def test_unregistered_descriptor_and_payload_need_no_repository_record(self):
        self.assertEqual(source_contract(canonical(self.contract), digest(self.contract), VERSION, 'stable'), self.contract)
        self.assertEqual(required_sidecars(self.contract), (SIDECAR,))
        self.assertEqual(validate_payload(self.raw, self.contract), self.raw)
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / SIDECAR).write_bytes(self.raw)
            self.assertEqual(read_payload(td, self.contract), self.raw)
        contract, sha, upstream = public_controls(VERSION, 'stable', ResolvedPublic())
        self.assertEqual(sha, digest(contract))
        self.assertEqual(upstream['source_kind'], 'verified-public-release')

    def test_manifest_generator_absence_and_size_are_exact(self):
        changes = [lambda c: c['frontend_lock']['generator'].update(lifecycle_scripts=True),
                   lambda c: c['frontend_lock']['generator'].update(node='99.0.0'),
                   lambda c: c['frontend_lock']['generator'].update(registry='https://untrusted.invalid'),
                   lambda c: c['frontend_lock'].update(bytes=MAX_LOCK + 1),
                   lambda c: c['frontend_lock'].update(bytes=True),
                   lambda c: c['frontend_lock'].update(manifest='{}'),
                   lambda c: c['source'].update(absent=[]),
                   lambda c: c['source']['files'].update({'frontend/package-lock.json': {'sha256': 'a' * 64, 'bytes': 12}})]
        for index, change in enumerate(changes):
            contract = copy.deepcopy(self.contract); change(contract)
            with self.subTest(index=index), self.assertRaises(ValueError):
                validate_descriptor(contract['frontend_lock'], contract['source'])

    def test_changed_payload_unknown_schema_root_or_unpinned_origin_reject(self):
        with self.assertRaises(ValueError): validate_payload(self.raw + b' ', self.contract)
        changes = [lambda x: x.update(lockfileVersion=2), lambda x: x['packages'][''].update(version='other'),
                   lambda x: x['packages'][''].update(dependencies={}),
                   lambda x: x['packages']['node_modules/synthetic'].update(resolved='https://untrusted.invalid/file.tgz'),
                   lambda x: x['packages']['node_modules/synthetic'].pop('integrity'),
                   lambda x: x['packages']['node_modules/synthetic'].update(link=True),
                   lambda x: x['packages']['node_modules/synthetic'].update(inBundle=True)]
        for index, change in enumerate(changes):
            lock = json.loads(self.raw); change(lock); raw = canonical(lock)
            contract = copy.deepcopy(self.contract)
            contract['frontend_lock'].update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
            with self.subTest(index=index), self.assertRaises(ValueError): validate_payload(raw, contract)

    def test_public_sidecar_cannot_be_omitted_or_substituted(self):
        for kind in ('missing', 'receipt-rebound-substitute', 'digest-substitute'):
            client = ResolvedPublic()
            if kind == 'missing':
                del client.bodies[SIDECAR]; client.rebind()
            else:
                client.bodies[SIDECAR] += b' '
                if kind == 'receipt-rebound-substitute': client.rebind()
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                public_controls(VERSION, 'stable', client)

    def test_ci_bundle_replays_sidecar_and_never_accepts_missing_lock(self):
        client = ResolvedPublic()
        provenance = {'run_id': client.run_id, 'build_repository_commit': client.head,
                      'artifact_sha256': 'd' * 64}
        with tempfile.TemporaryDirectory() as td, patch('resolved_transport.ControlGitHub', return_value=client):
            directory = Path(td)
            for name, raw in client.bodies.items():
                if name != 'release-validation.json': (directory / name).write_bytes(raw)
            contract, sha, upstream = ci_controls(directory, VERSION, 'stable', provenance)
            self.assertEqual(sha, digest(contract))
            self.assertEqual(upstream['source_kind'], 'verified-ci-artifact')
            (directory / SIDECAR).unlink()
            with self.assertRaises(FileNotFoundError): ci_controls(directory, VERSION, 'stable', provenance)

    def test_bundled_child_inherits_only_declared_pinned_parent(self):
        lock = json.loads(self.raw)
        parent = lock['packages']['node_modules/synthetic']
        parent['bundleDependencies'] = ['synthetic-child']
        lock['packages']['node_modules/synthetic/node_modules/synthetic-child'] = {'version': '1.0.0', 'inBundle': True}
        for valid in (True, False):
            if not valid: parent['bundleDependencies'] = []
            raw = canonical(lock); contract = copy.deepcopy(self.contract)
            contract['frontend_lock'].update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
            if valid: self.assertEqual(validate_payload(raw, contract), raw)
            else:
                with self.assertRaises(ValueError): validate_payload(raw, contract)

    def test_links_nonregular_missing_and_unexpected_files_reject(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td); payload = directory / SIDECAR
            with self.assertRaises(FileNotFoundError): read_payload(directory, self.contract)
            target = directory / 'target'; target.write_bytes(self.raw); payload.symlink_to(target)
            with self.assertRaises(ValueError): read_payload(directory, self.contract)
            payload.unlink(); payload.mkdir()
            with self.assertRaises(ValueError): read_payload(directory, self.contract)
            payload.rmdir(); payload.write_bytes(self.raw)
            with self.assertRaises(ValueError): read_payload(directory, SyntheticUpstream().contract)


if __name__ == '__main__':
    unittest.main()
