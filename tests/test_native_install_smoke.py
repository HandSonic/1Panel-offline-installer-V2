"""Native harness guards and credential-safe configuration; no service execution."""
import os
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from native_install_smoke import (NoRedirect, disposable_guard, configuration,
                                  regional_edition, require_exact_version,
                                  service_identity, verify_regional_edition,
                                  wait_for_panel)

class NativeSmokeGuard(unittest.TestCase):
    def test_refuses_local_and_self_hosted_environments(self):
        for env in [{}, {'GITHUB_ACTIONS':'true','RUNNER_ENVIRONMENT':'self-hosted'}]:
            with self.assertRaises(ValueError):disposable_guard('/tmp/package',env,uid=0,machine='x86_64')
    def test_requires_root_native_arch_and_runner_temporary_package(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'package';p.mkdir()
            env={'GITHUB_ACTIONS':'true','RUNNER_ENVIRONMENT':'github-hosted','RUNNER_TEMP':td}
            self.assertEqual(disposable_guard(p,env,uid=0,machine='x86_64'),'amd64')
            self.assertEqual(disposable_guard(p,env,uid=0,machine='aarch64'),'arm64')
            for path,uid,machine in [(p,1000,'x86_64'),(p,0,'riscv64'),('/opt/package',0,'x86_64'),(td,0,'x86_64')]:
                with self.assertRaises(ValueError):disposable_guard(path,env,uid=uid,machine=machine)
    def test_cli_password_stays_out_of_command_and_tokens_out_of_child(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/'install.sh').write_text('NON_INTERACTIVE=false\nfunction parse_args() { :; }\n')
            with patch.dict(os.environ,{'GH_TOKEN':'not-a-real-token','GITHUB_TOKEN':'not-a-real-token','ACTIONS_RUNTIME_TOKEN':'not-a-real-token'}):
                args,answers,env,mode=configuration(p,p/'data',19876,'tester','synthetic_password','entrance')
            self.assertEqual(mode,'cli');self.assertEqual(answers,[])
            self.assertNotIn('synthetic_password',' '.join(args))
            self.assertEqual(env['PANEL_PASSWORD'],'synthetic_password')
            for key in ['GH_TOKEN','GITHUB_TOKEN','ACTIONS_RUNTIME_TOKEN']:self.assertNotIn(key,env)

    def test_legacy_prompt_meaning_comes_from_package_language(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/'install.sh').write_text('function main(){ :; }\n');(p/'lang').mkdir()
            keys=['TXT_SET_INSTALL_DIR','TXT_SET_PANEL_PORT','TXT_SET_PANEL_ENTRANCE','TXT_SET_PANEL_USER','TXT_SET_PANEL_PASSWORD']
            (p/'lang/en.sh').write_text('\n'.join(k+'="meaning '+str(i)+'"' for i,k in enumerate(keys)))
            args,answers,env,mode=configuration(p,p/'data',19876,'tester','synthetic_password','entrance')
            self.assertEqual(mode,'interactive');self.assertEqual(len(answers),5)
            self.assertEqual(answers[-1],('meaning 4','synthetic_password'))
            self.assertNotIn('synthetic_password',' '.join(args))


class NativeEvidenceChecks(unittest.TestCase):
    def test_exact_version_rejects_prereleases_builds_and_prefixes(self):
        require_exact_version('Version: v2.3.2\n', 'v2.3.2')
        require_exact_version('Version: v2.3.2-beta.1\n', 'v2.3.2-beta.1')
        for version in ['v2.3.20', 'v2.3.2-beta', 'v2.3.2+build', 'v2.3.2_other',
                        'not-v2.3.2', 'v2.3.1']:
            with self.subTest(version=version), self.assertRaises(ValueError):
                require_exact_version('Version: ' + version, 'v2.3.2')

    def test_identity_reads_running_executable_not_symlink_target(self):
        with patch('native_install_smoke.run', side_effect=['', '123']) as command, \
                patch('native_install_smoke.digest', return_value='a' * 64) as hashing:
            self.assertEqual(service_identity('1panel-core'),
                             {'pid': 123, 'binary_sha256': 'a' * 64})
        hashing.assert_called_once_with('/proc/123/exe')
        self.assertEqual(command.call_args.args[0][-1], '1panel-core')

    def test_identity_rejects_zero_missing_or_malformed_pid(self):
        for pid in ['0', '', 'abc', '-1']:
            with self.subTest(pid=pid), \
                    patch('native_install_smoke.run', side_effect=['', pid]), \
                    patch('native_install_smoke.digest') as hashing, \
                    self.assertRaises(ValueError):
                service_identity('docker')
            hashing.assert_not_called()

    def test_native_probe_waits_for_restart_and_is_direct_loopback(self):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'panel'
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch('native_install_smoke.run', side_effect=[
                subprocess.CalledProcessError(3, ['systemctl']), '', '']) as commands, \
                patch('native_install_smoke.urllib.request.build_opener', return_value=opener) as build, \
                patch('native_install_smoke.time.sleep'):
            wait_for_panel(19876)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], NoRedirect)
        opener.open.assert_called_once_with('http://127.0.0.1:19876/native_entry', timeout=2)
        self.assertTrue(all(0 < call.kwargs['timeout'] <= 2 for call in commands.call_args_list))

    def test_native_probe_rejects_empty_http_success(self):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b''
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch('native_install_smoke.run', return_value=''), \
                patch('native_install_smoke.urllib.request.build_opener', return_value=opener), \
                patch('native_install_smoke.time.monotonic', side_effect=[0, 0, 0, 0, 1]), \
                self.assertRaisesRegex(ValueError, 'did not become ready'):
            wait_for_panel(19876, timeout=1)
        response.read.assert_called_once_with(1024)

    def test_native_probe_refuses_redirect(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {},
                                                        'https://external.invalid/'))

    def test_package_regional_edition_is_preserved_separately_from_source(self):
        with tempfile.TemporaryDirectory() as td:
            package = Path(td)
            script = package / 'install.sh'
            script.write_text('EDITION_FILE=".selected_edition"\n')
            self.assertEqual(regional_edition(package), 'cn')
            (package / '.selected_edition').write_text('intl\n')
            self.assertEqual(regional_edition(package), 'intl')
            verify_regional_edition('intl', 'PANEL_EDITION=intl\n')
            with self.assertRaises(ValueError):
                verify_regional_edition('intl', 'PANEL_EDITION=cn\n')
            (package / '.selected_edition').write_text('unknown\n')
            with self.assertRaises(ValueError):
                regional_edition(package)
            script.write_text('legacy installer\n')
            self.assertEqual(regional_edition(package), 'legacy')
            verify_regional_edition('legacy', 'no regional setting\n')


class NativeWorkflowMatrix(unittest.TestCase):
    def test_pr_matrix_covers_only_reviewed_current_representatives(self):
        workflow = (ROOT / '.github/workflows/native-install-smoke.yml').read_text()
        match = re.search(r"fromJSON\('(\[\s+.*?\])'\)", workflow, re.S)
        self.assertIsNotNone(match)
        rows = json.loads(match[1])
        self.assertEqual({(r['source'], r['arch'], r['scenario']) for r in rows}, {
            ('custom', 'amd64', 'existing'), ('custom', 'amd64', 'fresh'),
            ('custom', 'arm64', 'existing'), ('official', 'amd64', 'existing'),
            ('enterprise-docker', 'amd64', 'existing')})
        self.assertEqual(len(rows), 5)
        for row in rows:
            self.assertRegex(row['sha256'], r'^[0-9a-f]{64}$')
        self.assertIn("VERSION: ${{ inputs.version || 'v2.3.2' }}", workflow)
        self.assertIn('max-parallel: 2', workflow)
        self.assertIn('fail-fast: false', workflow)
        self.assertIn('cancel-in-progress: false', workflow)
        self.assertIn("matrix.arch == 'arm64' && 'ubuntu-24.04-arm'", workflow)
        self.assertNotIn('contents: write', workflow)

    def test_manual_matrix_quotes_all_inputs_and_keeps_single_selected_row(self):
        workflow = (ROOT / '.github/workflows/native-install-smoke.yml').read_text()
        for name in ['source', 'arch', 'docker_scenario', 'expected_sha256']:
            self.assertIn('toJSON(inputs.' + name + ')', workflow)
        for name, field in [('SOURCE', 'source'), ('ARCH', 'arch'),
                            ('SCENARIO', 'scenario'), ('EXPECTED_SHA256', 'sha256')]:
            self.assertIn(name + ': ${{ matrix.' + field + ' }}', workflow)

if __name__=='__main__':unittest.main()
