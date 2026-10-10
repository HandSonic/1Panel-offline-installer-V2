"""Synthetic automatic-path integration; never a claim of native or real CI success.

The upper GitHub/HTTP transports and lower package/native producer outputs are
invented. Production control authentication, resolution, plan consumers, native
admission, final artifact authentication, and publication routing remain active.
No application archive or installer executes, and no network request is made.
"""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import package_matrix
import resolved_transport
import runtime_publication
from publication_outcomes import filename
from resolved_inventory import ARCHES, canonical, digest
from runtime_contract import PLAN_PATH, PLAN_SHA, policy
from test_release_repair import FakeGitHub
from test_resolved_inventory import responses
from test_runtime_contract import runtime
from test_runtime_publication import Fixture, facts

VERSION = 'v2.101.0'
REPOSITORY = 'HandSonic/1Panel-offline-installer-V2'


class SyntheticUpstream:
    """Only external response bytes are replaced; consumer validators run normally."""

    repo = resolved_transport.UPSTREAM
    tag = VERSION

    def __init__(self):
        value = runtime(version=VERSION)
        self.contract = value['source_contract']
        self.config = value['configuration_sources']
        self.head = value['upstream']['producer_commit']
        self.run_id = 910001
        names = ['tests', 'prepare', 'build'] + [f'compile ({arch})' for arch in ARCHES]
        self.jobs = [dict(id=910100 + index, name=name, run_id=self.run_id,
                         run_attempt=1, head_sha=self.head, status='completed',
                         conclusion='success', html_url=f'https://github.com/{self.repo}/actions/runs/{self.run_id}/job/{910100 + index}')
                     for index, name in enumerate(names)]
        rows = list(value['upstream']['records'].values())
        self.bodies = {}
        for row in rows:
            raw = ('synthetic upstream archive ' + row['architecture']).encode()
            row.update(sha256=facts(raw)['sha256'], size=len(raw))
            self.bodies[row['file']] = raw
            self.bodies[row['file'] + '.sha256'] = (row['sha256'] + '  ' + row['file'] + '\n').encode()
        self.manifest = {
            'schema_version': 2, 'version': VERSION, 'producer_run_id': self.run_id,
            'producer_run_attempt': 1, 'producer_head_sha': self.head,
            'requested_architectures': list(ARCHES), 'artifacts': rows,
            'outcomes': [dict(architecture=arch, status='success', stage='compile', reason='',
                              job_id=job['id'], job_url=job['html_url'])
                         for arch, job in zip(ARCHES, self.jobs[3:])]}
        self.bodies.update({
            'resolved-source.json': canonical(self.contract),
            'build-manifest.json': canonical(self.manifest),
            'build-inputs.env': b'synthetic upper build input bytes\n',
            'checksums.txt': ''.join(row['sha256'] + '  ' + row['file'] + '\n' for row in rows).encode()})
        proof = dict(schema=2, contract='upstream-matrix', version=VERSION,
                     release_tag=VERSION, repository=self.repo, workflow_run_id=self.run_id,
                     workflow_run_attempt=1, workflow_commit=self.head,
                     files={name: facts(raw) for name, raw in self.bodies.items()},
                     requested_architectures=list(ARCHES), successful_architectures=list(ARCHES),
                     outcomes=self.manifest['outcomes'], producer_run_id=self.run_id,
                     producer_run_attempt=1, producer_head_sha=self.head)
        self.bodies['release-validation.json'] = canonical(proof)
        self.assets = [dict(id=920000 + index, name=name, size=len(raw),
                            digest='sha256:' + facts(raw)['sha256'])
                       for index, (name, raw) in enumerate(self.bodies.items())]
        self.state = dict(id=self.run_id, run_attempt=1, head_sha=self.head,
                          status='completed', conclusion='success', event='schedule',
                          path='.github/workflows/build.yml',
                          repository={'full_name': self.repo}, head_repository={'full_name': self.repo})
        self.log = 'VERIFIED_RELEASE_RECEIPT_SHA256=' + facts(self.bodies['release-validation.json'])['sha256'] + '\n'
        self.calls = []
        self.http = {}
        for part, text in self.config.items():
            url = ('https://raw.githubusercontent.com/1Panel-dev/1Panel/' +
                   self.contract['source']['commit'] + '/' + self.contract['configuration'][part]['path'])
            self.http[url] = {'status': 200, 'body': text.encode()}
        for source, available in [('official', [a for a in ARCHES if a != 'loong64']),
                                  ('enterprise', ['amd64', 'arm64'])]:
            checksum, observations = responses(VERSION, 'stable', source, available)
            self.http[checksum['url']] = checksum
            self.http.update({row['url']: row for row in observations.values()})

    def release(self):
        return copy.deepcopy(dict(id=930001, tag_name=VERSION, draft=False,
                                  prerelease=False, assets=self.assets))

    def asset_bytes(self, asset):
        # This replaces the gh download process, including its response body.
        return self.bodies[asset['name']]

    def run(self, *args):
        endpoint = args[-1]
        self.calls.append(endpoint)
        base = f'repos/{self.repo}/actions/runs/{self.run_id}/attempts/1'
        if endpoint == base:
            return json.dumps(self.state)
        if endpoint == base + '/jobs?per_page=100&page=1':
            return json.dumps({'total_count': len(self.jobs), 'jobs': self.jobs})
        if endpoint == f'repos/{self.repo}/actions/jobs/{self.jobs[2]["id"]}/logs':
            return self.log
        raise AssertionError('Unexpected upstream API request: ' + endpoint)

    def urlopen(self, request, timeout):
        url = request.full_url
        self.calls.append(url)
        row = self.http[url]  # Unknown URLs fail; there is no network fallback.
        if row['status'] == 404:
            raise HTTPError(url, 404, 'synthetic absent endpoint', {}, None)
        response = io.BytesIO(row.get('body', b''))
        response.status = 200
        response.geturl = lambda: url
        response.headers = {'Content-Length': str(row.get('bytes', len(row.get('body', b''))))}
        return response


class SyntheticPublication(FakeGitHub):
    """GitHub response/write boundary; real admission, ZIP checks and writer code run."""

    def __init__(self, fixture, state):
        super().__init__()
        self.repo = fixture.identity['repository']
        self.tag = fixture.identity['tag']
        self.transport, self.evidence = fixture.transport(final=True)
        self.evidence['run']['event'] = fixture.identity['event']
        self.present = state != 'absent'
        self.draft = state == 'draft'
        self.remote_tag = self.tag if self.present else None
        self.target = None
        self.body = 'old release notes' if self.present else None
        self.assets = {} if state == 'absent' else {1: ('checksums.txt', b'synthetic previous checksums')}
        self.writes = []

    def release(self):
        if not self.present:
            raise subprocess.CalledProcessError(1, 'synthetic gh', stderr='HTTP 404')
        return dict(super().release(), id=940001, tag_name=self.remote_tag, draft=self.draft,
                    target_commitish=self.target)

    def run(self, *args):
        if args[:2] == ('release', 'create'):
            self.writes.append(args)
            self.present = True
            self.remote_tag = args[2]
            self.draft = '--draft' in args
            self.target = args[args.index('--target') + 1] if '--target' in args else None
            self.body = args[args.index('--notes') + 1] if '--notes' in args else None
            return '{}'
        if 'draft=false' in args:
            self.writes.append(args)
            self.draft = False
            return '{}'
        return self.transport.run(*args)

    def download_zip(self, artifact, destination):
        return self.transport.download_zip(artifact, destination)

    def upload(self, path):
        self.writes.append(('upload', path.name))
        self.assets[max(self.assets, default=0) + 1] = (path.name, path.read_bytes())
        self.calls.append(('upload', path.name))

    def rename(self, identifier, name):
        self.writes.append(('rename', identifier, name))
        return super().rename(identifier, name)


class AutomaticSourceConsumptionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.environment = patch.dict(os.environ, {
            'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted',
            'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_EVENT_NAME': 'schedule',
            'PUBLICATION_OPERATION': 'build', 'GITHUB_SERVER_URL': 'https://github.com',
            'GITHUB_API_URL': 'https://api.github.com', 'GH_HOST': 'github.com',
            'GITHUB_RUN_ID': '900001', 'GITHUB_RUN_ATTEMPT': '2',
            'GITHUB_SHA': 'a' * 40, 'GITHUB_WORKFLOW_SHA': 'a' * 40,
            'GITHUB_WORKFLOW_REF': REPOSITORY + '/' + runtime_publication.WORKFLOW + '@refs/heads/master',
            'RUNNER_TEMP': str(self.directory), 'GITHUB_OUTPUT': str(self.directory / 'outputs'),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def resolve_plan(self, upper=None):
        upper = upper or SyntheticUpstream()
        args = SimpleNamespace(version=VERSION, tag=VERSION, repository=REPOSITORY,
                               mode='stable', work=str(self.directory / 'input'), upstream_source='release')
        with patch.object(resolved_transport, 'ControlGitHub', return_value=upper), \
             patch.object(resolved_transport, 'urlopen', side_effect=upper.urlopen):
            package_matrix.plan(args)
        _, plan = package_matrix.load_plan(args)
        return plan, args, upper

    def admitted_fixture(self, plan, event='schedule', missing_result=None):
        fixture = Fixture(self.directory, plan['resolved']['inventory']['matrix'])
        fixture.identity['event'] = event
        fixture.plan = plan
        fixture.proof.update(policy_fingerprint=policy(plan['resolved']), plan_sha256=digest(plan),
                             upstream_input=plan['upstream_input'])
        fixture.refresh_preparation()
        for result in fixture.results.values():
            result.update(plan_sha256=digest(plan), receipt_sha256=fixture.preparation['receipt_sha256'])
        if missing_result:
            del fixture.results[missing_result]
        # Synthetic package and native producer outputs enter at this boundary.
        # The actual per-product admission and final receipt checks run below.
        fixture.finalize()
        return fixture

    def publish(self, fixture, client):
        args = SimpleNamespace(version=VERSION, tag=VERSION, repository=REPOSITORY,
                               artifact_id='700003', receipt_sha256=fixture.result['receipt_sha256'],
                               native_acceptance_sha256=fixture.result['native_acceptance_sha256'],
                               journal=str(self.directory / 'publication-journal.json'))
        return runtime_publication.publish(args, client=client)

    def test_new_canonical_upper_release_reaches_real_resolver_plan_and_consumers(self):
        from embedded_configuration import expected_bytes
        from enterprise_contract import source as enterprise_source
        from official_source import source as official_source
        plan, _, upper = self.resolve_plan()
        value = plan['resolved']
        self.assertNotIn('custom_failure', value)
        self.assertEqual(plan['version'], VERSION)
        self.assertEqual(plan['upstream_input'], {'source_kind': 'verified-public-release'})
        self.assertEqual(value['source_contract'], upper.contract)
        self.assertEqual(value['upstream']['validation_sha256'], facts(upper.bodies['release-validation.json'])['sha256'])
        self.assertEqual(set(value['upstream']['records']), set(ARCHES))
        self.assertEqual((len(plan['rows']), len(plan['native_rows']), len(value['inventory']['upgrade_rows'])), (17, 12, 4))
        self.assertEqual(official_source(VERSION, 'amd64'), value['inventory']['official']['archives']['amd64'])
        self.assertEqual(enterprise_source(VERSION, 'arm64'), value['inventory']['enterprise']['archives']['arm64'])
        self.assertIn(VERSION.encode(), expected_bytes(VERSION, 'core')[1])
        identity, reread = runtime_publication.workflow_identity(VERSION, VERSION)
        self.assertEqual(reread, plan)
        self.assertEqual(identity['event'], 'schedule')
        self.assertEqual(os.environ[PLAN_SHA], facts(Path(os.environ[PLAN_PATH]).read_bytes())['sha256'])
        self.assertEqual({p.name for p in (self.directory / 'input').iterdir()}, {'plan.json'})
        for pattern in ('release-matrix-v*.json', 'official-sources-v*.json', 'enterprise-sources-v*.json'):
            self.assertEqual(list(ROOT.glob(pattern)), [])

    def test_upper_trust_failure_remains_explicit_custom_failure_with_vendor_inventory(self):
        for fault in ('control-bytes', 'receipt-marker', 'producer-job'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.directory) as temporary:
                previous = self.directory
                self.directory = Path(temporary)
                try:
                    upper = SyntheticUpstream()
                    if fault == 'control-bytes':
                        upper.bodies['resolved-source.json'] += b'\n'
                    elif fault == 'receipt-marker':
                        upper.log = 'VERIFIED_RELEASE_RECEIPT_SHA256=' + 'f' * 64 + '\n'
                    else:
                        upper.jobs[-1]['conclusion'] = 'failure'
                    plan, args, _ = self.resolve_plan(upper)
                    value = plan['resolved']
                    self.assertIsNone(value['source_contract'])
                    self.assertIn('Custom input authentication failed:', value['custom_failure'])
                    self.assertEqual(value['inventory']['matrix']['custom'], list(ARCHES))
                    self.assertEqual(len(value['inventory']['rows']), 17)
                    self.assertIn('amd64', value['inventory']['official']['archives'])
                    args.source, args.arch = 'custom', 'amd64'
                    with patch.object(package_matrix.subprocess, 'run') as execute, self.assertRaises(ValueError):
                        package_matrix.shard(args)
                    execute.assert_not_called()
                finally:
                    self.directory = previous

    def test_normal_build_routes_admitted_unregistered_version_to_new_draft_and_public_writer(self):
        for event, state in [('schedule', 'absent'), ('push', 'draft'), ('workflow_dispatch', 'public')]:
            with self.subTest(event=event, state=state), tempfile.TemporaryDirectory(dir=self.directory) as temporary:
                previous = self.directory
                self.directory = Path(temporary)
                try:
                    with patch.dict(os.environ, GITHUB_EVENT_NAME=event, RUNNER_TEMP=str(self.directory)):
                        plan, _, _ = self.resolve_plan()
                        fixture = self.admitted_fixture(plan, event)
                        client = SyntheticPublication(fixture, state)
                        result = self.publish(fixture, client)
                    self.assertEqual(result['phase'], 'complete')
                    self.assertFalse(client.draft)
                    proof = fixture.verify()
                    remote = {name: raw for name, raw in client.assets.values()}
                    self.assertTrue(set(proof['files']) | {'release-validation.json'} <= set(remote))
                    for name in set(proof['files']) | {'release-validation.json'}:
                        self.assertEqual(remote[name], (fixture.output / name).read_bytes())
                    creations = [call for call in client.writes if call[:2] == ('release', 'create')]
                    self.assertEqual(creations, [('release', 'create', VERSION, '--repo', REPOSITORY,
                        '--draft', '--target', fixture.identity['head_sha'], '--title', VERSION,
                        '--notes', '')] if state == 'absent' else [])
                    if state == 'absent':
                        self.assertEqual(client.target, fixture.identity['head_sha'])
                    self.assertEqual(sum('draft=false' in call for call in client.writes), int(state != 'public'))
                    self.assertEqual(client.body, '' if state == 'absent' else 'old release notes')
                    self.assertTrue(any('/actions/artifacts/700003' in call for call in client.evidence['calls']))
                finally:
                    self.directory = previous

    def test_normal_build_cannot_write_with_invalid_native_marker_or_changed_artifact(self):
        for fault in ('native-marker', 'artifact-bytes'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.directory) as temporary:
                previous = self.directory
                self.directory = Path(temporary)
                try:
                    with patch.dict(os.environ, RUNNER_TEMP=str(self.directory)):
                        plan, _, _ = self.resolve_plan()
                        fixture = self.admitted_fixture(plan)
                        client = SyntheticPublication(fixture, 'public')
                        before = copy.deepcopy(client.assets)
                        if fault == 'native-marker':
                            client.evidence['log'] = client.evidence['log'].replace('NATIVE_ACCEPTANCE_SHA256=', 'UNTRUSTED=')
                        else:
                            client.evidence['blobs'][700003] += b'tampered bytes'
                        with self.assertRaises(ValueError):
                            self.publish(fixture, client)
                    self.assertEqual(client.writes, [])
                    self.assertEqual(client.assets, before)
                    self.assertFalse((self.directory / 'publication-journal.json').exists())
                finally:
                    self.directory = previous

    def test_missing_native_upgrade_result_cannot_be_published_by_normal_writer(self):
        plan, _, _ = self.resolve_plan()
        fixture = self.admitted_fixture(plan, missing_result='upgrade:custom:amd64')
        client = SyntheticPublication(fixture, 'public')
        self.assertEqual(self.publish(fixture, client)['phase'], 'complete')
        failed_name = filename(VERSION, {'source': 'custom', 'arch': 'amd64'})
        remote = {name: raw for name, raw in client.assets.values()}
        self.assertNotIn(failed_name, remote)
        proof = json.loads(remote['release-validation.json'])
        failure = next(row for row in proof['outcomes'] if row['key'] == 'custom-amd64')
        self.assertEqual(failure['status'], 'failure')
        self.assertEqual(failure['stage'], 'native-acceptance')
        self.assertFalse(fixture.result['all_requested_passed'])
        self.assertEqual(len([name for name in remote if name.endswith('.tar.gz')]), 16)


if __name__ == '__main__':
    unittest.main()
