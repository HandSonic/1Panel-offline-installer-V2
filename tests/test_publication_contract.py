import hashlib,json,shutil,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import publication_contract as contracts

class ReceiptTests(unittest.TestCase):
 def fixture(self,root):
  matrix={'official':['amd64','arm64','armv7','ppc64le','s390x','riscv64'],'custom':contracts.ARCHES,'enterprise-original':['amd64','arm64'],'enterprise-docker':['amd64','arm64']}
  (root/'release-matrix-v2.3.2.json').write_text(json.dumps(matrix))
  for name in ['vendor','config']:
   shutil.copytree(contracts.ROOT/name,root/name)
  for name in ['official-sources-v2.3.2.json','enterprise-sources-v2.3.2.json']:
   shutil.copyfile(contracts.ROOT/name,root/name)
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
    return '2026-10-08T19:00:00.000Z VERIFIED_RELEASE_RECEIPT_SHA256='+'b'*64
  proof={'workflow_run_id':123,'workflow_commit':'a'*40}
  self.assertTrue(contracts.verify_validation_log(Client(),proof,'b'*64))
  with self.assertRaises(ValueError):contracts.verify_validation_log(Client(),proof,'c'*64)
 def test_log_read_diagnostic_preserves_boundary_without_leaking_urls(self):
  class Client:
   repo=contracts.REPOS['downstream17']
   def run(self,*args):
    if '/jobs?' in args[-1]:return json.dumps({'jobs':[{'id':17,'name':'publication_prepare','conclusion':'success'}]})
    raise contracts.subprocess.CalledProcessError(1,['gh'],stderr='gh: Resource not accessible by integration (HTTP 403) https://secret.example/?token=private')
  with self.assertRaises(ValueError) as error:contracts.verify_validation_log(Client(),{'workflow_run_id':123,'workflow_commit':'a'*40},'b'*64)
  self.assertIn('HTTP 403',str(error.exception));self.assertIn('Resource not accessible by integration',str(error.exception));self.assertNotIn('private',str(error.exception));self.assertNotIn('secret.example',str(error.exception));self.assertIn('exit=1',str(error.exception))
 def test_ansi_guard_retries_capture_only_and_strips_controls(self):
  class Client:
   repo=contracts.REPOS['downstream17']
   calls=[]
   def run(self,*args):
    self.calls.append(args)
    if '--allow-escape-sequences' not in args:raise contracts.subprocess.CalledProcessError(1,['gh'],stderr='the response contains terminal escape sequences; pass --allow-escape-sequences to output it anyway')
    return '\x1b[36mVERIFIED_RELEASE_RECEIPT_SHA256='+'b'*64+'\x1b[0m'
  client=Client();text=contracts.read_job_log(client,17)
  self.assertNotIn('\x1b',text);self.assertEqual(len(client.calls),2)
  self.assertEqual(client.calls[1],('api','--allow-escape-sequences','repos/'+client.repo+'/actions/jobs/17/logs'))
 def test_control_fragment_and_ambiguous_markers_cannot_verify(self):
  class Client:
   repo=contracts.REPOS['downstream17']
   text=''
   def run(self,*args):
    if '/jobs?' in args[-1]:return json.dumps({'jobs':[{'id':17,'name':'publication_prepare','conclusion':'success'}]})
    return self.text
  client=Client();proof={'workflow_run_id':123,'workflow_commit':'a'*40}
  for text in ['VERIFIED_RELEASE_\x1b[2JRECEIPT_SHA256='+'b'*64,'VERIFIED_RELEASE_\x1b[36mRECEIPT_SHA256='+'b'*64,'echo VERIFIED_RELEASE_RECEIPT_SHA256='+'b'*64,'VERIFIED_RELEASE_RECEIPT_SHA256='+'b'*64+'\nVERIFIED_RELEASE_RECEIPT_SHA256='+'c'*64]:
   client.text=text
   with self.subTest(text=text),self.assertRaises(ValueError):contracts.verify_validation_log(client,proof,'b'*64)
 def test_http_access_failure_is_not_retried(self):
  class Client:
   repo=contracts.REPOS['downstream17']
   calls=0
   def run(self,*args):
    self.calls+=1;raise contracts.subprocess.CalledProcessError(1,['gh'],stderr='HTTP 403 Forbidden')
  client=Client()
  with self.assertRaises(contracts.subprocess.CalledProcessError):contracts.read_job_log(client,17)
  self.assertEqual(client.calls,1)
 def test_untrusted_run_path_rejected_before_api_request(self):
  class Client:
   repo=contracts.REPOS['downstream17']
   def run(self,*args):raise AssertionError('API must not be called')
  with self.assertRaises(ValueError):contracts.verify_validation_log(Client(),{'workflow_run_id':'../../user','workflow_commit':'a'*40},'b'*64)

if __name__=='__main__':unittest.main()
