import hashlib,json,os,subprocess,sys,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import manual_publication as manual

class ManualPublicationTests(unittest.TestCase):
 def test_existing_entry_manual_write_gate_and_readonly_prepare(self):
  name='build-offline-v2.yml' if (ROOT/'.github/workflows/build-offline-v2.yml').exists() else 'build.yml'
  text=(ROOT/'.github/workflows'/name).read_text()
  prepare=text.split('  publication_prepare:',1)[1].split('  publication_repair:',1)[0]
  repair=text.split('  publication_repair:',1)[1]
  self.assertIn('contents: read',prepare);self.assertNotIn('contents: write',prepare)
  expected='repair-existing' if name.startswith('build-offline') else 'promote-existing'
  gate=next(line.strip() for line in repair.splitlines() if line.strip().startswith('if:'))
  expected_gate=f"if: github.event_name == 'workflow_dispatch' && inputs.operation == '{expected}'"
  if name.startswith('build-offline'):
   expected_gate="if: ${{ !cancelled() && inputs.candidate_predecessor == '' && github.event_name == 'workflow_dispatch' && inputs.operation == 'repair-existing' && needs.publication_plan.result == 'success' && needs.publication_acceptance.result == 'success' }}"
  self.assertEqual(gate,expected_gate)
  self.assertIn('EXPECTED_RECEIPT_SHA256',repair)
 def test_push_and_validate_only_cannot_execute_publication(self):
  with tempfile.TemporaryDirectory() as t:
   for event,operation in [('push','repair-existing'),('schedule','repair-existing'),('pull_request','repair-existing'),('workflow_dispatch','validate-repair')]:
    env=dict(os.environ,GITHUB_EVENT_NAME=event,PUBLICATION_OPERATION=operation)
    result=subprocess.run([sys.executable,str(ROOT/'scripts/manual_publication.py'),'publish','--repository',manual.REPOS['downstream17'],'--version','v2.3.2','--tag','v2.3.2','--work',t],env=env,capture_output=True,text=True)
    self.assertNotEqual(result.returncode,0);self.assertIn('runtime_publication.py',result.stderr)
 def test_zip_rejects_traversal_directories_and_symlinks(self):
  for name,mode in [('../escape',0),('/absolute',0),('nested/file',0),('./file',0),('file/.',0),('link',0o120777)]:
   with self.subTest(name=name),tempfile.TemporaryDirectory() as t:
    root=Path(t);archive=root/'input.zip'
    with zipfile.ZipFile(archive,'w') as z:
     info=zipfile.ZipInfo(name);info.external_attr=mode<<16;z.writestr(info,b'content')
    with self.assertRaises(ValueError):manual.extract_verified_zip(archive,root/'output')
    self.assertFalse((root/'output').exists())
 def test_selected_ci_identity_and_byte_hash_are_bound(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);archive=root/'original.zip'
   with zipfile.ZipFile(archive,'w') as z:z.writestr('fixture',b'CI bytes')
   data=archive.read_bytes();sha=hashlib.sha256(data).hexdigest();commit='a'*40
   run={'id':11,'status':'completed','conclusion':'success','path':'.github/workflows/build.yml'}
   asset={'expired':False,'workflow_run':{'id':11},'name':f'verified-1panel-v2.3.2-{commit}','digest':'sha256:'+sha,'size_in_bytes':len(data)}
   def download(args,**kwargs):kwargs['stdout'].write(data);return SimpleNamespace(returncode=0)
   with patch.object(manual,'github_json',side_effect=[run,asset]),patch.object(manual.subprocess,'run',side_effect=download),patch.object(manual,'validate_upstream_input') as validate,patch.object(manual,'verify_artifact_producer'):
    result=manual.fetch_ci_bundle(root/'input','v2.3.2','11','22',sha,commit,'downstream17')
    self.assertEqual(result['build_repository_commit'],commit);validate.assert_called_once()
   with patch.object(manual,'github_json',return_value=dict(run,conclusion='failure')):
    with self.assertRaises(ValueError):manual.fetch_ci_bundle(root/'bad','v2.3.2','11','22',sha,commit,'downstream17')
 def test_artifact_requires_successful_producer_upload_evidence(self):
  name='verified-1panel-v2.3.2-'+'a'*40;sha='b'*64
  logs=f'Artifact {name}.zip successfully finalized. Artifact ID 22\nSHA256 digest of uploaded artifact zip is {sha}'
  jobs={'jobs':[{'id':33,'name':'aggregate','conclusion':'success'}]}
  with patch.object(manual,'github_json',return_value=jobs),patch.object(manual.subprocess,'run',return_value=SimpleNamespace(stdout=logs)):
   manual.verify_artifact_producer('11','22',name,sha)
   with self.assertRaises(ValueError):manual.verify_artifact_producer('11','23',name,sha)
   with self.assertRaises(ValueError):manual.verify_artifact_producer('11','2',name,sha)
  with patch.object(manual,'github_json',return_value={'jobs':[{'id':33,'name':'aggregate','conclusion':'failure'}]}):
   with self.assertRaises(ValueError):manual.verify_artifact_producer('11','22',name,sha)
 def test_isolated_shard_validation_builds_exact_view(self):
  import validate_release
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'official').mkdir();name='1panel-v2.3.2-official-offline-linux-amd64.tar.gz';(root/'official'/name).write_bytes(b'archive fixture')
   def validate(view,version,matrix_path):
    self.assertEqual(json.loads(matrix_path.read_text()),{'official':['amd64']})
    self.assertEqual((view/'official'/name).read_bytes(),b'archive fixture')
    self.assertIn(name,(view/'checksums.txt').read_text())
   with patch.object(validate_release,'validate',side_effect=validate) as check:manual.isolated_check(root,'v2.3.2','official',['amd64'])
   check.assert_called_once()
 def test_wrong_version_tag_or_repository_rejected(self):
  for version,tag,repo in [('v2.3.2','v2.3.1',manual.REPOS['downstream17']),('../../escape','../../escape',manual.REPOS['downstream17']),('v2.3.2','v2.3.2','other/repo')]:
   with self.assertRaises(ValueError):manual.check_identity(version,tag,repo)

 def test_cli_receipt_refresh_rejects_automatic_and_other_manual_modes(self):
  with tempfile.TemporaryDirectory() as work:
   for event,operation in [('push','refresh-receipt'),('schedule','refresh-receipt'),('pull_request','refresh-receipt'),('workflow_dispatch','validate-receipt'),('workflow_dispatch','repair-existing'),('workflow_dispatch','build')]:
    with self.subTest(event=event,operation=operation):
     env=dict(os.environ,GITHUB_EVENT_NAME=event,PUBLICATION_OPERATION=operation)
     result=subprocess.run([sys.executable,str(ROOT/'scripts/manual_publication.py'),'refresh-receipt','--repository',manual.REPOS['downstream17'],'--version','v2.3.2','--tag','v2.3.2','--work',work],env=env,capture_output=True,text=True)
     self.assertNotEqual(result.returncode,0);self.assertIn('runtime_publication.py',result.stderr)
     self.assertEqual(list(Path(work).iterdir()),[])
 def test_receipt_refresh_does_not_authorize_general_package_repair(self):
  with tempfile.TemporaryDirectory() as work:
   env=dict(os.environ,GITHUB_EVENT_NAME='workflow_dispatch',PUBLICATION_OPERATION='refresh-receipt')
   result=subprocess.run([sys.executable,str(ROOT/'scripts/manual_publication.py'),'publish','--repository',manual.REPOS['downstream17'],'--version','v2.3.2','--tag','v2.3.2','--work',work],env=env,capture_output=True,text=True)
   self.assertNotEqual(result.returncode,0);self.assertIn('runtime_publication.py',result.stderr)

if __name__=='__main__':unittest.main()
