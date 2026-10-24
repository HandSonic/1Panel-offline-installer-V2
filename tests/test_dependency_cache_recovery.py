"""Exercise dependency cache recovery through the real shell acquisition path."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from test_offline import archive, binary

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'prepare_offline.sh').read_text()
HELPERS = SOURCE[SOURCE.index('download_if_missing()'):SOURCE.index('patch_install_script()')]
DEPENDENCIES = SOURCE[SOURCE.index('    local docker_tgz=""'):SOURCE.index('    if ! tar -tf "${app_tar}"')]
# Some local sandboxes lack /dev/fd. Keep both real pin readers and their
# callers, replacing only process substitution with equivalent captured input.
DEPENDENCIES = DEPENDENCIES.replace('< <(python3', '<<< "$(python3').replace('PYLOCK\n    )', 'PYLOCK\n    )"')


class DependencyCacheRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        (self.work / 'scripts').symlink_to(ROOT / 'scripts', target_is_directory=True)
        self.cache = self.work / 'cache'
        self.responses = self.work / 'responses'
        self.bin = self.work / 'bin'
        for directory in (self.cache, self.responses, self.bin):
            directory.mkdir()
        archive(self.responses / 'docker', 'amd64')
        (self.responses / 'compose').write_bytes(binary('amd64'))
        self.good = {name: (self.responses / name).read_bytes() for name in ('docker', 'compose')}
        self.urls = {name: f'https://fixture/{name}/1.0' for name in self.good}
        self.paths = {'docker': self.cache / 'docker-1.0-x86_64.tgz',
                      'compose': self.cache / 'docker-compose-1.0-x86_64'}
        for name, raw in self.good.items():
            pin = {'version': '1.0', 'url': self.urls[name], 'sha256': self.sha(raw)}
            (self.work / f'{name}-sources.json').write_text(json.dumps({'amd64': pin}))
        fake = self.bin / 'curl'
        fake.write_text('''#!/bin/bash
set -eu
while [[ $# -gt 0 ]]; do
    case "$1" in
        https://fixture/*) url="$1"; shift ;;
        -o) destination="$2"; shift 2 ;;
        *) shift ;;
    esac
done
name="${url#https://fixture/}"
name="${name%%/*}"
printf '%s\\n' "$name" >> "$REQUEST_LOG"
cp "$RESPONSE_DIR/$name" "$destination"
''')
        fake.chmod(0o755)
        self.log = self.work / 'requests'

    @staticmethod
    def sha(raw):
        return hashlib.sha256(raw).hexdigest()

    def seed(self, name, raw):
        path = self.paths[name]
        path.write_bytes(raw)
        Path(str(path) + '.source.json').write_text(json.dumps({
            'url': self.urls[name], 'bytes': len(raw), 'sha256': self.sha(raw)}))

    def wrong_pin(self, name):
        raw = bytearray(self.good[name])
        # Both remain valid payloads for amd64; only their pinned bytes differ.
        raw[4 if name == 'docker' else -1] ^= 1
        return bytes(raw)

    def wrong_arch(self, name):
        if name == 'compose':
            return binary('arm64')
        path = self.work / 'arm64.tgz'
        archive(path, 'arm64')
        return path.read_bytes()

    def run_dependencies(self, *, overrides=False, allow_missing=False):
        script = 'set -euo pipefail\n' + '\n'.join((
            'BASE_DIR=' + shlex.quote(str(self.work)),
            'CACHE_DIR=' + shlex.quote(str(self.cache)),
            'MIN_COMPOSE_SIZE=64', 'ALLOW_MISSING=' + str(allow_missing).lower(),
            'declare -a SKIPPED_ARCHES=()',
            'DOCKER_VERSION=' + ('2.0' if overrides else ''),
            'COMPOSE_VERSION=' + ('2.0' if overrides else ''),
            'APP_ARCH=amd64', 'DOCKER_ARCH=x86_64', 'COMPOSE_ARCH=x86_64',
            'arch=amd64', 'source_label=official', HELPERS,
            'build_dependencies() {\n' + DEPENDENCIES + '\necho DEPENDENCIES_READY\n}',
            'build_dependencies'))
        environment = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'],
                           RESPONSE_DIR=str(self.responses), REQUEST_LOG=str(self.log))
        return subprocess.run(['bash', '-c', script], env=environment,
                              capture_output=True, text=True, timeout=30)

    def requests(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def assert_admitted(self, name):
        path = self.paths[name]
        self.assertEqual(path.read_bytes(), self.good[name])
        self.assertEqual(json.loads(Path(str(path) + '.source.json').read_text()), {
            'url': self.urls[name], 'bytes': len(self.good[name]), 'sha256': self.sha(self.good[name])})
        self.assertFalse(Path(str(path) + '.part').exists())

    def test_rejected_download_is_not_cached_and_next_build_recovers(self):
        for name in self.good:
            with self.subTest(component=name):
                for component, raw in self.good.items():
                    (self.responses / component).write_bytes(raw)
                    for suffix in ('', '.source.json'):
                        Path(str(self.paths[component]) + suffix).unlink(missing_ok=True)
                (self.responses / name).write_bytes(self.wrong_pin(name))
                result = self.run_dependencies()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                for suffix in ('', '.part', '.source.json'):
                    self.assertFalse(Path(str(self.paths[name]) + suffix).exists())
                (self.responses / name).write_bytes(self.good[name])
                result = self.run_dependencies()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_admitted(name)

    def test_restored_wrong_pin_cache_redownloads_in_same_build(self):
        for name in self.good:
            self.seed(name, self.wrong_pin(name))
        result = self.run_dependencies()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.requests(), ['docker', 'compose'])
        for name in self.good:
            self.assert_admitted(name)

    def test_valid_cache_requires_no_network(self):
        for name, raw in self.good.items():
            self.seed(name, raw)
        result = self.run_dependencies()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.requests(), [])

    def test_matching_pin_does_not_bypass_architecture_validation(self):
        for name in self.good:
            with self.subTest(component=name):
                raw = self.wrong_arch(name)
                pin_path = self.work / f'{name}-sources.json'
                pin = json.loads(pin_path.read_text())
                original = pin_path.read_bytes()
                pin['amd64']['sha256'] = self.sha(raw)
                pin_path.write_text(json.dumps(pin))
                self.seed(name, raw)
                (self.responses / name).write_bytes(raw)
                result = self.run_dependencies()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                for suffix in ('', '.part', '.source.json'):
                    self.assertFalse(Path(str(self.paths[name]) + suffix).exists())
                pin_path.write_bytes(original)
                (self.responses / name).write_bytes(self.good[name])

    def test_allow_missing_still_skips_only_the_failed_product(self):
        (self.responses / 'compose').write_bytes(self.wrong_pin('compose'))
        result = self.run_dependencies(allow_missing=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Skip official/amd64', result.stdout)
        self.assertNotIn('DEPENDENCIES_READY', result.stdout)
        self.assert_admitted('docker')
        for suffix in ('', '.part', '.source.json'):
            self.assertFalse(Path(str(self.paths['compose']) + suffix).exists())

    def test_matching_pin_does_not_bypass_compose_minimum_size(self):
        raw = binary('amd64')[:20]
        pin_path = self.work / 'compose-sources.json'
        pin = json.loads(pin_path.read_text())
        pin['amd64']['sha256'] = self.sha(raw)
        pin_path.write_text(json.dumps(pin))
        self.seed('compose', raw)
        (self.responses / 'compose').write_bytes(raw)
        result = self.run_dependencies()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        for suffix in ('', '.part', '.source.json'):
            self.assertFalse(Path(str(self.paths['compose']) + suffix).exists())

    def test_explicit_version_still_checks_architecture_without_pin(self):
        result = self.run_dependencies(overrides=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (self.responses / 'docker').write_bytes(self.wrong_arch('docker'))
        cached = self.cache / 'docker-2.0-x86_64.tgz'
        raw = self.wrong_arch('docker')
        cached.write_bytes(raw)
        Path(str(cached) + '.source.json').write_text(json.dumps({
            'url': 'https://fixture/docker/2.0', 'bytes': len(raw), 'sha256': self.sha(raw)}))
        result = self.run_dependencies(overrides=True)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(cached.exists())
        self.assertFalse(Path(str(cached) + '.source.json').exists())


if __name__ == '__main__':
    unittest.main()
