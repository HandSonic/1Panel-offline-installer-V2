"""Malformed authenticated JSON stays inside the existing source failure boundary.

Only external transports use fixtures. These shapes are outside legitimate
producer output; their matching hashes must not turn them into valid inputs.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
from resolved_inventory import ARCHES, canonical
from resolved_transport import ci_controls, public_controls, resolve, verify_public_payload
from test_automatic_source_consumption import SyntheticUpstream, VERSION
from test_candidate_source_acquisition import SourceFixture
from test_package_public_source import Publisher
from test_validate_resolved_custom import Fixture
from test_upstream_lineage import LineagePublic
from test_resolved_inventory import responses


def rebind(upper, proof):
    """Keep fixture transport hashes valid so rejection reaches schema checks."""
    upper.bodies['build-manifest.json'] = canonical(upper.manifest)
    if isinstance(proof['files'], dict):
        raw = upper.bodies['build-manifest.json']
        proof['files']['build-manifest.json'] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    upper.bodies['release-validation.json'] = canonical(proof)
    for asset in upper.assets:
        raw = upper.bodies[asset['name']]
        asset.update(size=len(raw), digest='sha256:' + hashlib.sha256(raw).hexdigest())
    upper.log = 'VERIFIED_RELEASE_RECEIPT_SHA256=' + hashlib.sha256(upper.bodies['release-validation.json']).hexdigest() + '\n'


class MetadataFailureIsolationTests(unittest.TestCase):
    def assert_vendor_discovery(self, runtime, reads):
        self.assertTrue(any(row['source'] == 'official' for row in runtime['inventory']['rows']))
        self.assertTrue(any(row['source'] == 'enterprise-original' for row in runtime['inventory']['rows']))
        self.assertTrue(any('/package/v2/' in url for url in reads))
        self.assertTrue(any('/package/enterprise/' in url for url in reads))

    def test_bad_embedded_contract_isolated_to_its_architecture(self):
        for field in ('architectures', 'absent'):
            for bad in ({}, [], None):
                with self.subTest(field=field, value=bad):
                    publisher = Publisher()
                    damaged = Fixture('amd64')
                    if field == 'architectures':
                        damaged.contract[field][0] = bad
                    else:
                        damaged.contract['source'][field] = [bad]
                    publisher.publish(damaged)
                    runtime = publisher.runtime()
                    self.assertEqual(set(runtime['upstream']['records']), set(ARCHES) - {'amd64'})
                    self.assertEqual(set(runtime['inventory']['matrix']['custom']), set(ARCHES))
                    self.assertEqual(len(publisher.downloaded), len(ARCHES))
                    self.assert_vendor_discovery(runtime, publisher.reads)
                    self.assertEqual(verify_public_payload(runtime, 'arm64', publisher)['architecture'], 'arm64')
                    with self.assertRaises(ValueError):
                        verify_public_payload(runtime, 'amd64', publisher)

    def test_bad_legacy_controls_reject_custom_without_stopping_vendors(self):
        for fault in ('receipt-null', 'receipt-list', 'artifact-null', 'artifact-architecture', 'artifact-file'):
            with self.subTest(fault=fault):
                upper = SyntheticUpstream()
                proof = json.loads(upper.bodies['release-validation.json'])
                if fault == 'receipt-null': proof['files'] = None
                elif fault == 'receipt-list': proof['files'] = list(proof['files'])
                elif fault == 'artifact-null': upper.manifest['artifacts'][0] = None
                elif fault == 'artifact-architecture': upper.manifest['artifacts'][0]['architecture'] = {}
                else: upper.manifest['artifacts'][0]['file'] = {}
                rebind(upper, proof)
                with self.assertRaises(ValueError):
                    public_controls(VERSION, 'stable', upper)
                reads = []
                def read(url, method='GET'):
                    reads.append(url)
                    return {key: value for key, value in upper.http[url].items() if key != 'url'}
                runtime = resolve(VERSION, 'stable', ROOT, client=upper, read=read)
                self.assertIsNone(runtime['source_contract'])
                self.assertIn('custom_failure', runtime)
                self.assert_vendor_discovery(runtime, reads)

    def test_malformed_generation_artifact_is_rejected_before_authentication(self):
        upper = LineagePublic()
        generation = next(iter(upper.manifest['generations'].values()))
        generation['artifacts'][0] = None
        rebind(upper, json.loads(upper.bodies['release-validation.json']))
        with self.assertRaisesRegex(ValueError, 'accepted artifacts'):
            public_controls(VERSION, 'stable', upper)
        self.assertEqual(upper.calls, [])

    def test_legacy_ci_rows_reject_malformed_members_and_names(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = SourceFixture(Path(td))
            path = fixture.raws / 'build-manifest.json'
            original = path.read_bytes()
            self.assertEqual(ci_controls(fixture.raws, fixture.version, 'stable', fixture.provenance)[2]['source_kind'],
                             'verified-ci-artifact')
            for fault in ('null', 'architecture', 'file'):
                with self.subTest(fault=fault):
                    manifest = json.loads(original)
                    if fault == 'null': manifest['artifacts'][0] = None
                    else: manifest['artifacts'][0][fault] = {}
                    path.write_bytes(canonical(manifest))
                    with self.assertRaisesRegex(ValueError, 'architecture inventory'):
                        ci_controls(fixture.raws, fixture.version, 'stable', fixture.provenance)

    def test_malformed_workflow_responses_reject_without_type_errors(self):
        for fault in ('path', 'repository', 'head_repository', 'job'):
            with self.subTest(fault=fault):
                upper = SyntheticUpstream()
                if fault == 'job': upper.jobs[0] = None
                else: upper.state[fault] = None
                with self.assertRaises(ValueError):
                    public_controls(VERSION, 'stable', upper)

    def test_authenticated_public_row_semantics_fail_custom_only(self):
        for fault in ('source_commit', 'installer_commit', 'missing-field', 'extra-field'):
            with self.subTest(fault=fault):
                upper = SyntheticUpstream()
                row = upper.manifest['artifacts'][0]
                if fault == 'missing-field': del row['source_commit']
                elif fault == 'extra-field': row['unexpected'] = True
                else: row[fault] = 'f' * 40
                rebind(upper, json.loads(upper.bodies['release-validation.json']))
                # The original receipt/log/transport still authenticate. The
                # semantic mismatch must remain inside custom's failure boundary.
                public_controls(VERSION, 'stable', upper)
                reads = []
                def read(url, method='GET'):
                    reads.append(url)
                    return {key: value for key, value in upper.http[url].items() if key != 'url'}
                runtime = resolve(VERSION, 'stable', ROOT, client=upper, read=read)
                self.assertIsNone(runtime['source_contract'])
                self.assertIn('custom_failure', runtime)
                self.assertEqual(runtime['inventory']['matrix']['custom'], list(ARCHES))
                self.assert_vendor_discovery(runtime, reads)

    def test_authenticated_public_configuration_semantics_fail_custom_only(self):
        upper = SyntheticUpstream()
        upper.contract['configuration']['core']['normalized_sha256'] = 'f' * 64
        raw = canonical(upper.contract)
        sha = hashlib.sha256(raw).hexdigest()
        upper.bodies['resolved-source.json'] = raw
        for row in upper.manifest['artifacts']: row['resolved_contract_sha256'] = sha
        proof = json.loads(upper.bodies['release-validation.json'])
        proof['files']['resolved-source.json'] = {'bytes': len(raw), 'sha256': sha}
        rebind(upper, proof)
        public_controls(VERSION, 'stable', upper)
        reads = []
        def read(url, method='GET'):
            reads.append(url)
            return {key: value for key, value in upper.http[url].items() if key != 'url'}
        runtime = resolve(VERSION, 'stable', ROOT, client=upper, read=read)
        self.assertIsNone(runtime['source_contract'])
        self.assertIn('Runtime normalized configuration changed', runtime['custom_failure'])
        self.assert_vendor_discovery(runtime, reads)

    def test_authenticated_ci_row_semantics_fail_custom_only(self):
        for fault in ('source_commit', 'installer_commit', 'missing-field', 'extra-field'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                fixture = SourceFixture(Path(td))
                path = fixture.raws / 'build-manifest.json'
                manifest = json.loads(path.read_bytes())
                row = manifest['artifacts'][0]
                if fault == 'missing-field': del row['source_commit']
                elif fault == 'extra-field': row['unexpected'] = True
                else: row[fault] = 'f' * 40
                path.write_bytes(canonical(manifest))
                ci_controls(fixture.raws, fixture.version, 'stable', fixture.provenance)
                http = {}
                for source in ('official', 'enterprise'):
                    checksum, probes = responses(fixture.version, 'stable', source, ('amd64', 'arm64'))
                    http[checksum['url']] = checksum
                    http.update({probe['url']: probe for probe in probes.values()})
                reads = []
                def read(url, method='GET'):
                    reads.append(url)
                    if url in http: return {key: value for key, value in http[url].items() if key != 'url'}
                    return fixture.read(url)
                runtime = resolve(fixture.version, 'stable', ROOT, upstream_directory=fixture.raws,
                                  provenance=fixture.provenance, read=read)
                self.assertIsNone(runtime['source_contract'])
                self.assertIn('custom_failure', runtime)
                self.assert_vendor_discovery(runtime, reads)

    def test_shared_dependency_validation_still_fails_with_or_without_custom(self):
        for custom_error in (None, 'unavailable custom source'):
            with self.subTest(custom_error=custom_error), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                for name in ('docker', 'compose'):
                    data = json.loads((ROOT / (name + '-sources.json')).read_bytes())
                    if name == 'docker': data['amd64']['sha256'] = 'invalid'
                    (root / (name + '-sources.json')).write_bytes(canonical(data))
                upper = SyntheticUpstream()
                def read(url, method='GET'):
                    return {key: value for key, value in upper.http[url].items() if key != 'url'}
                with self.assertRaisesRegex(ValueError, 'Invalid SHA-256'):
                    resolve(VERSION, 'stable', root, client=upper, read=read, custom_error=custom_error)

    def test_shared_transport_and_json_errors_remain_fatal(self):
        for fault in ('vendor-observation', 'dependency-json'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                for name in ('docker', 'compose'):
                    raw = (ROOT / (name + '-sources.json')).read_bytes()
                    if fault == 'dependency-json' and name == 'docker': raw = b'not JSON'
                    (root / (name + '-sources.json')).write_bytes(raw)
                upper = SyntheticUpstream()
                def read(url, method='GET'):
                    result = {key: value for key, value in upper.http[url].items() if key != 'url'}
                    if fault == 'vendor-observation' and method == 'HEAD' and result['status'] == 200:
                        result['bytes'] = 0
                    return result
                with self.assertRaises(ValueError):
                    resolve(VERSION, 'stable', root, client=upper, read=read)

    def test_shared_final_validator_failure_cannot_be_hidden_by_custom_fallback(self):
        upper = SyntheticUpstream()
        def read(url, method='GET'):
            return {key: value for key, value in upper.http[url].items() if key != 'url'}
        # Deliberately fail the complete validator, regardless of custom state:
        # fallback may isolate custom evidence but cannot waive this gate.
        with patch('resolved_transport.validate', side_effect=ValueError('shared invariant failed')):
            with self.assertRaisesRegex(ValueError, 'shared invariant failed'):
                resolve(VERSION, 'stable', ROOT, client=upper, read=read)


if __name__ == '__main__': unittest.main()
