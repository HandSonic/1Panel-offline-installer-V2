import hashlib,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import publication_contract as contracts

class ReceiptTests(unittest.TestCase):
 def fixture(self,root):
  matrix={'official':['amd64','arm64','armv7','ppc64le','s390x','riscv64'],'custom':contracts.ARCHES,'enterprise-original':['amd64','arm64'],'enterprise-docker':['amd64','arm64']}
  (root/'release-matrix-v2.3.2.json').write_text(json.dumps(matrix))
  names=contracts.expected_names('downstream17','v2.3.2',root);files=[];sums=[]
  for name in sorted(names-{'checksums.txt'}):
   p=root/name;p.write_bytes(('verified '+name).encode());files.append(p);sums.append(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+name)
  checksum=root/'checksums.txt';checksum.write_text('\n'.join(sums)+'\n');files.append(checksum)
  with patch.object(contracts,'policy_fingerprint',return_value='reviewed-policy'):
   proof=contracts.make_proof(files,'downstream17','v2.3.2','v2.3.2',contracts.REPOS['downstream17'],123,'a'*40,root)
  assets=[{'id':i,'name':p.name,'size':p.stat().st_size,'digest':'sha256:'+hashlib.sha256(p.read_bytes()).hexdigest()} for i,p in enumerate(files)]
  assets.append({'id':100,'name':contracts.PROOF,'size':1,'digest':'unused-in-pure-receipt-check'})
  run={'id':123,'head_sha':'a'*40,'status':'completed','conclusion':'success','path':'.github/workflows/build-offline-v2.yml'}
  return proof,checksum.read_bytes(),assets,run
 def check(self,root,values):
  with patch.object(contracts,'policy_fingerprint',return_value='reviewed-policy'):
   return contracts.verify_receipt(*values,'downstream17','v2.3.2','v2.3.2',contracts.REPOS['downstream17'],root)
 def test_exact_full_receipt_is_verified_noop(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);values=self.fixture(root);self.assertTrue(self.check(root,values))
 def test_any_amd64_or_partial_matrix_is_insufficient(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);proof,checksums,assets,run=self.fixture(root)
   with self.assertRaises(ValueError):self.check(root,(proof,checksums,assets[:1],run))
 def test_bad_digest_stale_policy_wrong_commit_and_run_fail(self):
  for case in ['digest','policy','commit','run','checksum','extra','missing-proof','wrong-contract']:
   with self.subTest(case=case),tempfile.TemporaryDirectory() as t:
    root=Path(t);p,c,a,r=self.fixture(root)
    if case=='digest':a[0]['digest']='sha256:'+'0'*64
    if case=='policy':p['policy_fingerprint']='old-policy'
    if case=='commit':r['head_sha']='b'*40
    if case=='run':r['conclusion']='failure'
    if case=='checksum':c+=b'bad\n'
    if case=='extra':a.append({'name':'unreviewed-package.tar.gz','size':1})
    if case=='missing-proof':a.pop()
    if case=='wrong-contract':p['contract']='upstream7'
    with self.assertRaises(ValueError):self.check(root,(p,c,a,r))
 def test_known_recovery_backups_are_noncanonical(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);p,c,a,r=self.fixture(root);a.append({'name':a[0]['name']+'.backup-012345abcdef','size':10})
   self.assertTrue(self.check(root,(p,c,a,r)))
 def test_upstream_and_downstream_names_are_separate(self):
  upstream=contracts.expected_names('upstream7','v2.3.2')
  self.assertEqual(sum(n.endswith('.tar.gz') for n in upstream),7)
  self.assertIn('build-manifest.json',upstream);self.assertFalse(any('offline-linux' in n for n in upstream))

class ValidationLogTests(unittest.TestCase):
 def test_receipt_hash_must_exist_in_successful_validation_job(self):
  class Client:
   repo=contracts.REPOS['downstream17']
   def run(self,*args):
    if '/jobs?' in args[-1]:return json.dumps({'jobs':[{'id':17,'name':'publication_prepare','conclusion':'success'}]})
    return 'timestamp VERIFIED_RELEASE_RECEIPT_SHA256='+'b'*64
  proof={'workflow_run_id':123,'workflow_commit':'a'*40}
  self.assertTrue(contracts.verify_validation_log(Client(),proof,'b'*64))
  with self.assertRaises(ValueError):contracts.verify_validation_log(Client(),proof,'c'*64)
 def test_untrusted_run_path_rejected_before_api_request(self):
  class Client:
   repo=contracts.REPOS['downstream17']
   def run(self,*args):raise AssertionError('API must not be called')
  with self.assertRaises(ValueError):contracts.verify_validation_log(Client(),{'workflow_run_id':'../../user','workflow_commit':'a'*40},'b'*64)

if __name__=='__main__':unittest.main()
