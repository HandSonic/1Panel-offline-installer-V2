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
                                  appstore_payload, native_install,
                                  regional_edition, require_exact_version,
                                  service_identity, verify_regional_edition,
                                  verify_installed_appstore,
                                  wait_for_panel)

class NativeSmokeGuard(unittest.TestCase):
    def test_historical_installer_requires_exact_bound_bytes_without_current_helpers(self):
        from test_native_upgrade import IndependentArchiveTests
        import native_upgrade_input as binder
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive, binding, source, proof, body = IndependentArchiveTests().fixture(root)
            package, selected = binder.unpack_predecessor(archive, root / 'out', binding,
                'official', 'amd64', source, proof, root)
            expected = selected['configuration']['installer_sha256']
            with patch('native_install_smoke.disposable_guard', return_value='amd64'), \
                    patch('native_install_smoke.run', side_effect=RuntimeError('Reached native execution boundary')) as execute, \
                    patch('patch_installer.HELPERS', 'a different current helper implementation'):
                with self.assertRaisesRegex(RuntimeError, 'execution boundary'):
                    native_install(package, binding['version'], 'existing', root / 'result', 'a' * 64,
                                   predecessor_installer_sha256=expected)
                execute.assert_called_once_with(['systemctl', 'show-environment'])
                execute.reset_mock()
                for selected_hash in (None, '0' * 64):
                    with self.subTest(selected_hash=selected_hash), self.assertRaises(ValueError):
                        native_install(package, binding['version'], 'existing', root / 'result', 'a' * 64,
                                       predecessor_installer_sha256=selected_hash)
                (package / 'install.sh').write_bytes(body['install.sh'] + b'\n# Changed after binding\n')
                with self.assertRaisesRegex(ValueError, 'Historical installer differs'):
                    native_install(package, binding['version'], 'existing', root / 'result', 'a' * 64,
                                   predecessor_installer_sha256=expected)
                execute.assert_not_called()

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
    def test_runtime_matrix_covers_available_flavors_and_both_docker_scenarios(self):
        import itertools
        from test_runtime_contract import runtime
        for enterprise in (False,True):
            value=runtime(version='v2.99.0',enterprise=enterprise)
            sources=['custom','official']+(['enterprise-docker'] if enterprise else [])
            self.assertEqual({(r['source'],r['arch'],r['scenario']) for r in value['inventory']['native_rows']},
                set(itertools.product(sources,['amd64','arm64'],['fresh','existing'])))
            self.assertNotIn('enterprise-original',{r['source'] for r in value['inventory']['native_rows']})


class NativeAppStoreChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / 'package'
        self.package.mkdir()
        self.base = self.root / 'native-panel-data'
        self.source = self.package / 'appstore.tar.gz'
        self.source.write_bytes(b'exact AppStore payload')
        self.expected = appstore_payload(self.package)
        self.target = self.base / '1panel/resource/offline/appstore.tar.gz'

    def install_resource(self, raw=None):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_bytes(self.source.read_bytes() if raw is None else raw)

    def test_exact_resource_passes_but_missing_truncated_or_changed_bytes_fail(self):
        with self.assertRaisesRegex(ValueError, 'missing or unsafe'):
            verify_installed_appstore(self.base, self.expected)
        self.install_resource()
        verify_installed_appstore(self.base, self.expected)
        for raw in (b'truncated', b'x' * self.expected['bytes']):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, 'bytes differ'):
                self.target.write_bytes(raw)
                verify_installed_appstore(self.base, self.expected)

    def test_optional_absence_does_not_require_an_installed_resource(self):
        self.source.unlink()
        self.assertIsNone(appstore_payload(self.package))
        verify_installed_appstore(self.base, None)

    def test_source_must_be_a_nonempty_regular_file_without_symlink(self):
        self.source.write_bytes(b'')
        with self.assertRaises(ValueError):
            appstore_payload(self.package)
        self.source.unlink()
        self.source.mkdir()
        with self.assertRaises(ValueError):
            appstore_payload(self.package)
        self.source.rmdir()
        for destination in (self.root / 'missing', self.root / 'external'):
            if destination.name == 'external':
                destination.write_bytes(b'external resource')
            self.source.symlink_to(destination)
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                appstore_payload(self.package)
            self.source.unlink()

    def test_installed_file_and_parent_symlinks_are_rejected(self):
        self.target.parent.mkdir(parents=True)
        self.target.symlink_to(self.source)
        with self.assertRaisesRegex(ValueError, 'missing or unsafe'):
            verify_installed_appstore(self.base, self.expected)
        self.target.unlink()
        self.target.parent.rmdir()
        self.target.parent.symlink_to(self.package, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'missing or unsafe'):
            verify_installed_appstore(self.base, self.expected)

    def test_native_install_rejects_silent_copy_failure_and_uses_preinstall_pin(self):
        from patch_installer import HELPERS
        (self.package / 'install.sh').write_text(HELPERS)
        # Community manifests omit AppStore; the exact candidate archive supplies it.
        (self.package / 'offline-manifest.json').write_text(json.dumps({
            'source': 'official', 'app_version': 'v2.4.1', 'architecture': 'amd64',
            'payloads': {},
        }))
        result = self.root / 'result.json'
        original_exists = Path.exists
        def exists(path):
            if str(path) in ('/usr/local/bin/1panel-core', '/usr/local/bin/1panel-agent'):
                return False
            return original_exists(path)
        for mutation in ('missing', 'changed-during-install'):
            self.source.write_bytes(b'exact AppStore payload')
            def install(*args):
                if mutation == 'changed-during-install':
                    self.source.write_bytes(b'x' * self.expected['bytes'])
                    self.install_resource()
            with self.subTest(mutation=mutation), \
                    patch.dict(os.environ, RUNNER_TEMP=str(self.root)), \
                    patch('native_install_smoke.disposable_guard', return_value='amd64'), \
                    patch('validate_payload.directory_payloads', return_value=[]), \
                    patch.object(Path, 'exists', exists), \
                    patch('native_install_smoke.run', return_value='27.0.1'), \
                    patch('native_install_smoke.service_identity', return_value={'pid': 1}), \
                    patch('native_install_smoke.configuration', return_value=([], [], {}, 'cli')), \
                    patch('native_install_smoke.expect_install', side_effect=install) as installer, \
                    patch('native_install_smoke.wait_for_panel') as ready, \
                    self.assertRaisesRegex(ValueError, 'Installed AppStore'):
                native_install(self.package, 'v2.4.1', 'existing', result, 'a' * 64)
            installer.assert_called_once()
            ready.assert_called_once()
            self.assertFalse(result.exists())


if __name__=='__main__':unittest.main()
