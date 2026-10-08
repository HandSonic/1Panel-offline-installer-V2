import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from validate_payload import APP_REQUIRED
from validate_upstream import verify_directory,checksum_sidecar

class UpstreamManifestTests(unittest.TestCase):
    def fixture(self,root):
        files={}
        for name in APP_REQUIRED+['install.sh']:
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(('upstream '+name).encode())
            files[name]={'size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        data={'schema_version':1,'version':'v2.3.2','architecture':'amd64','edition':'community',
              'source_commit':'a'*40,'installer_commit':'b'*40,'build_repository_commit':'c'*40,'files':files}
        (root/'manifest.json').write_text(json.dumps(data));return data
    def test_sidecar_binds_hash_to_exact_flat_filename(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'sidecar';path.write_text('a'*64+'  package.tar.gz\n')
            self.assertEqual(checksum_sidecar(path,'package.tar.gz'),'a'*64)
            with self.assertRaises(ValueError):checksum_sidecar(path,'other.tar.gz')
            path.write_text('a'*64+'  ../package.tar.gz\n')
            with self.assertRaises(ValueError):checksum_sidecar(path,'package.tar.gz')
    def test_complete_manifest(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);self.fixture(root);self.assertEqual(verify_directory(root,'amd64','v2.3.2')['architecture'],'amd64')
    def test_changed_file_fails(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);self.fixture(root);(root/'1panel-core').write_bytes(b'changed')
            with self.assertRaises(ValueError):verify_directory(root,'amd64','v2.3.2')
    def test_extra_file_fails(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);self.fixture(root);(root/'unrecorded').write_bytes(b'extra')
            with self.assertRaises(ValueError):verify_directory(root,'amd64','v2.3.2')
    def test_legacy_custom_without_manifest_fails(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(FileNotFoundError):verify_directory(Path(t),'amd64','v2.3.2')
    def test_wrong_architecture_fails(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);self.fixture(root)
            with self.assertRaises(ValueError):verify_directory(root,'arm64','v2.3.2')

if __name__=='__main__':unittest.main()
