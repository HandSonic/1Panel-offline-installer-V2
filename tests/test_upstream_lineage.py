"""Synthetic producer generations; no network or application execution."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from resolved_inventory import ARCHES, canonical, digest
from runtime_contract import validate as validate_runtime
from resolved_transport import public_controls
from upstream_lineage import authenticate, generation_id, receipt_binding, validate
from test_automatic_source_consumption import SyntheticUpstream, VERSION
from test_runtime_contract import runtime
from test_validate_resolved_custom import Fixture


def manifest(run_id, commit, rows, accepted):
    return {'schema_version': 2, 'version': VERSION, 'requested_architectures': list(ARCHES),
            'producer_run_id': run_id, 'producer_run_attempt': 1, 'producer_head_sha': commit,
            'artifacts': [dict(rows[arch], build_repository_commit=commit) for arch in ARCHES if arch in accepted],
            'outcomes': [dict(architecture=arch, status='success' if arch in accepted else 'failure',
                stage='compile', reason='' if arch in accepted else 'compiler unavailable', job_id=run_id + index,
                job_url=f'https://github.com/HandSonic/1Panel-Build-v2/actions/runs/{run_id}/job/{run_id + index}')
                for index, arch in enumerate(ARCHES)]}


def lineage():
    value = runtime(version=VERSION)
    rows = value['upstream']['records']
    old = manifest(100001, 'a' * 40, rows, {'amd64'})
    current = manifest(200001, 'b' * 40, rows, set(ARCHES))
    keys = generation_id(old), generation_id(current)
    selected = {arch: keys[0] if arch == 'amd64' else keys[1] for arch in ARCHES}
    manifests = dict(zip(keys, (old, current)))
    return {'schema_version': 3, 'version': VERSION, 'requested_architectures': list(ARCHES),
            'resolved_contract_sha256': value['source_contract_sha256'], 'current_generation': keys[1],
            'generations': manifests, 'artifact_generations': selected,
            'artifacts': [next(row for row in manifests[selected[arch]]['artifacts'] if row['architecture'] == arch)
                          for arch in ARCHES]}


class LineagePublic(SyntheticUpstream):
    def __init__(self):
        super().__init__()
        self.manifest = lineage()
        self.generation_runs = {}
        self.generation_jobs = {}
        for generation in self.manifest['generations'].values():
            run_id, head = generation['producer_run_id'], generation['producer_head_sha']
            state = dict(self.state, id=run_id, head_sha=head,
                         conclusion='failure' if any(r['status'] == 'failure' for r in generation['outcomes']) else 'success')
            jobs = [dict(self.jobs[0], id=run_id + 100 + i, name=name, run_id=run_id,
                         head_sha=head) for i, name in enumerate(('tests', 'prepare', 'build'))]
            jobs.extend(dict(self.jobs[0], id=row['job_id'], name='compile (' + row['architecture'] + ')',
                             run_id=run_id, head_sha=head, conclusion=row['status'], html_url=row['job_url'])
                        for row in generation['outcomes'])
            self.generation_runs[run_id] = state
            self.generation_jobs[run_id] = jobs
        rows = self.manifest['artifacts']
        self.bodies['build-manifest.json'] = canonical(self.manifest)
        self.bodies['checksums.txt'] = ''.join(row['sha256'] + '  ' + row['file'] + '\n' for row in rows).encode()
        proof = json.loads(self.bodies['release-validation.json'])
        proof.update(schema=3, files={name: {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
            for name, raw in self.bodies.items() if name != 'release-validation.json'})
        # Synthetic payload facts must match the selected immutable row, not a newer producer.
        for row in rows:
            proof['files'][row['file']] = {'bytes': row['size'], 'sha256': row['sha256']}
        for key in ('outcomes', 'producer_run_id', 'producer_run_attempt', 'producer_head_sha'):
            proof.pop(key)
        for key in ('requested_architectures', 'resolved_contract_sha256', 'current_generation', 'generations', 'artifact_generations'):
            proof[key] = self.manifest[key]
        self.bodies['release-validation.json'] = canonical(proof)
        self.assets = [dict(id=920000 + index, name=name, size=proof['files'].get(name, {}).get('bytes', len(raw)),
            digest='sha256:' + proof['files'].get(name, {}).get('sha256', hashlib.sha256(raw).hexdigest()))
            for index, (name, raw) in enumerate(self.bodies.items())]
        self.log = 'VERIFIED_RELEASE_RECEIPT_SHA256=' + hashlib.sha256(self.bodies['release-validation.json']).hexdigest() + '\n'

    def run(self, *args):
        endpoint = args[-1]
        for run_id in self.generation_runs:
            base = f'repos/{self.repo}/actions/runs/{run_id}/attempts/1'
            if endpoint == base:
                return json.dumps(self.generation_runs[run_id])
            if endpoint == base + '/jobs?per_page=100&page=1':
                jobs = self.generation_jobs[run_id]
                return json.dumps({'total_count': len(jobs), 'jobs': jobs})
        return super().run(*args)


class LineageTests(unittest.TestCase):
    def test_exact_selected_rows_keep_both_producers(self):
        value = lineage()
        self.assertEqual(validate(value, VERSION), list(ARCHES))
        with patch('upstream_lineage.verify_jobs') as verify:
            self.assertEqual(authenticate(value, VERSION, object()), list(ARCHES))
        self.assertEqual([call.args[2] for call in verify.call_args_list], ['a' * 40, 'b' * 40])
        proof = {key: value[key] for key in ('requested_architectures', 'resolved_contract_sha256',
                 'current_generation', 'generations', 'artifact_generations')}
        proof['successful_architectures'] = list(ARCHES)
        receipt_binding(proof, value, list(ARCHES))
        proof['artifact_generations'] = {}
        with self.assertRaises(ValueError): receipt_binding(proof, value, list(ARCHES))

    def test_public_transport_authenticates_all_original_attempts(self):
        client = LineagePublic()
        contract, sha, upstream = public_controls(VERSION, 'stable', client)
        self.assertEqual(sha, digest(contract))
        self.assertIsNone(upstream['producer_commit'])
        self.assertEqual(upstream['records']['amd64']['build_repository_commit'], 'a' * 40)
        self.assertEqual(upstream['records']['arm64']['build_repository_commit'], 'b' * 40)
        value = runtime(version=VERSION)
        value['upstream'] = upstream
        self.assertEqual(validate_runtime(value, VERSION), value)
        for job_name in ('tests', 'prepare', 'build', 'compile (amd64)'):
            bad = LineagePublic()
            for job in bad.generation_jobs[100001]:
                if job['name'] == job_name: job['conclusion'] = 'cancelled'
            with self.subTest(job=job_name), self.assertRaises(ValueError):
                public_controls(VERSION, 'stable', bad)

    def test_generation_hash_uses_ascii_escaping_even_for_reason_text(self):
        value = lineage()
        old = next(iter(value['generations'].values()))
        old['outcomes'][1]['reason'] = '工具不可用'
        expected = hashlib.sha256(json.dumps(old, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
        self.assertEqual(generation_id(old), expected)
        self.assertNotEqual(generation_id(old), digest(old))

    def test_repacked_manifest_binds_the_selected_architecture_producer(self):
        from runtime_contract import manifest_contract
        value = runtime(version=VERSION)
        value['upstream']['matrix_manifest'] = lineage()
        value['upstream']['records'] = {row['architecture']: row for row in value['upstream']['matrix_manifest']['artifacts']}
        value['upstream']['producer_commit'] = None
        contract = value['source_contract']; fixture = Fixture()
        control = fixture.files['1pctl'].replace(fixture.version.encode(), VERSION.encode())
        for arch, commit in [('amd64', 'a' * 40), ('arm64', 'b' * 40)]:
            data = {'schema_version': 1, 'edition': 'community', 'version': VERSION,
                    'architecture': arch, 'source_commit': contract['source']['commit'],
                    'installer_commit': contract['installer']['commit'], 'mode': 'stable',
                    'build_repository_commit': commit, 'resolved_contract_sha256': digest(contract),
                    **{key + '_version': item for key, item in contract['toolchain'].items()},
                    'files': {name: {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                              for name, raw in fixture.files.items()}}
            with patch('runtime_contract.selected', return_value=value):
                self.assertEqual(manifest_contract(data, control, VERSION, arch), data)
                data['build_repository_commit'] = 'c' * 40
                with self.assertRaises(ValueError): manifest_contract(data, control, VERSION, arch)

    def test_public_unknown_schema_never_downgrades_to_legacy(self):
        for schema in (True, 0, 4, '3', None):
            client = LineagePublic()
            client.manifest['schema_version'] = schema
            client.bodies['build-manifest.json'] = canonical(client.manifest)
            with self.subTest(schema=schema), self.assertRaisesRegex(ValueError, 'Unsupported upstream aggregate schema'):
                public_controls(VERSION, 'stable', client)

    def test_tampering_mixed_source_unreferenced_nested_or_empty_generations_reject(self):
        changes = [lambda v: v['artifacts'][0].update(sha256='f' * 64),
                   lambda v: v['artifact_generations'].update(amd64=v['current_generation']),
                   lambda v: v.update(current_generation='f' * 64),
                   lambda v: v['generations'].update({'f' * 64: copy.deepcopy(v)}),
                   lambda v: v.update(resolved_contract_sha256='f' * 64),
                   lambda v: v['artifacts'].reverse(),
                   lambda v: v.update(schema_version=True)]
        for index, mutate in enumerate(changes):
            value = lineage(); mutate(value)
            with self.subTest(index=index), self.assertRaises(ValueError): validate(value, VERSION)
        for kind in ('empty', 'two-commits', 'unreferenced', 'too-many'):
            value = lineage(); current = value['generations'].pop(value['current_generation'])
            if kind == 'empty':
                current['artifacts'] = []
                for row in current['outcomes']: row.update(status='failure', reason='no compiler')
            elif kind == 'two-commits': current['artifacts'][0]['build_repository_commit'] = 'c' * 40
            new = generation_id(current); old = value['current_generation']; value['current_generation'] = new
            value['generations'][new] = current
            value['artifact_generations'] = {arch: new if key == old else key for arch, key in value['artifact_generations'].items()}
            if kind in ('unreferenced', 'too-many'):
                for offset in range(1 if kind == 'unreferenced' else 8):
                    extra = copy.deepcopy(current); extra['producer_run_id'] += 1000 + offset
                    for row in extra['outcomes']:
                        row['job_url'] = f'https://github.com/HandSonic/1Panel-Build-v2/actions/runs/{extra["producer_run_id"]}/job/{row["job_id"]}'
                    value['generations'][generation_id(extra)] = extra
            with self.subTest(kind=kind), self.assertRaises(ValueError): validate(value, VERSION)


if __name__ == '__main__':
    unittest.main()
