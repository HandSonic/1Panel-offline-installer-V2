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

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
from resolved_inventory import ARCHES, canonical
from resolved_transport import ci_controls, public_controls, resolve, verify_public_payload
from test_automatic_source_consumption import SyntheticUpstream, VERSION
from test_candidate_source_acquisition import SourceFixture
from test_package_public_source import Publisher
from test_validate_resolved_custom import Fixture
from test_upstream_lineage import LineagePublic


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


if __name__ == '__main__': unittest.main()
