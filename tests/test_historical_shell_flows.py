"""Execute historical configuration flows in a bounded PTY and temporary root.

These tests run the actual patched Bash argument/language/configuration functions.
They deliberately do NOT start daemons or establish native runtime coverage.
System probes are stubs; no install, firewall, or upgrade operation is invoked.
"""
import errno
import hashlib
import signal
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from patch_installer import patch
FIXTURES = ROOT / 'tests/fixtures/historical-installers'
ROWS = json.loads((FIXTURES / 'index.json').read_text())['fixtures']


def drive_pty(argv, answers, env, timeout=12):
    """Send one answer only after its expected semantic prompt is observed."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                            env=env, start_new_session=True)
    os.close(slave)
    pending = list(answers)
    output = b''
    consumed = 0
    deadline = time.monotonic() + timeout
    try:
        while True:
            if time.monotonic() >= deadline:
                raise AssertionError('Historical flow timed out; next prompt: ' +
                                     (pending[0][0] if pending else '<exit>'))
            readable, _, _ = select.select([master], [], [], .05)
            if readable:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b''
                if data:
                    output += data
                    if len(output) > 1024 * 1024:
                        raise AssertionError('Historical flow exceeded output limit')
                    if pending:
                        marker, answer = pending[0]
                        found = output.find(marker.encode(), consumed)
                        if found >= 0:
                            consumed = found + len(marker)
                            os.write(master, answer.encode() + b'\n')
                            pending.pop(0)
                elif proc.poll() is not None:
                    break
            elif proc.poll() is not None:
                break
        code = proc.wait(timeout=1)
        if pending:
            raise AssertionError('Exited before prompt: ' + pending[0][0])
        return code, output.decode(errors='replace')
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        os.close(master)


def configuration_flow(row, cli=False, bad_args=None, edition='intl', benign_comment=False):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        script = root / 'install.sh'
        original = (FIXTURES / row['file']).read_bytes()
        assert hashlib.sha256(original).hexdigest() == row['source_sha256']
        script.write_bytes(original)
        patch(script)
        source = script.read_text()
        # Immutable reviewed fixtures terminate in a single main invocation.
        source, count = re.subn(r'(?m)^main\s*$', '', source)
        assert count == 1
        if benign_comment:
            source += '\n# Harmless optional setting\nFUTURE_OPTION="preserved"\n'
        # Keep source argument parsing, language initialization and all selected
        # functions intact. Never invoke the destructive main entry point.
        source += '''
Set_Dir
Set_Port
Set_Entrance
Set_Username
Set_Password
printf '%s\\n' "$PANEL_BASE_DIR" "$PANEL_PORT" "$PANEL_ENTRANCE" "$PANEL_USERNAME" "$PANEL_PASSWORD" "${selected_edition:-legacy}" "$selected_lang" > "$RESULT"
'''
        stubs = '''
clear(){ :; }
which(){ if [[ "$1" == stty ]]; then return 1; fi; command -v "$@"; }
ss(){ :; }
netstat(){ :; }
lsof(){ return 1; }
'''
        script.write_text(stubs + source)
        (root / 'lang').mkdir()
        # Translation keys are the semantic interface, independent of wording.
        keys = sorted(set(re.findall(r'\bTXT_[A-Z0-9_]+\b', source)))
        (root / 'lang/en.sh').write_text('\n'.join(k + '=' + shlex.quote('PROMPT[' + k + ']') for k in keys))
        (root / '.selected_edition').write_text(edition)
        path = str(root / 'panel-data')
        values = [path, '19876', 'entry_123', 'tester_123', 'Pass_123456']
        env = {k: v for k, v in os.environ.items() if not k.startswith('PANEL_')}
        env.update(RESULT=str(root / 'result'), TERM='dumb')
        argv = ['bash', str(script)]
        answers = []
        if bad_args is not None:
            env['PANEL_INSTALL_DIR'] = path
            argv += bad_args
        elif cli:
            argv += ['--non-interactive', '--lang', 'en', '--install-dir', path,
                     '--port', values[1], '--entrance', values[2], '--username', values[3]]
            env['PANEL_PASSWORD'] = values[4]
        else:
            names = ['TXT_LANG_CHOICE_MSG', 'TXT_SET_INSTALL_DIR',
                     'TXT_SET_PANEL_PORT', 'TXT_SET_PANEL_ENTRANCE',
                     'TXT_SET_PANEL_USER', 'TXT_SET_PANEL_PASSWORD']
            answers = [('PROMPT[' + name + ']', answer)
                       for name, answer in zip(names, ['1'] + values)]
        code, output = drive_pty(argv, answers, env)
        result = (root / 'result').read_text().splitlines() if (root / 'result').exists() else None
        expected = values + [edition if row['edition_selection'] else 'legacy', 'en']
        return code, result, expected, output


class HistoricalConfigurationFlows(unittest.TestCase):
    def test_all_twelve_cohorts_accept_prompt_driven_configuration(self):
        for row in ROWS:
            with self.subTest(cohort=row['installer_commit']):
                code, result, expected, output = configuration_flow(row)
                self.assertEqual(code, 0)
                self.assertEqual(result, expected)

    def test_cli_only_for_cohorts_that_expose_it(self):
        for row in ROWS:
            if row['non_interactive_cli']:
                with self.subTest(cohort=row['installer_commit']):
                    code, result, expected, output = configuration_flow(row, cli=True)
                    self.assertEqual(code, 0)
                    self.assertEqual(result, expected)
                    self.assertNotIn('PROMPT[TXT_SET_PANEL_PASSWORD]', output)

    def test_cli_rejects_unknown_options_and_missing_values(self):
        for row in ROWS:
            if row['non_interactive_cli']:
                for args in [['--not-a-real-option'], ['--port']]:
                    with self.subTest(cohort=row['installer_commit'], args=args):
                        code, result, _, _ = configuration_flow(row, bad_args=args)
                        self.assertNotEqual(code, 0)
                        self.assertIsNone(result)

    def test_cli_rejects_invalid_configuration(self):
        for row in ROWS:
            if row['non_interactive_cli']:
                for option, value in [('--port', '70000'), ('--install-dir', 'relative'),
                                      ('--entrance', 'x'), ('--username', 'x'),
                                      ('--password', 'short')]:
                    with self.subTest(cohort=row['installer_commit'], option=option):
                        code, result, _, _ = configuration_flow(row, bad_args=[
                            '--non-interactive', '--lang', 'en', option, value])
                        self.assertNotEqual(code, 0)
                        self.assertIsNone(result)

    def test_main_keeps_historical_order_and_appstore_reachability(self):
        for row in ROWS:
            with self.subTest(cohort=row['installer_commit']):
                text = (FIXTURES / row['file']).read_text()
                match = re.search(r'(?ms)^function main\s*\(\)\s*\{(.*?)^}', text)
                self.assertIsNotNone(match)
                calls = re.findall(r'^\s*([A-Za-z_][A-Za-z_0-9]*)\s*$', match[1], re.M)
                expected = ['Check_Root', 'Prepare_System', 'Set_Dir', 'Install_Docker',
                            'Set_Port', 'Set_Firewall', 'Set_Entrance', 'Set_Username',
                            'Set_Password', 'Init_Panel']
                if row['appstore_install']:
                    expected += ['Install_AppStore']
                expected += ['Get_Ip', 'Check_Ready', 'Show_Result']
                stubs = '\n'.join(name + '(){ echo ' + name + '; }' for name in calls)
                result = subprocess.run(['bash', '-c', stubs + '\n' + match[0] + '\nmain'],
                                        capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), expected)

    def test_appstore_cohort_copies_its_own_archive_and_skips_absent_optional_file(self):
        for row in ROWS:
            if not row['appstore_install']:
                continue
            text = (FIXTURES / row['file']).read_text()
            function = re.search(r'(?ms)^function Install_AppStore\s*\(\)\s*\{.*?^}', text)[0]
            with self.subTest(cohort=row['installer_commit']), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                package = root / 'package'; package.mkdir()
                runtime = root / 'runtime'
                env = dict(os.environ, CURRENT_DIR=str(package), RUN_BASE_DIR=str(runtime))
                command = ['bash', '-c', 'log(){ :; }\n' + function + '\nInstall_AppStore']
                result = subprocess.run(command, env=env, capture_output=True, timeout=3)
                self.assertEqual(result.returncode, 0)
                self.assertFalse(runtime.exists())
                payload = b'opaque cohort-owned resource, no extraction by installer'
                (package / 'appstore.tar.gz').write_bytes(payload)
                result = subprocess.run(command, env=env, capture_output=True, timeout=3)
                self.assertEqual(result.returncode, 0)
                self.assertEqual((runtime / 'resource/offline/appstore.tar.gz').read_bytes(), payload)

    def test_editions_and_comments_do_not_change_configuration(self):
        for row in ROWS:
            if row['edition_selection']:
                with self.subTest(cohort=row['installer_commit']):
                    code, result, expected, _ = configuration_flow(row, cli=row['non_interactive_cli'], edition='cn', benign_comment=True)
                    self.assertEqual(code, 0)
                    self.assertEqual(result, expected)


if __name__ == '__main__':
    unittest.main()
