import json,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import package_matrix as matrix

class PackageMatrixTests(unittest.TestCase):
 def setUp(self):
  from test_runtime_contract import runtime
  self.resolver=patch('resolved_transport.resolve',side_effect=lambda version,mode,root,**kw:runtime(root,version,mode,enterprise=version!='v2.2.4'))
  self.resolver.start();self.addCleanup(self.resolver.stop)
  def outcomes(facts):
   result=[]
   shards=Path(os.environ['ONEPANEL_RESOLVED_PLAN']).parent.parent/'shards'
   for i,row in enumerate(facts['rows']):
    attempts=[int(p.name.split('-')[2]) for p in shards.glob('package-shard-*-'+row['key'])]
    attempt=max(attempts) if attempts else 1
    result.append({**row,'status':'success','stage':'package','reason':'','producer_run_attempt':attempt,
                   'job_id':900001+i,'job_url':f'https://github.com/{facts["repository"]}/actions/runs/{facts["workflow_run_id"]}/job/{900001+i}'})
   return result
  self.outcomes=patch.object(matrix,'package_outcomes',side_effect=outcomes)
  self.outcomes.start();self.addCleanup(self.outcomes.stop)
 def test_discovered_products_use_independent_jobs(self):
  from test_runtime_contract import runtime
  from runtime_fixtures import activate
  with tempfile.TemporaryDirectory() as td:
   value=runtime();activate(self,td,value);rows=matrix.matrix_rows(value['version'])
   self.assertEqual(len(rows),17)
   self.assertEqual(len({r['key'] for r in rows}),17)
   self.assertEqual(sum(r['source']=='enterprise-original' for r in rows),2)
 def fixture(self,root,version='v2.3.2'):
  args=SimpleNamespace(version=version,repository='HandSonic/1Panel-offline-installer-V2',tag=version,work=str(root/'input'),upstream_source='release',output=str(root/'output'),shards=str(root/'shards'))
  matrix.plan(args);facts=json.loads((root/'input/plan.json').read_text());(root/'shards').mkdir()
  for row in facts['rows']:
   folder=root/'shards'/('package-shard-1-'+row['key']);folder.mkdir();files={}
   for source in [row['source']]:
    rel=f'{source}/1panel-{version}-{source}-offline-linux-{row["arch"]}.tar.gz';file=folder/rel;file.parent.mkdir();file.write_bytes(b'fixture');files[rel]=matrix.digest(file)
   record={'identity':{k:facts[k] for k in ['version','repository','tag','workflow_commit','workflow_run_id']},'row':row,'files':files,'plan_sha256':matrix.digest(root/'input/plan.json')['sha256']}
   record['companions']={}
   if row['source']=='enterprise-docker':
    rel=f'companions/1panel-{version}-enterprise-original-offline-linux-{row["arch"]}.tar.gz';file=folder/rel;file.parent.mkdir();file.write_bytes(('synthetic archive enterprise'+row['arch']).encode());record['companions'][rel]=matrix.digest(file)
   (folder/'shard.json').write_text(json.dumps(record))
  return args
 def test_unavailable_enterprise_still_requires_all_thirteen_packages(self):
  for fault in ['complete','official','custom','enterprise']:
   with self.subTest(fault=fault),tempfile.TemporaryDirectory() as t,patch.dict(os.environ,GITHUB_SHA='a'*40,GITHUB_RUN_ID='123'):
    root=Path(t);args=self.fixture(root,'v2.2.4')
    self.assertEqual(len(list((root/'shards').iterdir())),13)
    if fault in ['official','custom']:
     folder=root/('shards/package-shard-1-'+fault+'-amd64');folder.rename(root/'missing')
    elif fault=='enterprise':(root/'shards/package-shard-1-enterprise-docker-amd64').mkdir()
    with patch.object(matrix,'validate_payloads',side_effect=lambda d,c,v,**kw:list(d.glob('*/*.tar.gz'))+[d/'checksums.txt']) as validate:
     if fault=='complete':
      matrix.aggregate(args);validate.assert_called_once()
      self.assertEqual(len(json.loads((root/'output/control/release-validation.json').read_text())['files']),14)
     else:
      with self.assertRaises(ValueError):matrix.aggregate(args)
      validate.assert_not_called();self.assertFalse((root/'output/control/release-validation.json').exists())

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
   def full(directory,contract,version,**kw):
    paths=list(directory.glob('*/*.tar.gz'));self.assertEqual(len(paths),17);return paths+[directory/'checksums.txt']
   with patch.object(matrix,'validate_payloads',side_effect=full) as validate:matrix.aggregate(args)
   validate.assert_called_once();proof=json.loads((root/'output/control/release-validation.json').read_text());self.assertEqual(len(proof['files']),18)
 def test_partial_rerun_selects_latest_matching_shard(self):
  import shutil
  with tempfile.TemporaryDirectory() as t,patch.dict(os.environ,GITHUB_SHA='a'*40,GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='2'):
   root=Path(t);args=self.fixture(root)
   old=root/'shards/package-shard-1-official-amd64';new=root/'shards/package-shard-2-official-amd64';shutil.copytree(old,new)
   next(old.glob('*/*.tar.gz')).write_bytes(b'superseded corrupt artifact')
   with patch.object(matrix,'validate_payloads',side_effect=lambda d,c,v,**kw:list(d.glob('*/*.tar.gz'))+[d/'checksums.txt']):matrix.aggregate(args)
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
 def test_normal_correction_tag_and_mode_are_preserved(self):
  with tempfile.TemporaryDirectory() as t,patch.dict(os.environ,GITHUB_SHA='a'*40,GITHUB_RUN_ID='123'):
   args=SimpleNamespace(version='v2.99.0-beta.1',repository='HandSonic/1Panel-offline-installer-V2',tag='v2.99.0-beta.1-offline-r1',work=str(Path(t)/'plan'),upstream_source='release',mode='beta')
   matrix.plan(args);_,facts=matrix.load_plan(args);self.assertEqual(facts['mode'],'beta');self.assertEqual(facts['tag'],args.tag)
   args.mode='stable'
   with self.assertRaises(ValueError):matrix.load_plan(args)
 def test_normal_and_manual_graphs_share_validation_but_gate_writers(self):
  text=(ROOT/'.github/workflows/build-offline-v2.yml').read_text()
  normal=text.split('  build:\n',1)[1].split('  publication_plan:',1)[0]
  self.assertIn("needs.publication_acceptance.result == 'success'",normal)
  self.assertIn("needs.publication_plan.outputs.build_required == 'true'",normal)
  self.assertIn('EXPECTED_NATIVE_SHA256',normal)
  self.assertIn("inputs.upstream_source || 'release'",text)
  self.assertIn("offline-run-${{ github.run_id }}-${{ github.run_attempt }}",text)
  self.assertIn("offline-release-${{ needs.build_plan.outputs.release_tag }}",normal)
 def test_unknown_version_and_newline_inputs_fail_before_outputs(self):
  with tempfile.TemporaryDirectory() as t,patch.dict(os.environ,GITHUB_SHA='a'*40,GITHUB_RUN_ID='123',GITHUB_OUTPUT=str(Path(t)/'outputs')):
   root=Path(t)
   with self.assertRaisesRegex(ValueError,'source/edition/architecture discovery'):matrix.matrix_rows('v9.9.9')
   for version,tag,mode in [('v2.3.2\ninjected=true','v2.3.2','stable'),('v2.3.2','v2.3.2\ninjected=true','stable'),('v2.3.2','v2.3.2','stable\ninjected=true')]:
    args=SimpleNamespace(version=version,repository='HandSonic/1Panel-offline-installer-V2',tag=tag,mode=mode,work=str(root/'input'),upstream_source='release')
    with self.assertRaises(ValueError):matrix.plan(args)
   self.assertFalse((root/'outputs').exists());self.assertFalse((root/'input').exists())
  text=(ROOT/'.github/workflows/build-offline-v2.yml').read_text()
  self.assertLess(text.index('python3 scripts/package_matrix.py plan'),text.index('- name: Export effective version identity'))
  self.assertNotIn('mv release-matrix-full.json',text)
 def test_workflow_matrix_has_no_release_write_permission(self):
  text=(ROOT/'.github/workflows/build-offline-v2.yml').read_text();packages=text.split('  publication_packages:',1)[1].split('  publication_prepare:',1)[0]
  self.assertIn('max-parallel: 3',packages);self.assertIn('fail-fast: false',packages)
  self.assertIn('contents: read',packages);self.assertNotIn('contents: write',packages)
  self.assertNotIn('manual_publication.py publish',packages)
  self.assertIn('needs: [publication_plan, publication_packages]',text)
  self.assertGreaterEqual(text.count('artifact-ids: ${{ needs.publication_plan.outputs.plan_artifact_id }}'),7)
  self.assertIn('artifact-ids: ${{ needs.publication_plan.outputs.upstream_artifact_id }}',text)

 def test_obsolete_transition_routes_are_absent(self):
  import subprocess,yaml
  workflow=yaml.load((ROOT/'.github/workflows/build-offline-v2.yml').read_text(),Loader=yaml.BaseLoader)
  self.assertEqual(workflow['on']['workflow_dispatch']['inputs']['operation']['options'],['build','validate-repair','repair-existing'])
  self.assertNotIn('publication_revalidate',workflow['jobs'])
  self.assertNotIn('publication_receipt_refresh',workflow['jobs'])
  self.assertFalse((ROOT/'.github/workflows/native-install-smoke.yml').exists())
  result=subprocess.run([sys.executable,str(ROOT/'scripts/package_matrix.py'),'revalidate'],capture_output=True,text=True)
  self.assertNotEqual(result.returncode,0);self.assertIn('invalid choice',result.stderr)

if __name__=='__main__':unittest.main()
