import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from release_asset_repair import repair

class FakeGitHub:
    repo='owner/repo';tag='v2.3.2'
    def __init__(self, failure=None):
        self.assets={1:('package.tar.gz',b'old archive'),2:('checksums.txt',b'old checksum')}
        self.body='old release notes';self.failure=failure;self.failed=False;self.calls=[]
    def release(self):return {'body':self.body,'assets':[{'id':i,'name':n,'size':len(b),'digest':'sha256:'+hashlib.sha256(b).hexdigest()} for i,(n,b) in self.assets.items()]}
    def upload(self,path):
        self.calls.append(('upload',path.name))
        if any(n==path.name for n,b in self.assets.values()):raise RuntimeError('Canonical upload does not clobber')
        if self.failure=='upload':raise RuntimeError('Upload failed')
        self.assets[max(self.assets,default=0)+1]=(path.name,path.read_bytes())
        if self.failure=='uncertain-upload' and not self.failed:
            self.failed=True;raise RuntimeError('Upload response lost')
    def download(self,name,directory):
        data=next(b for n,b in self.assets.values() if n==name)
        (directory/name).write_bytes(b'bad' if self.failure=='bad_readback' else data)
    def rename(self,asset_id,name):
        self.calls.append(('rename',asset_id,name));raise AssertionError('No automatic rename')
    def notes(self,body):
        self.calls.append(('notes',body));raise AssertionError('No release-note mutation')

class RepairTests(unittest.TestCase):
    def fixture(self,failure=None):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
        paths=[]
        for name in ('package.tar.gz','checksums.txt'):
            path=root/name;path.write_bytes(('new '+name).encode());paths.append(path)
        return FakeGitHub(failure),paths,root/'journal.json'

    def test_conflicting_bytes_stop_with_exact_identity_before_any_upload(self):
        client,files,journal=self.fixture();before=copy.deepcopy(client.assets)
        new=files[0].parent/'additional.tar.gz';new.write_bytes(b'new missing archive');files.insert(0,new)
        with self.assertRaisesRegex(ValueError,'deletion confirmation'):repair(client,files,journal)
        state=json.loads(journal.read_text());self.assertEqual(state['phase'],'confirmation_required')
        expected=[{'asset_id':i,'name':name,'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
                   'reason':'different-content'} for i,(name,raw) in before.items()]
        self.assertEqual(state['conflicts'],sorted(expected,key=lambda row:row['name']))
        self.assertEqual(client.calls,[]);self.assertEqual(client.assets,before)

    def test_identical_content_is_a_true_noop_without_backups_or_notes(self):
        client,files,journal=self.fixture()
        client.assets={i+1:(p.name,p.read_bytes()) for i,p in enumerate(files)}
        before=copy.deepcopy(client.assets)
        result=repair(client,files,journal)
        self.assertEqual(result['phase'],'complete');self.assertEqual(result['operations'],[])
        self.assertEqual(client.assets,before);self.assertEqual(client.calls,[])
        self.assertEqual(client.body,'old release notes')

    def test_missing_only_uploads_canonical_names_with_checksums_last(self):
        client,files,journal=self.fixture();client.assets={}
        side=files[0].parent/'package.tar.gz.sha256';side.write_bytes(b'sha');files.insert(0,side)
        result=repair(client,files,journal)
        self.assertEqual(result['phase'],'complete')
        uploads=[call[1] for call in client.calls if call[0]=='upload']
        self.assertEqual(uploads[0],'package.tar.gz');self.assertEqual(set(uploads[1:]),{'checksums.txt','package.tar.gz.sha256'})
        self.assertEqual({n for n,b in client.assets.values()},{p.name for p in files})
        self.assertFalse(any('.backup-' in n or '.staged-' in n for n,b in client.assets.values()))
        self.assertTrue(all(call[0]=='upload' for call in client.calls));self.assertEqual(client.body,'old release notes')

    def test_external_conflict_removal_then_same_path_fills_missing_files(self):
        client,files,journal=self.fixture()
        with self.assertRaises(ValueError):repair(client,files,journal)
        approved=[row['asset_id'] for row in json.loads(journal.read_text())['conflicts']]
        for asset_id in approved:del client.assets[asset_id]  # External, separately confirmed action.
        result=repair(client,files,journal.with_name('rerun.json'))
        self.assertEqual(result['phase'],'complete')
        self.assertEqual({n for n,b in client.assets.values()},{p.name for p in files})

    def test_retired_canonical_and_paired_sidecar_require_confirmation(self):
        client,files,journal=self.fixture();client.repo='HandSonic/1Panel-offline-installer-V2'
        name='1panel-v2.3.2-custom-offline-linux-arm64.tar.gz'
        client.assets={1:(name,b'old package'),2:(name+'.sha256',b'old sha'),3:('release-validation.json',b'old receipt')}
        files=[p for p in files if p.name!='checksums.txt']
        with self.assertRaisesRegex(ValueError,'deletion confirmation'):
            repair(client,files,journal,update_notes=False,retire_names=[name],retire_version=client.tag,retire_controls=True)
        state=json.loads(journal.read_text());self.assertEqual(state['phase'],'confirmation_required')
        self.assertEqual({row['asset_id'] for row in state['conflicts']},{1,2,3})
        self.assertTrue(all(row['reason']=='retired-canonical' for row in state['conflicts']))
        self.assertEqual(client.calls,[])

    def test_old_asset_without_server_hash_is_read_before_equality_or_conflict(self):
        for identical in (False,True):
            with self.subTest(identical=identical):
                client,files,journal=self.fixture()
                if identical:client.assets={i+1:(p.name,p.read_bytes()) for i,p in enumerate(files)}
                release=client.release
                def no_digest():
                    value=release()
                    for asset in value['assets']:asset['digest']=None
                    return value
                client.release=no_digest
                if identical:self.assertEqual(repair(client,files,journal)['phase'],'complete')
                else:
                    with self.assertRaises(ValueError):repair(client,files,journal)
                    self.assertTrue(all(len(row['sha256'])==64 for row in json.loads(journal.read_text())['conflicts']))
                self.assertEqual(client.calls,[])

    def test_legacy_backups_remain_read_compatible_but_staged_assets_block(self):
        for suffix in ('backup','staged'):
            with self.subTest(suffix=suffix):
                client,files,journal=self.fixture()
                client.assets={i+1:(p.name,p.read_bytes()) for i,p in enumerate(files)}
                client.assets[9]=('package.tar.gz.'+suffix+'-0123456789ab',b'historical bytes')
                if suffix=='backup':self.assertEqual(repair(client,files,journal)['phase'],'complete')
                else:
                    with self.assertRaisesRegex(ValueError,'staged'):repair(client,files,journal)
                self.assertEqual(client.calls,[]);self.assertEqual(client.assets[9][1],b'historical bytes')

    def test_preflight_identity_change_never_uploads(self):
        client,files,journal=self.fixture();client.assets={};release=client.release;reads=[0]
        def changing():
            reads[0]+=1
            if reads[0]==2:client.assets[9]=('package.tar.gz',b'human bytes')
            return release()
        client.release=changing
        with self.assertRaisesRegex(ValueError,'preflight'):repair(client,files,journal)
        self.assertEqual(client.calls,[])

    def test_changed_existing_or_new_unexpected_asset_during_upload_stops_without_rollback(self):
        for fault in ('existing','extra'):
            with self.subTest(fault=fault):
                client,files,journal=self.fixture()
                client.assets={1:(files[0].name,files[0].read_bytes())};upload=client.upload
                def change(path):
                    upload(path)
                    if fault=='existing':client.assets[1]=(files[0].name,b'human replacement')
                    else:client.assets[9]=('unrelated.tar.gz',b'human bytes')
                client.upload=change
                with self.assertRaises(ValueError):repair(client,files,journal)
                self.assertTrue(all(call[0]=='upload' for call in client.calls))
                self.assertEqual(json.loads(journal.read_text())['phase'],'failed')

    def test_uncertain_upload_is_preserved_and_later_run_verifies_before_resuming(self):
        client,files,journal=self.fixture('uncertain-upload');client.assets={}
        with self.assertRaisesRegex(RuntimeError,'response lost'):repair(client,files,journal)
        state=json.loads(journal.read_text());self.assertEqual(state['phase'],'failed')
        self.assertEqual(state['upload_intent']['name'],'package.tar.gz')
        self.assertEqual(repair(client,files,journal.with_name('rerun.json'))['phase'],'complete')
        self.assertEqual([c for c in client.calls if c==('upload','package.tar.gz')],[('upload','package.tar.gz')])

    def test_failed_upload_and_bad_readback_stop_without_new_sidecars(self):
        for failure in ('upload','bad_readback'):
            with self.subTest(failure=failure):
                client,files,journal=self.fixture(failure);client.assets={}
                with self.assertRaises((ValueError,RuntimeError)):repair(client,files,journal)
                self.assertEqual(client.calls,[('upload','package.tar.gz')])
                self.assertEqual(json.loads(journal.read_text())['phase'],'failed')
                self.assertNotIn('checksums.txt',{n for n,b in client.assets.values()})

    def test_retirement_scope_and_unexpected_canonical_assets_fail_before_upload(self):
        for name in ('checksums.txt','release-validation.json','1panel-v2.3.1-custom-offline-linux-arm64.tar.gz'):
            client,files,journal=self.fixture()
            with self.subTest(name=name),self.assertRaises(ValueError):
                repair(client,files,journal,update_notes=False,retire_names=[name],retire_version='v2.3.2')
            self.assertEqual(client.calls,[])
        client,files,journal=self.fixture();client.assets[9]=('unexpected.tar.gz',b'unknown')
        with self.assertRaises(ValueError):repair(client,files,journal)
        self.assertEqual(client.calls,[])

    def test_retirement_requires_version_bound_to_destination_tag(self):
        name='1panel-v2.3.2-custom-offline-linux-arm64.tar.gz'
        for version in (None,'','v2.3.1','v2.3.2/other','v2.3.2.*'):
            client,files,journal=self.fixture();client.tag='v2.3.2-review'
            with self.subTest(version=version),self.assertRaises(ValueError):
                repair(client,files,journal,update_notes=False,retire_names=[name],retire_version=version)
            self.assertEqual(client.calls,[]);self.assertFalse(journal.exists())

class ReleasePaginationTests(unittest.TestCase):
 def test_full_immutable_inventory_replaces_truncated_embedded_assets(self):
  from release_asset_repair import GitHub
  from unittest.mock import patch
  rows=[{'id':n+1,'name':'archive-'+str(n)} for n in range(137)]
  client=GitHub('example/repo','v2.99.0');calls=[]
  def read(*args):
   endpoint=args[-1];calls.append(endpoint)
   if '/tags/' in endpoint:return json.dumps({'id':9,'tag_name':'v2.99.0','assets':rows[:30]})
   page=int(endpoint.rsplit('=',1)[1]);return json.dumps(rows[(page-1)*100:page*100])
  with patch.object(client,'run',side_effect=read):self.assertEqual(client.release()['assets'],rows)
  self.assertEqual(len(calls),3)
 def test_duplicate_pages_and_changed_release_identity_fail(self):
  from release_asset_repair import GitHub
  from unittest.mock import patch
  client=GitHub('example/repo','v2.99.0')
  for response in ({'id':True,'tag_name':'v2.99.0'},{'id':9,'tag_name':'v2.98.0'}):
   with patch.object(client,'run',return_value=json.dumps(response)),self.assertRaises(ValueError):client.release()
  rows=[{'id':n+1,'name':str(n)} for n in range(100)]
  responses=[json.dumps({'id':9,'tag_name':'v2.99.0'}),json.dumps(rows),json.dumps([rows[0]])]
  with patch.object(client,'run',side_effect=responses),self.assertRaisesRegex(ValueError,'Duplicate'):client.release()
 def test_failed_later_page_never_returns_a_partial_inventory(self):
  import subprocess
  from release_asset_repair import GitHub
  from unittest.mock import patch
  client=GitHub('example/repo','v2.99.0')
  rows=[{'id':n+1,'name':str(n)} for n in range(100)]
  responses=[json.dumps({'id':9,'tag_name':'v2.99.0'}),json.dumps(rows),subprocess.CalledProcessError(1,['gh'])]
  with patch.object(client,'run',side_effect=responses),self.assertRaises(subprocess.CalledProcessError):client.release()
