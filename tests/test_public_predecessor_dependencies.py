"""Synthetic historical lock controls and payloads; no downloads or execution."""
import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import native_upgrade_input as upgrade
import public_predecessor_dependencies as dependencies
import test_native_upgrade as upgrade_tests
from test_public_predecessor import fixture as public_fixture, rebind_controls
from resolved_inventory import canonical


def facts(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def pin(component, raw, arch='amd64', size=True):
    platform = {'amd64': 'x86_64', 'arm64': 'aarch64'}[arch]
    version = '99.1.0' if component == 'docker' else 'v99.1.0'
    url = (f'https://download.docker.com/linux/static/stable/{platform}/docker-{version}.tgz'
           if component == 'docker' else
           f'https://github.com/docker/compose/releases/download/{version}/docker-compose-linux-{platform}')
    result = dict(facts(raw), url=url, version=version)
    if not size:
        del result['bytes']
    return result


class HistoricalFixture:
    repo = dependencies.REPOSITORY

    def __init__(self, source='official', arch='amd64', modern=False, inventories=None, package=None):
        self.source, self.arch = source, arch
        self.args = (upgrade_tests.PublicBootstrapTests().modern_fixture()[0] if modern else public_fixture())
        self.run_state = self.args[5]
        self.receipt = json.loads(self.args[2])
        if package is not None:
            name, raw = package
            self.receipt['files'][name] = facts(raw)
            for asset in self.args[1]['assets']:
                if asset['name'] == name:
                    asset.update(size=len(raw), digest='sha256:' + facts(raw)['sha256'])
            checksums = ''.join(p['sha256'] + '  ' + n + '\n' for n, p in self.receipt['files'].items()
                                if n.endswith('.tar.gz')).encode()
            rebind_controls(self.args, self.receipt, checksums)
        self.job = dict(id=600007, name='publication_acceptance' if modern else 'publication_prepare',
            run_id=self.run_state['id'], head_sha=self.run_state['head_sha'],
            run_attempt=self.run_state['run_attempt'], status='completed', conclusion='success')
        self.log = 'VERIFIED_RELEASE_RECEIPT_SHA256=' + facts(self.args[2])['sha256'] + '\n'
        if modern:
            self.log += 'NATIVE_ACCEPTANCE_SHA256=' + self.receipt['files']['native-acceptance.json']['sha256'] + '\n'
        self.contents, self.requests = {}, []
        for component in ('docker', 'compose'):
            inventory = inventories[component] if inventories else {arch: pin(component, b'synthetic ' + component.encode(), arch)}
            self.set_lock(component, canonical(inventory))
        self.binding = upgrade.bind_public(*self.args[:4], self.run_state, source, arch)
        self.binding['receipt_validator'] = upgrade.verify_receipt_log(
            self, self.run_state, self.binding['receipt_sha256'], self.receipt)
        self.requests.clear()

    def set_lock(self, component, raw):
        name = component + '-sources.json'
        endpoint = f'repos/{self.repo}/contents/{name}?ref={self.receipt["workflow_commit"]}'
        blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        self.contents[component] = dict(type='file', name=name, path=name,
            url='https://api.github.com/' + endpoint, sha=blob, size=len(raw), encoding='base64',
            content=base64.encodebytes(raw).decode(),
            git_url=f'https://api.github.com/repos/{self.repo}/git/blobs/{blob}')

    def run(self, *args):
        endpoint = args[-1]
        self.requests.append(endpoint)
        if endpoint == f'repos/{self.repo}/actions/runs/{self.receipt["workflow_run_id"]}':
            return json.dumps(self.run_state)
        if endpoint.endswith('/jobs?filter=all&per_page=100&page=1'):
            return json.dumps({'total_count': 1, 'jobs': [self.job]})
        if endpoint == f'repos/{self.repo}/actions/jobs/{self.job["id"]}/logs':
            return self.log
        for value in self.contents.values():
            # Match the requested path independently of response URL tampering.
            if endpoint == f'repos/{self.repo}/contents/{value["name"]}?ref={self.receipt["workflow_commit"]}':
                return json.dumps(value)
        raise AssertionError('Unexpected network endpoint: ' + endpoint)

    def call(self):
        return dependencies.historical_dependency_pins(self, self.binding, self.args[2], self.source, self.arch)


class HistoricalDependenciesTests(unittest.TestCase):
    def test_original_commit_pins_and_byte_evidence_for_each_native_product(self):
        for source in ('official', 'custom'):
            for arch in ('amd64', 'arm64'):
                with self.subTest(source=source, arch=arch):
                    f = HistoricalFixture(source, arch)
                    pins, evidence = f.call()
                    self.assertEqual(pins['docker'][arch], pin('docker', b'synthetic docker', arch))
                    self.assertEqual(evidence['workflow_commit'], f.receipt['workflow_commit'])
                    self.assertEqual(evidence['receipt_validator'], f.binding['receipt_validator'])
                    self.assertEqual(evidence['archive'], f.binding['archive'])
                    contents = [r for r in f.requests if '/contents/' in r]
                    self.assertEqual(len(contents), 2)
                    self.assertTrue(all(r.endswith('?ref=' + f.receipt['workflow_commit']) for r in contents))
                    for component in ('docker', 'compose'):
                        raw = base64.b64decode(f.contents[component]['content'])
                        self.assertEqual({k: evidence['locks'][component][k] for k in ('bytes', 'sha256')}, facts(raw))

    def test_schema2_partial_success_retains_exact_acceptance_binding(self):
        f = HistoricalFixture(modern=True)
        self.assertEqual(f.run_state['conclusion'], 'failure')
        _, evidence = f.call()
        self.assertEqual(evidence['receipt_validator']['job_name'], 'publication_acceptance')

    def test_receipt_run_and_validator_tampering_fail_before_lock_reads(self):
        for modern in (False, True):
            for fault in ('receipt', 'head', 'run', 'attempt', 'cancelled', 'missing-validator',
                          'validator-log', 'job-failure', 'validator-job', 'archive', 'receipt-asset'):
                with self.subTest(modern=modern, fault=fault):
                    f = HistoricalFixture(modern=modern)
                    if fault == 'receipt': f.args[2] += b'\n'
                    if fault == 'head': f.run_state['head_sha'] = 'b' * 40
                    if fault == 'run': f.run_state['id'] += 1
                    if fault == 'attempt': f.run_state['run_attempt'] += 1
                    if fault == 'cancelled': f.run_state['conclusion'] = 'cancelled'
                    if fault == 'missing-validator': del f.binding['receipt_validator']
                    if fault == 'validator-log': f.log += 'modified authenticated log\n'
                    if fault == 'job-failure': f.job['conclusion'] = 'failure'
                    if fault == 'validator-job': f.binding['receipt_validator']['job_id'] += 1
                    if fault == 'archive': f.binding['archive']['sha256'] = 'b' * 64
                    if fault == 'receipt-asset': f.binding['assets']['release-validation.json']['bytes'] += 1
                    with self.assertRaises(ValueError): f.call()
                    self.assertFalse(any('/contents/' in r for r in f.requests))

    def test_contents_path_ref_origin_blob_encoding_and_size_fail_closed(self):
        changes = [('path', 'other.json'), ('url', 'https://api.github.com/repos/untrusted/repo/contents/docker-sources.json'),
                   ('url', f'https://api.github.com/repos/{dependencies.REPOSITORY}/contents/docker-sources.json?ref=main'),
                   ('type', 'symlink'), ('target', 'elsewhere'), ('submodule_git_url', 'elsewhere'),
                   ('sha', 'b' * 40), ('git_url', 'https://api.github.com/repos/untrusted/repo/git/blobs/' + 'a' * 40),
                   ('content', '%%%'), ('encoding', 'none'), ('size', True), ('size', 0),
                   ('size', dependencies.MAX_CONTROL + 1), ('size', 1),
                   ('content', 'A' * (2 * dependencies.MAX_CONTROL + 1)),
                   ('extra', 'A' * (3 * dependencies.MAX_CONTROL))]
        for field, value in changes:
            with self.subTest(field=field, value=str(value)[:120]):
                f = HistoricalFixture()
                f.contents['docker'][field] = value
                with self.assertRaises(ValueError): f.call()
                self.assertFalse(any('/contents/compose-' in r for r in f.requests))
        f = HistoricalFixture()
        f.set_lock('docker', b'{"amd64":{},"amd64":{}}')
        with self.assertRaisesRegex(ValueError, 'Duplicate'): f.call()

    def test_missing_original_file_never_tries_current_lock_or_other_ref(self):
        f = HistoricalFixture()
        f.contents['docker'] = {'name': 'docker-sources.json', 'message': 'Not Found'}
        with self.assertRaises(ValueError): f.call()
        self.assertEqual(len([r for r in f.requests if '/contents/' in r]), 1)

    def test_canonical_origin_version_architecture_and_lock_pins_are_required(self):
        for component in ('docker', 'compose'):
            good = pin(component, b'synthetic')
            changes = [('url', good['url'].replace('https://', 'http://')),
                       ('url', good['url'] + '?redirect=1'), ('url', good['url'].replace('x86_64', 'aarch64')),
                       ('url', good['url'].replace('docker.com', 'docker.com.example.test').replace('github.com', 'github.com.example.test')),
                       ('version', '99.2.0'), ('sha256', 'not a digest'), ('bytes', True), ('bytes', 0)]
            for field, value in changes:
                with self.subTest(component=component, field=field, value=value):
                    f = HistoricalFixture()
                    wrong = dict(good, **{field: value})
                    f.set_lock(component, canonical({'amd64': wrong}))
                    with self.assertRaises(ValueError): f.call()
        f = HistoricalFixture()
        f.set_lock('docker', b'{"arm64":{}}')
        with self.assertRaises(ValueError): f.call()

    def test_candidate_bindings_cannot_enter_ordinary_public_adapter(self):
        f = HistoricalFixture()
        f.binding['kind'] = 'current-run-candidate-predecessor-bootstrap'
        with self.assertRaises(ValueError): f.call()
        self.assertEqual(f.requests, [])

    def payload_fixture(self, directory, size, invalid_elf=False):
        archive, _, source, source_proof, body = upgrade_tests.IndependentArchiveTests().fixture(directory)
        manifest = json.loads(body['offline-manifest.json'])
        if invalid_elf:
            body['docker-compose'] = b'synthetic invalid ELF'
            manifest['payloads']['docker-compose'] = facts(body['docker-compose'])
        inventories = {}
        for component, payload in (('docker', 'docker.tgz'), ('compose', 'docker-compose')):
            original = pin(component, body[payload], size=size)
            inventories[component] = {'amd64': original}
            manifest['inputs'][component] = dict(original, **facts(body[payload]))
            current = dict(original, version='100.0.0', sha256='f' * 64)
            (directory / (component + '-sources.json')).write_bytes(canonical({'amd64': current}))
        body['offline-manifest.json'] = canonical(manifest)
        raw = upgrade_tests.tar_bytes(body, archive.name.removesuffix('.tar.gz'))
        archive.write_bytes(raw)
        f = HistoricalFixture(inventories=inventories, package=(archive.name, raw))
        return f, archive, source, source_proof, body

    def test_old_pins_accept_real_payload_after_current_locks_change_with_or_without_sizes(self):
        for size in (False, True):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as td:
                directory = Path(td)
                f, archive, source, source_proof, body = self.payload_fixture(directory, size)
                pins, proof = f.call()
                with self.assertRaisesRegex(ValueError, 'Unreviewed predecessor dependency'):
                    upgrade.unpack_predecessor(archive, directory / 'current-lock-result', f.binding,
                        f.source, f.arch, source, source_proof, directory)
                package, _ = upgrade.unpack_predecessor(archive, directory / 'historical-result', f.binding,
                    f.source, f.arch, source, source_proof, directory, dependency_pins=pins)
                self.assertEqual((package / 'docker.tgz').read_bytes(), body['docker.tgz'])
                self.assertEqual(proof['size_authority']['docker'],
                                 'original-lock' if size else 'receipt-bound-package-manifest')

    def test_size_less_lock_cannot_accept_self_consistent_payload_tampering(self):
        for fault in ('hash', 'size'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                directory = Path(td)
                f, archive, source, source_proof, body = self.payload_fixture(directory, False)
                pins, _ = f.call()
                manifest = json.loads(body['offline-manifest.json'])
                if fault == 'hash':
                    body['docker-compose'] += b'tampering'
                    manifest['payloads']['docker-compose'] = facts(body['docker-compose'])
                    manifest['inputs']['compose'].update(facts(body['docker-compose']))
                else:
                    manifest['inputs']['compose']['bytes'] += 1
                body['offline-manifest.json'] = canonical(manifest)
                raw = upgrade_tests.tar_bytes(body, archive.name.removesuffix('.tar.gz'))
                archive.write_bytes(raw)
                f.binding['archive'].update(facts(raw))
                with self.assertRaisesRegex(ValueError, 'Unreviewed predecessor dependency'):
                    upgrade.unpack_predecessor(archive, directory / 'out', f.binding,
                        f.source, f.arch, source, source_proof, directory, dependency_pins=pins)

    def test_authenticated_hashes_still_require_actual_elf_validation(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            f, archive, source, source_proof, _ = self.payload_fixture(directory, False, invalid_elf=True)
            pins, _ = f.call()
            with self.assertRaisesRegex(ValueError, 'Not an ELF'):
                upgrade.unpack_predecessor(archive, directory / 'out', f.binding,
                    f.source, f.arch, source, source_proof, directory, dependency_pins=pins)

    def test_present_original_lock_size_must_match_actual_payload(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            f, archive, source, source_proof, body = self.payload_fixture(directory, True)
            wrong = pin('compose', body['docker-compose'])
            wrong['bytes'] += 1
            f.set_lock('compose', canonical({'amd64': wrong}))
            pins, _ = f.call()
            with self.assertRaisesRegex(ValueError, 'dependency size differs'):
                upgrade.unpack_predecessor(archive, directory / 'out', f.binding,
                    f.source, f.arch, source, source_proof, directory, dependency_pins=pins)


if __name__ == '__main__':
    unittest.main()
