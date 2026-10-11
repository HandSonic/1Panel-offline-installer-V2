import hashlib,json,os,subprocess,sys,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import manual_publication as manual

class ManualPublicationTests(unittest.TestCase):
 def test_zip_rejects_traversal_directories_and_symlinks(self):
  for name,mode in [('../escape',0),('/absolute',0),('nested/file',0),('./file',0),('file/.',0),('link',0o120777)]:
   with self.subTest(name=name),tempfile.TemporaryDirectory() as t:
    root=Path(t);archive=root/'input.zip'
    with zipfile.ZipFile(archive,'w') as z:
     info=zipfile.ZipInfo(name);info.external_attr=mode<<16;z.writestr(info,b'content')
    with self.assertRaises(ValueError):manual.extract_verified_zip(archive,root/'output')
    self.assertFalse((root/'output').exists())
 def test_selected_ci_identity_and_byte_hash_are_bound(self):
  from test_ci_source_authentication import CITransportFixture
  for failure in (False,True):
   with tempfile.TemporaryDirectory() as t:
    fixture=CITransportFixture(Path(t))
    if failure:fixture.run['conclusion']='failure'
    with patch.object(manual,'validate_upstream_input',wraps=manual.validate_upstream_input) as validate:
     if failure:
      with self.assertRaises(ValueError):fixture.execute()
     else:
      result=fixture.execute()
      self.assertEqual(result['build_repository_commit'],fixture.producer);validate.assert_called_once()
 def test_artifact_requires_successful_producer_upload_evidence(self):
  from test_ci_source_authentication import CITransportFixture
  with tempfile.TemporaryDirectory() as t:
   fixture=CITransportFixture(Path(t))
   with patch.object(manual,'github_json',side_effect=fixture.api),patch.object(manual,'read_job_log',return_value=fixture.upload_log):
    proof=manual.verify_artifact_producer(fixture.run_id,666,fixture.name,fixture.zip_sha)
    self.assertEqual(proof['run_attempt'],2)
    for artifact_id in (667,66):
     with self.assertRaises(ValueError):manual.verify_artifact_producer(fixture.run_id,artifact_id,fixture.name,fixture.zip_sha)
    fixture.job['conclusion']='failure'
    with self.assertRaises(ValueError):manual.verify_artifact_producer(fixture.run_id,666,fixture.name,fixture.zip_sha)
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


if __name__=='__main__':unittest.main()
