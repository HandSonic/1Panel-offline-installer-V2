"""Authenticated script samples and synthetic transitions; no native execution."""
import copy
import ast
import hashlib
import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from test_service_layout import original_files
import test_native_upgrade as archive_tests
from test_native_upgrade import INSTALLER, CONTROL, tar_bytes, facts
import native_upgrade_input as binder
import runtime_native_acceptance as acceptance
from upgrade_configuration import (configuration_profile, edition_transition, installed_configuration,
                                   migrated_configuration, package_configuration, protected_assignments,
                                   PROTECTED_KEYS)
from service_layout import complete_shell, command_boundary


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.legacy = original_files()
        self.old = configuration_profile(self.legacy['install.sh'], self.legacy['1pctl'])
        self.new = configuration_profile(INSTALLER, CONTROL)

    def test_control_sample_matches_immutable_git_blob_and_sha256(self):
        index = json.loads((ROOT / 'tests/fixtures/upgrade-configuration/index.json').read_text())
        self.assertEqual(len(CONTROL), index['bytes'])
        self.assertEqual(hashlib.sha256(CONTROL).hexdigest(), index['sha256'])
        self.assertEqual(hashlib.sha1(b'blob ' + str(len(CONTROL)).encode() + b'\0' + CONTROL).hexdigest(), index['git_blob_sha'])

    def test_real_legacy_has_no_region_and_target_default_is_introduced(self):
        self.assertFalse(self.old['edition_selection'])
        self.assertEqual(self.old['default_edition'], 'legacy')
        self.assertEqual(edition_transition(self.old, self.old),
                         {'kind': 'legacy-no-selector', 'before': 'legacy', 'after': 'legacy'})
        self.assertEqual(edition_transition(self.old, self.new),
                         {'kind': 'introduce-target-default', 'before': 'legacy', 'after': 'cn'})
        intl = configuration_profile(INSTALLER, CONTROL.replace(b'PANEL_EDITION=cn', b'PANEL_EDITION=intl'))
        self.assertEqual(edition_transition(self.old, intl)['after'], 'intl')
        # LANGUAGE=en in the genuine legacy sample did not select intl.
        self.assertIn(b'LANGUAGE=en', self.legacy['1pctl'])

    def test_modern_regions_are_preserved_and_loss_is_rejected(self):
        for selected in ('cn', 'intl'):
            self.assertEqual(edition_transition(self.new, self.new, selected),
                             {'kind': 'preserve-selection', 'before': selected, 'after': selected})
        with self.assertRaisesRegex(ValueError, 'lose'):
            edition_transition(self.new, self.old)

    def test_missing_partial_renamed_or_nonliteral_interfaces_are_not_legacy(self):
        pairs = [
            (INSTALLER, CONTROL.replace(b'PANEL_EDITION=cn\n', b'')),
            (INSTALLER, CONTROL.replace(b'PANEL_EDITION', b'PANEL_REGION')),
            (INSTALLER.replace(b'.selected_edition', b'.selected_region'), CONTROL),
            (INSTALLER.replace(b'PANEL_EDITION', b'PANEL_REGION'), CONTROL),
            (INSTALLER.replace(b'EDITION_FILE=".selected_edition"', b'EDITION_FILE=".renamed"'), CONTROL),
            (INSTALLER, CONTROL + b'\nPANEL_EDITION=intl\n'),
            (INSTALLER, CONTROL + b'\n  PANEL_EDITION=intl\n'),
            (INSTALLER, CONTROL + b'\nexport PANEL_EDITION=intl\n'),
            (INSTALLER, CONTROL.replace(b'PANEL_EDITION=cn', b'PANEL_EDITION=$(echo cn)')),
            (INSTALLER, CONTROL.replace(b'PANEL_EDITION=cn', b'PANEL_EDITION=future-region')),
            (self.legacy['install.sh'], CONTROL),
            (self.legacy['install.sh'], self.legacy['1pctl'] + b'PANEL_REGION=cn\n'),
            (INSTALLER, CONTROL.replace(b'PANEL_EDITION=cn', b'if false; then\nPANEL_EDITION=cn\nfi')),
            (INSTALLER, CONTROL.replace(b'PANEL_EDITION=cn', b'exit 0\nPANEL_EDITION=cn')),
        ]
        for installer, control in pairs:
            with self.subTest(installer=hashlib.sha256(installer).hexdigest(), control=control[-40:]), self.assertRaises(ValueError):
                configuration_profile(installer, control)

    def test_every_protected_command_list_or_declaration_reassignment_is_rejected(self):
        forms = (':; {key}=forged', ': && {key}=forged', 'false || {key}=forged',
                 'declare {key}=forged', 'readonly {key}=forged', 'export {key}=forged',
                 '{key}+=forged', '{key}[0]=forged', 'declare \\\n{key}=forged',
                 'if true; then {key}=forged; fi')
        for key in PROTECTED_KEYS:
            for form in forms:
                control = CONTROL + ('\n' + form.format(key=key) + '\n').encode()
                with self.subTest(key=key, form=form):
                    with self.assertRaises(ValueError):
                        configuration_profile(INSTALLER, control)
                    with self.assertRaises(ValueError):
                        installed_configuration(control.decode(), 'cn')

    def test_comments_quotes_and_heredoc_examples_do_not_become_reassignments(self):
        examples = (
            '# :; PANEL_EDITION=intl\n',
            '# eval "PANEL_EDITION=intl"\n',
            'printf "%s\\n" ":; PANEL_EDITION=intl"\n',
            "printf '%s\\n' ':; PANEL_EDITION=intl'\n",
            "printf '%s\\n' 'eval \"PANEL_EDITION=intl\"'\n",
            "cat <<'EXAMPLE'\nPANEL_EDITION=intl\n: && BASE_DIR=forged\nEXAMPLE\n",
            'cat <<EXAMPLE\nPANEL_EDITION=intl\nreadonly LANGUAGE=forged\neval "PANEL_EDITION=intl"\nEXAMPLE\n',
        )
        original = installed_configuration(CONTROL.decode(), 'cn')
        for example in examples:
            control = CONTROL + ('\n' + example).encode()
            with self.subTest(example=example):
                self.assertEqual(configuration_profile(INSTALLER, control)['default_edition'], 'cn')
                self.assertEqual(installed_configuration(control.decode(), 'cn'), original)

    def test_dynamic_assignment_builtins_cannot_hide_behind_quoted_arguments(self):
        operations = (
            "eval 'PANEL_EDITION=intl'", 'eval "$DYNAMIC_CONFIG"',
            '"eval" "PANEL_EDITION=intl"', "'eval' 'PANEL_EDITION=intl'",
            "declare 'PANEL_EDITION=intl'", "readonly 'PANEL_EDITION=intl'",
            'declare "$DYNAMIC_CONFIG"', "export 'BASE_DIR=elsewhere'",
            "unset PANEL_EDITION", "let 'ORIGINAL_PORT=10000'",
            'source "$DYNAMIC_CONFIG"', '. "$DYNAMIC_CONFIG"',
        )
        for operation in operations:
            control = CONTROL + ('\n' + operation + '\n').encode()
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError): configuration_profile(INSTALLER, control)
                with self.assertRaises(ValueError): installed_configuration(control.decode(), 'cn')

    def test_assignment_candidate_flood_is_bounded_before_any_bash_probe(self):
        control = CONTROL + b'\n# :; PANEL_EDITION=intl\n' * 65
        with patch('upgrade_configuration.complete_shell', side_effect=AssertionError('No Bash before candidate bound')):
            with self.assertRaisesRegex(ValueError, 'candidate count'):
                configuration_profile(INSTALLER, control)
            with self.assertRaisesRegex(ValueError, 'candidate count'):
                installed_configuration(control.decode(), 'cn')
            with self.assertRaisesRegex(ValueError, 'candidate count'):
                installed_configuration((CONTROL + b'\n# eval example\n' * 65).decode(), 'cn')

    def test_direct_builtin_destinations_are_checked_for_every_protected_key(self):
        forms = ('printf -v {key} %s forged', 'printf -v{key} %s forged',
                 'printf "%n" {key}', 'read -r {key} <<< forged',
                 'read -ra {key} <<< forged', 'readarray -t {key} <<< forged',
                 'mapfile -t {key} <<< forged', 'getopts x {key} -x',
                 'for {key} in forged; do :; done', 'select {key} in forged; do break; done')
        for key in PROTECTED_KEYS:
            for form in forms:
                control = CONTROL + ('\n' + form.format(key=key) + '\n').encode()
                with self.subTest(key=key, form=form):
                    with self.assertRaises(ValueError): configuration_profile(INSTALLER, control)
                    with self.assertRaises(ValueError): installed_configuration(control.decode(), 'cn')

    def test_continued_arithmetic_and_parameter_writes_are_rejected(self):
        forms = ('PANEL_EDITION\\\n=intl', 'PANEL_EDI\\\nTION=intl',
                 'printf -v PANEL_EDI\\\nTION %s intl', 'read "$CONFIG_DEST" <<< intl',
                 'mapfile -C "$CALLBACK" other', 'declare -n other=PANEL_EDITION',
                 '((PANEL_EDITION++))', '((++PANEL_EDITION))', '((PANEL_EDITION--))',
                 '((--PANEL_EDITION))', '((PANEL_EDITION += 1))', '((PANEL_EDITION = 1))',
                 'for ((PANEL_EDITION=0; PANEL_EDITION<1; PANEL_EDITION++)); do :; done',
                 'echo "$((PANEL_EDITION++))"', 'echo "$((\nPANEL_EDITION++\n))"',
                 ': ${PANEL_EDITION:=intl}', ': "${PANEL_EDITION=intl}"')
        for operation in forms:
            control = CONTROL + ('\n' + operation + '\n').encode()
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError): configuration_profile(INSTALLER, control)
                with self.assertRaises(ValueError): installed_configuration(control.decode(), 'cn')

    def test_unrelated_builtins_and_continued_literal_examples_remain_supported(self):
        examples = ('read -rp "Region: " answer', 'read -ra answers <<< text',
                    'readarray -t lines <<< text', 'mapfile -t lines <<< text',
                    'printf -v message "%s" hello', 'printf "%n" count',
                    'printf "%s\\n" "$PANEL_EDITION"', 'getopts x option -x',
                    'for item in one two; do :; done', 'select item in one; do break; done',
                    'export OTHER=value', 'readonly OTHER=value', 'declare other=value',
                    '((other++))', ': "${OTHER:=default}"', ': "${PANEL_EDITION}"',
                    '# read PANEL_EDITION\n# PANEL_EDI\\\nTION=intl',
                    "printf '%s\\n' 'PANEL_EDI\\\nTION=intl'",
                    "cat <<'EXAMPLE'\nPANEL_EDI\\\nTION=intl\nread PANEL_EDITION\nEXAMPLE")
        original = installed_configuration(CONTROL.decode(), 'cn')
        for example in examples:
            control = CONTROL + ('\n' + example + '\n').encode()
            with self.subTest(example=example):
                self.assertEqual(configuration_profile(INSTALLER, control)['default_edition'], 'cn')
                self.assertEqual(installed_configuration(control.decode(), 'cn'), original)

    def test_continuation_and_new_writer_floods_are_bounded_before_bash(self):
        for addition, message in ((b'\n# PANEL_EDI\\\nTION=intl' * 65, 'candidate count'),
                                  (b'\n# read other' * 65, 'candidate count'),
                                  (b'\n# example\\\n' * 129, 'continuation count')):
            with patch('upgrade_configuration.complete_shell', side_effect=AssertionError('No Bash before bounds')):
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    installed_configuration((CONTROL + addition).decode(), 'cn')

    def test_standalone_comment_exception_never_hides_expanding_heredoc_or_quote(self):
        for expression in ('${PANEL_EDITION:=intl}', '${PANEL_EDITION=intl}', '$((PANEL_EDITION++))'):
            with self.subTest(expression=expression):
                comment = ('\n  # ' + expression + '\n').encode()
                self.assertEqual(configuration_profile(INSTALLER, CONTROL + comment)['default_edition'], 'cn')
                self.assertEqual(installed_configuration((CONTROL + comment).decode(), 'cn'),
                                 installed_configuration(CONTROL.decode(), 'cn'))
                for wrapper in ('cat <<EXAMPLE\n# {expression}\nEXAMPLE\n', 'message="\n# {expression}\n"\n'):
                    control = CONTROL + ('\n' + wrapper.format(expression=expression)).encode()
                    with self.assertRaises(ValueError): configuration_profile(INSTALLER, control)
                    with self.assertRaises(ValueError): installed_configuration(control.decode(), 'cn')

    def test_standalone_updater_guard_matches_shared_source_exactly(self):
        shell = (ROOT / 'upgrade_offline.sh').read_text()
        embedded = shell.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        nodes = {node.name: node for node in ast.parse(embedded).body if isinstance(node, ast.FunctionDef)}
        for function in (complete_shell, command_boundary, protected_assignments):
            self.assertEqual(ast.get_source_segment(embedded, nodes[function.__name__]), inspect.getsource(function).rstrip())

    def test_existing_fields_and_absence_are_checked_separately(self):
        before = installed_configuration(self.legacy['1pctl'].decode(), 'legacy')
        after = migrated_configuration(before, CONTROL.decode())
        self.assertEqual(after, dict(before, PANEL_EDITION='cn'))
        self.assertNotIn('CHANGE_USER_INFO', after)
        for region in ('cn', 'intl'):
            modern = dict(before, PANEL_EDITION=region, CHANGE_USER_INFO='use_existing')
            self.assertEqual(migrated_configuration(modern, CONTROL.decode()), modern)
        for control, region in [(self.legacy['1pctl'].decode(), 'cn'), (CONTROL.decode(), 'legacy'),
                                (CONTROL.decode(), 'intl')]:
            with self.assertRaises(ValueError):
                installed_configuration(control, region)

    def test_genuine_legacy_resources_bootstrap_without_edition_or_multi_init(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive, binding, source, proof, body = archive_tests.IndependentArchiveTests().fixture(root, original=self.legacy)
            package, selected = binder.unpack_predecessor(archive, root / 'out', binding, 'official', 'amd64', source, proof, root)
            self.assertEqual(selected['configuration'], package_configuration(package))
            self.assertFalse(selected['configuration']['edition_selection'])
            self.assertEqual((package / '1pctl').read_bytes(), self.legacy['1pctl'])
            self.assertEqual((package / '1panel-core.service').read_bytes(), self.legacy['1panel-core.service'])
            self.assertFalse((package / 'initscript').exists())
            self.assertFalse((package / '.selected_edition').exists())

    def test_self_consistent_legacy_manifest_cannot_hide_missing_or_changed_source(self):
        for key in ('1panel-agent.service', '1pctl'):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                archive, binding, source, proof, body = archive_tests.IndependentArchiveTests().fixture(root, original=self.legacy)
                body[key] = b'forged resource'
                manifest = json.loads(body['offline-manifest.json'])
                manifest['payloads'][key] = facts(body[key]); body['offline-manifest.json'] = json.dumps(manifest).encode()
                raw = tar_bytes(body, archive.name.removesuffix('.tar.gz')); archive.write_bytes(raw)
                binding['archive'].update(facts(raw))
                with self.assertRaises(ValueError):
                    binder.unpack_predecessor(archive, root / 'out', binding, 'official', 'amd64', source, proof, root)


class TransitionEvidenceTests(unittest.TestCase):
    def fixture(self, before, after):
        transition = edition_transition(before, after)
        candidate = {'archive_sha256': '1' * 64, 'receipt_sha256': '2' * 64, 'workflow_run_id': 101,
                     'workflow_run_attempt': 1, 'workflow_commit': 'a' * 40, 'version': 'v2.101.0'}
        proof = {'schema': 2, 'target': candidate, '_file_sha256': '3' * 64,
                 'predecessor_package': {'configuration': before}, 'target_configuration': after,
                 'edition_transition': transition, 'predecessor': {'kind': 'current-run-public-predecessor-bootstrap',
                     'historical_native_acceptance': 'not-claimed', 'version': 'v2.100.0', 'archive': {'sha256': '4' * 64}}}
        installed = {'_file_sha256': '5' * 64, 'status': 'passed', 'evidence_level': 'native-install',
                     'source': 'official', 'architecture': 'amd64', 'version': 'v2.100.0',
                     'regional_edition': transition['before'], 'archive_sha256': '4' * 64}
        result = {'schema': 1, 'status': 'passed', 'evidence_level': 'native-upgrade', 'source': 'official',
                  'architecture': 'amd64', 'version': candidate['version'], 'target_archive_sha256': '1' * 64,
                  'target_receipt_sha256': '2' * 64, 'target_run_id': 101, 'target_run_attempt': 1,
                  'target_commit': 'a' * 40, 'input_provenance_sha256': '3' * 64,
                  'predecessor_install_result_sha256': '5' * 64, 'predecessor_archive_sha256': '4' * 64,
                  'regional_edition': transition['after'], 'edition_transition': transition,
                  'rollback': 'passed; one-shot real systemd synthetic test field',
                  'upgrade': 'passed; unchanged fixed target upgrade.sh', 'database_before': {},
                  'database_after_rollback': {}, 'docker_process': {'pid': 1, 'binary_sha256': '6' * 64},
                  **{flag: True for flag in ('database_user_rows_preserved', 'durable_settings_preserved',
                     'user_configuration_preserved', 'persistent_user_file_preserved', 'docker_unchanged')}}
        return result, candidate, proof, installed

    def check(self, value):
        result, candidate, proof, installed = value
        acceptance.check_result(result, candidate, 'upgrade', 'official', 'amd64', None, proof, installed)

    def test_all_supported_transitions_need_exact_typed_evidence(self):
        legacy = original_files()
        old = configuration_profile(legacy['install.sh'], legacy['1pctl'])
        new = configuration_profile(INSTALLER, CONTROL)
        for before, after in ((old, old), (old, new), (new, new)):
            value = self.fixture(before, after); self.check(value)
            for fault in ('missing-profile', 'missing-transition', 'false-preservation', 'before', 'after', 'default', 'hash'):
                changed = copy.deepcopy(value); result, _, proof, installed = changed
                if fault == 'missing-profile': proof.pop('target_configuration')
                elif fault == 'missing-transition': result.pop('edition_transition')
                elif fault == 'false-preservation': result['edition_transition']['kind'] = 'invented'
                elif fault == 'before': installed['regional_edition'] = 'wrong'
                elif fault == 'after': result['regional_edition'] = 'wrong'
                elif fault == 'default': proof['target_configuration']['default_edition'] = 'unknown'
                elif fault == 'hash': proof['target_configuration']['control_sha256'] = ''
                with self.subTest(transition=value[0]['edition_transition'], fault=fault), self.assertRaises(ValueError):
                    self.check(changed)
