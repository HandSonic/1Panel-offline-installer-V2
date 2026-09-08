"""Upgrade regression tests: every filesystem path and service is sandboxed."""
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "upgrade_offline.sh"


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="upgrade-test-", dir=os.environ.get("TMPDIR", str(SCRIPT.parent)))
        self.root = Path(self.tmp.name)
        self.package = self.root / "package"
        self.package.mkdir()
        self.bin = self.root / "usr/local/bin"
        self.bin.mkdir(parents=True)
        self.base = self.root / "opt/panel home"
        self.data = self.base / "1panel"
        for name in ("db", "conf", "config", "geo"):
            (self.data / name).mkdir(parents=True)
        self.units = self.root / "etc/systemd/system"
        self.units.mkdir(parents=True)
        (self.root / "run/systemd/system").mkdir(parents=True)
        (self.root / "etc/init.d").mkdir(parents=True)
        self.mock = self.root / "mockbin"
        self.mock.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.env = dict(os.environ, PATH=str(self.mock) + ":" + os.environ["PATH"], TEST_ROOT=str(self.root), PANEL_HEALTH_TIMEOUT="2")
        text = SCRIPT.read_text()
        # These are the only system locations the script is allowed to access.
        for path in ("/usr/local/bin", "/etc/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system", "/etc/init.d", "/run/systemd/system", "/etc/rc.common"):
            text = text.replace(path, str(self.root) + path)
        text = text.replace('[[ $EUID -eq 0 ]]', '[[ 1 -eq 1 ]]')
        self.script = self.package / "upgrade.sh"
        self.script.write_text(text)
        for name in ("1panel-core", "1panel-agent"):
            (self.bin / name).write_text("old " + name)
            (self.package / name).write_text("new " + name)
            (self.units / (name + ".service")).write_text("custom admin service " + name)
            (self.state / name).touch()
        self.old_config = (
            "#!/bin/bash\nBASE_DIR=" + str(self.base) + "\n"
            "ORIGINAL_VERSION=v2.2.4\nORIGINAL_PORT=12345\n"
            "ORIGINAL_PASSWORD='a\\b$literal|&'\nLANGUAGE=zh\n"
            "FUTURE_SETTING='preserve me'\n"
            "DANGEROUS='$(touch " + str(self.root / "executed") + ")'\n"
            "main() { :; }\nmain\n"
        )
        (self.bin / "1pctl").write_text(self.old_config)
        (self.package / "1pctl").write_text("#!/bin/bash\nBASE_DIR=/opt\nORIGINAL_VERSION=v2.2.5\nORIGINAL_PORT=8090\nORIGINAL_PASSWORD=new\nLANGUAGE=en\nif true; then :; fi\nmain() { :; }\nmain\n")
        (self.bin / "lang").mkdir()
        (self.bin / "lang/zh.sh").write_text("old language")
        (self.data / "geo/GeoIP.mmdb").write_text("old geo")
        (self.data / "conf/app.yaml").write_text("old config")
        for name in ("core", "agent"):
            with sqlite3.connect(self.data / ("db/" + name + ".db")) as db:
                db.execute("CREATE TABLE settings (key TEXT, value TEXT)")
                db.execute("INSERT INTO settings VALUES ('SystemVersion', 'v2.2.4')")
        self.write_mock("sleep", "#!/bin/bash\nexit 0\n")
        self.write_mock("systemctl", r'''#!/bin/bash
set -u
name=${2:-}; name=${name%.service}
[[ "$1" == is-active ]] && { name=${3%.service}; }
printf '%s\n' "$*" >> "$TEST_ROOT/service.log"
case "$1" in
  daemon-reload) exit 0;;
  stop) rm -f "$TEST_ROOT/state/$name"; exit 0;;
  is-active) [[ -f "$TEST_ROOT/state/$name" ]]; exit $?;;
  start)
    if [[ "${FAIL_NEW:-}" == 1 && "$name" == 1panel-core ]] && /bin/grep -q '^new' "$TEST_ROOT/usr/local/bin/1panel-core"; then
      printf broken > "$TEST_ROOT/opt/panel home/1panel/conf/app.yaml"
      printf migration > "$TEST_ROOT/opt/panel home/1panel/db/new-migration.db"
      rm -f "$TEST_ROOT/state/$name"
      exit 1
    fi
    touch "$TEST_ROOT/state/$name"
    [[ "${RECOVER_START:-}" == 1 && "$name" == 1panel-core ]] && exit 1
    exit 0;;
esac
exit 1
''')

    def tearDown(self):
        self.tmp.cleanup()

    def write_mock(self, name, text):
        file = self.mock / name
        file.write_text(text)
        file.chmod(0o755)

    def run_upgrade(self, *args):
        return subprocess.run(["/bin/bash", str(self.script), *args], env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)

    def versions(self):
        result = []
        for name in ("core", "agent"):
            with sqlite3.connect(self.data / ("db/" + name + ".db")) as db:
                result.append(db.execute("SELECT value FROM settings WHERE key='SystemVersion'").fetchone()[0])
        return result

    def test_success_preserves_unknown_config_units_and_missing_optional_resources(self):
        result = self.run_upgrade()
        self.assertEqual(result.returncode, 0, result.stdout)
        config = (self.bin / "1pctl").read_text()
        self.assertIn("FUTURE_SETTING='preserve me'", config)
        self.assertIn("ORIGINAL_PASSWORD='a\\b$literal|&'", config)
        self.assertIn("ORIGINAL_VERSION=v2.2.5", config)
        self.assertLess(config.index("FUTURE_SETTING="), config.index("if true"))
        self.assertFalse((self.root / "executed").exists())
        self.assertEqual((self.data / "geo/GeoIP.mmdb").read_text(), "old geo")
        self.assertEqual((self.units / "1panel-core.service").read_text(), "custom admin service 1panel-core")
        self.assertEqual(self.versions(), ["v2.2.5", "v2.2.5"])
        self.assertTrue((self.state / "1panel-agent").exists())

    def test_core_failure_rolls_back_databases_and_all_files(self):
        self.env["FAIL_NEW"] = "1"
        (self.data / "db/unrelated.db-wal").write_text("old wal")
        result = self.run_upgrade()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Rollback completed", result.stdout)
        self.assertEqual((self.bin / "1panel-core").read_text(), "old 1panel-core")
        self.assertEqual((self.bin / "1pctl").read_text(), self.old_config)
        self.assertEqual(self.versions(), ["v2.2.4", "v2.2.4"])
        self.assertEqual((self.data / "conf/app.yaml").read_text(), "old config")
        self.assertEqual((self.data / "db/unrelated.db-wal").read_text(), "old wal")
        self.assertFalse((self.data / "db/new-migration.db").exists())
        self.assertTrue((self.state / "1panel-core").exists())
        self.assertTrue((self.state / "1panel-agent").exists())

    def test_failed_backup_does_not_replace_files(self):
        self.write_mock("cp", '#!/bin/bash\nfor a in "$@"; do [[ "$a" == *backup_*/root* ]] && exit 1; done\nexec /bin/cp "$@"\n')
        result = self.run_upgrade()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual((self.bin / "1pctl").read_text(), self.old_config)
        self.assertEqual(self.versions(), ["v2.2.4", "v2.2.4"])
        self.assertTrue((self.state / "1panel-core").exists())

    def test_partial_resource_copy_rolls_back(self):
        (self.package / "GeoIP.mmdb").write_text("new geo")
        self.write_mock("cp", '#!/bin/bash\nif [[ "$2" == */package/GeoIP.mmdb ]]; then printf partial > "$3"; exit 1; fi\nexec /bin/cp "$@"\n')
        result = self.run_upgrade()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual((self.data / "geo/GeoIP.mmdb").read_text(), "old geo")
        self.assertEqual((self.bin / "1pctl").read_text(), self.old_config)

    def test_transient_start_error_can_recover(self):
        self.env["RECOVER_START"] = "1"
        result = self.run_upgrade()
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_downgrade_requires_force(self):
        config = self.old_config.replace("v2.2.4", "v2.3.0")
        (self.bin / "1pctl").write_text(config)
        result = self.run_upgrade()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.root / "service.log").exists())
        result = self.run_upgrade("--force")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_works_without_python_or_sqlite(self):
        for command in ("dirname", "tee", "uname", "mktemp", "chmod", "rm", "date", "mkdir", "cp", "touch", "readlink", "bash"):
            if not (self.mock / command).exists():
                (self.mock / command).symlink_to(shutil.which(command))
        self.env["PATH"] = str(self.mock)
        result = self.run_upgrade()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.versions(), ["v2.2.4", "v2.2.4"])
        self.assertIn("No usable database helper", result.stdout)

    def test_renamed_wrong_arch_binary_is_rejected(self):
        self.write_mock("uname", '#!/bin/bash\necho x86_64\n')
        self.write_mock("file", '#!/bin/bash\necho "ELF 64-bit LSB executable, ARM aarch64"\n')
        result = self.run_upgrade()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("does not match", result.stdout)
        self.assertFalse((self.root / "service.log").exists())

    def test_repeated_upgrade_preserves_raw_base_directory_for_go_reader(self):
        first = self.run_upgrade()
        self.assertEqual(first.returncode, 0, first.stdout)
        # Exact v2.2.5 ctl_conf.LoadFromFile behavior: trim the raw value;
        # it never decodes quoting or shell backslash escapes.
        raw = next(line[len("BASE_DIR="):].strip() for line in (self.bin / "1pctl").read_text().splitlines() if line.startswith("BASE_DIR="))
        self.assertEqual(raw, str(self.base))
        second = self.run_upgrade()
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertFalse((self.data / ".offline-upgrade.lock").exists())
        self.assertFalse(list(self.package.glob(".1pctl_*")))

    def test_failed_backup_preserves_previously_stopped_service(self):
        (self.state / "1panel-agent").unlink()
        self.write_mock("cp", '#!/bin/bash\nfor a in "$@"; do [[ "$a" == *backup_*/root* ]] && exit 1; done\nexec /bin/cp "$@"\n')
        result = self.run_upgrade()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.state / "1panel-agent").exists())
        self.assertTrue((self.state / "1panel-core").exists())

    def test_rollback_removes_new_unit_and_restores_symlinked_database(self):
        self.env["FAIL_NEW"] = "1"
        (self.units / "1panel-core.service").unlink()
        (self.package / "initscript").mkdir()
        (self.package / "initscript/1panel-core.service").write_text("new unit")
        target = self.root / "external-database"
        (self.data / "db").rename(target)
        (self.data / "db").symlink_to(target, target_is_directory=True)
        result = self.run_upgrade()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Rollback completed", result.stdout)
        self.assertFalse((self.units / "1panel-core.service").exists())
        self.assertTrue((self.data / "db").is_symlink())
        self.assertEqual(self.versions(), ["v2.2.4", "v2.2.4"])
        self.assertFalse((target / "new-migration.db").exists())

    def test_prerelease_downgrade_is_rejected(self):
        (self.bin / "1pctl").write_text(self.old_config.replace("v2.2.4", "v2.2.5-rc.10"))
        new = (self.package / "1pctl").read_text().replace("v2.2.5", "v2.2.5-rc.2")
        (self.package / "1pctl").write_text(new)
        result = self.run_upgrade()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Refusing downgrade", result.stdout)
        self.assertFalse((self.root / "service.log").exists())


if __name__ == "__main__":
    unittest.main()
