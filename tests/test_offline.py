import gzip
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from validate_payload import ARCHES, REQUIRED, docker, elf, members, APP_REQUIRED
from patch_installer import patch, HELPERS
from validate_release import validate

def binary(arch):
    cls, endian, machine = ARCHES[arch]
    b = bytearray(64)
    b[:4] = b'\x7fELF'; b[4] = cls; b[5] = endian
    b[18:20] = machine.to_bytes(2, 'little' if endian == 1 else 'big')
    return bytes(b)

def archive(path, arch, omit=None, wrong=None, extra=None):
    with open(path, 'wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as compressed, tarfile.open(fileobj=compressed, mode='w') as a:
        for name in REQUIRED:
            if name == omit: continue
            content = binary(wrong or arch)
            m = tarfile.TarInfo('docker/' + name); m.size=len(content); m.mode=0o755
            a.addfile(m, io.BytesIO(content))
        if extra:
            m=tarfile.TarInfo(extra);m.size=1;a.addfile(m,io.BytesIO(b'x'))

class ValidationTests(unittest.TestCase):
    def test_all_seven_elf_architectures(self):
        for arch in ARCHES:
            with self.subTest(arch=arch): elf(binary(arch), arch)
    def test_wrong_architecture_and_html(self):
        with self.assertRaises(ValueError): elf(binary('amd64'), 'arm64')
        with self.assertRaises(ValueError): elf(b'<html>not found</html>', 'amd64')
    def test_all_seven_archives(self):
        with tempfile.TemporaryDirectory() as t:
            for arch in ARCHES:
                p=Path(t)/'docker.tgz';archive(p,arch)
                self.assertEqual(len(docker(p,arch)),8)
    def test_missing_payload_each_required_binary(self):
        with tempfile.TemporaryDirectory() as t:
            for missing in REQUIRED:
                p=Path(t)/'docker.tgz';archive(p,'amd64',omit=missing)
                with self.assertRaises(ValueError): docker(p,'amd64')
    def test_wrong_arch_and_traversal(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'docker.tgz'
            archive(p,'amd64',wrong='arm64')
            with self.assertRaises(ValueError): docker(p,'amd64')
            archive(p,'amd64',extra='../escape')
            with self.assertRaises(ValueError): docker(p,'amd64')
    def test_truncated_gzip(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'docker.tgz';archive(p,'amd64');p.write_bytes(p.read_bytes()[:-6])
            with self.assertRaises((EOFError,OSError)): docker(p,'amd64')
    def test_patch_current_upstream_and_idempotence(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'install.sh';p.write_bytes((ROOT/'tests/fixtures/historical-installers/3faa744fd158283470b48b3a971dc98cc390c7f291d4287b170416ae862dd28f.sh').read_bytes())
            patch(p); first=p.read_text();patch(p)
            self.assertEqual(first,p.read_text());self.assertIn(HELPERS,first)
            self.assertIn('function Install_AppStore()',first)
            self.assertNotIn('function Install_Docker_Offline()',first)
            self.assertNotIn('curl',HELPERS)
            subprocess.run(['bash','-n',str(p)],check=True)
    def test_patch_actual_v232_release(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'install.sh';p.write_bytes((ROOT/'tests/fixtures/historical-installers/3faa744fd158283470b48b3a971dc98cc390c7f291d4287b170416ae862dd28f.sh').read_bytes())
            patch(p);self.assertIn(HELPERS,p.read_text())
            subprocess.run(['bash','-n',str(p)],check=True)
    def test_unknown_upstream_fails_without_modification(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'install.sh'; original='#!/bin/bash\necho unknown\n';p.write_text(original)
            with self.assertRaises(ValueError): patch(p)
            self.assertEqual(p.read_text(),original)
    def test_patch_preserves_last_function_trailing_calls(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'install.sh'
            p.write_text('CURRENT_DIR=/tmp\nfunction log() { :; }\nfunction Install_Docker(){\n :\n}\necho SENTINEL\n')
            patch(p)
            self.assertTrue(p.read_text().endswith('echo SENTINEL\n'))
    def test_partial_patch_fails(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'install.sh';p.write_text('# OFFLINE_INSTALLER_PATCH_V2\n')
            with self.assertRaises(ValueError): patch(p)
    def test_failed_curl_tries_next_candidate(self):
        # Extract library functions only. Never execute any downloaded installer.
        source=(ROOT/'prepare_offline.sh').read_text()
        functions=source[source.index('download_if_missing()'):source.index('handle_missing_arch()')]
        with tempfile.TemporaryDirectory() as t:
            t=Path(t); fake=t/'curl';fake.write_text('#!/bin/bash\nexit 22\n');fake.chmod(0o755)
            script=f'BASE_DIR={ROOT}\n'+functions+f'\ndownload_with_candidates {t}/missing.tgz archive 0 https://invalid/one https://invalid/two\n'
            r=subprocess.run(['bash','-c',script],env=dict(os.environ,PATH=str(t)+':'+os.environ['PATH']),capture_output=True,text=True)
            self.assertNotEqual(r.returncode,0)
            self.assertEqual(r.stdout.count('Downloading https://invalid/'),2)
            self.assertFalse((t/'missing.tgz').exists())
    def test_failed_first_candidate_then_valid_second(self):
        source=(ROOT/'prepare_offline.sh').read_text()
        functions=source[source.index('download_if_missing()'):source.index('handle_missing_arch()')]
        with tempfile.TemporaryDirectory() as t:
            t=Path(t);fixture=t/'valid.tgz';archive(fixture,'amd64')
            fake=t/'curl'
            fake.write_text('#!/bin/bash\n[[ "$*" == *invalid/first* ]] && exit 22\nwhile [[ $# -gt 0 ]]; do\n if [[ "$1" == "-o" ]]; then cp "'+str(fixture)+'" "$2"; exit 0; fi\n shift\ndone\nexit 2\n')
            fake.chmod(0o755)
            script=f'BASE_DIR={ROOT}\n'+functions+f'\ndownload_with_candidates {t}/download.tgz archive 0 https://invalid/first https://fixture/second\n'
            r=subprocess.run(['bash','-c',script],env=dict(os.environ,PATH=str(t)+':'+os.environ['PATH']),capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(json.loads((t/'download.tgz.source.json').read_text())['url'],'https://fixture/second')
    def test_changed_cached_bytes_fail_provenance(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'artifact';p.write_bytes(b'original')
            tool=ROOT/'scripts/validate_payload.py'
            subprocess.run([sys.executable,str(tool),'provenance',str(p),'https://fixture'],check=True)
            p.write_bytes(b'changed')
            result=subprocess.run([sys.executable,str(tool),'cached',str(p),'https://fixture'],capture_output=True)
            self.assertNotEqual(result.returncode,0)
    def test_empty_partial_matrix_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            t=Path(t);(t/'checksums.txt').write_text('')
            with self.assertRaises(ValueError):validate(t,'v2.3.2',ROOT/'tests/fixtures/community-matrix.json')
    def test_checksum_paths_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            t=Path(t);(t/'checksums.txt').write_text('0'*64+'  official/file.tar.gz\n')
            with self.assertRaises(ValueError):validate(t,'v2.3.2',ROOT/'tests/fixtures/community-matrix.json')
    def make_release(self, root, omit=None, unpinned=False, dev_config=False, unreviewed_upgrade_source=None):
        matrix=json.loads((ROOT/'tests/fixtures/community-matrix.json').read_text())
        from test_runtime_contract import runtime
        from runtime_fixtures import activate,dependencies,refresh,vendor_pins
        from resolved_inventory import digest as contract_digest,INSTALLER_REQUIRED
        dependencies(root)
        value=runtime(version='v2.3.2',enterprise=False)
        source_contract=value['source_contract']
        resources={name:(HELPERS.encode() if name=='install.sh' else b'ORIGINAL_VERSION=version\n' if name=='1pctl' else b'fixture') for name in INSTALLER_REQUIRED}
        source_contract['installer']['resources']={name:{'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()} for name,raw in resources.items()}
        source_contract['resources']['geoip'].update(bytes=len(b'fixture'),sha256=hashlib.sha256(b'fixture').hexdigest())
        refresh(value);activate(self,root,value)
        (root/'upgrade_offline.sh').write_bytes((ROOT/'upgrade_offline.sh').read_bytes())
        checks=[]
        fixture_locks={'docker':{},'compose':{}}
        official_locks={}
        for source, arches in matrix.items():
            for arch in arches:
                prefix=f'1panel-v2.3.2-{source}-offline-linux-{arch}'
                (root/source).mkdir(exist_ok=True)
                dp=root/'docker.tgz';archive(dp,arch)
                data={'docker.tgz':dp.read_bytes(),'docker-compose':binary(arch),
                      'docker.service':b'service','upgrade.sh':(b'#!/bin/bash\n# obsolete updater\n' if source==unreviewed_upgrade_source else (ROOT/'upgrade_offline.sh').read_bytes()),
                      'install.sh':HELPERS.encode()}
                for name in APP_REQUIRED: data[name]=binary(arch) if name in ['1panel-core','1panel-agent'] else b'fixture'
                if source=='custom':
                    from embedded_configuration import expected_bytes
                    for component in ['core','agent']:data['1panel-'+component]+=expected_bytes('v2.3.2',component)[0 if dev_config and component=='core' else 1]
                    data['1pctl']=b'ORIGINAL_VERSION=v2.3.2\n'
                inputs={}
                for c,name in [('docker','docker.tgz'),('compose','docker-compose')]:
                    inputs[c]={'url':f'https://fixture.invalid/{c}/{arch}','version':'fixture',
                               'bytes':len(data[name]),'sha256':hashlib.sha256(data[name]).hexdigest()}
                    fixture_locks[c][arch]=dict(inputs[c])
                m={'inputs':inputs,'architecture':arch,'source':source,'app_version':'v2.3.2',
                   'payloads':{n:{'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()} for n,b in data.items()},
                   'docker_binaries':docker(dp,arch)}
                if source=='custom':
                    m['inputs']['app']={'url':'https://fixture.invalid/app','checksum_url':'https://fixture.invalid/app.sha256','sha256':'d'*64,'upstream_sha256':'d'*64}
                    upstream={'schema_version':1,'architecture':arch,'version':'v2.3.2','edition':'community',
                              'source_commit':source_contract['source']['commit'],'installer_commit':source_contract['installer']['commit'],'build_repository_commit':value['upstream']['producer_commit'],
                              'resolved_contract_sha256':contract_digest(source_contract),
                              'mode':'stable',**{k+'_version':v for k,v in source_contract['toolchain'].items()},
                              'files':{name:{'size':len(body),'sha256':hashlib.sha256(body).hexdigest()} for name,body in data.items() if name not in ['docker.tgz','docker-compose','docker.service']}}
                    raw=json.dumps(upstream).encode();data['manifest.json']=raw
                    m['upstream_provenance']=upstream;m['upstream_manifest_sha256']=hashlib.sha256(raw).hexdigest()
                else:
                    m['inputs']['app']={'version':'v2.3.2','url':f'https://resource.fit2cloud.com/1panel/package/v2/stable/v2.3.2/release/1panel-v2.3.2-linux-{arch}.tar.gz','sha256':'e'*64,'bytes':123}
                    official_locks[arch]=dict(m['inputs']['app'])
                if unpinned: m['inputs']['docker']['version']='unreviewed'
                data['offline-manifest.json']=json.dumps(m).encode()
                if omit: data.pop(omit)
                dest=root/source/(prefix+'.tar.gz')
                with tarfile.open(dest,'w:gz') as output:
                    for name,body in data.items():
                        member=tarfile.TarInfo(prefix+'/'+name);member.size=len(body)
                        output.addfile(member,io.BytesIO(body))
                checks.append(hashlib.sha256(dest.read_bytes()).hexdigest()+'  '+dest.name)
        for c, pins in fixture_locks.items(): (root/(c+'-sources.json')).write_text(json.dumps(pins))
        value['inventory']['official']=vendor_pins(value['version'],'official',official_locks)
        refresh(value,root);activate(self,root,value)
        (root/'checksums.txt').write_text('\n'.join(checks)+'\n')
        return matrix
    def test_full_thirteen_package_release_and_missing_asset(self):
        with tempfile.TemporaryDirectory() as t:
            t=Path(t);self.make_release(t)
            self.assertEqual(validate(t,'v2.3.2',ROOT/'tests/fixtures/community-matrix.json',lock_root=t),13)
            next((t/'custom').glob('*.tar.gz')).unlink()
            with self.assertRaises(ValueError):validate(t,'v2.3.2',ROOT/'tests/fixtures/community-matrix.json',lock_root=t)
    def test_self_consistent_obsolete_community_upgrader_rejected(self):
        for source in ('official', 'custom'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as t:
                t=Path(t);self.make_release(t,unreviewed_upgrade_source=source)
                with self.assertRaisesRegex(ValueError, 'Community upgrade script differs from reviewed source; rebuild required'):
                    validate(t,'v2.3.2',ROOT/'tests/fixtures/community-matrix.json',lock_root=t)

    def test_missing_application_payload_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            t=Path(t);self.make_release(t,omit='1panel-core')
            with self.assertRaises((ValueError,KeyError)):validate(t,'v2.3.2',ROOT/'tests/fixtures/community-matrix.json',lock_root=t)
    def test_unreviewed_version_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            t=Path(t);self.make_release(t,unpinned=True)
            with self.assertRaises(ValueError):validate(t,'v2.3.2',ROOT/'tests/fixtures/community-matrix.json',lock_root=t)
    def test_release_payload_corruption(self):
        with tempfile.TemporaryDirectory() as t:
            t=Path(t);self.make_release(t)
            victim=next((t/'custom').glob('*.tar.gz'))
            victim.write_bytes(victim.read_bytes()+b'corrupt')
            with self.assertRaises(ValueError):validate(t,'v2.3.2',ROOT/'tests/fixtures/community-matrix.json',lock_root=t)
    def test_outer_gzip_missing_trailer_rejected_even_with_matching_checksum(self):
        with tempfile.TemporaryDirectory() as t:
            t=Path(t);self.make_release(t);victim=next((t/'official').glob('*.tar.gz'))
            victim.write_bytes(victim.read_bytes()[:-8])
            checksum=t/'checksums.txt';lines=[]
            for line in checksum.read_text().splitlines():
                old,name=line.split('  ',1)
                lines.append((hashlib.sha256(victim.read_bytes()).hexdigest() if name==victim.name else old)+'  '+name)
            checksum.write_text('\n'.join(lines)+'\n')
            with self.assertRaises((ValueError,EOFError,OSError)):validate(t,'v2.3.2',ROOT/'tests/fixtures/community-matrix.json',lock_root=t)
    def test_custom_dev_config_fails_even_with_self_consistent_hashes(self):
        with tempfile.TemporaryDirectory() as t:
            t=Path(t);self.make_release(t,dev_config=True)
            with self.assertRaisesRegex(ValueError,'normalized production configuration'):
                validate(t,'v2.3.2',ROOT/'tests/fixtures/community-matrix.json',lock_root=t)
    def test_custom_producer_contract_rejects_self_consistent_unreviewed_inputs(self):
        for fault in ['node_version','npm_version','go_version','installer_commit','GeoIP.mmdb','lang/en.sh','1pctl']:
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as td:
                root=Path(td);self.make_release(root)
                path=root/'custom/1panel-v2.3.2-custom-offline-linux-amd64.tar.gz';prefix=path.name.removesuffix('.tar.gz')+'/'
                with tarfile.open(path) as archive:
                    contents={m.name[len(prefix):]:archive.extractfile(m).read() for m in archive.getmembers() if m.isfile()}
                upstream=json.loads(contents['manifest.json']);offline=json.loads(contents['offline-manifest.json'])
                if fault in ['node_version','npm_version','go_version']:upstream[fault]='0.0.1'
                elif fault=='installer_commit':upstream[fault]='0'*40
                else:
                    contents[fault]+=b'\nchanged resource\n'
                    upstream['files'][fault]={'size':len(contents[fault]),'sha256':hashlib.sha256(contents[fault]).hexdigest()}
                    offline['payloads'][fault]={'bytes':len(contents[fault]),'sha256':hashlib.sha256(contents[fault]).hexdigest()}
                contents['manifest.json']=json.dumps(upstream).encode()
                offline['upstream_provenance']=upstream
                offline['upstream_manifest_sha256']=hashlib.sha256(contents['manifest.json']).hexdigest()
                contents['offline-manifest.json']=json.dumps(offline).encode()
                with tarfile.open(path,'w:gz') as archive:
                    for name,body in contents.items():
                        member=tarfile.TarInfo(prefix+name);member.size=len(body);archive.addfile(member,io.BytesIO(body))
                checks=root/'checksums.txt';lines=[]
                for line in checks.read_text().splitlines():
                    old,name=line.split('  ',1);lines.append((hashlib.sha256(path.read_bytes()).hexdigest() if name==path.name else old)+'  '+name)
                checks.write_text('\n'.join(lines)+'\n')
                with self.assertRaisesRegex(ValueError,'resolved producer|resolved source'):
                    validate(root,'v2.3.2',ROOT/'tests/fixtures/community-matrix.json',lock_root=root)
    def test_declared_matrix_is_thirteen_and_all_arches(self):
        m=json.loads((ROOT/'tests/fixtures/community-matrix.json').read_text())
        self.assertEqual(sum(map(len,m.values())),13)
        self.assertEqual(set().union(*map(set,m.values())),set(ARCHES))

if __name__ == '__main__': unittest.main()
