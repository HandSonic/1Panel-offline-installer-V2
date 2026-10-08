import json,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import package_matrix as matrix

class PackageMatrixTests(unittest.TestCase):
 def test_declared_17_products_use_15_jobs_and_pair_enterprise(self):
  rows=matrix.matrix_rows('v2.3.2')
  self.assertEqual(len(rows),15)
  self.assertEqual(sum(2 if r['source']=='enterprise-docker' else 1 for r in rows),17)
  self.assertEqual(len({r['key'] for r in rows}),15)
 def fixture(self,root):
  args=SimpleNamespace(version='v2.3.2',repository='HandSonic/1Panel-offline-installer-V2',tag='v2.3.2',work=str(root/'input'),upstream_source='release',output=str(root/'output'),shards=str(root/'shards'))
  matrix.plan(args);facts=json.loads((root/'input/plan.json').read_text());(root/'shards').mkdir()
  for row in facts['rows']:
   folder=root/'shards'/('package-shard-1-'+row['key']);folder.mkdir();files={}
   for source in [row['source']]+(['enterprise-original'] if row['source']=='enterprise-docker' else []):
    rel=f'{source}/1panel-v2.3.2-{source}-offline-linux-{row["arch"]}.tar.gz';file=folder/rel;file.parent.mkdir();file.write_bytes(b'fixture');files[rel]=matrix.digest(file)
   record={'identity':{k:facts[k] for k in ['version','repository','tag','workflow_commit','workflow_run_id']},'row':row,'files':files,'plan_sha256':matrix.digest(root/'input/plan.json')['sha256']}
   (folder/'shard.json').write_text(json.dumps(record))
  return args
 def test_missing_extra_corrupt_and_wrong_provenance_shards_fail(self):
  for fault in ['missing','extra','bytes','commit','plan']:
   with self.subTest(fault=fault),tempfile.TemporaryDirectory() as t,patch.dict(os.environ,GITHUB_SHA='a'*40,GITHUB_RUN_ID='123'):
    root=Path(t);args=self.fixture(root);folder=root/'shards/package-shard-1-official-amd64'
    if fault=='missing':folder.rename(root/'missing')
    elif fault=='extra':(root/'shards/unknown').mkdir()
    elif fault=='bytes':next(folder.glob('*/*.tar.gz')).write_bytes(b'changed')
    else:
     p=folder/'shard.json';v=json.loads(p.read_text())
     if fault=='commit':v['identity']['workflow_commit']='b'*40
     else:v['plan_sha256']='0'*64
     p.write_text(json.dumps(v))
    with patch.object(matrix,'validate_payloads') as validate, self.assertRaises(ValueError):matrix.aggregate(args)
    validate.assert_not_called();self.assertFalse((root/'output/control/release-validation.json').exists())
 def test_aggregate_must_pass_full_validator_before_receipt(self):
  with tempfile.TemporaryDirectory() as t,patch.dict(os.environ,GITHUB_SHA='a'*40,GITHUB_RUN_ID='123'):
   root=Path(t);args=self.fixture(root)
   with patch.object(matrix,'validate_payloads',side_effect=ValueError('invalid package')) as validate,self.assertRaises(ValueError):matrix.aggregate(args)
   validate.assert_called_once();self.assertFalse((root/'output/control/release-validation.json').exists())
 def test_complete_aggregate_invokes_one_full_gate_then_receipt(self):
  with tempfile.TemporaryDirectory() as t,patch.dict(os.environ,GITHUB_SHA='a'*40,GITHUB_RUN_ID='123'):
   root=Path(t);args=self.fixture(root)
   def full(directory,contract,version):
    paths=list(directory.glob('*/*.tar.gz'));self.assertEqual(len(paths),17);return paths+[directory/'checksums.txt']
   with patch.object(matrix,'validate_payloads',side_effect=full) as validate:matrix.aggregate(args)
   validate.assert_called_once();proof=json.loads((root/'output/control/release-validation.json').read_text());self.assertEqual(len(proof['files']),18)
 def test_partial_rerun_selects_latest_matching_shard(self):
  import shutil
  with tempfile.TemporaryDirectory() as t,patch.dict(os.environ,GITHUB_SHA='a'*40,GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='2'):
   root=Path(t);args=self.fixture(root)
   old=root/'shards/package-shard-1-official-amd64';new=root/'shards/package-shard-2-official-amd64';shutil.copytree(old,new)
   next(old.glob('*/*.tar.gz')).write_bytes(b'superseded corrupt artifact')
   with patch.object(matrix,'validate_payloads',side_effect=lambda d,c,v:list(d.glob('*/*.tar.gz'))+[d/'checksums.txt']):matrix.aggregate(args)
   self.assertTrue((root/'output/control/release-validation.json').is_file())
 def test_rerun_rejects_future_and_changed_plan(self):
  import shutil
  for fault in ['future','plan']:
   with self.subTest(fault=fault),tempfile.TemporaryDirectory() as t,patch.dict(os.environ,GITHUB_SHA='a'*40,GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='2'):
    root=Path(t);args=self.fixture(root);attempt=3 if fault=='future' else 2
    new=root/f'shards/package-shard-{attempt}-official-amd64';shutil.copytree(root/'shards/package-shard-1-official-amd64',new)
    if fault=='plan':
     p=new/'shard.json';value=json.loads(p.read_text());value['plan_sha256']='0'*64;p.write_text(json.dumps(value))
    with self.assertRaises(ValueError):matrix.aggregate(args)
 def test_workflow_matrix_has_no_release_write_permission(self):
  text=(ROOT/'.github/workflows/build-offline-v2.yml').read_text();packages=text.split('  publication_packages:',1)[1].split('  publication_prepare:',1)[0]
  self.assertIn('max-parallel: 3',packages);self.assertIn('fail-fast: false',packages)
  self.assertIn('contents: read',packages);self.assertNotIn('contents: write',packages)
  self.assertNotIn('manual_publication.py publish',packages)
  self.assertIn('needs: [publication_plan, publication_packages]',text)
  self.assertEqual(text.count('artifact-ids: ${{ needs.publication_plan.outputs.plan_artifact_id }}'),2)
  self.assertIn('artifact-ids: ${{ needs.publication_plan.outputs.upstream_artifact_id }}',text)

if __name__=='__main__':unittest.main()
