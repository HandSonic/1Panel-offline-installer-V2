"""Publisher-trusted package format; no network or application execution."""
import copy
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'tests')]
from resolved_inventory import ARCHES, canonical, digest
from resolved_transport import public_controls, resolve, verify_public_payload
from package_public_source import SOURCE_KIND, metadata
from test_validate_resolved_custom import Fixture
from test_resolved_inventory import responses


class Publisher:
    repo = 'HandSonic/1Panel-Build-v2'
    tag = 'v2.99.0'

    def __init__(self):
        self.bodies = {}
        self.assets = []
        self.downloaded = []
        self.reads = []
        self.mutate = None
        self.http = {}
        for arch in ARCHES:
            self.publish(Fixture(arch))
        self.refresh()
        for source, arches in [('official',[a for a in ARCHES if a!='loong64']),('enterprise',['amd64','arm64'])]:
            checksum, probes = responses(self.tag,'stable',source,arches)
            self.http[checksum['url']] = checksum
            self.http.update({p['url']:p for p in probes.values()})

    def publish(self, f):
        f.files['build-metadata/resolved-source.json'] = canonical(f.contract)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)/'raw.tar.gz'
            f.archive(p, lambda m: m.update(publication_format='package-only-v1'))
            out = io.BytesIO()
            with tarfile.open(p) as src, tarfile.open(fileobj=out, mode='w:gz') as dst:
                for item in src:
                    if '/build-metadata/' in item.name:item.mode = 0o644
                    dst.addfile(item, src.extractfile(item))
            raw = out.getvalue()
        name = f'1panel-{self.tag}-linux-{f.arch}.tar.gz'
        self.bodies[name] = raw
        self.bodies[name+'.sha256'] = (hashlib.sha256(raw).hexdigest()+'  '+name+'\n').encode()
        for part, raw in f.config.items():
            self.http['https://raw.githubusercontent.com/1Panel-dev/1Panel/'+f.contract['source']['commit']+'/'+f.contract['configuration'][part]['path']] = {'status':200,'body':raw}
        for name in f.contract['installer']['resources']:
            raw = f.files[name]
            if name == '1pctl':raw = raw.replace(b'ORIGINAL_VERSION=v2.99.0', b'ORIGINAL_VERSION=version')
            self.http['https://raw.githubusercontent.com/1Panel-dev/installer/'+f.contract['installer']['commit']+'/'+name] = {'status':200,'body':raw}
        self.refresh()

    def refresh(self):
        self.assets = [dict(id=100+i,name=n,size=len(b),digest='sha256:'+hashlib.sha256(b).hexdigest(),state='uploaded',
                            url=f'https://api.github.com/repos/{self.repo}/releases/assets/{100+i}')
                       for i,(n,b) in enumerate(self.bodies.items())]

    def release(self):
        return copy.deepcopy(dict(id=11,tag_name=self.tag,draft=False,prerelease=False,assets=self.assets))

    def asset_bytes(self,asset):return self.bodies[asset['name']]
    def download_asset(self,asset,destination):
        self.downloaded.append(asset['name'])
        Path(destination).write_bytes(self.bodies[asset['name']])
        if self.mutate:self.mutate(self)
    def run(self,*args):raise AssertionError('New format must not query Actions history')
    def read(self,url,method='GET'):
        self.reads.append(url)
        return {k:v for k,v in self.http[url].items() if k!='url'}
    def runtime(self):return resolve(self.tag,'stable',ROOT,client=self,read=self.read)


class PackagePublicTests(unittest.TestCase):
    def test_new_plan_and_each_architecture_bind_exact_assets_without_ci(self):
        p=Publisher();runtime=p.runtime()
        self.assertEqual(runtime['upstream']['source_kind'],SOURCE_KIND)
        self.assertEqual(set(runtime['upstream']['records']),set(ARCHES))
        self.assertEqual(len(p.downloaded),len(ARCHES))
        immutable = [url for url in p.reads if 'raw.githubusercontent.com' in url]
        self.assertEqual(len(immutable), len(set(immutable)))
        for arch in ARCHES:
            self.assertIsNone(verify_public_payload(runtime,arch,p)['build_repository_commit'])

    def test_missing_first_archive_uses_another_architecture_and_only_missing_shard_fails(self):
        p=Publisher();name=f'1panel-{p.tag}-linux-amd64.tar.gz'
        p.assets=[a for a in p.assets if a['name']!=name]
        runtime=p.runtime()
        self.assertIsNotNone(runtime['source_contract'])
        self.assertNotIn('amd64',runtime['upstream']['records'])
        with self.assertRaises(ValueError):verify_public_payload(runtime,'amd64',p)
        verify_public_payload(runtime,'arm64',p)
        self.assertEqual(len([r for r in runtime['inventory']['rows'] if r['source']=='custom']),7)

    def test_bad_sibling_checksum_fails_only_that_shard(self):
        p=Publisher();name=f'1panel-{p.tag}-linux-s390x.tar.gz.sha256'
        p.bodies[name]=b'0'*64+b'  wrong.tar.gz\n';p.refresh()
        runtime=p.runtime();verify_public_payload(runtime,'amd64',p)
        with self.assertRaises(ValueError):verify_public_payload(runtime,'s390x',p)

    def test_changed_selected_asset_after_plan_is_rejected(self):
        p=Publisher();runtime=p.runtime()
        p.assets[0]['id']+=10000
        with self.assertRaises(ValueError):verify_public_payload(runtime,'amd64',p)

    def test_download_mutation_is_rejected(self):
        p=Publisher();p.mutate=lambda obj:obj.assets[0].update(id=obj.assets[0]['id']+10000)
        with self.assertRaises(ValueError):public_controls(p.tag,'stable',p,payload_architectures=('amd64',))

    def test_marker_damage_never_falls_back_to_legacy_control_protocol(self):
        p=Publisher();name=f'1panel-{p.tag}-linux-amd64.tar.gz'
        p.bodies[name]=b'not an archive';p.refresh()
        with self.assertRaises((ValueError,tarfile.TarError,OSError)):
            public_controls(p.tag,'stable',p,payload_architectures=('amd64',))

    def test_official_installer_mismatch_rejects_custom_only(self):
        p=Publisher()
        url=next(url for url in p.http if '/installer/' in url)
        p.http[url]['body'] += b'tampered'
        runtime=p.runtime()
        self.assertIsNone(runtime['source_contract'])
        self.assertIn('Immutable official installer resource mismatch', runtime['custom_failure'])
        self.assertTrue(any(row['source']=='official' for row in runtime['inventory']['rows']))

    def test_wrong_repository_rejected(self):
        p=Publisher();p.repo='someone/other'
        with self.assertRaises(ValueError):public_controls(p.tag,'stable',p)

if __name__=='__main__':unittest.main()
