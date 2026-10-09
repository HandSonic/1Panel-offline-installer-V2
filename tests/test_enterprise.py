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
from test_runtime_contract import runtime
from runtime_fixtures import activate,dependencies,refresh,vendor_pins

class EnterpriseTests(unittest.TestCase):
    def fixture(self, root, version='v2.3.2', appstore=True, installer=None):
        arch='amd64';cache=root/'cache';cache.mkdir()
        prefix=f'1panel-{version}-linux-{arch}/'
        source=cache/f'enterprise-{version}-{arch}.tar.gz'
        data={name:(binary(arch) if name in ['1panel-core','1panel-agent'] else ('official '+name).encode()) for name in APP_REQUIRED}
        if installer is None:
            samples=Path(__file__).resolve().parent/'fixtures/historical-installers'
            row=next(row for row in json.loads((samples/'index.json').read_text())['fixtures'] if row['appstore_install']==appstore)
            installer=(samples/row['file']).read_bytes()+b'\necho main-preserved\n'
        data['install.sh']=installer
        for name in ['1panel-core.service','1panel-agent.service']:
            data[name]=('preserved vendor root service '+name).encode()
        data['upgrade.sh']=b'#!/bin/bash\n# enterprise official upgrade unchanged\n'
        if appstore:data['appstore.tar.gz']=b'official enterprise appstore bytes'
        with tarfile.open(source,'w:gz') as output:
            for name,body in data.items():
                info=tarfile.TarInfo(prefix+name);info.size=len(body);output.addfile(info,io.BytesIO(body))
        docker_file=cache/'docker-fixture-amd64.tgz';archive(docker_file,arch)
        compose=cache/'compose-fixture-amd64';compose.write_bytes(binary(arch))
        dependencies(root)
        for component,path in [('docker',docker_file),('compose',compose)]:
            file=root/f'{component}-sources.json';pins=json.loads(file.read_text())
            pins[arch]=dict(digest(path),version='fixture',url='https://fixture.invalid/'+component);file.write_text(json.dumps(pins))
        (root/'docker.service').write_text('reviewed service')
        value=runtime(root,version)
        value['inventory']['enterprise']=vendor_pins(version,'enterprise',{arch:digest(source)})
        self.runtime=refresh(value,root);activate(self,root,self.runtime)
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
            pin=dict(self.runtime['inventory']['enterprise']['archives']['amd64'])
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
        from enterprise_contract import validate_layout
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);cache,data=self.fixture(root)
            with tarfile.open(cache/'enterprise-v2.3.2-amd64.tar.gz') as archive:
                entries={m.name:m for m in archive.getmembers()}
            prefix='1panel-v2.3.2-linux-amd64/'
            for installer in (data['install.sh'].replace(b'    Install_AppStore',b'    : # Install_AppStore'),b'unsupported cohort'):
                with self.assertRaises(ValueError):validate_layout(entries,prefix,installer,'v2.3.2',root)
            entries.pop(prefix+'appstore.tar.gz')
            with self.assertRaises(ValueError):validate_layout(entries,prefix,data['install.sh'],'v2.3.2',root)

    def test_historical_originals_and_upgrade_resources_are_preserved(self):
        repository=Path(__file__).resolve().parents[1]
        samples=repository/'tests/fixtures/historical-installers'
        seen=set()
        for row in json.loads((samples/'index.json').read_text())['fixtures']:
            if row['appstore_install'] or row['source_sha256'] in seen:continue
            seen.add(row['source_sha256']);version='v2.99.0'
            with self.subTest(sample=row['file']),tempfile.TemporaryDirectory() as td:
                installer=(samples/row['file']).read_bytes()
                root=Path(td);cache,data=self.fixture(root,version,appstore=False,installer=installer)
                with mock_patch.object(prepare_enterprise,'ROOT',root):
                    original,enhanced=prepare_enterprise.build(version,'amd64',cache,root/'out')
                self.assertEqual(original.read_bytes(),(cache/f'enterprise-{version}-amd64.tar.gz').read_bytes())
                with tarfile.open(enhanced) as output:
                    prefix=f'1panel-{version}-linux-amd64/'
                    for name,content in data.items():
                        if name!='install.sh':self.assertEqual(output.extractfile(prefix+name).read(),content)
                    modified=output.extractfile(prefix+'install.sh').read()
                    self.assertEqual(b'NON_INTERACTIVE=' in modified,row['non_interactive_cli'])
                    self.assertNotIn(b'function Install_AppStore',modified)
                (root/'out/checksums.txt').write_text(''.join(digest(p)['sha256']+'  '+p.name+'\n' for p in [original,enhanced]))
                self.assertEqual(validate(root/'out',version,{'enterprise-original':['amd64'],'enterprise-docker':['amd64']},lock_root=root),2)

if __name__=='__main__':unittest.main()
