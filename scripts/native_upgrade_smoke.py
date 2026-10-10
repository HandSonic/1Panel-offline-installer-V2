#!/usr/bin/env python3
"""Real predecessor installation, rollback and upgrade on disposable hosted VMs.

Never run on a workstation. No Docker mocks, command wrappers, package patches,
or production test switches. A temporary systemd ExecStartPre drop-in fails one
real target-core start, then is removed before the successful upgrade attempt.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
import time

from native_install_smoke import (digest, disposable_guard, native_install, run,
    require_exact_version, service_identity, verify_regional_edition, wait_for_panel)
from native_candidate_input import current_identity, json_object, require, recovery_request, RECOVERY_INPUT
from public_predecessor import semver
from resolved_inventory import canonical
from publication_contract import ROOT

BIN = Path('/usr/local/bin')
SERVICES = ('1panel-core', '1panel-agent')
RUNTIME_CORE_DROPIN = Path('/run/systemd/system/1panel-core.service.d')
PROTECTED_KEYS = ('BASE_DIR', 'ORIGINAL_PORT', 'ORIGINAL_USERNAME', 'ORIGINAL_PASSWORD',
                  'ORIGINAL_ENTRANCE', 'LANGUAGE', 'PANEL_EDITION', 'CHANGE_USER_INFO')
# Only durable application settings; runtime/version/counter fields may change.
SETTING_KEYS = ('UserName', 'Password', 'ServerPort', 'SecurityEntrance',
                'PanelName', 'Language', 'SessionTimeout', 'MFAStatus', 'BindAddress')


CURRENT_STAGE = 'not-started'


def stage(name, passed=False):
    global CURRENT_STAGE
    if not passed:
        CURRENT_STAGE = name
    print('NATIVE_UPGRADE_STAGE=' + name + (':passed' if passed else ':started'), flush=True)


def control_configuration(path):
    values = {}
    for key in PROTECTED_KEYS:
        found = re.findall(r'^' + key + r'=(.*)$', path.read_text(), re.M)
        require(len(found) <= 1, 'Ambiguous installed configuration')
        if found:
            values[key] = found[0]
    require(set(('BASE_DIR', 'ORIGINAL_PORT', 'ORIGINAL_USERNAME', 'ORIGINAL_PASSWORD',
                 'ORIGINAL_ENTRANCE', 'PANEL_EDITION')).issubset(values), 'Missing installed user configuration')
    require(values['PANEL_EDITION'] == 'intl', 'International edition was not retained')
    return values


def database_state(base, expected_version, token=None):
    states = {}
    for component in ('core', 'agent'):
        path = base / '1panel/db' / (component + '.db')
        require(path.is_file() and not path.is_symlink(), 'Missing real installed database')
        with sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=10) as con:
            require(con.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'Database integrity failed')
            require(con.execute("SELECT value FROM settings WHERE key='SystemVersion'").fetchall() == [(expected_version,)],
                    'Installed database version mismatch')
            if token is not None:
                con.execute('CREATE TABLE native_upgrade_probe (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
                con.execute('INSERT INTO native_upgrade_probe VALUES (1, ?)', (token,))
            rows = con.execute('SELECT id, value FROM native_upgrade_probe ORDER BY id').fetchall()
            require(len(rows) == 1 and rows[0][0] == 1, 'Persistent database probe missing')
            placeholders = ','.join('?' for _ in SETTING_KEYS)
            settings = con.execute('SELECT key, value FROM settings WHERE key IN (' + placeholders + ') ORDER BY key', SETTING_KEYS).fetchall()
            states[component] = {'probe': rows, 'settings': settings}
    return states


def database_evidence(base):
    result = {}
    for component in ('core', 'agent'):
        path = base / '1panel/db' / (component + '.db')
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10) as con:
            schema = con.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
            version = con.execute("SELECT value FROM settings WHERE key='SystemVersion'").fetchall()
            require(len(version) == 1, 'Missing/ambiguous database version evidence')
            result[component] = {'version': version[0][0], 'schema': schema,
                                 'schema_sha256': hashlib.sha256(json.dumps(schema).encode()).hexdigest()}
    return result


def installed_resources(base):
    paths = [BIN / '1pctl', base / '1panel/geo/GeoIP.mmdb']
    paths += [Path('/etc/systemd/system') / (name + '.service') for name in SERVICES]
    require(all(p.is_file() and not p.is_symlink() for p in paths), 'Missing/unsafe installed resources')
    languages = sorted(p for p in (BIN / 'lang').rglob('*') if p.is_file())
    require(languages and all(not p.is_symlink() for p in languages), 'Missing/unsafe installed language resources')
    return {str(p): digest(p) for p in paths + languages}


def verify_live(package, version, config, db_state, data, data_sha, docker, base):
    wait_for_panel(19876)
    identities = {name: service_identity(name) for name in SERVICES}
    for name, identity in identities.items():
        require(identity['binary_sha256'] == digest(package / name) == digest(BIN / name),
                'Real running/installed panel bytes do not match the bound package')
    require_exact_version(run([str(BIN / '1pctl'), 'version']), version)
    verify_regional_edition('intl', (BIN / '1pctl').read_text())
    require(control_configuration(BIN / '1pctl') == config, 'User configuration changed')
    require(database_state(base, version) == db_state, 'Persistent database/user settings changed')
    require(data.is_file() and digest(data) == data_sha, 'Persistent user file changed')
    require(service_identity('docker') == docker, 'Docker daemon was replaced or restarted during upgrade')
    return identities


def validate_inputs(proof_path, version, source, arch, env=os.environ, root=ROOT, tag=''):
    proof = json_object(Path(proof_path).read_bytes())
    old, target = Path(proof['predecessor_package_path']), Path(proof['target_package_path'])
    require(disposable_guard(old, env) == arch and disposable_guard(target, env) == arch,
            'Upgrade package architecture differs from native runner')
    tag = tag or version
    identity = current_identity(version, tag, source, arch, env, root)
    target_proof = proof['target']
    require(all(target_proof.get(k) == v for k, v in {
        'version': version, 'source': source, 'arch': arch, 'release_tag': tag,
        'repository': identity['repository'], 'workflow_run_id': identity['run_id'],
        'workflow_run_attempt': identity['run_attempt'], 'workflow_commit': identity['head_sha']}.items()),
        'Upgrade target is not the current exact candidate')
    lock, selected = proof['predecessor'], proof['predecessor_package']
    request = recovery_request(env.get(RECOVERY_INPUT, ''), version, env)
    if request is not None:
        from native_upgrade_input import candidate_binding
        require(proof.get('read_only_recovery') == request == candidate_binding(lock, version, source, arch),
                'Candidate recovery differs from explicit read-only input')
        require(request['head_sha'] == identity['head_sha'] and request['run_id'] != identity['run_id'],
                'Candidate predecessor must be an independent run at the same reviewed commit')
    else:
        require('read_only_recovery' not in proof and lock.get('kind') == 'current-run-public-predecessor-bootstrap',
                'Canonical public predecessor required without explicit candidate mode')
    require(proof.get('schema') == 2 and
            lock.get('historical_native_acceptance') == 'not-claimed' and
            proof.get('predecessor_binding_sha256') == hashlib.sha256(canonical(lock)).hexdigest(),
            'Predecessor bootstrap binding changed')
    require(lock['selection']['target_version'] == version and
            semver(lock['version'], lock['mode']) < semver(version, lock['mode']) and
            selected['source'] == source and selected['arch'] == arch and
            lock['archive']['sha256'] == next(iter(selected['files'].values()))['sha256'],
            'Predecessor source/channel/target/archive mismatch')
    for package, expected_version, manifest_sha in [
            (old, lock['version'], selected['manifest_sha256']),
            (target, version, proof['target_manifest_sha256'])]:
        require(digest(package / 'offline-manifest.json') == manifest_sha, 'Bound package manifest changed')
        manifest = json_object((package / 'offline-manifest.json').read_bytes())
        require(manifest['app_version'] == expected_version and manifest['source'] == source and
                manifest['architecture'] == arch, 'Upgrade manifest identity mismatch')
        for name, facts in manifest['payloads'].items():
            path = package / name
            require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(package.resolve()) and
                    path.stat().st_size == facts['bytes'] and digest(path) == facts['sha256'], 'Bound package payload changed')
    require((target / 'upgrade.sh').read_bytes() == (root / 'upgrade_offline.sh').read_bytes(),
            'Only the fixed reviewed target upgrader may execute')
    for name, sha in selected['binaries'].items():
        require(digest(old / name) == sha, 'Predecessor source binary changed')
    return proof, lock, selected, old, target


def run_upgrade(target):
    # Do not export Actions tokens, panel passwords or production test switches.
    env = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
           'LANG': 'C.UTF-8', 'HOME': '/root'}
    proc = subprocess.Popen(['bash', str(target / 'upgrade.sh')], cwd=target, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        return proc.wait(timeout=240)
    except subprocess.TimeoutExpired:
        # TERM triggers the upgrader's own rollback trap. Bound its recovery too.
        import signal
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        raise ValueError('Real upgrade exceeded its bounded execution/recovery time')


@contextmanager
def failure_probe(temp):
    """Runtime-only, one-shot real systemd start failure, never a command mock."""
    directory = RUNTIME_CORE_DROPIN
    require(not directory.exists(), 'Unexpected existing runtime core override')
    script, marker = temp / 'native-upgrade-start-failure.py', temp / 'native-upgrade-failure-observed.json'
    require(not script.exists() and not marker.exists(), 'Failure probe already exists')
    script.write_text('''#!/usr/bin/python3
import hashlib,json,pathlib,sqlite3,sys
marker=pathlib.Path(sys.argv[1])
if marker.exists():
    state=json.loads(marker.read_text())
    state['successful_prestarts']+=1
    marker.write_text(json.dumps(state))
    sys.exit(0)
core=pathlib.Path('/usr/local/bin/1panel-core')
h=hashlib.sha256()
with core.open('rb') as stream:
    for block in iter(lambda:stream.read(1024*1024),b''): h.update(block)
versions=[]
for name in ('core','agent'):
    path=pathlib.Path(sys.argv[2])/'1panel/db'/(name+'.db')
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as con:
        versions.append(con.execute("SELECT value FROM settings WHERE key='SystemVersion'").fetchall())
marker.write_text(json.dumps({'core_sha256':h.hexdigest(),'versions':versions,
                              'failure_count':1,'successful_prestarts':0}))
sys.exit(1)
''')
    script.chmod(0o700)
    # Hosted RUNNER_TEMP is checked before this, and cannot contain unit escapes.
    for path in (script, marker, temp / 'native-panel-data'):
        require(re.fullmatch(r'/[A-Za-z0-9_./-]+', str(path)) is not None, 'Unsafe systemd probe path')
    dropin = directory / 'native-upgrade-failure.conf'
    directory.mkdir()
    try:
        dropin.write_text('[Service]\nExecStartPre=/usr/bin/python3 ' + str(script) + ' ' + str(marker) + ' ' + str(temp / 'native-panel-data') + '\n')
        run(['systemctl', 'daemon-reload'])
        yield marker, {'script_sha256': digest(script), 'dropin_sha256': digest(dropin),
                       'script': script.read_text(), 'dropin': dropin.read_text()}
    finally:
        dropin.unlink(missing_ok=True)
        directory.rmdir()
        run(['systemctl', 'daemon-reload'])


def native_upgrade(provenance, version, source, arch, result, env=os.environ, root=ROOT, tag=''):
    stage('input-binding')
    # The guard runs before writes, subprocesses, or service operations.
    disposable_guard(Path(provenance), env)
    temp = Path(env['RUNNER_TEMP']).resolve()
    result = Path(result).resolve()
    require(result.is_relative_to(temp) and not result.exists(), 'Evidence output must be new and runner-local')
    proof, lock, selected, old, target = validate_inputs(provenance, version, source, arch, env, root, tag=tag)
    require(old != target, 'Predecessor and target must be distinct')
    stage('input-binding', passed=True)
    started = time.monotonic()
    stage('predecessor-install')
    # Only this harmless selection marker is added before running the unchanged
    # predecessor installer. The historical upgrade.sh is never called.
    (old / '.selected_edition').write_text('intl\n')
    install_result = temp / 'predecessor-install-result.json'
    predecessor_archive = next(iter(selected['files'].values()))['sha256']
    native_install(old, lock['version'], 'existing', install_result, predecessor_archive)
    installed = json_object(install_result.read_bytes())
    require(installed['installer_mode'] == selected['installer_mode'] and
            installed['regional_edition'] == 'intl', 'Predecessor installer/edition contract changed')
    stage('predecessor-install', passed=True)
    stage('predecessor-readiness-and-persistence')
    base = temp / 'native-panel-data'
    config = control_configuration(BIN / '1pctl')
    require(shlex.split(config['BASE_DIR']) == [str(base)], 'Unexpected predecessor data directory')
    token = 'native-upgrade:' + secrets.token_hex(16) + ':持久数据:quotes\'"'
    db_state = database_state(base, lock['version'], token)
    data = base / '1panel/native-upgrade-user-file.txt'
    data.write_text(token + '\n')
    data.chmod(0o600)
    data_sha = digest(data)
    docker = service_identity('docker')
    before = verify_live(old, lock['version'], config, db_state, data, data_sha, docker, base)
    resources = installed_resources(base)
    schema_before = database_evidence(base)
    stage('predecessor-readiness-and-persistence', passed=True)
    try:
        stage('rollback-fault-setup')
        with failure_probe(temp) as (marker, probe_facts):
            stage('rollback-fault-setup', passed=True)
            stage('rollback-mutation-observation')
            require(run_upgrade(target) != 0, 'Controlled real service-start failure unexpectedly succeeded')
            observation = json_object(marker.read_bytes())
            require(observation.get('core_sha256') == digest(target / '1panel-core') and
                    observation.get('versions') == [[[version]], [[version]]] and
                    observation.get('failure_count') == 1 and observation.get('successful_prestarts', 0) >= 1,
                    'Rollback probe did not observe actual target binary/database mutation')
            stage('rollback-mutation-observation', passed=True)
            stage('rollback-state')
            rolled_back = verify_live(old, lock['version'], config, db_state, data, data_sha, docker, base)
            require(installed_resources(base) == resources, 'Rollback did not restore all control/resource/service bytes')
            schema_rollback = database_evidence(base)
            require(schema_rollback == schema_before, 'Rollback database schema/version differs from predecessor')
            stage('rollback-state', passed=True)
        stage('successful-upgrade')
        require(run_upgrade(target) == 0, 'Real native predecessor upgrade failed')
        stage('successful-upgrade', passed=True)
        stage('target-readiness-and-persistence')
        after = verify_live(target, version, config, db_state, data, data_sha, docker, base)
        schema_after = database_evidence(base)
        require(digest(base / '1panel/geo/GeoIP.mmdb') == digest(target / 'GeoIP.mmdb'),
                'Upgraded resource differs from target')
        stage('target-readiness-and-persistence', passed=True)
        stage('acceptance-evidence')
        result.write_text(json.dumps({
            'schema': 1, 'status': 'passed', 'evidence_level': 'native-upgrade',
            'version': version, 'predecessor_version': lock['version'], 'source': source, 'architecture': arch,
            'regional_edition': 'intl', 'docker_scenario': 'existing', 'docker_process': docker,
            'target_archive_sha256': proof['target']['archive_sha256'],
            'target_receipt_sha256': proof['target']['receipt_sha256'],
            'target_run_id': proof['target']['workflow_run_id'],
            'target_run_attempt': proof['target']['workflow_run_attempt'],
            'target_commit': proof['target']['workflow_commit'],
            'predecessor_archive_sha256': predecessor_archive,
            **({'read_only_recovery': proof['read_only_recovery'], 'publication_eligible': False,
                'predecessor_candidate': lock['candidate']} if 'read_only_recovery' in proof else {
                'predecessor_release_id': lock['release_id'], 'predecessor_asset_id': lock['archive']['asset_id'],
                'predecessor_receipt_run': lock['receipt_run']}),
            'predecessor_acceptance': 'installed in this candidate run; no historical native acceptance claimed',
            'predecessor_install_result_sha256': digest(install_result),
            'input_provenance_sha256': digest(provenance),
            'predecessor_binding_sha256': proof['predecessor_binding_sha256'],
            'before_processes': before, 'rollback_processes': rolled_back, 'after_processes': after,
            'rollback': 'passed; one-shot real systemd core start failure after binary/database mutation',
            'rollback_fault_observation': observation, 'rollback_fault': probe_facts,
            'database_before': schema_before, 'database_after_rollback': schema_rollback,
            'database_after_upgrade': schema_after,
            'upgrade': 'passed; unchanged fixed target upgrade.sh',
            'database_integrity': 'core.db and agent.db passed before, after rollback and after upgrade',
            'database_user_rows_preserved': True, 'durable_settings_preserved': True,
            'user_configuration_preserved': True, 'persistent_user_file_preserved': True,
            'docker_unchanged': True, 'predecessor_installer_mode': installed['installer_mode'],
            'enterprise_original': 'not covered; passthrough remains byte-identical',
            'offline_network_isolation': 'not-enforced', 'runner_os': platform.platform(),
            'runner_arch': env.get('RUNNER_ARCH'), 'runner_name': env.get('RUNNER_NAME'),
            'runner_environment': env.get('RUNNER_ENVIRONMENT'),
            'elapsed_seconds': round(time.monotonic() - started, 3)}, indent=2) + '\n')
        stage('acceptance-evidence', passed=True)
    finally:
        for package in (old, target):
            for name in ('install.log', 'upgrade.log'):
                (package / name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['provenance', 'version', 'source', 'arch', 'result']:
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--tag', default='', help='Exact resolved release tag; defaults to version')
    args = parser.parse_args()
    try:
        native_upgrade(args.provenance, args.version, args.source, args.arch, args.result, tag=args.tag)
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError, sqlite3.Error):
        print('NATIVE_UPGRADE_STAGE=' + CURRENT_STAGE + ':failed', file=sys.stderr)
        print('Native upgrade acceptance failed; no release replacement is authorized.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
