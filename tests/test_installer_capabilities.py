"""Existing historical adapters recognize interfaces, not a future version list."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from installer_capabilities import inspect_installer, require_argument_parser_call

FIXTURES = ROOT / 'tests/fixtures/historical-installers'


class InstallerCapabilitiesTests(unittest.TestCase):
    def fixtures(self):
        for row in json.loads((FIXTURES / 'index.json').read_text())['fixtures']:
            raw = (FIXTURES / row['file']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row['source_sha256'])
            yield row, raw

    def test_all_existing_historical_interfaces_are_detected_without_version(self):
        for row, raw in self.fixtures():
            with self.subTest(cohort=row['installer_commit']):
                members = {'install.sh', '1pctl', 'lang/en.sh'}
                if row['appstore_install']:
                    members.add('appstore.tar.gz')
                result = inspect_installer(raw, members, 'enterprise' if row['appstore_install'] else 'official')
                self.assertEqual(result['adapter'], 'cli' if row['non_interactive_cli'] else 'interactive')
                self.assertEqual(result['edition_selection'], row['edition_selection'])
                self.assertEqual(result['appstore_required'], row['appstore_install'])

    def test_harmless_new_optional_variable_does_not_need_a_version_record(self):
        row, raw = next(self.fixtures())
        result = inspect_installer(raw + b'\nOPTIONAL_NEW_VENDOR_VALUE=unused\n', set(), 'official')
        self.assertEqual(result['adapter'], 'interactive')
        self.assertEqual(result['edition_selection'], row['edition_selection'])

    def test_incompatible_required_interface_fails_before_runtime(self):
        cli = next(raw for row, raw in self.fixtures() if row['non_interactive_cli'] and not row['appstore_install'])
        for fault, raw in [
            ('option', cli.replace(b'--username)', b'--renamed-user)')),
            ('password-env', cli.replace(b'${PANEL_PASSWORD:-}', b'${UNSUPPORTED_PASSWORD:-}')),
            ('partial-cli', cli.replace(b'NON_INTERACTIVE=false', b'OTHER_INTERACTIVE=false', 1)),
            ('parser-not-invoked', cli.replace(b'parse_args "$@"\n', b'', 1)),
            ('parser-commented', cli.replace(b'parse_args "$@"\n', b'# parse_args "$@"\n', 1)),
            ('parser-in-unused-function', cli.replace(b'parse_args "$@"\n', b'function unused() {\nparse_args "$@"\n}\n', 1)),
            ('parser-in-bare-function', cli.replace(b'parse_args "$@"\n', b'unused() {\nparse_args "$@"\n}\n', 1)),
            ('parser-in-function-no-parens', cli.replace(b'parse_args "$@"\n', b'function unused {\nparse_args "$@"\n}\n', 1)),
            ('parser-in-indented-function', cli.replace(b'parse_args "$@"\n', b'  unused() {\nparse_args "$@"\n  }\n', 1)),
            ('parser-in-heredoc', cli.replace(b'parse_args "$@"\n', b'cat <<SYNTHETIC\nparse_args "$@"\nSYNTHETIC\n', 1)),
            ('parser-in-quoted-heredoc', cli.replace(b'parse_args "$@"\n', b'cat <<\'SYNTHETIC\'\nparse_args "$@"\nSYNTHETIC\n', 1)),
            ('parser-in-tab-heredoc', cli.replace(b'parse_args "$@"\n', b'cat <<-"SYNTHETIC"\n\tparse_args "$@"\n\tSYNTHETIC\n', 1)),
            ('parser-in-false-conditional', cli.replace(b'parse_args "$@"\n', b'if false; then\nparse_args "$@"\nfi\n', 1)),
            ('parser-in-multiline-string', cli.replace(b'parse_args "$@"\n', b"UNUSED='\nparse_args \"$@\"\n'\n", 1)),
            ('parser-in-nested-brace-function', cli.replace(b'parse_args "$@"\n', b'function unused() {\n{\n:\n}\nparse_args "$@"\n}\n', 1)),
            ('parser-after-continuation', cli.replace(b'parse_args "$@"\n', b'false &&\nparse_args "$@"\n', 1)),
            ('parser-after-escaped-newline', cli.replace(b'parse_args "$@"\n', b"printf '%s' " + bytes([92, 10]) + b'parse_args "$@"\n', 1)),
            ('parser-after-odd-backslashes', cli.replace(b'parse_args "$@"\n', b"printf '%s' " + bytes([92, 92, 92, 10]) + b'parse_args "$@"\n', 1)),
            ('docker-interface', cli.replace(b'function Install_Docker()', b'function New_Docker()', 1)),
            ('syntax', cli + b'\nif then broken\n'),
        ]:
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                inspect_installer(raw, set(), 'official')

    def test_renamed_or_partial_edition_selector_cannot_become_legacy(self):
        raw = next(raw for row, raw in self.fixtures() if row['edition_selection'] and not row['appstore_install'])
        for fault, changed in [
            ('renamed-file', raw.replace(b'.selected_edition', b'.regional_edition')),
            ('missing-assignment', raw.replace(b'EDITION_FILE=".selected_edition"', b'')),
            ('missing-read', raw.replace(b'selected_edition=$(cat "$CURRENT_DIR/$EDITION_FILE")', b'selected_edition="cn"')),
            ('missing-control-update', raw.replace(b'PANEL_EDITION', b'UNSUPPORTED_REGION')),
        ]:
            with self.subTest(fault=fault), self.assertRaisesRegex(ValueError, 'edition-selection'):
                inspect_installer(changed, set(), 'official')

    def test_parser_prefix_checks_are_bounded_and_ignore_inert_examples(self):
        with patch('installer_capabilities.subprocess.run') as run, self.assertRaisesRegex(ValueError, 'candidate count'):
            require_argument_parser_call('parse_args "$@"\n' * 17)
        run.assert_not_called()
        cli = next(raw for row, raw in self.fixtures() if row['non_interactive_cli'] and not row['appstore_install'])
        example = b"cat <<'SYNTHETIC'\nparse_args \"$@\"\nSYNTHETIC\n"
        self.assertEqual(inspect_installer(cli + example, set(), 'official')['adapter'], 'cli')

    def test_appstore_requirement_cannot_be_inferred_from_version_or_silently_dropped(self):
        old = next(raw for row, raw in self.fixtures() if not row['appstore_install'])
        new = next(raw for row, raw in self.fixtures() if row['appstore_install'])
        for raw, members, source in [(old, {'appstore.tar.gz'}, 'enterprise'),
                                      (new, set(), 'enterprise'),
                                      (new, {'appstore.tar.gz'}, 'custom')]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                inspect_installer(raw, members, source)


if __name__ == '__main__':
    unittest.main()
