import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch as mock_patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import prepare_enterprise
from validate_payload import APP_REQUIRED,digest
from validate_release import validate
from test_offline import archive,binary

class EnterpriseTests(unittest.TestCase):
    def fixture(self, root, version='v2.3.2', appstore=True):
        arch='amd64';cache=root/'cache';cache.mkdir()
        prefix=f'1panel-{version}-linux-{arch}/'
        source=cache/f'enterprise-{version}-{arch}.tar.gz'
        data={name:(binary(arch) if name in ['1panel-core','1panel-agent'] else ('official '+name).encode()) for name in APP_REQUIRED}
        data['install.sh']=b'CURRENT_DIR=/tmp\nfunction log() { :; }\nfunction Install_Docker(){\n :\n}\necho main-preserved\n'
        if appstore:data['install.sh'] += b'function Install_AppStore(){\n :\n}\n'
        data['upgrade.sh']=b'#!/bin/bash\n# enterprise official upgrade unchanged\n'
        if appstore:data['appstore.tar.gz']=b'official enterprise appstore bytes'
        (root/'config').mkdir()
        (root/'config/enterprise-contracts.json').write_text(json.dumps({version:{
            'installer_sha256':hashlib.sha256(data['install.sh']).hexdigest(), 'appstore_required':appstore}}))
        with tarfile.open(source,'w:gz') as output:
            for name,body in data.items():
                info=tarfile.TarInfo(prefix+name);info.size=len(body);output.addfile(info,io.BytesIO(body))
        (root/f'enterprise-sources-{version}.json').write_text(json.dumps({arch:dict(digest(source),version=version,url='https://fixture.invalid/enterprise')}))
        docker_file=cache/'docker-fixture-amd64.tgz';archive(docker_file,arch)
        compose=cache/'compose-fixture-amd64';compose.write_bytes(binary(arch))
        for component,path in [('docker',docker_file),('compose',compose)]:
            (root/f'{component}-sources.json').write_text(json.dumps({arch:dict(digest(path),version='fixture',url='https://fixture.invalid/'+component)}))
        (root/'docker.service').write_text('reviewed service')
        return cache,data
    def test_original_exact_and_enhanced_preserves_enterprise(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);cache,data=self.fixture(root);out=root/'out'
            # Real QA cache may link to audited bytes on another filesystem.
            compose=cache/'compose-fixture-amd64';real=cache/'compose-real';compose.rename(real);compose.symlink_to(real)
            with mock_patch.object(prepare_enterprise,'ROOT',root):
                original,enhanced=prepare_enterprise.build('v2.3.2','amd64',cache,out)
            self.assertEqual(original.read_bytes(),(cache/'enterprise-v2.3.2-amd64.tar.gz').read_bytes())
            with tarfile.open(enhanced) as archive:
                prefix='1panel-v2.3.2-linux-amd64/'
                for name,body in data.items():
                    if name!='install.sh':self.assertEqual(archive.extractfile(prefix+name).read(),body)
                self.assertIn(b'echo main-preserved',archive.extractfile(prefix+'install.sh').read())
                self.assertEqual(archive.extractfile(prefix+'upgrade.sh').read(),data['upgrade.sh'])
            matrix=root/'matrix.json';matrix.write_text(json.dumps({'enterprise-original':['amd64'],'enterprise-docker':['amd64']}))
            (out/'checksums.txt').write_text('\n'.join(digest(p)['sha256']+'  '+p.name for p in [original,enhanced])+'\n')
            self.assertEqual(validate(out,'v2.3.2',matrix,lock_root=root),2)
            original.write_bytes(original.read_bytes()+b'tamper')
            with self.assertRaises(ValueError):validate(out,'v2.3.2',matrix,lock_root=root)
    def test_wrong_enterprise_source_checksum_fails(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);cache,data=self.fixture(root)
            pin=json.loads((root/'enterprise-sources-v2.3.2.json').read_text())['amd64']
            pin['sha256']='0'*64
            with mock_patch.object(prepare_enterprise.subprocess,'run',side_effect=RuntimeError('network blocked in fixture')):
                with self.assertRaises(RuntimeError):prepare_enterprise.acquire(pin,cache/'enterprise-v2.3.2-amd64.tar.gz')

    def test_v225_preserves_missing_appstore_and_original_upgrade(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);cache,data=self.fixture(root,'v2.2.5',appstore=False);out=root/'out'
            with mock_patch.object(prepare_enterprise,'ROOT',root):
                original,enhanced=prepare_enterprise.build('v2.2.5','amd64',cache,out)
            self.assertEqual(original.read_bytes(),(cache/'enterprise-v2.2.5-amd64.tar.gz').read_bytes())
            with tarfile.open(enhanced) as archive:
                names=archive.getnames();prefix='1panel-v2.2.5-linux-amd64/'
                self.assertNotIn(prefix+'appstore.tar.gz',names)
                self.assertNotIn(b'Install_AppStore',archive.extractfile(prefix+'install.sh').read())
                self.assertEqual(archive.extractfile(prefix+'upgrade.sh').read(),data['upgrade.sh'])
            matrix=root/'matrix.json';matrix.write_text(json.dumps({'enterprise-original':['amd64'],'enterprise-docker':['amd64']}))
            (out/'checksums.txt').write_text(''.join(digest(p)['sha256']+'  '+p.name+'\n' for p in [original,enhanced]))
            self.assertEqual(validate(out,'v2.2.5',matrix,lock_root=root),2)

    def test_capability_mismatch_and_changed_installer_fail(self):
        for change in ['appstore_required','installer_sha256']:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as t:
                root=Path(t);cache,_=self.fixture(root);p=root/'config/enterprise-contracts.json';data=json.loads(p.read_text())
                data['v2.3.2'][change]=False if change=='appstore_required' else '0'*64;p.write_text(json.dumps(data))
                with mock_patch.object(prepare_enterprise,'ROOT',root),self.assertRaises(ValueError):
                    prepare_enterprise.build('v2.3.2','amd64',cache,root/'out')

if __name__=='__main__':unittest.main()
