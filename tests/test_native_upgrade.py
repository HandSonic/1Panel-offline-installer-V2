"""Synthetic public bootstrap and real-harness guards; never execute services."""
import copy
from contextlib import redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import native_upgrade_input as binder
import native_upgrade_smoke as smoke
from test_public_predecessor import fixture as public_fixture
from validate_payload import APP_REQUIRED, PAYLOAD_REQUIRED, REQUIRED, docker
from patch_installer import patch as patch_installer


def sha(raw): return hashlib.sha256(raw).hexdigest()
def facts(raw): return {'bytes': len(raw), 'sha256': sha(raw)}


def tar_bytes(files, prefix):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz') as out:
        for name, raw in files.items():
            member = tarfile.TarInfo(prefix + '/' + name); member.size = len(raw); member.mode = 0o755
            out.addfile(member, io.BytesIO(raw))
    return data.getvalue()


INSTALLER = b'''#!/bin/bash
CURRENT_DIR=$(pwd)
EDITION_FILE=".selected_edition"
function log() {
    :
}
function Install_Docker() {
    :
}
function Set_Parameters() {
    echo "$TXT_SET_INSTALL_DIR $TXT_SET_PANEL_PORT $TXT_SET_PANEL_ENTRANCE $TXT_SET_PANEL_USER $TXT_SET_PANEL_PASSWORD"
    sed -i "s/PANEL_EDITION=.*/PANEL_EDITION=${selected_edition}/" /usr/local/bin/1pctl
}
selected_edition=$(cat "$CURRENT_DIR/$EDITION_FILE")
'''


class PublicBootstrapTests(unittest.TestCase):
    def test_input_failure_retains_exact_check_and_stage_in_stderr(self):
        arguments = ['native_upgrade_input.py']
        for name in ('version', 'source', 'arch', 'target-input', 'target-provenance',
                     'target-controls-id', 'target-receipt-sha256', 'output', 'provenance'):
            arguments += ['--' + name, 'synthetic']
        output = io.StringIO()
        with patch.object(sys, 'argv', arguments), \
                patch.object(binder, 'INPUT_STAGE', 'independent-predecessor-source-validation'), \
                patch.object(binder, 'materialize', side_effect=ValueError('Synthetic exact source mismatch')), \
                redirect_stderr(output):
            self.assertEqual(binder.main(), 1)
        self.assertIn('Traceback (most recent call last)', output.getvalue())
        self.assertIn('ValueError: Synthetic exact source mismatch', output.getvalue())
        self.assertIn('NATIVE_UPGRADE_INPUT_STAGE=independent-predecessor-source-validation:failed',
                      output.getvalue())

    def test_public_receipt_pins_are_bootstrap_inputs_not_native_acceptance(self):
        selection, release, receipt, checksums, matrix, run = public_fixture()
        value = binder.bind_public(selection, release, receipt, checksums, run, 'official', 'amd64')
        self.assertEqual(value['historical_native_acceptance'], 'not-claimed')
        self.assertEqual(value['kind'], 'current-run-public-predecessor-bootstrap')
        self.assertEqual(value['archive']['name'], '1panel-v2.100.0-official-offline-linux-amd64.tar.gz')

    def test_public_asset_id_hash_size_archive_inventory_and_run_fail_closed(self):
        args = public_fixture()
        for field, value in [('digest', 'sha256:' + 'b' * 64), ('size', 1), ('id', 700999), ('state', 'pending')]:
            changed = copy.deepcopy(args)
            asset = next(a for a in changed[1]['assets'] if '-official-' in a['name'])
            asset[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                binder.bind_public(*changed[:4], changed[5], 'official', 'amd64')
        changed = copy.deepcopy(args); changed[5]['conclusion'] = 'failure'
        with self.assertRaises(ValueError): binder.bind_public(*changed[:4], changed[5], 'official', 'amd64')

    def test_receipt_validator_logs_require_exact_identity_and_marker(self):
        run = public_fixture()[5]
        job = {'id': 600001, 'name': 'publication_prepare', 'run_id': run['id'],
               'run_attempt': run['run_attempt'], 'head_sha': run['head_sha'],
               'status': 'completed', 'conclusion': 'success'}
        client = SimpleNamespace(repo=binder.REPOSITORY)
        marker = 'VERIFIED_RELEASE_RECEIPT_SHA256=' + 'c' * 64 + '\n'
        with patch.object(binder, 'listed', return_value=[job]), patch.object(binder, 'read_job_log', return_value=marker):
            self.assertEqual(binder.verify_receipt_log(client, run, 'c' * 64)['job_id'], job['id'])
        for fault in ('head', 'attempt', 'run', 'missing-log', 'duplicate-marker'):
            changed = dict(job); log = marker
            if fault == 'head': changed['head_sha'] = 'b' * 40
            if fault == 'attempt': changed['run_attempt'] = run['run_attempt'] + 1
            if fault == 'run': changed['run_id'] += 1
            if fault == 'missing-log': log = ''
            if fault == 'duplicate-marker': log += marker
            with self.subTest(fault=fault), patch.object(binder, 'listed', return_value=[changed]), \
                    patch.object(binder, 'read_job_log', return_value=log), self.assertRaises(ValueError):
                binder.verify_receipt_log(client, run, 'c' * 64)

    def modern_fixture(self):
        from test_public_predecessor import rebind_controls
        selection, release, raw, checksums, matrix, run = public_fixture()
        proof = json.loads(raw)
        proof.update(schema=2, contract='downstream-matrix', workflow_run_attempt=2,
                     plan_sha256=sha(b'synthetic plan'), preparation_receipt_sha256=sha(b'synthetic preparation'))
        proof['requested_products'] = [{'source': s, 'arch': a, 'key': s + '-' + a}
            for s, arches in matrix.items() for a in arches]
        proof['outcomes'] = [dict(r, status='success', stage='native-acceptance', reason='') for r in proof['requested_products']]
        failed = next(r for r in proof['outcomes'] if r['key'] == 'custom-arm64')
        failed.update(status='failure', reason='synthetic native failure')
        removed = '1panel-v2.100.0-custom-offline-linux-arm64.tar.gz'
        del proof['files'][removed]
        release['assets'] = [a for a in release['assets'] if a['name'] != removed]
        native_raw = b'{"kind":"synthetic unsigned native subject"}'
        proof['files']['native-acceptance.json'] = facts(native_raw)
        release['assets'].append({'id': 799999, 'name': 'native-acceptance.json', 'state': 'uploaded',
            'url': binder.API_ROOT + '/releases/assets/799999', 'size': len(native_raw), 'digest': 'sha256:' + sha(native_raw)})
        checksums = ''.join(p['sha256'] + '  ' + n + '\n' for n, p in proof['files'].items() if n.endswith('.tar.gz')).encode()
        args = [selection, release, raw, checksums, matrix, run]
        rebind_controls(args, proof, checksums)
        args[5]['conclusion'] = 'failure'
        return args, proof

    def test_schema2_partial_public_release_uses_exact_acceptance_job_not_whole_run_success(self):
        args, proof = self.modern_fixture()
        value = binder.bind_public(*args[:4], args[5], 'official', 'amd64')
        self.assertEqual(value['historical_native_acceptance'], 'not-claimed')
        with self.assertRaises(ValueError): binder.bind_public(*args[:4], args[5], 'custom', 'arm64')
        run = args[5]
        job = {'id': 600001, 'name': 'publication_acceptance', 'run_id': run['id'],
               'run_attempt': 2, 'head_sha': run['head_sha'], 'status': 'completed', 'conclusion': 'success'}
        marker = 'VERIFIED_RELEASE_RECEIPT_SHA256=' + sha(args[2]) + '\n'
        marker += 'NATIVE_ACCEPTANCE_SHA256=' + proof['files']['native-acceptance.json']['sha256'] + '\n'
        client = SimpleNamespace(repo=binder.REPOSITORY)
        with patch.object(binder, 'listed', return_value=[job]), patch.object(binder, 'read_job_log', return_value=marker):
            binder.verify_receipt_log(client, run, sha(args[2]), proof)
        for fault in ('prepare-only', 'wrong-attempt', 'missing-native-marker', 'cancelled'):
            changed = dict(job); log = marker
            if fault == 'prepare-only': changed['name'] = 'publication_prepare'
            if fault == 'wrong-attempt': changed['run_attempt'] = 1
            if fault == 'missing-native-marker': log = marker.splitlines()[0] + '\n'
            if fault == 'cancelled': changed['conclusion'] = 'cancelled'
            with self.subTest(fault=fault), patch.object(binder, 'listed', return_value=[changed]), \
                    patch.object(binder, 'read_job_log', return_value=log), self.assertRaises(ValueError):
                binder.verify_receipt_log(client, run, sha(args[2]), proof)

    def test_current_public_acceptance_noop_authenticates_without_installing(self):
        args, proof = self.modern_fixture()
        release, run = args[1], args[5]
        job = {'id': 600001, 'name': 'publication_acceptance', 'run_id': run['id'], 'run_attempt': 2,
               'head_sha': run['head_sha'], 'status': 'completed', 'conclusion': 'success'}
        log = 'VERIFIED_RELEASE_RECEIPT_SHA256=' + sha(args[2]) + '\n'
        log += 'NATIVE_ACCEPTANCE_SHA256=' + proof['files']['native-acceptance.json']['sha256'] + '\n'
        def read(*argv):
            endpoint = argv[-1]
            if endpoint.endswith('/releases/' + str(release['id'])): return json.dumps(release)
            if '/releases/' in endpoint and '/assets?' in endpoint: return json.dumps(release['assets'])
            if endpoint.endswith('/runs/' + str(run['id'])): return json.dumps(run)
            if '/jobs?filter=all' in endpoint: return json.dumps({'total_count': 1, 'jobs': [job]})
            if endpoint.endswith('/jobs/600001/logs'): return log
            raise AssertionError(endpoint)
        client = SimpleNamespace(repo=binder.REPOSITORY, run=read)
        with patch.object(smoke, 'native_upgrade', side_effect=AssertionError('No install for no-op')):
            result = binder.verify_public_acceptance(client, release, args[2], args[3])
        self.assertEqual(result['validator']['job_id'], 600001)
        self.assertEqual(result['native_subject_sha256'], proof['files']['native-acceptance.json']['sha256'])
        job['run_attempt'] = 1
        with self.assertRaises(ValueError): binder.verify_public_acceptance(client, release, args[2], args[3])

    def test_snapshot_ignores_download_counts_but_never_changed_asset_identity(self):
        release = public_fixture()[1]
        changed = copy.deepcopy(release)
        for asset in changed['assets']: asset['download_count'] = 100
        self.assertEqual(binder.release_snapshot(release), binder.release_snapshot(changed))
        changed['assets'][0]['digest'] = 'sha256:' + 'd' * 64
        self.assertNotEqual(binder.release_snapshot(release), binder.release_snapshot(changed))

    def test_missing_or_failed_catalogue_page_cannot_become_empty_success(self):
        for response in ('{}', '{"message":"rate limited"}', 'null'):
            with self.subTest(response=response), self.assertRaises(ValueError):
                binder.array_pages(SimpleNamespace(run=lambda *args: response), 'synthetic')

    def test_official_source_is_independent_of_custom_source_controls(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td); body = {'1panel-core': b'synthetic non-executable placeholder'}
            raw = tar_bytes(body, '1panel-v2.100.0-linux-amd64'); pin = dict(facts(raw), url='https://example.test/synthetic')
            def download(pin, destination): destination.write_bytes(raw)
            # Source archive validation correctly rejects malformed core bytes only
            # later; this checks transport isolation without running package code.
            with patch.object(binder, 'discover_vendor', return_value={'archives': {'amd64': pin}}), \
                    patch.object(binder, 'download_url', side_effect=download), \
                    patch.object(binder, 'public_controls', side_effect=AssertionError('Official must not read custom controls')):
                returned, provenance = binder.source_archive('v2.100.0', 'stable', 'official', 'amd64', work)
                self.assertEqual(returned, body); self.assertEqual(provenance['kind'], 'canonical-vendor')


class IndependentArchiveTests(unittest.TestCase):
    def fixture(self, work, source_kind='official', installer=INSTALLER):
        binary = b'\x7fELF\x02\x01' + b'\0' * 12 + b'\x3e\x00' + b'synthetic non-executable body'
        source = {name: ('synthetic source ' + name).encode() for name in APP_REQUIRED}
        source.update({'1panel-core': binary, '1panel-agent': binary, 'install.sh': installer,
                       'extra-source-resource': b'synthetic pinned resource'})
        script = work / 'install.sh'; script.write_bytes(installer); patch_installer(script)
        docker_raw = tar_bytes({name: binary for name in REQUIRED}, 'docker')
        source_pin = {'bytes': 123, 'sha256': sha(b'synthetic original archive')}
        body = dict(source, **{'install.sh': script.read_bytes(), 'upgrade.sh': b'OLD UPGRADER MUST NEVER EXECUTE',
                              'docker.tgz': docker_raw, 'docker-compose': binary, 'docker.service': b'synthetic service'})
        (work / 'docker.service').write_bytes(body['docker.service'])
        inputs = {'app': source_pin}
        for name, payload in [('docker', 'docker.tgz'), ('compose', 'docker-compose')]:
            pin = dict(facts(body[payload]), url='https://example.test/' + name, version='99.1.0')
            inputs[name] = pin; (work / (name + '-sources.json')).write_text(json.dumps({'amd64': pin}))
        manifest = {'schema': 1, 'source': source_kind, 'architecture': 'amd64', 'app_version': 'v2.100.0',
                    'inputs': inputs, 'payloads': {name: facts(body[name]) for name in PAYLOAD_REQUIRED},
                    'docker_binaries': docker(io.BytesIO(docker_raw), 'amd64')}
        body['offline-manifest.json'] = json.dumps(manifest).encode()
        name = f'1panel-v2.100.0-{source_kind}-offline-linux-amd64.tar.gz'
        raw = tar_bytes(body, name.removesuffix('.tar.gz')); archive = work / name; archive.write_bytes(raw)
        binding = {'version': 'v2.100.0', 'archive': dict(facts(raw), name=name, asset_id=700001)}
        return archive, binding, source, {'pin': source_pin}, body

    def test_exact_source_plus_offline_patch_preserves_unused_old_upgrader(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td); archive, binding, source, provenance, body = self.fixture(work)
            package, selected = binder.unpack_predecessor(archive, work / 'out', binding, 'official', 'amd64', source, provenance, work)
            self.assertEqual((package / 'upgrade.sh').read_bytes(), b'OLD UPGRADER MUST NEVER EXECUTE')
            self.assertEqual(selected['binaries']['1panel-core'], facts(source['1panel-core'])['sha256'])
            self.assertEqual(selected['installer_mode'], 'interactive')

    def test_optional_appstore_source_can_bootstrap_without_inventing_a_payload(self):
        hook = b'''function Install_AppStore() {
    local appstore_file="${CURRENT_DIR}/appstore.tar.gz"
    if [[ ! -f "$appstore_file" ]]; then
        return
    fi
    tar -xf "$appstore_file"
}
Install_AppStore
'''
        for source_kind in ('official', 'custom'):
            with self.subTest(source=source_kind), tempfile.TemporaryDirectory() as td:
                work = Path(td)
                archive, binding, source, provenance, body = self.fixture(work, source_kind, INSTALLER + hook)
                package, selected = binder.unpack_predecessor(archive, work / 'out', binding,
                    source_kind, 'amd64', source, provenance, work)
                self.assertFalse((package / 'appstore.tar.gz').exists())
                self.assertEqual((package / 'install.sh').read_bytes(), body['install.sh'])
                self.assertEqual(selected['source'], source_kind)

    def test_self_consistent_manifest_cannot_override_independent_source(self):
        for target in ('1panel-core', 'install.sh', 'extra-source-resource', 'docker.service', 'docker-compose'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as td:
                work = Path(td); archive, binding, source, provenance, body = self.fixture(work)
                body[target] += b'\nself-consistent tampering'
                manifest = json.loads(body['offline-manifest.json'])
                if target in manifest['payloads']: manifest['payloads'][target] = facts(body[target])
                body['offline-manifest.json'] = json.dumps(manifest).encode()
                raw = tar_bytes(body, archive.name.removesuffix('.tar.gz')); archive.write_bytes(raw)
                binding['archive'].update(facts(raw))
                with self.assertRaises(ValueError):
                    binder.unpack_predecessor(archive, work / 'out', binding, 'official', 'amd64', source, provenance, work)

    def test_unbound_added_file_and_missing_source_file_are_rejected(self):
        for addition in (False, True):
            with tempfile.TemporaryDirectory() as td:
                work = Path(td); archive, binding, source, provenance, body = self.fixture(work)
                if addition: body['unbound'] = b'synthetic unbound file'
                else: del body['extra-source-resource']
                raw = tar_bytes(body, archive.name.removesuffix('.tar.gz')); archive.write_bytes(raw)
                binding['archive'].update(facts(raw))
                with self.assertRaises(ValueError):
                    binder.unpack_predecessor(archive, work / 'out', binding, 'official', 'amd64', source, provenance, work)


class NativeUpgradeStateTests(unittest.TestCase):
    def test_local_non_hosted_call_fails_before_subprocess_or_mutation(self):
        with tempfile.TemporaryDirectory() as td, patch.object(smoke, 'run', side_effect=AssertionError('Must not execute')):
            with self.assertRaises(ValueError):
                smoke.native_upgrade(Path(td) / 'proof.json', 'v2.101.0', 'official', 'amd64', Path(td) / 'result.json', env={})
            self.assertFalse((Path(td) / 'result.json').exists())

    def test_failure_dropin_cleanup_on_assertion_and_setup_failure(self):
        for setup_fails in (False, True):
            with tempfile.TemporaryDirectory() as td:
                temp = Path(td); dropin = temp / 'unit-overrides'
                effects = [RuntimeError('synthetic reload failure'), ''] if setup_fails else None
                with patch.object(smoke, 'RUNTIME_CORE_DROPIN', dropin), patch.object(smoke, 'run', side_effect=effects, return_value='') as calls:
                    with self.assertRaises(RuntimeError):
                        with smoke.failure_probe(temp) as (marker, facts):
                            self.assertTrue((dropin / 'native-upgrade-failure.conf').exists())
                            self.assertEqual(facts['script_sha256'], smoke.digest(temp / 'native-upgrade-start-failure.py'))
                            raise RuntimeError('synthetic assertion failure')
                    self.assertFalse(dropin.exists())
                    self.assertEqual(calls.call_args_list[-1].args[0], ['systemctl', 'daemon-reload'])

    def test_real_sqlite_probe_and_settings_survive_version_change(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); directory = base / '1panel/db'; directory.mkdir(parents=True)
            for name in ['core', 'agent']:
                with sqlite3.connect(directory / (name + '.db')) as con:
                    con.execute('CREATE TABLE settings (key TEXT, value TEXT)')
                    con.executemany('INSERT INTO settings VALUES (?,?)', [('SystemVersion', 'v2.100.0'), ('UserName', 'test'), ('Password', 'hash'), ('ServerPort', '19876')])
            before = smoke.database_state(base, 'v2.100.0', 'unicode:持久:quotes\'"')
            for name in ['core', 'agent']:
                with sqlite3.connect(directory / (name + '.db')) as con:
                    con.execute("UPDATE settings SET value='v2.101.0' WHERE key='SystemVersion'")
            self.assertEqual(smoke.database_state(base, 'v2.101.0'), before)
            with sqlite3.connect(directory / 'agent.db') as con: con.execute('DELETE FROM native_upgrade_probe')
            with self.assertRaises(ValueError): smoke.database_state(base, 'v2.101.0')

    def test_regional_edition_and_duplicate_configuration_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / '1pctl'
            text = '\n'.join(k + '=' + ('intl' if k == 'PANEL_EDITION' else 'value') for k in smoke.PROTECTED_KEYS) + '\n'
            path.write_text(text); self.assertEqual(smoke.control_configuration(path)['PANEL_EDITION'], 'intl')
            for bad in [text.replace('PANEL_EDITION=intl', 'PANEL_EDITION=cn'), text + 'ORIGINAL_PORT=1\n', text.replace('BASE_DIR=value\n', '')]:
                path.write_text(bad)
                with self.assertRaises(ValueError): smoke.control_configuration(path)


if __name__ == '__main__': unittest.main()
