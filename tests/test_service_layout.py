"""Static authenticated service-layout samples and synthetic packaging mutations.

No package program, service, installer, or upgrader is executed by these tests.
Only bash -n parses script text, as it does in the production recognizer.
"""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch as mock_patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from service_layout import service_layout
from validate_payload import (APP_BASE_REQUIRED, APP_REQUIRED, OFFLINE_REQUIRED,
                              directory_payloads, digest, manifest, payload_required)
from patch_installer import patch
from validate_release import validate
from test_offline import archive, binary
from test_runtime_contract import runtime
from runtime_fixtures import activate, dependencies, refresh, vendor_pins

SAMPLES = ROOT / 'tests/fixtures/service-layout'


def original_files():
    index = json.loads((SAMPLES / 'index.json').read_text())
    result = {}
    for row in index['files']:
        raw = (SAMPLES / row['file']).read_bytes()
        assert digest(SAMPLES / row['file']) == {k: row[k] for k in ('sha256', 'bytes')}
        assert row['file'].startswith(row['sha256'])
        result[row['member']] = raw
    return result


def write_archive(path, prefix, files, symlink=None):
    with tarfile.open(path, 'w:gz') as output:
        for name, raw in files.items():
            info = tarfile.TarInfo(prefix + '/' + name)
            info.size = len(raw)
            if name == symlink:
                info.type = tarfile.SYMTYPE; info.linkname = '1panel-core.service'; info.size = 0
            output.addfile(info, None if name == symlink else io.BytesIO(raw))


class ServiceLayoutTests(unittest.TestCase):
    def setUp(self):
        self.original = original_files()
        self.raw = self.original['install.sh']
        self.names = set(self.original)

    def test_actual_root_systemd_and_existing_multi_init_samples(self):
        result = service_layout(self.raw, self.names)
        self.assertEqual(result, {'layout': 'root-systemd', 'managers': ['systemd'],
                                 'required': ['1panel-agent.service', '1panel-core.service']})
        samples = ROOT / 'tests/fixtures/historical-installers'
        for row in json.loads((samples / 'index.json').read_text())['fixtures']:
            with self.subTest(source=row['source_sha256']):
                raw = (samples / row['file']).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), row['source_sha256'])
                result = service_layout(raw, set(APP_REQUIRED))
                self.assertEqual(set(result['managers']), {'systemd', 'sysvinit', 'openrc', 'procd'})
                self.assertEqual(set(result['required']), set(APP_REQUIRED) - set(APP_BASE_REQUIRED))

    def test_missing_pairs_unknown_conflicting_and_inactive_references_fail(self):
        core = b'    cp ./1panel-core.service /etc/systemd/system\n'
        faults = {
            'missing-reference': self.raw.replace(core, b''),
            'comment-only': self.raw.replace(core, b'    # ' + core.lstrip()),
            'string-only': self.raw.replace(core, b'    printf "%s\\n" "cp ./1panel-core.service /etc/systemd/system"\n'),
            'multiline-string': self.raw.replace(core, b'    printf "%s\\n" "\n' + core + b'"\n'),
            'heredoc': self.raw.replace(core, b'    cat <<EOF\n' + core + b'EOF\n'),
            'continued-line': self.raw.replace(core, b'    echo \\\n' + core),
            'uncalled-init': self.raw.replace(b'    Init_Panel\n', b'    : # Init_Panel\n'),
            'uncalled-main': self.raw.removesuffix(b'main\n') + b'# main\n',
            'unreachable-copy': self.raw.replace(core, b'    if false; then\n' + core + b'    fi\n'),
            'duplicate-copy': self.raw.replace(core, core + core),
            'mixed-layout': self.raw.replace(core, core.replace(b'./1panel', b'./initscript/1panel')),
            'unknown-directory': self.raw.replace(core, core.replace(b'./1panel', b'./services/1panel')),
            'unknown-kind': self.raw.replace(core, core.replace(b'.service', b'.unknown')),
            'wrong-destination': self.raw.replace(core, core.replace(b'/etc/systemd/system', b'/etc/init.d/1panel-core')),
            'extra-absolute-copy': self.raw.replace(core, core + b'    cp /unknown/1panel-core.service /etc/systemd/system\n'),
            'extra-variable-copy': self.raw.replace(core, core + b'    cp "$SERVICE" /etc/systemd/system\n'),
        }
        for label, raw in faults.items():
            with self.subTest(fault=label), self.assertRaises(ValueError):
                service_layout(raw, self.names | {'initscript/1panel-core.service'})
        with self.assertRaisesRegex(ValueError, 'missing'):
            service_layout(self.raw, self.names - {'1panel-agent.service'})

    def test_inactive_nested_helper_does_not_grant_capabilities(self):
        sample = ROOT / 'tests/fixtures/historical-installers/3faa744fd158283470b48b3a971dc98cc390c7f291d4287b170416ae862dd28f.sh'
        raw = sample.read_bytes()
        for replacement in (b'    # install_and_configure\n', b'    echo "install_and_configure"\n'):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                service_layout(raw.replace(b'    install_and_configure\n', replacement), set(APP_REQUIRED))

    def test_unconditional_termination_before_calls_and_copies_is_rejected(self):
        targets = [b'    cp ./1panel-core.service /etc/systemd/system\n', b'    Init_Panel\n', b'main\n']
        for target in targets:
            for termination in (b'return 0\n', b'exit 0\n', b':; return 0\n', b'printf ok; exit 0; :\n'):
                with self.subTest(target=target, termination=termination), self.assertRaisesRegex(ValueError, 'Unconditional termination'):
                    service_layout(self.raw.replace(target, termination + target), self.names)

    def test_closed_conditions_and_noncommands_do_not_become_unconditional_termination(self):
        core = b'    cp ./1panel-core.service /etc/systemd/system\n'
        additions = (
            b'    if false; then\n        return 0\n    fi\n',
            b'    if false; then :; exit 0; fi\n',
            b'    # ; return 0\n',
            b'    printf ok # ; exit 0\n',
            b'    printf "%s\\n" "\nreturn 0\n"\n',
            b'    cat <<EOF\nexit 0\nEOF\n',
        )
        expected = service_layout(self.raw, self.names)
        for addition in additions:
            with self.subTest(addition=addition):
                self.assertEqual(service_layout(self.raw.replace(core, addition + core), self.names), expected)

    def test_nested_branch_termination_is_rejected(self):
        sample = ROOT / 'tests/fixtures/historical-installers/3faa744fd158283470b48b3a971dc98cc390c7f291d4287b170416ae862dd28f.sh'
        raw = sample.read_bytes()
        for kind in ('service', 'procd', 'openrc', 'init'):
            line = next(line for line in raw.splitlines(keepends=True)
                        if b'cp ./initscript/1panel-core.' + kind.encode() + b' ' in line)
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'Unconditional termination'):
                service_layout(raw.replace(line, b'        :; return 0\n' + line), set(APP_REQUIRED))

    def test_nested_resources_must_match_the_real_manager_branch(self):
        sample = ROOT / 'tests/fixtures/historical-installers/3faa744fd158283470b48b3a971dc98cc390c7f291d4287b170416ae862dd28f.sh'
        raw = sample.read_bytes()
        faults = {
            'fake-predicate': raw.replace(b'if [ -f /etc/rc.common ]; then', b'if false; then\n        echo "if [ -f /etc/rc.common ]; then"'),
            'swapped-manager': raw.replace(b'.procd /etc/init.d/', b'.swap /etc/init.d/').replace(
                b'.openrc /etc/init.d/', b'.procd /etc/init.d/').replace(b'.swap /etc/init.d/', b'.openrc /etc/init.d/'),
            'commented-reference': raw.replace(b'            cp ./initscript/1panel-core.init', b'            # cp ./initscript/1panel-core.init'),
        }
        for name, changed in faults.items():
            with self.subTest(fault=name), self.assertRaises(ValueError):
                service_layout(changed, set(APP_REQUIRED))

    def test_optional_comments_do_not_need_version_configuration(self):
        self.assertEqual(service_layout(self.raw + b'\n# optional new setting\nOPTIONAL=unused\n', self.names),
                         service_layout(self.raw, self.names))

    def test_excess_candidates_fail_before_per_copy_bash_processes(self):
        from service_layout import complete_shell
        sample = ROOT / 'tests/fixtures/historical-installers/3faa744fd158283470b48b3a971dc98cc390c7f291d4287b170416ae862dd28f.sh'
        for raw, line, names in [
                (self.raw, b'    cp ./1panel-core.service /etc/systemd/system\n', self.names),
                (sample.read_bytes(), b'        cp ./initscript/1panel-core.service /etc/systemd/system\n', set(APP_REQUIRED))]:
            changed = raw.replace(line, line * 10000)
            self.assertLess(len(changed), 2 * 1024 * 1024)
            with self.subTest(nested='initscript/1panel-core.service' in names), \
                    mock_patch('service_layout.complete_shell', wraps=complete_shell) as parse:
                with self.assertRaisesRegex(ValueError, 'candidate count'):
                    service_layout(changed, names)
                self.assertLessEqual(parse.call_count, 7)

    def test_excess_termination_candidates_fail_before_bash(self):
        with mock_patch('service_layout.complete_shell', side_effect=AssertionError('No Bash for oversized candidate list')):
            with self.assertRaisesRegex(ValueError, 'Termination candidate count'):
                service_layout(self.raw + b'\n# synthetic candidate flood\n' + b'return 0\n' * 65, self.names)


class ServicePackagingTests(unittest.TestCase):
    def prepare(self, root):
        version = 'v2.99.0'  # Deliberately unregistered; no version routing.
        source = original_files()
        for name in APP_BASE_REQUIRED:
            if name not in source:
                source[name] = binary('amd64') if name.startswith('1panel-') else b'synthetic resource'
        source['1pctl'] = source['1pctl'].replace(b'ORIGINAL_VERSION=v2.0.0', b'ORIGINAL_VERSION=v2.99.0')
        app = root / 'original.tar.gz'
        write_archive(app, f'1panel-{version}-linux-amd64', source)
        out = root / 'output'; package = root / 'package'; package.mkdir()
        for name, raw in source.items():
            path = package / name; path.parent.mkdir(exist_ok=True, parents=True); path.write_bytes(raw)
        docker_path = root / 'docker.tgz'; archive(docker_path, 'amd64')
        compose = root / 'docker-compose'; compose.write_bytes(binary('amd64'))
        extra = {'docker.tgz': docker_path.read_bytes(), 'docker-compose': compose.read_bytes(),
                 'docker.service': b'synthetic docker service', 'upgrade.sh': (ROOT / 'upgrade_offline.sh').read_bytes()}
        for name, raw in extra.items(): (package / name).write_bytes(raw)
        patch(package / 'install.sh')
        dependencies(root)
        for component, path in [('app', app), ('docker', docker_path), ('compose', compose)]:
            pin = dict(digest(path), url='https://fixture.invalid/' + component)
            Path(str(path) + '.source.json').write_text(json.dumps(pin))
            if component != 'app':
                lock_path = root / (component + '-sources.json'); lock = json.loads(lock_path.read_text())
                lock['amd64'] = dict(pin, version='fixture'); lock_path.write_text(json.dumps(lock))
        manifest(package, 'amd64', 'official', version, 'fixture', 'fixture', app, docker_path, compose)
        (root / 'upgrade_offline.sh').write_bytes(extra['upgrade.sh'])
        value = runtime(root, version, enterprise=False)
        value['inventory']['official'] = vendor_pins(version, 'official', {'amd64': digest(app)})
        refresh(value, root); activate(self, root, value)
        # Match the canonical official URL in the synthetic manifest provenance.
        m = json.loads((package / 'offline-manifest.json').read_text())
        m['inputs']['app'] = value['inventory']['official']['archives']['amd64']
        (package / 'offline-manifest.json').write_text(json.dumps(m))
        return package, source, out, app, docker_path, compose

    def finish(self, package, out, mutate=None, symlink=None):
        files = {p.relative_to(package).as_posix(): p.read_bytes() for p in package.rglob('*') if p.is_file()}
        if mutate: mutate(files)
        prefix = '1panel-v2.99.0-official-offline-linux-amd64'
        path = out / 'official' / (prefix + '.tar.gz'); path.parent.mkdir(parents=True, exist_ok=True)
        write_archive(path, prefix, files, symlink=symlink)
        (out / 'checksums.txt').write_text(digest(path)['sha256'] + '  ' + path.name + '\n')
        return path

    def test_root_package_manifest_release_and_resource_preservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); package, source, out, *_ = self.prepare(root)
            wanted = set(OFFLINE_REQUIRED + APP_BASE_REQUIRED + ['1panel-core.service', '1panel-agent.service'])
            self.assertEqual(set(directory_payloads(package)), wanted)
            self.assertEqual(set(json.loads((package / 'offline-manifest.json').read_text())['payloads']), wanted)
            self.assertNotIn('upgrade.sh', source)
            for name in ('1panel-core.service', '1panel-agent.service', '1pctl'):
                self.assertEqual((package / name).read_bytes(), source[name])
            self.finish(package, out)
            self.assertEqual(validate(out, 'v2.99.0', {'official': ['amd64']}, root), 1)
            from native_install_smoke import regional_edition
            self.assertEqual(regional_edition(package), 'legacy')

    def test_repack_rejects_service_and_control_byte_changes(self):
        for name in ('1panel-core.service', '1panel-agent.service', '1pctl'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); package, _, _, app, docker_path, compose = self.prepare(root)
                (package / name).write_bytes((package / name).read_bytes() + b'\n# changed\n')
                with self.assertRaisesRegex(ValueError, 'changed during repack'):
                    manifest(package, 'amd64', 'official', 'v2.99.0', 'fixture', 'fixture', app, docker_path, compose)

    def test_missing_empty_and_symlink_service_files_fail(self):
        for fault in ('missing', 'empty', 'symlink'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); package, _, out, *_ = self.prepare(root)
                target = package / '1panel-agent.service'
                if fault == 'empty': target.write_bytes(b'')
                else:
                    target.unlink()
                    if fault == 'symlink': target.symlink_to('1panel-core.service')
                with self.assertRaises(ValueError): directory_payloads(package)
                self.finish(package, out, symlink='1panel-agent.service' if fault == 'symlink' else None)
                with self.assertRaises(ValueError): validate(out, 'v2.99.0', {'official': ['amd64']}, root)

    def test_rebound_manifest_cannot_remove_required_service_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); package, _, out, *_ = self.prepare(root)
            def remove(files):
                del files['1panel-agent.service']
                m = json.loads(files['offline-manifest.json']); del m['payloads']['1panel-agent.service']
                files['offline-manifest.json'] = json.dumps(m).encode()
            self.finish(package, out, remove)
            with self.assertRaises(ValueError): validate(out, 'v2.99.0', {'official': ['amd64']}, root)

    def test_initial_native_guard_rejects_missing_or_unsafe_service_before_host_calls(self):
        import native_install_smoke as native
        for fault in ('missing', 'empty', 'symlink'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); package, _, _, *_ = self.prepare(root)
                service = package / '1panel-core.service'
                if fault == 'empty': service.write_bytes(b'')
                else:
                    service.unlink()
                    if fault == 'symlink': service.symlink_to('1panel-agent.service')
                with mock_patch.object(native, 'disposable_guard', return_value='amd64'), \
                        mock_patch.object(native, 'run', side_effect=AssertionError('Must not touch host')) as run:
                    with self.assertRaises(ValueError):
                        native.native_install(package, 'v2.99.0', 'existing', root / 'result.json', 'a' * 64)
                    run.assert_not_called()

    def test_service_recognizer_is_policy_bound(self):
        from runtime_contract import policy
        value = runtime(); before = policy(value); read_bytes = Path.read_bytes
        def changed(path):
            raw = read_bytes(path)
            return raw + b'\n# changed\n' if path == ROOT / 'scripts/service_layout.py' else raw
        with mock_patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(policy(value), before)


if __name__ == '__main__':
    unittest.main()
