#!/usr/bin/env python3
"""Real install smoke test; only for disposable GitHub-hosted native Linux VMs.

Input is an already validated, extracted offline package. This installs real
services on the VM. It refuses local/self-hosted/production execution. Results
are scoped to this exact package, runner architecture and Docker scenario.
"""
import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import pty
import re
import secrets
import select
import shlex
import signal
import subprocess
import termios
import time
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def run(argv, **kw):
    return subprocess.run(argv, check=True, capture_output=True, text=True,
                          timeout=kw.pop('timeout', 60), **kw).stdout.strip()


def disposable_guard(package, env=os.environ, uid=None, machine=None):
    uid = os.geteuid() if uid is None else uid
    machine = platform.machine() if machine is None else machine
    if uid != 0 or env.get('GITHUB_ACTIONS') != 'true' or env.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise ValueError('Real install tests require root on a disposable GitHub-hosted runner')
    temp = Path(env.get('RUNNER_TEMP', '/missing')).resolve()
    package = Path(package).resolve()
    if not temp.is_dir() or not package.is_relative_to(temp) or package == temp:
        raise ValueError('Validated package must be inside this runner temporary directory')
    if machine not in ['x86_64', 'aarch64']:
        raise ValueError('No reviewed native runner mapping for this architecture')
    return 'amd64' if machine == 'x86_64' else 'arm64'


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def require_exact_version(output, expected):
    # A stable release must not pass with a prerelease/build suffix or prefix.
    if not re.search(r'(?<![A-Za-z0-9_.+-])' + re.escape(expected) +
                     r'(?![A-Za-z0-9_.+-])', output):
        raise ValueError('Running panel version does not match requested version')


def service_identity(name):
    run(['systemctl', 'is-active', '--quiet', name])
    pid = run(['systemctl', 'show', '--property=MainPID', '--value', name])
    if not pid.isdecimal() or int(pid) <= 0:
        raise ValueError('Active service does not expose a live main process')
    return {'pid': int(pid), 'binary_sha256': digest('/proc/' + pid + '/exe')}


def wait_for_panel(port, timeout=60):
    # Historical Check_Ready may restart both services just before returning.
    # Never let proxy variables or a redirect turn this into an external probe.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + timeout
    def remaining_timeout():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Readiness deadline reached')
        return min(2, remaining)
    while True:
        try:
            for name in ['1panel-core', '1panel-agent']:
                run(['systemctl', 'is-active', '--quiet', name], timeout=remaining_timeout())
            with opener.open('http://127.0.0.1:' + str(port) + '/native_entry', timeout=remaining_timeout()) as response:
                if response.status == 200 and response.read(1024):
                    return
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        if time.monotonic() >= deadline:
            raise ValueError('Local panel services/UI did not become ready')
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def regional_edition(package):
    if '.selected_edition' not in (package / 'install.sh').read_text():
        return 'legacy'
    selected = package / '.selected_edition'
    value = selected.read_text().strip() if selected.exists() else 'cn'
    if value not in ['cn', 'intl']:
        raise ValueError('Unsupported package regional edition')
    return value


def verify_regional_edition(expected, installed_ctl):
    if expected != 'legacy' and not re.search(
            r'^PANEL_EDITION=' + re.escape(expected) + r'\s*$', installed_ctl, re.M):
        raise ValueError('Installed regional edition differs from package selection')


def expect_install(argv, answers, env, cwd, timeout=240):
    """Never echo installer output: historical installers display credentials."""
    master, slave = pty.openpty()
    def terminal():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                            env=env, cwd=cwd, preexec_fn=terminal)
    os.close(slave)
    pending = list(answers)
    output = b''
    position = 0
    deadline = time.monotonic() + timeout
    try:
        while True:
            if time.monotonic() >= deadline:
                raise ValueError('Installer timeout; remaining prompt count: ' + str(len(pending)))
            ready, _, _ = select.select([master], [], [], .1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b''
                if chunk:
                    output += chunk
                    if len(output) > 2 * 1024 * 1024:
                        raise ValueError('Installer output limit exceeded')
                    if pending:
                        prompt, answer = pending[0]
                        found = output.find(prompt.encode(), position)
                        if found >= 0:
                            position = found + len(prompt)
                            os.write(master, answer.encode() + b'\n')
                            pending.pop(0)
                elif proc.poll() is not None:
                    break
            elif proc.poll() is not None:
                break
        if proc.wait() != 0 or pending:
            raise ValueError('Installer failed or exited before configured prompts completed')
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        os.close(master)


def configuration(package, base, port, username, password, entrance):
    text = (package / 'install.sh').read_text()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('PANEL_', 'GH_', 'GITHUB_TOKEN', 'ACTIONS_RUNTIME_TOKEN'))}
    env['PATH'] = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    (package / '.selected_language').write_text('en\n')
    cli = bool(re.search(r'^function parse_args\s*\(', text, re.M)) and 'NON_INTERACTIVE=' in text
    args = ['bash', str(package / 'install.sh')]
    answers = []
    if cli:
        args += ['--non-interactive', '--lang', 'en', '--install-dir', str(base),
                 '--port', str(port), '--entrance', entrance, '--username', username]
        env['PANEL_PASSWORD'] = password
    else:
        keys = ['TXT_SET_INSTALL_DIR', 'TXT_SET_PANEL_PORT', 'TXT_SET_PANEL_ENTRANCE',
                'TXT_SET_PANEL_USER', 'TXT_SET_PANEL_PASSWORD']
        # The package's own reviewed English translation defines prompt meaning.
        values = run(['bash', '-c', 'source "$1"; shift; for key; do printf "%s\\0" "${!key}"; done',
                      'prompts', str(package / 'lang/en.sh'), *keys]).split('\0')
        if len(values) != 6 or values[-1] != '' or any(not value for value in values[:-1]):
            raise ValueError('Package does not expose required semantic prompts')
        answers = list(zip(values[:-1], [str(base), str(port), entrance, username, password]))
    return args, answers, env, 'cli' if cli else 'interactive'


def native_install(package, expected_version, scenario, result_path, archive_sha256):
    package = Path(package).resolve()
    arch = disposable_guard(package)
    if scenario not in ['fresh', 'existing']:
        raise ValueError('Unsupported Docker scenario')
    if not re.fullmatch(r'[0-9a-f]{64}', archive_sha256):
        raise ValueError('Verified archive SHA-256 is required for evidence binding')
    manifest = json.loads((package / 'offline-manifest.json').read_text())
    if manifest.get('app_version') != expected_version or manifest.get('architecture') != arch:
        raise ValueError('Package version/native architecture mismatch')
    if manifest.get('source') not in ['official', 'custom', 'enterprise-docker']:
        raise ValueError('Unsupported native installer source')
    if manifest['source'] in ('official', 'custom'):
        from validate_payload import directory_payloads
        if set(directory_payloads(package)) != set(manifest['payloads']):
            raise ValueError('Native payload inventory differs from installer service layout')
    from patch_installer import HELPERS
    if HELPERS not in (package / 'install.sh').read_text():
        raise ValueError('Package does not contain the reviewed offline Docker implementation')
    for name, facts in manifest['payloads'].items():
        path = package / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(package):
            raise ValueError('Unsafe or missing payload')
        if path.stat().st_size != facts['bytes'] or digest(path) != facts['sha256']:
            raise ValueError('Package payload differs from validated manifest')
    run(['systemctl', 'show-environment'])
    selected_edition = regional_edition(package)
    for name in ['1panel-core', '1panel-agent']:
        if Path('/usr/local/bin/' + name).exists():
            raise ValueError('Runner already has panel installed')
    docker_before = None
    docker_identity_before = None
    if scenario == 'existing':
        docker_before = run(['docker', '--host', 'unix:///var/run/docker.sock', 'version', '--format', '{{.Server.Version}}'])
        docker_identity_before = service_identity('docker')
    else:
        # Preserve runner Docker entrypoints, never modify a user's machine.
        for unit in ['docker.socket', 'docker.service']:
            probe = subprocess.run(['systemctl', 'show', '--property=LoadState', '--value', unit],
                                   capture_output=True, text=True, timeout=30)
            if probe.stdout.strip() != 'not-found':
                probe.check_returncode()
                run(['systemctl', 'stop', unit])
        saved = Path(os.environ['RUNNER_TEMP']) / 'native-docker-entrypoints'
        saved.mkdir(exist_ok=False)
        for location in ['/usr/bin/docker', '/usr/local/bin/docker']:
            path = Path(location)
            if path.exists() or path.is_symlink():
                path.rename(saved / location.replace('/', '_'))
        probe = subprocess.run(['bash', '-c', 'command -v docker'], capture_output=True)
        if probe.returncode == 0:
            raise ValueError('Unexpected Docker CLI remains on fresh-install runner')
    base = Path(os.environ['RUNNER_TEMP']) / 'native-panel-data'
    port = 19876
    password = 'Test_' + secrets.token_hex(10)
    args, answers, env, mode = configuration(package, base, port, 'native_test', password, 'native_entry')
    start = time.monotonic()
    try:
        expect_install(args, answers, env, package)
        wait_for_panel(port)
        for name in ['1panel-core', '1panel-agent', '1pctl']:
            installed = Path('/usr/local/bin') / name
            if not installed.is_file():
                raise ValueError('Installer did not install required component')
            if name != '1pctl' and digest(installed) != digest(package / name):
                raise ValueError('Installed application bytes differ from exact package')
        panel_processes = {}
        for name in ['1panel-core', '1panel-agent']:
            panel_processes[name] = service_identity(name)
            if panel_processes[name]['binary_sha256'] != digest(package / name):
                raise ValueError('Running service bytes differ from exact package')
        verify_regional_edition(selected_edition, Path('/usr/local/bin/1pctl').read_text())
        version = run(['/usr/local/bin/1pctl', 'version'])
        require_exact_version(version, expected_version)
        daemon = run(['docker', '--host', 'unix:///var/run/docker.sock', 'version', '--format', '{{.Server.Version}}'])
        docker_identity = service_identity('docker')
        if scenario == 'fresh' and daemon != manifest['inputs']['docker']['version']:
            raise ValueError('Fresh Docker daemon version differs from bundled payload')
        if scenario == 'fresh':
            for name, facts in manifest['docker_binaries'].items():
                installed = Path('/usr/local/bin') / Path(name).name
                if digest(installed) != facts['sha256']:
                    raise ValueError('Installed Docker bytes differ from bundled payload')
            if docker_identity['binary_sha256'] != manifest['docker_binaries']['docker/dockerd']['sha256']:
                raise ValueError('Running Docker daemon differs from bundled payload')
        if scenario == 'existing' and (daemon != docker_before or docker_identity != docker_identity_before):
            raise ValueError('Existing Docker daemon unexpectedly replaced or restarted')
        compose = run(['docker', 'compose', 'version', '--short'])
        if compose.removeprefix('v') != manifest['inputs']['compose']['version'].removeprefix('v'):
            raise ValueError('Actual Compose version differs from bundled payload')
        compose_path = '/usr/local/lib/docker/cli-plugins/docker-compose'
        if digest(compose_path) != digest(package / 'docker-compose'):
            raise ValueError('Installed Compose bytes differ from package')
        bundled_compose = run([compose_path, 'version', '--short'])
        if bundled_compose.removeprefix('v') != compose.removeprefix('v'):
            raise ValueError('Bundled Compose execution differs from Docker plugin integration')
        result = {'schema': 1, 'status': 'passed', 'evidence_level': 'native-install',
                  'version': expected_version, 'architecture': arch, 'source': manifest['source'],
                  'archive_sha256': archive_sha256,
                  'manifest_sha256': digest(package / 'offline-manifest.json'),
                  'docker_scenario': scenario, 'docker_version': daemon, 'compose_version': compose,
                  'installer_mode': mode, 'runner_os': platform.platform(),
                  'regional_edition': selected_edition, 'installer_language': 'en',
                  'panel_processes': panel_processes, 'docker_process': docker_identity,
                  'agent_version_command': 'not-tested; running binary hash verified',
                  'elapsed_seconds': round(time.monotonic() - start, 3),
                  'upgrade': 'not-tested', 'rollback': 'not-tested',
                  'offline_network_isolation': 'not-enforced'}
        Path(result_path).write_text(json.dumps(result, indent=2) + '\n')
    finally:
        # Do not persist logs that might contain generated panel credentials.
        (package / 'install.log').unlink(missing_ok=True)
        (package / 'upgrade.log').unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--docker-scenario', choices=['fresh', 'existing'], required=True)
    parser.add_argument('--result', required=True)
    parser.add_argument('--archive-sha256', required=True)
    args = parser.parse_args()
    native_install(args.package, args.version, args.docker_scenario, args.result, args.archive_sha256)
