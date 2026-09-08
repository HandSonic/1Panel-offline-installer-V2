"""Offline quick-start tests using a local mock server command and inert tarballs."""
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "quick_start.sh"


class QuickStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="quick-test-", dir=os.environ.get("TMPDIR", str(SCRIPT.parent)))
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.mock = self.root / "mockbin"
        self.mock.mkdir()
        self.env = dict(os.environ, PATH=str(self.mock) + ":" + os.environ["PATH"], TEST_ROOT=str(self.root))
        self.version = "v2.2.5"
        self.arch = "amd64"
        self.write_archive()
        self.write_mock("uname", '#!/bin/bash\nprintf "%s\\n" "${TEST_ARCH:-x86_64}"\n')
        self.write_mock("curl", r'''#!/bin/bash
printf '%s\n' "$*" >> "$TEST_ROOT/requests"
output=""; url=""
while (($#)); do
  case "$1" in --output|-o) output=$2; shift 2;;
    --retry|--retry-delay|--connect-timeout|--max-time) shift 2;;
    https://*) url=$1; shift;; *) shift;; esac
done
case "$url" in
  */latest) cp "$TEST_ROOT/latest" "$output";;
  */checksums.txt) cp "$TEST_ROOT/checksums.txt" "$output";;
  *.tar.gz) [[ "${FAIL_DOWNLOAD:-}" == 1 ]] && exit 22; cp "$TEST_ROOT/archive.tar.gz" "$output";;
  *) exit 22;;
esac
''')

    def tearDown(self):
        self.tmp.cleanup()

    def write_mock(self, name, text):
        path = self.mock / name
        path.write_text(text)
        path.chmod(0o755)

    @property
    def filename(self):
        return f"1panel-{self.version}-linux-{self.arch}.tar.gz"

    def write_archive(self, status=0, bad_path=False):
        with tarfile.open(self.root / "archive.tar.gz", "w:gz") as archive:
            name = "../../escaped" if bad_path else self.filename[:-7] + "/install.sh"
            data = f'#!/bin/bash\nprintf installed > "$TEST_ROOT/installed"\nexit {status}\n'.encode()
            item = tarfile.TarInfo(name)
            item.size = len(data)
            item.mode = 0o755
            archive.addfile(item, io.BytesIO(data))
        digest = hashlib.sha256((self.root / "archive.tar.gz").read_bytes()).hexdigest()
        (self.root / "checksums.txt").write_text(digest + "  " + self.filename + "\n")
        (self.root / "latest").write_text(self.version + "\n")

    def run_script(self):
        return subprocess.run(["/bin/bash", str(SCRIPT)], cwd=self.work, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)

    def test_fresh_download_verified_and_status_propagated(self):
        self.write_archive(status=7)
        result = self.run_script()
        self.assertEqual(result.returncode, 7, result.stdout)
        self.assertTrue((self.root / "installed").exists())
        self.assertNotIn("-k", (self.root / "requests").read_text())

    def test_new_corrupt_download_is_not_executed(self):
        (self.root / "checksums.txt").write_text("0" * 64 + "  " + self.filename + "\n")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.root / "installed").exists())
        self.assertFalse((self.work / self.filename).exists())

    def test_valid_cache_skips_download_and_propagates_status(self):
        self.write_archive(status=9)
        (self.work / self.filename).write_bytes((self.root / "archive.tar.gz").read_bytes())
        self.env["FAIL_DOWNLOAD"] = "1"
        result = self.run_script()
        self.assertEqual(result.returncode, 9, result.stdout)
        self.assertTrue((self.root / "installed").exists())
        self.assertNotIn("/" + self.filename, (self.root / "requests").read_text())

    def test_bad_cache_is_replaced_with_verified_download(self):
        (self.work / self.filename).write_text("corrupt")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((self.work / self.filename).read_bytes(), (self.root / "archive.tar.gz").read_bytes())

    def test_checksum_can_use_previous_absolute_builder_paths(self):
        checksum = (self.root / "checksums.txt").read_text().replace("  1panel", "  /opt/1Panel/1panel")
        (self.root / "checksums.txt").write_text(checksum)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_missing_architecture_manifest_never_executes(self):
        self.env["TEST_ARCH"] = "riscv64"
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("此架构", result.stdout)
        self.assertFalse((self.root / "installed").exists())

    def test_loongarch_alias_uses_available_mirror_release(self):
        self.arch = "loong64"
        self.write_archive()
        self.env["TEST_ARCH"] = "loongarch64"
        self.env["PANEL_RESOURCE_BASE"] = "https://example.invalid/mirror/v2"
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("example.invalid", (self.root / "requests").read_text())

    def test_path_traversal_is_rejected(self):
        self.write_archive(bad_path=True)
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.root / "escaped").exists())
        self.assertFalse((self.root / "installed").exists())


if __name__ == "__main__":
    unittest.main()
