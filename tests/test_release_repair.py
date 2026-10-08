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
    def upload(self,path):self.assets[max(self.assets)+1]=(path.name,path.read_bytes());self.calls.append(('upload',path.name))
    def download(self,name,directory):
        data=next(b for n,b in self.assets.values() if n==name)
        (directory/name).write_bytes(data if self.failure!='bad_readback' or '.staged-' not in name else b'bad')
    def rename(self,asset_id,name):
        self.calls.append(('rename',asset_id,name))
        if self.failure=='switch' and asset_id>2 and '.staged-' not in name and not self.failed:
            self.failed=True
            # Simulate successful server mutation followed by lost response.
            self.assets[asset_id]=(name,self.assets[asset_id][1]);raise RuntimeError('uncertain PATCH result')
        if name in [n for i,(n,b) in self.assets.items() if i!=asset_id]:raise RuntimeError('name collision')
        self.assets[asset_id]=(name,self.assets[asset_id][1])
    def notes(self,body):
        if self.failure=='notes' and body!='old release notes' and not self.failed:
            self.failed=True;raise RuntimeError('notes update failed')
        self.body=body

class RepairTests(unittest.TestCase):
    def run_repair(self,failure=None):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);root=Path(t.name)
        files=[]
        for name in ['package.tar.gz','checksums.txt']:
            p=root/name;p.write_bytes(('new '+name).encode());files.append(p)
        return FakeGitHub(failure),files,root/'journal.json'
    def test_complete_keeps_original_backups_and_new_canonical(self):
        client,files,journal=self.run_repair();state=repair(client,files,journal)
        self.assertEqual(state['phase'],'complete')
        self.assertEqual(len(client.assets),4)
        for op in state['operations']:
            self.assertEqual(client.assets[op['old_id']][0],op['backup'])
            self.assertEqual(client.assets[op['new_id']][0],op['canonical'])
            self.assertTrue(op['old_digest'].startswith('sha256:'))
    def test_new_assets_can_be_added_and_are_recoverable(self):
        client,files,journal=self.run_repair();new=files[0].parent/'enterprise.tar.gz';new.write_bytes(b'enterprise bytes')
        files.insert(1,new)
        state=repair(client,files,journal)
        added=next(op for op in state['operations'] if op['canonical']=='enterprise.tar.gz')
        self.assertIsNone(added['old_id'])
        self.assertEqual(client.assets[added['new_id']][0],'enterprise.tar.gz')
    def test_new_asset_switch_failure_restores_missing_state(self):
        client,files,journal=self.run_repair('notes');new=files[0].parent/'enterprise.tar.gz';new.write_bytes(b'enterprise bytes');files.insert(1,new)
        with self.assertRaises(RuntimeError):repair(client,files,journal)
        self.assertNotIn('enterprise.tar.gz',[name for name,body in client.assets.values()])
        self.assertTrue(any(name.startswith('enterprise.tar.gz.staged-') for name,body in client.assets.values()))
    def test_uncertain_switch_rolls_back_using_observed_ids(self):
        client,files,journal=self.run_repair('switch')
        with self.assertRaises(RuntimeError):repair(client,files,journal)
        self.assertEqual(json.loads(journal.read_text())['phase'],'rolled_back')
        self.assertEqual(client.assets[1],('package.tar.gz',b'old archive'))
        self.assertEqual(client.assets[2],('checksums.txt',b'old checksum'))
        self.assertEqual(len(client.assets),4)
    def test_failed_readback_never_renames_original(self):
        client,files,journal=self.run_repair('bad_readback')
        with self.assertRaises(ValueError):repair(client,files,journal)
        self.assertFalse(any(c[0]=='rename' for c in client.calls))
        self.assertEqual(client.assets[1][0],'package.tar.gz')
    def test_staging_failure_preserves_concurrent_human_notes(self):
        client,files,journal=self.run_repair('bad_readback')
        download=client.download
        def concurrent_download(name,directory):
            if '.staged-' in name:client.body='concurrent human release notes'
            return download(name,directory)
        client.download=concurrent_download
        with self.assertRaises(ValueError):repair(client,files,journal)
        self.assertEqual(client.body,'concurrent human release notes')
    def test_concurrent_asset_identity_change_stops_switch(self):
        client,files,journal=self.run_repair();original_release=client.release;counter=[0]
        def concurrent_release():
            counter[0]+=1
            if counter[0]==4:client.assets[1]=('human-renamed.tar.gz',client.assets[1][1])
            return original_release()
        client.release=concurrent_release
        with self.assertRaises(ValueError):repair(client,files,journal)
        self.assertEqual(client.assets[1][0],'human-renamed.tar.gz')
        self.assertFalse(any(call[0]=='rename' for call in client.calls))
    def test_unexpected_canonical_asset_rejected_before_upload(self):
        client,files,journal=self.run_repair();client.assets[9]=('unexpected-canonical.tar.gz',b'unknown')
        with self.assertRaises(ValueError):repair(client,files,journal)
        self.assertEqual(client.calls,[])
    def test_concurrent_unexpected_canonical_addition_rolls_back(self):
        client,files,journal=self.run_repair();release=client.release;counter=[0]
        def add_extra_at_final_check():
            counter[0]+=1
            if counter[0]==6:client.assets[9]=('unexpected-canonical.tar.gz',b'unknown')
            return release()
        client.release=add_extra_at_final_check
        with self.assertRaises(ValueError):repair(client,files,journal)
        self.assertEqual(client.assets[1][0],'package.tar.gz');self.assertEqual(client.assets[2][0],'checksums.txt')
        self.assertEqual(client.assets[9][0],'unexpected-canonical.tar.gz')
    def test_notes_failure_restores_canonical_assets(self):
        client,files,journal=self.run_repair('notes')
        with self.assertRaises(RuntimeError):repair(client,files,journal)
        self.assertEqual(json.loads(journal.read_text())['phase'],'rolled_back')
        self.assertEqual(client.assets[1][0],'package.tar.gz')
        self.assertEqual(client.body,'old release notes')

if __name__=='__main__':unittest.main()
