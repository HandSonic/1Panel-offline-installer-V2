"""Synthetic schema2 product isolation and nonpublishing vendor companions."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import native_candidate_input as candidate
import runtime_contract
from test_native_candidate_input import Fixture, encoded, zip_bytes


class RuntimeFixture(Fixture):
    def __init__(self, temp, source='custom', arch='amd64'):
        super().__init__(temp, source=source, arch=arch)
        self.refresh_dynamic()

    def refresh_dynamic(self):
        self.proof['plan_sha256'] = candidate.digest_bytes(encoded(self.plan))['sha256']
        self.env['ONEPANEL_RESOLVED_PLAN_SHA256'] = self.proof['plan_sha256']
        self.record['plan_sha256'] = self.proof['plan_sha256']
        self.refresh_controls(); self.refresh_shard()

    def fail_product(self, key):
        row = next(r for r in self.proof['outcomes'] if r['key'] == key)
        row.update(status='failure', reason='Synthetic package failed')
        name = f'1panel-{self.version}-{row["source"]}-offline-linux-{row["arch"]}.tar.gz'
        del self.proof['files'][name]
        self.checksums = ''.join(f'{p["sha256"]}  {n}\n' for n,p in self.proof['files'].items() if n != 'checksums.txt').encode()
        self.proof['files']['checksums.txt'] = candidate.digest_bytes(self.checksums)
        self.artifacts[:] = [a for a in self.artifacts if a['name'] != 'package-shard-1-' + key]
        self.refresh_dynamic()

    def execute(self):
        with patch.object(runtime_contract, 'selected', return_value=self.runtime), \
             patch.object(candidate, 'policy_fingerprint', return_value=self.proof['policy_fingerprint']):
            return super().execute()


class RuntimeCandidateTests(unittest.TestCase):
    def test_actual_canonical_plan_roundtrip_uses_stable_product_order(self):
        from resolved_inventory import canonical
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td))
            raw=canonical(f.plan);f.plan=json.loads(raw)
            path=Path(td)/'authenticated-plan.json';path.write_bytes(raw)
            sha=candidate.digest_bytes(raw)['sha256']
            f.env.update(ONEPANEL_RESOLVED_PLAN=str(path),ONEPANEL_RESOLVED_PLAN_SHA256=sha)
            f.proof['plan_sha256']=sha;f.record['plan_sha256']=sha
            f.refresh_controls(entries=[('publication-work/control/'+candidate.PROOF,encoded(f.proof)),
                ('publication-work/release/checksums.txt',f.checksums),('matrix-input/plan.json',raw)])
            f.refresh_shard()
            client=SimpleNamespace(repo=f.repo,run=f.client_run,download_zip=f.download_zip)
            with patch.dict(os.environ,f.env):
                result=candidate.materialize(f.args,f.env,client)
            self.assertTrue(Path(result['archive_path']).is_file())
            self.assertEqual(list(f.plan['resolved']['inventory']['matrix'])[0],'custom')
            self.assertEqual(f.proof['requested_products'][0]['source'],'official')

    def test_unrelated_failed_missing_shard_does_not_block_successful_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            f = RuntimeFixture(Path(td)); f.fail_product('official-arm64')
            result = f.execute()
            self.assertTrue(Path(result['archive_path']).is_file())

    def test_selected_failed_or_missing_outcome_is_rejected(self):
        for fault in ('failed', 'missing', 'duplicate'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                f = RuntimeFixture(Path(td))
                if fault == 'failed': f.fail_product('custom-amd64')
                if fault == 'missing': f.proof['outcomes'].pop()
                if fault == 'duplicate': f.proof['outcomes'].append(copy.deepcopy(f.proof['outcomes'][0]))
                f.refresh_dynamic()
                with self.assertRaises(ValueError): f.execute()

    def test_enterprise_companion_is_pinned_even_when_original_product_failed(self):
        with tempfile.TemporaryDirectory() as td:
            f = RuntimeFixture(Path(td), source='enterprise-docker'); f.fail_product('enterprise-original-amd64')
            result = f.execute(); proof = json.loads(Path(result['provenance_path']).read_text())
            self.assertEqual(len(proof['files']), 1)
            self.assertTrue(next(iter(proof['files'])).startswith('enterprise-docker/'))
            self.assertEqual(len(proof['companions']), 1)
            companion = Path(f.args.output) / 'enterprise-original' / Path(next(iter(proof['companions']))).name
            self.assertTrue(companion.is_file())

    def test_companion_cannot_use_self_claimed_replacement_bytes(self):
        for fault in ('record', 'bytes', 'omitted'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as td:
                f = RuntimeFixture(Path(td), source='enterprise-docker')
                name = next(iter(f.record['companions']))
                if fault == 'record': f.record['companions'][name]['sha256'] = 'a' * 64
                if fault == 'bytes': f.files[name] = b'x' * len(f.files[name])
                if fault == 'omitted': del f.record['companions']
                f.refresh_dynamic()
                with self.assertRaises((ValueError, KeyError)): f.execute()

    def test_selected_outcome_exact_producer_attempt_and_job_required(self):
        for field, value in [('producer_run_attempt', 2), ('job_id', 1999), ('job_url', 'https://example.test/fake')]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as td:
                f = RuntimeFixture(Path(td))
                next(r for r in f.proof['outcomes'] if r['key'] == 'custom-amd64')[field] = value
                f.refresh_dynamic()
                with self.assertRaises(ValueError): f.execute()

    def test_runtime_admits_authorized_build_events_but_never_pull_requests(self):
        for event in ('push', 'schedule', 'workflow_dispatch'):
            with self.subTest(event=event), tempfile.TemporaryDirectory() as td:
                f = RuntimeFixture(Path(td)); f.env.update(GITHUB_EVENT_NAME=event, PUBLICATION_OPERATION='build')
                f.run['event'] = event; f.execute()
        with tempfile.TemporaryDirectory() as td:
            f = RuntimeFixture(Path(td)); f.env.update(GITHUB_EVENT_NAME='pull_request', PUBLICATION_OPERATION='build')
            with self.assertRaises(ValueError): f.execute()
            self.assertEqual(f.calls, [])


if __name__ == '__main__': unittest.main()
