import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from validate_payload import APP_BASE_REQUIRED, APP_REQUIRED
from validate_upstream import verify_directory,checksum_sidecar

class UpstreamManifestTests(unittest.TestCase):
    def fixture(self,root,legacy=False):
        fixtures=Path(__file__).resolve().parent/'fixtures'
        if legacy:
            rows=json.loads((fixtures/'service-layout/index.json').read_text())['files']
            installer=next(fixtures/'service-layout'/row['file'] for row in rows if row['member']=='install.sh')
            required=APP_BASE_REQUIRED+['1panel-core.service','1panel-agent.service']
        else:
            row=json.loads((fixtures/'historical-installers/index.json').read_text())['fixtures'][0]
            installer=fixtures/'historical-installers'/row['file']
            required=APP_REQUIRED
        files={}
        for name in required+['install.sh']:
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(installer.read_bytes() if name=='install.sh' else ('upstream '+name).encode())
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


    def test_root_systemd_manifest_uses_authenticated_installer_interface(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);self.fixture(root,legacy=True)
            self.assertEqual(verify_directory(root,'amd64','v2.3.2')['architecture'],'amd64')
            self.assertFalse((root/'initscript').exists())

    def test_missing_required_service_is_not_replaced_by_other_layout(self):
        for legacy in (False,True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as t:
                root=Path(t);data=self.fixture(root,legacy=legacy)
                name='1panel-agent.service' if legacy else 'initscript/1panel-agent.procd'
                del data['files'][name];(root/name).unlink()
                (root/'manifest.json').write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError,'Required service resource missing'):
                    verify_directory(root,'amd64','v2.3.2')

    def test_hashed_unknown_or_inactive_installer_is_not_accepted(self):
        for raw in (b'#!/bin/bash\nexit 0\n', b'#!/bin/bash\n# cp ./1panel-core.service /etc/systemd/system\n'):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as t:
                root=Path(t);data=self.fixture(root,legacy=True)
                (root/'install.sh').write_bytes(raw)
                data['files']['install.sh']={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
                (root/'manifest.json').write_text(json.dumps(data))
                with self.assertRaises(ValueError):verify_directory(root,'amd64','v2.3.2')

    def test_hash_tamper_precedes_layout_recognition(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);self.fixture(root,legacy=True)
            (root/'install.sh').write_bytes(b'#!/bin/bash\nexit 0\n')
            with self.assertRaisesRegex(ValueError,'hash mismatch'):
                verify_directory(root,'amd64','v2.3.2')

    def test_symlinked_service_is_rejected_even_when_bytes_match(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);self.fixture(root,legacy=True)
            path=root/'1panel-agent.service';raw=path.read_bytes();path.unlink()
            with tempfile.TemporaryDirectory() as other:
                target=Path(other)/'service';target.write_bytes(raw);path.symlink_to(target)
                with self.assertRaisesRegex(ValueError,'Unsafe'):
                    verify_directory(root,'amd64','v2.3.2')

if __name__=='__main__':unittest.main()
