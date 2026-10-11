import copy
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import native_candidate_input as candidate


def encoded(value):
    return (json.dumps(value, sort_keys=True) + '\n').encode()


def zip_bytes(entries):
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries:
                archive.writestr(name, content)
    return output.getvalue()


class Fixture:
    def __init__(self, root, source='custom', arch='amd64', attempt=1, shard_attempt=None, prepare_attempt=None, version='v2.3.1'):
        self.root = root
        self.repo = candidate.REPOS['downstream17']
        self.version = version
        self.attempt = attempt
        self.prepare_attempt = attempt if prepare_attempt is None else prepare_attempt
        self.shard_attempt = self.prepare_attempt if shard_attempt is None else shard_attempt
        self.env = {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted',
                    'GITHUB_REPOSITORY': self.repo, 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                    'PUBLICATION_OPERATION': 'validate-repair',
                    'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_API_URL': 'https://api.github.com',
                    'GITHUB_SHA': 'a' * 40, 'GITHUB_WORKFLOW_SHA': 'a' * 40,
                    'GITHUB_WORKFLOW_REF': self.repo + '/' + candidate.WORKFLOW + '@refs/heads/master',
                    'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': str(attempt),
                    'RUNNER_TEMP': str(root), 'GITHUB_OUTPUT': str(root / 'outputs')}
        self.args = SimpleNamespace(version=self.version, tag=self.version, source=source, arch=arch,
                                    controls_artifact_id='91', receipt_sha256='', output=str(root / 'input'),
                                    provenance=str(root / 'input-provenance.json'))
        from test_runtime_contract import runtime
        self.runtime=runtime(version=version,enterprise=version!='v2.2.4')
        with patch('runtime_contract.selected',return_value=self.runtime):
            self.identity = candidate.current_identity(self.version, self.version, source, arch, self.env)
        self.run = {'id': 123, 'head_sha': 'a' * 40, 'run_attempt': attempt,
                    'repository': {'id': 8, 'full_name': self.repo},
                    'head_repository': {'id': 8, 'full_name': self.repo},
                    'path': candidate.WORKFLOW, 'event': 'workflow_dispatch',
                    'status': 'in_progress', 'conclusion': None}
        self.plan = {'version': self.version, 'tag': self.version, 'repository': self.repo,
                     'workflow_run_id': '123', 'workflow_commit': 'a' * 40, 'mode': 'stable',
                     'rows': self.identity['rows'], 'native_rows': self.identity['native_rows'], 'upstream_input': {'source_kind': 'verified-public-release'}, 'resolved':self.runtime}
        self.payloads = {name: ('fixture package ' + name).encode() for name in
                         {f'1panel-{self.version}-{s}-offline-linux-{a}.tar.gz' for s,arches in self.runtime['inventory']['matrix'].items() for a in arches}}
        self.checksums = ''.join(candidate.digest_bytes(data)['sha256'] + '  ' + name + '\n'
                                 for name, data in sorted(self.payloads.items())).encode()
        with patch('runtime_contract.selected',return_value=self.runtime):
            self.expected_policy=candidate.policy_fingerprint('downstream17',self.version)
        self.proof = {'schema': 2, 'contract': 'downstream-matrix', 'version': self.version,
                      'release_tag': self.version, 'repository': self.repo, 'workflow_run_id': 123,
                      'workflow_commit': 'a' * 40,
                      'policy_fingerprint': self.expected_policy,
                      'workflow_run_attempt':self.prepare_attempt,
                      'plan_sha256':candidate.digest_bytes(encoded(self.plan))['sha256'],
                      'requested_products':self.identity['rows'],
                      'outcomes':[dict(r,status='success',stage='package',reason='',producer_run_attempt=self.shard_attempt,job_id=1002 if r==self.identity['row'] else 2000+n,job_url=f'https://github.com/{self.repo}/actions/runs/123/job/{1002 if r==self.identity["row"] else 2000+n}') for n,r in enumerate(self.identity['rows'])],
                      'upstream_input': self.plan['upstream_input'],
                      'files': {name: candidate.digest_bytes(data) for name, data in self.payloads.items()}}
        self.proof['files']['checksums.txt'] = candidate.digest_bytes(self.checksums)
        self.env['ONEPANEL_RESOLVED_PLAN_SHA256']=self.proof['plan_sha256']
        sources = [source] + (['enterprise-original'] if source == 'enterprise-docker' else [])
        self.files = {f'{s}/1panel-{self.version}-{s}-offline-linux-{arch}.tar.gz':
                      self.payloads[f'1panel-{self.version}-{s}-offline-linux-{arch}.tar.gz'] for s in sources}
        self.record = {'identity': {k: self.plan[k] for k in
                                   ('version', 'tag', 'repository', 'workflow_run_id', 'workflow_commit')},
                       'row': self.identity['row'], 'plan_sha256': candidate.digest_bytes(encoded(self.plan))['sha256'],
                       'files': {name: candidate.digest_bytes(data) for name, data in self.files.items()}}
        self.record['companions']={}
        if source=='enterprise-docker':
            original=f'enterprise-original/1panel-{version}-enterprise-original-offline-linux-{arch}.tar.gz'
            body=self.files.pop(original);self.record['files'].pop(original)
            companion='companions/'+Path(original).name
            self.files[companion]=body;self.record['companions'][companion]=candidate.digest_bytes(body)
            self.runtime['inventory']['enterprise']['archives'][arch].update(candidate.digest_bytes(body))
        self.proof['plan_sha256']=candidate.digest_bytes(encoded(self.plan))['sha256']
        self.record['plan_sha256']=self.proof['plan_sha256'];self.env['ONEPANEL_RESOLVED_PLAN_SHA256']=self.proof['plan_sha256']
        self.artifacts = [self.metadata(10 + n, f'package-shard-{self.shard_attempt}-{row["key"]}', b'other shard')
                          for n, row in enumerate(self.identity['rows'])]
        self.shard = next(a for a in self.artifacts if a['name'].endswith('-' + self.identity['row']['key']))
        self.controls = self.metadata(91, f'publication-controls-123-{self.prepare_attempt}', b'controls')
        self.artifacts.append(self.controls)
        self.aggregate = self.metadata(92, f'verified-publication-123-{attempt}', b'full aggregate must not download')
        self.artifacts.append(self.aggregate)
        self.jobs = {}
        self.prepare = self.job(1001, 'publication_prepare', self.prepare_attempt)
        self.producer = self.job(1002, f'publication_packages ({source}, {arch})', self.shard_attempt)
        self.jobs.setdefault(self.prepare_attempt, []).append(self.prepare)
        self.jobs.setdefault(self.shard_attempt, []).append(self.producer)
        self.blobs, self.logs, self.calls, self.downloads = {}, {}, [], []
        self.refresh_controls()
        self.refresh_shard()

    def metadata(self, number, name, data):
        facts = candidate.digest_bytes(data)
        return {'id': number, 'name': name, 'digest': 'sha256:' + facts['sha256'],
                'size_in_bytes': facts['bytes'], 'expired': False,
                'workflow_run': {'id': 123, 'head_sha': 'a' * 40, 'repository_id': 8, 'head_repository_id': 8}}

    def job(self, number, name, attempt):
        return {'id': number, 'name': name, 'run_id': 123, 'run_attempt': attempt,
                'head_sha': 'a' * 40, 'status': 'completed', 'conclusion': 'success'}

    def upload_log(self, artifact):
        return ('2026-10-09T01:57:08.9770409Z SHA256 digest of uploaded artifact zip is ' + artifact['digest'][7:] + '\n'
                '2026-10-09T01:57:08.9771727Z Finalizing artifact upload\n'
                '2026-10-09T01:57:09.1854184Z Artifact ' + artifact['name'] + '.zip successfully finalized. Artifact ID ' + str(artifact['id']) + '\n')

    def refresh_controls(self, entries=None):
        receipt = encoded(self.proof)
        self.args.receipt_sha256 = candidate.digest_bytes(receipt)['sha256']
        data = zip_bytes(entries if entries is not None else [
            ('publication-work/control/' + candidate.PROOF, receipt),
            ('publication-work/release/checksums.txt', self.checksums),
            ('matrix-input/plan.json', encoded(self.plan))])
        self.controls.update(self.metadata(91, self.controls['name'], data))
        self.blobs[91] = data
        self.logs[1001] = ('2026-10-09T01:56:09.1854184Z VERIFIED_RELEASE_RECEIPT_SHA256=' + self.args.receipt_sha256 + '\n' +
                           self.upload_log(self.controls) + self.upload_log(self.aggregate))

    def refresh_shard(self, entries=None):
        data = zip_bytes(entries if entries is not None else list(self.files.items()) + [('shard.json', encoded(self.record))])
        self.shard.update(self.metadata(self.shard['id'], self.shard['name'], data))
        self.blobs[self.shard['id']] = data
        self.logs[1002] = self.upload_log(self.shard)

    def client_run(self, *args):
        self.calls.append(args)
        endpoint = args[-1]
        base = f'repos/{self.repo}/actions/'
        if endpoint == base + 'runs/123':
            return json.dumps(self.run)
        if endpoint == base + 'artifacts/91':
            return json.dumps(self.controls)
        if endpoint.startswith(base + 'runs/123/artifacts?'):
            return json.dumps({'total_count': len(self.artifacts), 'artifacts': self.artifacts})
        if endpoint.startswith(base + 'runs/123/jobs?filter=all'):
            jobs = [job for values in self.jobs.values() for job in values]
            return json.dumps({'total_count': len(jobs), 'jobs': jobs})
        for attempt, jobs in self.jobs.items():
            if endpoint.startswith(base + f'runs/123/attempts/{attempt}/jobs?'):
                return json.dumps({'total_count': len(jobs), 'jobs': jobs})
        for number, log in self.logs.items():
            if endpoint == base + f'jobs/{number}/logs':
                return log
        raise AssertionError('Unexpected API read: ' + endpoint)

    def download_zip(self, artifact, path):
        self.downloads.append(artifact['id'])
        path.write_bytes(self.blobs[artifact['id']])

    def execute(self):
        client = SimpleNamespace(repo=self.repo, run=self.client_run, download_zip=self.download_zip)
        with patch('runtime_contract.selected',return_value=self.runtime),patch.object(candidate,'policy_fingerprint',return_value=self.expected_policy):
            return candidate.materialize(self.args,self.env,client)


class NativeCandidateInputTests(unittest.TestCase):
    def assert_rejected(self, fixture):
        with self.assertRaises((ValueError, zipfile.BadZipFile)):
            fixture.execute()
        self.assertFalse(Path(fixture.args.output).exists())
        self.assertFalse(Path(fixture.args.provenance).exists())
        self.assertFalse(Path(fixture.env['GITHUB_OUTPUT']).exists())

    def test_exact_current_run_candidate_downloads_only_controls_and_one_shard(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            summary = fixture.execute()
            self.assertEqual(fixture.downloads, [91, fixture.shard['id']])
            self.assertEqual(set(str(p.relative_to(fixture.args.output)) for p in Path(fixture.args.output).rglob('*') if p.is_file()), set(fixture.files))
            self.assertEqual(candidate.digest(summary['archive_path'])['sha256'], summary['archive_sha256'])
            provenance = json.loads(Path(summary['provenance_path']).read_text())
            self.assertEqual(provenance['workflow_run_attempt'], 1)
            self.assertEqual(provenance['receipt_sha256'], fixture.args.receipt_sha256)
            self.assertEqual(provenance['controls']['artifact_id'], 91)
            self.assertEqual(provenance['shard']['zip_sha256'], fixture.shard['digest'][7:])
            self.assertIn('archive_sha256=' + summary['archive_sha256'], Path(fixture.env['GITHUB_OUTPUT']).read_text())
            self.assertEqual(sum(args[-1].endswith('/runs/123') for args in fixture.calls), 2)

    def test_enterprise_pair_and_all_native_sources_arches(self):
        for source in ('official', 'custom', 'enterprise-docker'):
            for arch in ('amd64', 'arm64'):
                with self.subTest(source=source, arch=arch), tempfile.TemporaryDirectory() as temp:
                    fixture = Fixture(Path(temp), source, arch)
                    fixture.execute()
                    self.assertEqual(len(list(Path(fixture.args.output).rglob('*.tar.gz'))), 2 if source == 'enterprise-docker' else 1)

    def test_thirteen_package_candidate_requires_complete_eight_native_plan(self):
        for source in ('official', 'custom'):
            for arch in ('amd64', 'arm64'):
                with self.subTest(source=source, arch=arch), tempfile.TemporaryDirectory() as temp:
                    fixture = Fixture(Path(temp), source, arch, version='v2.2.4')
                    self.assertEqual(len(fixture.plan['rows']), 13)
                    self.assertEqual(len(fixture.plan['native_rows']), 8)
                    fixture.execute()
                    self.assertEqual(len(fixture.downloads), 2)
                    self.assertEqual(len(list(Path(fixture.args.output).rglob('*.tar.gz'))), 1)

    def test_unavailable_enterprise_candidate_cannot_be_requested(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, 'native candidate row'):
                Fixture(Path(temp), 'enterprise-docker', 'amd64', version='v2.2.4')
            self.assertFalse((Path(temp) / 'input').exists())

    def test_candidate_rejects_missing_duplicate_and_unsupported_native_rows(self):
        for fault in ('missing', 'duplicate', 'unsupported', 'empty'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp), version='v2.2.4')
                if fault == 'missing': fixture.plan['native_rows'].pop()
                elif fault == 'duplicate': fixture.plan['native_rows'].append(fixture.plan['native_rows'][0])
                elif fault == 'unsupported': fixture.plan['native_rows'][0]['source'] = 'enterprise-docker'
                else: fixture.plan['native_rows'] = []
                fixture.refresh_controls()
                self.assert_rejected(fixture)

    def test_partial_rerun_reuses_successful_prior_producer(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp), attempt=2, shard_attempt=1)
            fixture.execute()
            provenance = json.loads(Path(fixture.args.provenance).read_text())
            self.assertEqual(provenance['controls']['producer_attempt'], 2)
            self.assertEqual(provenance['shard']['producer_attempt'], 1)

    def test_partial_native_rerun_reuses_exact_successful_prior_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp), attempt=2, prepare_attempt=1)
            newer_shard = copy.deepcopy(fixture.shard)
            newer_shard.update(id=900, name='package-shard-2-custom-amd64')
            fixture.artifacts.append(newer_shard)
            fixture.execute()
            provenance = json.loads(Path(fixture.args.provenance).read_text())
            self.assertEqual(provenance['workflow_run_attempt'], 2)
            self.assertEqual(provenance['controls']['producer_attempt'], 1)
            self.assertEqual(provenance['shard']['producer_attempt'], 1)
            self.assertNotIn(900, fixture.downloads)

    def test_superseded_prior_controls_or_successful_preparation_fail_closed(self):
        for fault in ('controls', 'prepare', 'future-controls'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp), attempt=2, prepare_attempt=1)
                if fault in ('controls', 'future-controls'):
                    newer = copy.deepcopy(fixture.controls)
                    newer.update(id=900, name='publication-controls-123-' + ('3' if fault == 'future-controls' else '2'))
                    fixture.artifacts.append(newer)
                else:
                    fixture.jobs[2] = [fixture.job(1003, 'publication_prepare', 2)]
                self.assert_rejected(fixture)
                self.assertEqual(fixture.downloads, [])

    def test_latest_available_shard_attempt_is_selected(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp), attempt=2)
            old = copy.deepcopy(fixture.shard)
            old.update(id=900, name='package-shard-1-custom-amd64', expired=True)
            fixture.artifacts.insert(0, old)
            fixture.execute()
            self.assertNotIn(900, fixture.downloads)

    def test_context_guards_fail_before_api_or_download(self):
        faults = {'GITHUB_ACTIONS': 'false', 'RUNNER_ENVIRONMENT': 'self-hosted',
                  'GITHUB_REPOSITORY': 'fork/repo', 'GITHUB_RUN_ID': '../../other',
                  'GITHUB_RUN_ATTEMPT': '0', 'GITHUB_SHA': 'bad', 'GITHUB_WORKFLOW_SHA': 'b' * 40,
                  'GITHUB_WORKFLOW_REF': candidate.REPOS['downstream17'] + '/.github/workflows/other.yml@refs/heads/master',
                  'GITHUB_EVENT_NAME': 'pull_request', 'PUBLICATION_OPERATION': 'unrecognized',
                  'GITHUB_SERVER_URL': 'https://example.test', 'GITHUB_API_URL': 'https://example.test',
                  'GH_HOST': 'example.test', 'RUNNER_TEMP': '/missing'}
        for key, value in faults.items():
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                fixture.env[key] = value
                self.assert_rejected(fixture)
                self.assertEqual(fixture.calls, [])
                self.assertEqual(fixture.downloads, [])

    def test_wrong_current_run_head_attempt_path_repository_event_or_conclusion(self):
        for key, value in [('id', 124), ('head_sha', 'b' * 40), ('run_attempt', 2),
                           ('path', '.github/workflows/other.yml'), ('event', 'pull_request'),
                           ('repository', {'id': 8, 'full_name': 'fork/repo'}),
                           ('head_repository', {'id': 9, 'full_name': 'fork/repo'}),
                           ('status', 'cancelled'), ('status', 'completed')]:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                fixture.run[key] = value
                self.assert_rejected(fixture)
                self.assertEqual(fixture.downloads, [])

    def test_artifact_identity_expiry_digest_size_and_run_are_checked(self):
        for which in ('controls', 'shard'):
            for fault in ('expired', 'name', 'digest', 'size', 'run', 'head', 'repository'):
                with self.subTest(which=which, fault=fault), tempfile.TemporaryDirectory() as temp:
                    fixture = Fixture(Path(temp))
                    artifact = getattr(fixture, which)
                    if fault == 'expired': artifact['expired'] = True
                    if fault == 'name': artifact['name'] += '-wrong'
                    if fault == 'digest': artifact['digest'] = 'sha1:' + 'b' * 40
                    if fault == 'size': artifact['size_in_bytes'] = candidate.SHARD_LIMIT + 1
                    if fault == 'run': artifact['workflow_run']['id'] = 124
                    if fault == 'head': artifact['workflow_run']['head_sha'] = 'b' * 40
                    if fault == 'repository': artifact['workflow_run']['repository_id'] = 9
                    self.assert_rejected(fixture)
                    self.assertEqual(fixture.downloads, [])

    def test_missing_ambiguous_future_and_unexpected_artifact_rows(self):
        for fault in ('missing', 'duplicate-name', 'duplicate-id', 'future', 'unknown-row', 'malformed'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                if fault == 'missing': fixture.artifacts.remove(fixture.shard)
                else:
                    extra = copy.deepcopy(fixture.shard)
                    extra['id'] = 900
                    if fault == 'duplicate-id': extra.update(id=fixture.shard['id'], name='unrelated')
                    if fault == 'future': extra['name'] = 'package-shard-2-custom-amd64'
                    if fault == 'unknown-row': extra['name'] = 'package-shard-1-other-amd64'
                    if fault == 'malformed': extra['name'] = 'package-shard-01-custom-amd64'
                    fixture.artifacts.append(extra)
                self.assert_rejected(fixture)

    def test_successful_job_requires_exact_attempt_run_head_name_and_status(self):
        for which in ('prepare', 'producer'):
            for key, value in [('run_attempt', 2), ('run_id', 124), ('head_sha', 'b' * 40),
                               ('status', 'in_progress'), ('conclusion', 'failure'), ('name', 'unrelated')]:
                with self.subTest(which=which, key=key), tempfile.TemporaryDirectory() as temp:
                    fixture = Fixture(Path(temp))
                    getattr(fixture, which)[key] = value
                    self.assert_rejected(fixture)
                    self.assertEqual(fixture.downloads, [])

    def test_ambiguous_producer_job_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            fixture.jobs[1].append(copy.deepcopy(fixture.producer))
            self.assert_rejected(fixture)

    def test_wrong_or_cross_upload_markers_and_missing_receipt_are_rejected(self):
        for fault in ('sha', 'id', 'name', 'mixed', 'duplicate', 'receipt', 'ambiguous-receipt', 'echo', 'ansi-fragment'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                log = fixture.logs[1002]
                if fault == 'sha': log = log.replace(fixture.shard['digest'][7:], 'b' * 64)
                if fault == 'id': log = log.replace('Artifact ID ' + str(fixture.shard['id']), 'Artifact ID 999')
                if fault == 'name': log = log.replace(fixture.shard['name'], 'other')
                if fault == 'mixed': log = fixture.upload_log(fixture.aggregate) + log.splitlines()[-1] + '\n'
                if fault == 'duplicate': log += log
                if fault == 'echo': log = '\n'.join('echo ' + line for line in log.splitlines())
                if fault == 'ansi-fragment': log = log.replace('Artifact ID', 'Artifact\x1b[2J ID')
                if fault == 'receipt': fixture.logs[1001] = fixture.logs[1001].replace(fixture.args.receipt_sha256, 'b' * 64)
                if fault == 'ambiguous-receipt': fixture.logs[1001] += 'VERIFIED_RELEASE_RECEIPT_SHA256=' + 'b' * 64 + '\n'
                fixture.logs[1002] = log
                self.assert_rejected(fixture)
                self.assertEqual(fixture.downloads, [])

    def test_ansi_colored_upload_tokens_are_safely_read(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            fixture.logs[1002] = fixture.logs[1002].replace(fixture.shard['digest'][7:], '\x1b[36m' + fixture.shard['digest'][7:] + '\x1b[0m')
            fixture.execute()

    def test_downloaded_zip_hash_and_size_must_match_server(self):
        for which in ('controls', 'shard'):
            for fault in ('bytes', 'size'):
                with self.subTest(which=which, fault=fault), tempfile.TemporaryDirectory() as temp:
                    fixture = Fixture(Path(temp))
                    artifact = getattr(fixture, which)
                    data = fixture.blobs[artifact['id']]
                    fixture.blobs[artifact['id']] = (bytes([data[0] ^ 1]) + data[1:]) if fault == 'bytes' else data + b'X'
                    self.assert_rejected(fixture)

    def test_receipt_hash_and_complete_current_policy_identity(self):
        cases = [('schema', True), ('schema', 1), ('contract', 'upstream7'), ('version', 'v2.3.2'), ('release_tag', 'v2.3.1-other'),
                 ('repository', 'fork/repo'), ('workflow_run_id', 124), ('workflow_commit', 'b' * 40),
                 ('policy_fingerprint', 'b' * 64)]
        for key, value in cases:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                fixture.proof[key] = value
                fixture.refresh_controls()
                self.assert_rejected(fixture)
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            fixture.args.receipt_sha256 = 'b' * 64
            fixture.logs[1001] = fixture.logs[1001].replace(candidate.digest_bytes(encoded(fixture.proof))['sha256'], 'b' * 64)
            self.assert_rejected(fixture)

    def test_whole_receipt_and_checksums_are_verified_for_unselected_packages(self):
        for fault in ('missing', 'extra', 'hash', 'bytes', 'bool-size', 'checksum-content', 'checksum-duplicate', 'checksum-hash'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                name = '1panel-v2.3.1-official-offline-linux-s390x.tar.gz'
                if fault == 'missing': del fixture.proof['files'][name]
                if fault == 'extra': fixture.proof['files']['unreviewed.tar.gz'] = candidate.digest_bytes(b'bad')
                if fault == 'hash': fixture.proof['files'][name]['sha256'] = 'bad'
                if fault == 'bytes': fixture.proof['files'][name]['bytes'] = 0
                if fault == 'bool-size': fixture.proof['files'][name]['bytes'] = True
                if fault == 'checksum-content': fixture.checksums += b'changed'
                if fault == 'checksum-duplicate':
                    fixture.checksums += fixture.checksums.splitlines()[0] + b'\n'
                    fixture.proof['files']['checksums.txt'] = candidate.digest_bytes(fixture.checksums)
                if fault == 'checksum-hash': fixture.proof['files'][name]['sha256'] = 'b' * 64
                fixture.refresh_controls()
                self.assert_rejected(fixture)

    def test_plan_identity_rows_and_upstream_provenance(self):
        for key, value in [('version', 'v2.3.2'), ('tag', 'v2.3.1-other'), ('repository', 'fork/repo'),
                           ('workflow_run_id', '124'), ('workflow_commit', 'b' * 40), ('rows', []), ('native_rows', []),
                           ('mode', 'unknown'), ('upstream_input', {'source_kind': 'unverified'})]:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                fixture.plan[key] = value
                fixture.refresh_controls()
                self.assert_rejected(fixture)

    def test_shard_identity_row_plan_manifest_and_archive_bytes(self):
        for fault in ('identity', 'row', 'plan', 'missing-file', 'hash', 'bytes', 'archive', 'extra'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                name = next(iter(fixture.files))
                if fault == 'identity': fixture.record['identity']['workflow_commit'] = 'b' * 40
                if fault == 'row': fixture.record['row'] = {'source': 'custom', 'arch': 'arm64', 'key': 'custom-arm64'}
                if fault == 'plan': fixture.record['plan_sha256'] = 'b' * 64
                if fault == 'missing-file': fixture.record['files'] = {}
                if fault == 'hash': fixture.record['files'][name]['sha256'] = 'b' * 64
                if fault == 'bytes': fixture.record['files'][name]['bytes'] += 1
                if fault == 'archive': fixture.files[name] = b'X' * len(fixture.files[name])
                if fault == 'extra': fixture.files['unexpected'] = b'bad'
                fixture.refresh_shard()
                self.assert_rejected(fixture)

    def test_strict_zip_whitelist_rejects_traversal_links_duplicates_special_and_oversize(self):
        for fault in ('traversal', 'absolute', 'backslash', 'symlink', 'fifo', 'directory', 'duplicate', 'missing', 'oversize'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                entries = list(fixture.files.items()) + [('shard.json', encoded(fixture.record))]
                name, data = entries[0]
                if fault == 'traversal': entries[0] = ('../escape', data)
                if fault == 'absolute': entries[0] = ('/escape', data)
                if fault == 'backslash': entries[0] = (name.replace('/', '\\'), data)
                if fault in ('symlink', 'fifo', 'directory'):
                    entry = zipfile.ZipInfo(name)
                    entry.create_system = 3
                    entry.external_attr = ({'symlink': stat.S_IFLNK, 'fifo': stat.S_IFIFO, 'directory': stat.S_IFDIR}[fault] | 0o644) << 16
                    entries[0] = (entry, data)
                if fault == 'duplicate': entries.append(entries[0])
                if fault == 'missing': entries.pop()
                if fault == 'oversize': entries[0] = (name, data + b'X')
                fixture.refresh_shard(entries)
                self.assert_rejected(fixture)
                self.assertFalse((Path(temp) / 'escape').exists())

    def test_controls_zip_extra_missing_and_duplicate_json(self):
        for fault in ('extra', 'missing', 'duplicate-json'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                entries = [('publication-work/control/' + candidate.PROOF, encoded(fixture.proof)),
                           ('publication-work/release/checksums.txt', fixture.checksums),
                           ('matrix-input/plan.json', encoded(fixture.plan))]
                if fault == 'extra': entries.append(('extra.txt', b'extra'))
                if fault == 'missing': entries.pop()
                if fault == 'duplicate-json': entries[-1] = (entries[-1][0], b'{"mode":"stable","mode":"dev"}')
                fixture.refresh_controls(entries)
                self.assert_rejected(fixture)

    def test_no_existing_or_outside_runner_input_can_be_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            output = Path(fixture.args.output)
            output.mkdir()
            (output / 'preserved').write_text('keep')
            with self.assertRaises(ValueError): fixture.execute()
            self.assertEqual((output / 'preserved').read_text(), 'keep')
            self.assertEqual(fixture.downloads, [])
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            fixture.args.output = str(Path(temp).parent / 'outside-candidate')
            self.assert_rejected(fixture)
            self.assertEqual(fixture.downloads, [])

    def test_provenance_default_is_outside_archive_only_input(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            fixture.args.provenance = ''
            summary = fixture.execute()
            self.assertEqual(summary['provenance_path'], str(Path(temp) / 'input.provenance.json'))

    def test_changed_current_attempt_at_final_read_never_exposes_input(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            original = fixture.client_run
            reads = 0
            def changed(*args):
                nonlocal reads
                if args[-1].endswith('/runs/123'):
                    reads += 1
                    if reads == 2: fixture.run['run_attempt'] = 2
                return original(*args)
            fixture.client_run = changed
            self.assert_rejected(fixture)

    def test_pagination_and_truncation_fail_closed(self):
        calls = []
        def run(*args):
            calls.append(args[-1])
            return json.dumps({'total_count': 101, 'artifacts': [{'id': n} for n in (range(100) if args[-1].endswith('page=1') else [100])]})
        client = SimpleNamespace(run=run)
        self.assertEqual(len(candidate.listed(client, 'collection', 'artifacts')), 101)
        self.assertEqual(len(calls), 2)
        client.run = lambda *args: json.dumps({'total_count': 2, 'artifacts': [{'id': 1}]})
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            candidate.listed(client, 'collection', 'artifacts')

    def test_download_stream_caps_actual_bytes_and_reaps_process(self):
        class Process:
            def __init__(self): self.stdout = io.BytesIO(b'larger-than-declared'); self.killed = False
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def kill(self): self.killed = True
            def wait(self): return 0
        with tempfile.TemporaryDirectory() as temp:
            process = Process()
            client = candidate.CandidateGitHub(candidate.REPOS['downstream17'], 'v2.3.1')
            with patch.object(candidate.subprocess, 'Popen', return_value=process), self.assertRaises(ValueError):
                client.download_zip({'id': 1, 'size_in_bytes': 3}, Path(temp) / 'download.zip')
            self.assertTrue(process.killed)
            self.assertLessEqual((Path(temp) / 'download.zip').stat().st_size, 3)


if __name__ == '__main__':
    unittest.main()
