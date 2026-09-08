import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tarfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for source, target in [("install-offline.sh", "install.sh"), ("offline-env.sh", "offline-env.sh")]:
            shutil.copy(ROOT / "scripts" / source, self.root / target)

    def run_bash(self, code):
        return subprocess.run(["bash", "-c", code, "test", str(self.root)], text=True, capture_output=True)

    def test_changed_upstream_functions_and_argument_passthrough(self):
        # No dependency on function names, whitespace, prompts, or upstream layout.
        (self.root / "install-upstream.sh").write_text('''#!/bin/bash
function NewInstallEntry () {
    printf 'args:%s:%s\\n' "$1" "$2"
    if curl https://example.invalid; then exit 91; fi
    if wget https://example.invalid; then exit 92; fi
}
NewInstallEntry "$@"
exit 17
''')
        result = self.run_bash('source "$1/install.sh"; require_root() { :; }; ensure_docker() { :; }; ensure_compose() { :; }; main "--config" "a b"')
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertIn('args:--config:a b', result.stdout)

    def test_help_never_prepares_dependencies(self):
        (self.root / "install-upstream.sh").write_text('printf "help:%s\\n" "$1"\n')
        result = self.run_bash('source "$1/install.sh"; ensure_docker() { exit 99; }; ensure_compose() { exit 98; }; main --help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('help:--help', result.stdout)

    def test_healthy_existing_engine_and_compose_are_preserved(self):
        result = self.run_bash('source "$1/install.sh"; docker() { return 0; }; install() { exit 99; }; start_docker() { exit 98; }; ensure_docker; ensure_compose')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Reusing', result.stdout)

    def test_existing_broken_engine_is_not_overwritten(self):
        result = self.run_bash('source "$1/install.sh"; docker() { return 1; }; start_docker() { return 1; }; wait_for_docker() { return 1; }; install() { exit 99; }; ensure_docker')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('preserved its binaries', result.stderr)

    def test_community_docker_archive_directory_is_discovered(self):
        directory = self.root / 'vendor-docker-99.1.0'
        directory.mkdir()
        for name in ['docker', 'dockerd']:
            file = directory / name
            file.write_text('#!/bin/sh\nexit 0\n')
            file.chmod(0o755)
        with tarfile.open(self.root / 'docker.tgz', 'w:gz') as archive:
            archive.add(directory, arcname=directory.name)
        launcher = self.root / 'install.sh'
        launcher.write_text(launcher.read_text().replace('/usr/local/bin', str(self.root / 'bin')))
        result = self.run_bash('export TMPDIR="$1"; source "$1/install.sh"; command() { if [[ "$*" == "-v docker" ]]; then return 1; fi; builtin command "$@"; }; install_docker_service() { :; }; start_docker() { :; }; wait_for_docker() { :; }; ensure_docker; cleanup')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'bin/docker').is_file())

    def test_ambiguous_docker_directories_stop_before_copy(self):
        with tarfile.open(self.root / 'docker.tgz', 'w:gz') as archive:
            for parent in ['one', 'two']:
                directory = self.root / parent
                directory.mkdir()
                for name in ['docker', 'dockerd']:
                    file = directory / name
                    file.write_text('#!/bin/sh\nexit 0\n')
                    file.chmod(0o755)
                archive.add(directory, arcname=parent)
        result = self.run_bash('export TMPDIR="$1"; source "$1/install.sh"; command() { if [[ "$*" == "-v docker" ]]; then return 1; fi; builtin command "$@"; }; install() { exit 99; }; trap cleanup EXIT; ensure_docker')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('multiple client/daemon', result.stderr)


if __name__ == '__main__':
    unittest.main()
