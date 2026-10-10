"""Negative trust and independent-failure tests with invented versions/jobs."""
import copy,json,sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from test_resolved_inventory import responses
from test_runtime_contract import runtime,ROOT
from runtime_contract import validate
from runtime_inventory import vendor,plan
from resolved_inventory import ARCHES
from publication_outcomes import products,collect
from upstream_outcomes import verify_jobs,validate as upper_validate

class RuntimeIsolationTests(unittest.TestCase):
 def test_one_vendor_arch_failure_preserves_other_arches_and_requested_failure(self):
  checksum,obs=responses('v2.99.0','stable','official',ARCHES)
  obs['arm64'].update(status=0,bytes=None)
  value=vendor('v2.99.0','stable','official',checksum,obs)
  self.assertEqual(set(value['archives']),set(ARCHES)-{'arm64'})
  self.assertEqual(value['failures'],{'arm64':'Vendor archive endpoint unavailable'})
  self.assertEqual(value['requested_architectures'],list(ARCHES))
 def test_vendor_checksum_failure_is_not_absence_or_failure_of_other_source(self):
  checksum,obs=responses('v2.99.0','stable','enterprise',('amd64','arm64'))
  checksum.update(status=0,body=b'')
  failed=vendor('v2.99.0','stable','enterprise',checksum,obs)
  self.assertEqual(failed['availability'],'failed');self.assertEqual(set(failed['failures']),set(ARCHES))
  v=runtime();i=v['inventory'];i,_=plan(v['version'],v['mode'],v['source_contract'],v['source_contract_sha256'],i['official'],failed,i['docker'],i['compose'])
  self.assertEqual(i['matrix']['official'],v['inventory']['matrix']['official'])
  self.assertEqual(i['matrix']['enterprise-original'],list(ARCHES))
 def test_absent_custom_contract_preserves_independent_vendor_inputs(self):
  v=runtime();v.update(source_contract=None,source_contract_sha256=None,upstream=None,configuration_sources={},custom_failure='Custom source contract authentication failed')
  i=v['inventory'];v['inventory'],_=plan(v['version'],v['mode'],None,None,i['official'],i['enterprise'],i['docker'],i['compose'])
  self.assertIs(validate(v,v['version']),v)
  self.assertEqual(len(v['inventory']['rows']),17)
  v['custom_failure']=''
  with self.assertRaises(ValueError):validate(v,v['version'])
 def test_custom_resolution_failure_and_vendor_network_failure_do_not_mask_each_other(self):
  from resolved_transport import resolve
  def read(url,method='GET'):
   if '/enterprise/' in url:raise OSError('synthetic transient')
   checksum,obs=responses('v2.99.0','stable','official',('amd64','arm64'))
   if url.endswith('checksums.txt'):return {k:v for k,v in checksum.items() if k!='url'}
   return next({k:v for k,v in row.items() if k!='url'} for row in obs.values() if row['url']==url)
  with patch('resolved_transport.public_controls',side_effect=ValueError('bad source contract')):
   v=resolve('v2.99.0','stable',ROOT,read=read)
  self.assertIsNone(v['source_contract']);self.assertEqual(set(v['inventory']['official']['archives']),{'amd64','arm64'})
  self.assertEqual(v['inventory']['enterprise']['availability'],'failed')
 def test_latest_package_failure_cannot_reuse_old_success(self):
  rows=products({'official':['amd64','arm64']});jobs=[]
  for index,row in enumerate(rows):
   for attempt in (1,2):
    jobs.append({'id':100+index*10+attempt,'name':f'publication_packages ({row["source"]}, {row["arch"]})','run_id':91,'run_attempt':attempt,'head_sha':'a'*40,'status':'completed','conclusion':'failure' if index==1 and attempt==2 else 'success','html_url':f'https://github.com/HandSonic/1Panel-offline-installer-V2/actions/runs/91/job/{100+index*10+attempt}'})
  result=collect(jobs,rows,91,2,'a'*40)
  self.assertEqual([r['status'] for r in result],['success','failure'])
  self.assertEqual([r['producer_run_attempt'] for r in result],[2,2])
  for fault in ('cancelled','skipped','in_progress'):
   wrong=copy.deepcopy(jobs);wrong[-1]['conclusion']=fault
   with self.assertRaises(ValueError):collect(wrong,rows,91,2,'a'*40)

class UpperClient:
 def __init__(self,pr=False):
  self.head='a'*40;self.executed='c'*40 if pr else self.head;self.base='b'*40
  self.run={'id':91,'run_attempt':2,'head_sha':self.head,'status':'completed','conclusion':'failure','path':'.github/workflows/build.yml','event':'pull_request' if pr else 'workflow_dispatch','repository':{'full_name':'HandSonic/1Panel-Build-v2'},'head_repository':{'full_name':'HandSonic/1Panel-Build-v2'},'pull_requests':[{'base':{'sha':self.base},'head':{'sha':self.head}}]}
  names=['tests','prepare','build']+[f'compile ({a})' for a in ARCHES]
  self.jobs=[{'id':i+1,'name':n,'head_sha':self.head,'run_id':91,'run_attempt':2,'status':'completed','conclusion':'failure' if n=='compile (arm64)' else 'success','html_url':f'https://github.com/HandSonic/1Panel-Build-v2/actions/runs/91/job/{i+1}'} for i,n in enumerate(names)]
  outcomes=[{'architecture':a,'status':'failure' if a=='arm64' else 'success','stage':'compile','reason':'synthetic compile failure' if a=='arm64' else '', 'job_id':i+4,'job_url':self.jobs[i+3]['html_url']} for i,a in enumerate(ARCHES)]
  self.manifest={'schema_version':2,'version':'v2.99.0','producer_run_id':91,'producer_run_attempt':2,'producer_head_sha':self.head,'requested_architectures':list(ARCHES),'outcomes':outcomes,'artifacts':[{'architecture':a} for a in ARCHES if a!='arm64']}
  self.merge={'sha':self.executed,'parents':[{'sha':self.base},{'sha':self.head}]}

class UpperOutcomeTests(unittest.TestCase):
 def fixture(self,pr=False):
  c=UpperClient(pr);c.state=c.run
  c.api=lambda *args:json.dumps(c.merge if '/git/commits/' in args[-1] else {'total_count':len(c.jobs),'jobs':c.jobs} if '/jobs?' in args[-1] else c.state)
  c.run=c.api
  return c
 def test_exact_partial_attempt_and_pr_merge_identity(self):
  for pr in (False,True):
   c=self.fixture(pr);self.assertEqual(verify_jobs(c.manifest,'v2.99.0',c.executed,c),[a for a in ARCHES if a!='arm64'])
 def test_shared_failures_cancel_missing_or_undeclared_jobs_reject(self):
  mutations=[lambda c:c.jobs[0].update(conclusion='failure'),lambda c:c.state.update(conclusion='cancelled'),lambda c:c.jobs.pop(),lambda c:c.jobs[0].update(run_attempt=1),lambda c:c.jobs[0].update(head_sha='f'*40),lambda c:c.manifest['outcomes'].pop(),lambda c:c.manifest['artifacts'].append({'architecture':'arm64'}),lambda c:c.jobs.append(dict(c.jobs[0],id=99,name='unknown',conclusion='failure'))]
  for mutate in mutations:
   c=self.fixture();mutate(c)
   with self.assertRaises(ValueError):verify_jobs(c.manifest,'v2.99.0',c.executed,c)
 def test_receipt_validation_is_bound_to_exact_attempt_job_and_marker(self):
  from resolved_transport import verify_current_validation
  proof={'workflow_run_id':91,'workflow_run_attempt':2,'workflow_commit':'a'*40}
  job={'id':77,'name':'publication_prepare','run_id':91,'run_attempt':2,'head_sha':'a'*40,'status':'completed','conclusion':'success'}
  class Client:
   def run(self,*args):
    assert '/attempts/2/jobs?' in args[-1]
    return json.dumps({'total_count':1,'jobs':[job]})
  with patch('resolved_transport.read_job_log',return_value='VERIFIED_RELEASE_RECEIPT_SHA256='+'d'*64+'\n'):
   verify_current_validation(Client(),proof,'d'*64)
   for field,value in [('run_attempt',1),('head_sha','b'*40),('run_id',92),('status','in_progress')]:
    before=job[field];job[field]=value
    with self.assertRaises(ValueError):verify_current_validation(Client(),proof,'d'*64)
    job[field]=before
  with patch('resolved_transport.read_job_log',return_value='VERIFIED_RELEASE_RECEIPT_SHA256='+'e'*64+'\n'):
   with self.assertRaises(ValueError):verify_current_validation(Client(),proof,'d'*64)
 def test_pr_head_is_not_executed_merge_and_parents_cannot_drift(self):
  for parents in (['b'*40,'f'*40],['a'*40,'b'*40],['a'*40]):
   c=self.fixture(True);c.merge['parents']=[{'sha':p} for p in parents]
   with self.assertRaises(ValueError):verify_jobs(c.manifest,'v2.99.0',c.executed,c)

if __name__=='__main__':unittest.main()
