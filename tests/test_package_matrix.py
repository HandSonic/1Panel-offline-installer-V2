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
  self.discovery=patch('resolved_transport.discover_vendor',side_effect=lambda version,mode,source:
      runtime(ROOT,version,mode,enterprise=version!='v2.2.4')['inventory'][source])
  self.discovery.start();self.addCleanup(self.discovery.stop)
  def outcomes(facts,shards):
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
 def test_retry_plan_and_receipt_include_only_pending_product(self):
  def pending(facts,root,**kwargs):return [row for row in facts['rows'] if row['key']=='custom-amd64']
  with tempfile.TemporaryDirectory() as t,patch.dict(os.environ,GITHUB_SHA='a'*40,GITHUB_RUN_ID='123',PUBLICATION_OPERATION='build',GITHUB_OUTPUT=str(Path(t)/'outputs')),patch('package_retry.pending_rows',side_effect=pending):
   root=Path(t);args=self.fixture(root);_,facts=matrix.load_plan(args)
   self.assertEqual([row['key'] for row in facts['rows']],['custom-amd64'])
   self.assertEqual(len(facts['resolved']['inventory']['rows']),17)
   self.assertEqual(len(facts['native_rows']),2)
   outputs=(root/'outputs').read_text()
   self.assertIn('build_required=true\n',outputs)
   planned_outputs=dict(line.split('=',1) for line in outputs.splitlines())
   self.assertEqual(planned_outputs['checksum_required'],'false')
   self.assertEqual(planned_outputs['issues_present'],'false');self.assertEqual(json.loads(planned_outputs['issues_json']),[])
   self.assertNotIn('native_matrix',planned_outputs);self.assertNotIn('upgrade_matrix',planned_outputs)
   with patch.object(matrix,'validate_payloads',side_effect=lambda d,c,v,**kw:list(d.glob('*/*.tar.gz'))+[d/'checksums.txt']):matrix.aggregate(args)
   aggregated_outputs=dict(line.split('=',1) for line in (root/'outputs').read_text().splitlines())
   self.assertEqual(json.loads(aggregated_outputs['native_matrix'])['include'],facts['native_rows'])
   self.assertEqual(json.loads(aggregated_outputs['upgrade_matrix'])['include'],[{'source':'custom','arch':'amd64'}])
   self.assertEqual((aggregated_outputs['native_count'],aggregated_outputs['upgrade_count']),('2','1'))
   proof=json.loads((root/'output/control/release-validation.json').read_text())
   self.assertEqual(proof['requested_products'],facts['rows'])
   self.assertEqual(set(proof['files']),{'1panel-v2.3.2-custom-offline-linux-amd64.tar.gz','checksums.txt'})
 def test_complete_published_plan_skips_every_package_and_native_job(self):
  with tempfile.TemporaryDirectory() as t,patch.dict(os.environ,GITHUB_SHA='a'*40,GITHUB_RUN_ID='123',PUBLICATION_OPERATION='build',GITHUB_OUTPUT=str(Path(t)/'outputs')),patch('package_retry.pending_rows',return_value=[]):
   args=SimpleNamespace(version='v2.3.2',repository='HandSonic/1Panel-offline-installer-V2',tag='v2.3.2',work=str(Path(t)/'input'),upstream_source='release')
   matrix.plan(args);_,facts=matrix.load_plan(args)
   self.assertEqual(facts['rows'],[]);self.assertEqual(facts['native_rows'],[])
   self.assertIn('build_required=false\n',(Path(t)/'outputs').read_text())
   self.assertIn('checksum_required=false\n',(Path(t)/'outputs').read_text())
 def test_full_rerun_ignores_only_historical_unrequested_shards(self):
  def pending(facts,root,**kwargs):return [row for row in facts['rows'] if row['key']=='custom-amd64']
  with tempfile.TemporaryDirectory() as t,patch.dict(os.environ,GITHUB_SHA='a'*40,GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='2',PUBLICATION_OPERATION='build'),patch('package_retry.pending_rows',side_effect=pending):
   root=Path(t);args=self.fixture(root)
   (root/'shards/package-shard-1-custom-amd64').rename(root/'shards/package-shard-2-custom-amd64')
   stale=root/'shards/package-shard-1-official-riscv64';stale.mkdir();(stale/'shard.json').write_text('old unrelated bytes')
   with patch.object(matrix,'validate_payloads',side_effect=lambda d,c,v,**kw:list(d.glob('*/*.tar.gz'))+[d/'checksums.txt']):
    matrix.aggregate(args)
   proof=json.loads((root/'output/control/release-validation.json').read_text())
   self.assertEqual([row['key'] for row in proof['requested_products']],['custom-amd64'])
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
  for fault in ['missing','extra','bytes','companion','commit','plan']:
   with self.subTest(fault=fault),tempfile.TemporaryDirectory() as t,patch.dict(os.environ,GITHUB_SHA='a'*40,GITHUB_RUN_ID='123'):
    root=Path(t);args=self.fixture(root);folder=root/'shards/package-shard-1-official-amd64'
    if fault=='missing':folder.rename(root/'missing')
    elif fault=='extra':(root/'shards/unknown').mkdir()
    elif fault=='bytes':next(folder.glob('*/*.tar.gz')).write_bytes(b'changed')
    elif fault=='companion':next((root/'shards/package-shard-1-enterprise-docker-amd64/companions').iterdir()).write_bytes(b'changed')
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
    # Original enterprise bytes are still available to the local payload gate.
    companions=directory.parent/'verification/enterprise-original'
    self.assertEqual(len(list(companions.iterdir())),2)
    for arch in ('amd64','arm64'):
     companion=companions/f'1panel-{version}-enterprise-original-offline-linux-{arch}.tar.gz'
     self.assertEqual(companion.read_bytes(),('synthetic archive enterprise'+arch).encode())
    paths=list(directory.glob('*/*.tar.gz'));self.assertEqual(len(paths),17);return paths+[directory/'checksums.txt']
   with patch.object(matrix,'validate_payloads',side_effect=full) as validate:matrix.aggregate(args)
   validate.assert_called_once();proof=json.loads((root/'output/control/release-validation.json').read_text());self.assertEqual(len(proof['files']),18)
 def test_failed_custom_reports_do_not_discard_successful_vendor_shards_when_api_is_queued(self):
  import copy
  self.outcomes.stop()
  with tempfile.TemporaryDirectory() as t,patch.dict(os.environ,GITHUB_SHA='a'*40,GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1',PACKAGE_MATRIX_RESULT='success',GITHUB_OUTPUT=str(Path(t)/'outputs')):
   root=Path(t);args=self.fixture(root);_,facts=matrix.load_plan(args);jobs=[]
   for index,row in enumerate(facts['rows']):
    record_args=copy.copy(args)
    record_args.source=row['source'];record_args.arch=row['arch']
    record_args.output=str(root/'shards'/('package-shard-1-'+row['key']))
    record_args.result='failure' if row['source']=='custom' else 'success'
    matrix.record(record_args)
    jobs.append({'id':900001+index,'name':f'publication_packages ({row["source"]}, {row["arch"]})',
      'run_id':123,'run_attempt':1,'head_sha':'a'*40,'status':'queued','conclusion':None,
      'html_url':f'https://github.com/{args.repository}/actions/runs/123/job/{900001+index}'})
   with patch('native_candidate_input.listed',return_value=jobs),patch.object(matrix,'validate_payloads',side_effect=lambda d,c,v,**kw:list(d.glob('*/*.tar.gz'))+[d/'checksums.txt']):
    matrix.aggregate(args)
   proof=json.loads((root/'output/control/release-validation.json').read_text())
   self.assertEqual(sum(row['status']=='failure' for row in proof['outcomes']),7)
   self.assertEqual(sum(row['status']=='success' for row in proof['outcomes']),10)
   self.assertEqual(len(proof['files']),11)
   self.assertFalse((root/'output/release/custom').exists())
   outputs=dict(line.split('=',1) for line in (root/'outputs').read_text().splitlines())
   native=json.loads(outputs['native_matrix'])['include']
   self.assertEqual({row['source'] for row in native},{'official','enterprise-docker'})
   self.assertEqual(len(native),8)
   self.assertEqual(json.loads(outputs['upgrade_matrix'])['include'],
                    [{'source':'official','arch':'amd64'},{'source':'official','arch':'arm64'}])
   self.assertEqual((outputs['native_count'],outputs['upgrade_count']),('8','2'))
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
 def test_unknown_version_and_newline_inputs_fail_before_outputs(self):
  with tempfile.TemporaryDirectory() as t,patch.dict(os.environ,GITHUB_SHA='a'*40,GITHUB_RUN_ID='123',GITHUB_OUTPUT=str(Path(t)/'outputs')):
   root=Path(t)
   with self.assertRaisesRegex(ValueError,'source/edition/architecture discovery'):matrix.matrix_rows('v9.9.9')
   for version,tag,mode in [('v2.3.2\ninjected=true','v2.3.2','stable'),('v2.3.2','v2.3.2\ninjected=true','stable'),('v2.3.2','v2.3.2','stable\ninjected=true')]:
    args=SimpleNamespace(version=version,repository='HandSonic/1Panel-offline-installer-V2',tag=tag,mode=mode,work=str(root/'input'),upstream_source='release')
    with self.assertRaises(ValueError):matrix.plan(args)
   self.assertFalse((root/'outputs').exists());self.assertFalse((root/'input').exists())
  import yaml
  workflow=yaml.load((ROOT/'.github/workflows/build-offline-v2.yml').read_text(),Loader=yaml.BaseLoader)
  job=workflow['jobs']['publication_check'];steps=job['steps']
  identity=next(step for step in steps if step.get('id')=='identity')
  check=next(step for step in steps if step.get('id')=='check')
  self.assertLess(steps.index(identity),steps.index(check))
  self.assertEqual(job['outputs']['release_tag'],'${{ steps.identity.outputs.release_tag }}')
  plan=workflow['jobs']['publication_plan']
  self.assertIn('publication_check',plan['needs'])
  self.assertIn("needs.publication_check.result == 'success'",plan['if'])
  self.assertEqual(plan['outputs']['release_tag'],'${{ needs.publication_check.outputs.release_tag }}')

if __name__=='__main__':unittest.main()
