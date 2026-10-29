"""Package and checksum writers retain their separate publication gates."""
import json,os,subprocess,unittest
from test_workflow_concurrency import JOBS,expression
class PublishWorkflowTests(unittest.TestCase):
 def test_normal_and_manual_writer_use_exact_final_artifact_and_native_markers(self):
  for name in ('publication_publish',):
   job=JOBS[name];self.assertIn('publication_acceptance',job['needs'])
   step=next(s for s in job['steps'] if s.get('id')=='publish')
   self.assertEqual(step['env']['ACCEPTED_ARTIFACT_ID'],'${{ needs.publication_acceptance.outputs.artifact_id }}')
   self.assertEqual(step['env']['EXPECTED_RECEIPT_SHA256'],'${{ needs.publication_acceptance.outputs.receipt_sha256 }}')
   self.assertEqual(step['env']['EXPECTED_NATIVE_SHA256'],'${{ needs.publication_acceptance.outputs.native_acceptance_sha256 }}')
   self.assertNotIn('gh release',step['run']);self.assertNotIn('release-matrix-',step['run'])
   self.assertIn('--journal publication-work/control/repair-journal.json',step['run'])
 def test_only_writer_jobs_can_modify_releases(self):
  writers={n for n,j in JOBS.items() if j['permissions']['contents']=='write'}
  self.assertEqual(writers,{'publication_publish','publication_checksums'})
  checksum=JOBS['publication_checksums']
  self.assertEqual(checksum['needs'],'publication_check')
  for result in ('success','failure','cancelled','skipped'):
   for required in ('true','false',''):
    self.assertEqual(expression(checksum['if'],{'needs.publication_check.result':result,
      'needs.publication_check.outputs.checksum_required':required}),result=='success' and required=='true')
  self.assertTrue(any('publication_check.py checksums' in step.get('run','') for step in checksum['steps']))
  for name in ('publication_packages','publication_prepare','publication_native','publication_upgrade','publication_acceptance'):
   self.assertNotIn('continue-on-error',JOBS[name])
 def test_normal_noop_uses_resolved_runtime_acceptance(self):
  self.assertEqual(JOBS['publication_plan']['outputs']['build_required'],'${{ steps.plan.outputs.build_required }}')
  self.assertFalse(any('runtime_publication.py status' in s.get('run','') for s in JOBS['publication_plan']['steps']))
  for name in ('publication_packages','publication_prepare'):
   self.assertEqual(JOBS[name]['env']['PUBLICATION_OPERATION'],"${{ inputs.operation || 'build' }}")
 def test_product_failures_are_recorded_but_infrastructure_and_cancellation_block_upload(self):
  job=JOBS['publication_packages'];steps=job['steps']
  package=next(s for s in steps if s.get('id')=='package')
  record=next(s for s in steps if s.get('id')=='outcome_record')
  upload=next(s for s in steps if s.get('uses')=='actions/upload-artifact@v4')
  self.assertEqual(package['continue-on-error'],'true')
  self.assertEqual(record['env']['BRANCH_RESULT'],'${{ steps.package.outcome }}')
  self.assertIn('package_matrix.py record',record['run']);self.assertIn('--output package-shard --result "$BRANCH_RESULT"',record['run'])
  self.assertLess(steps.index(package),steps.index(record));self.assertLess(steps.index(record),steps.index(upload))
  for step in steps:
   if step is not package:self.assertNotIn('continue-on-error',step)
  for cancelled,outcome,wanted in ((False,'success',True),(False,'failure',False),(True,'success',False)):
   condition=upload['if'].replace('steps.outcome_record.outcome',repr(outcome))
   self.assertEqual(expression(condition,{},cancelled),wanted)
   self.assertEqual(expression(record['if'],{},cancelled),not cancelled)
  self.assertEqual(JOBS['publication_prepare']['env']['PACKAGE_MATRIX_RESULT'],'${{ needs.publication_packages.result }}')
 def test_partial_matrix_is_red_after_admitted_products_can_be_published(self):
  job=JOBS['publication_status']
  self.assertEqual(set(job['needs']),{'publication_check','publication_plan','publication_checksums','publication_acceptance','publication_publish'})
  self.assertEqual(JOBS['publication_acceptance']['outputs']['all_requested_passed'],'${{ steps.admit.outputs.all_requested_passed }}')
  self.assertEqual(job['env']['ALL_REQUESTED_PASSED'],'${{ needs.publication_acceptance.outputs.all_requested_passed }}')
  for result,cancelled,wanted in (('success',False,True),('failure',False,False),('skipped',False,False),('success',True,False)):
   self.assertEqual(expression(job['if'],{'needs.publication_acceptance.result':result},cancelled),wanted)
  for complete,wanted in (('true',0),('false',1),('',1)):
   result=subprocess.run(['bash','-c',job['steps'][0]['run']],env=dict(os.environ,ALL_REQUESTED_PASSED=complete,CHECK_ISSUES_PRESENT='false',PLAN_ISSUES_PRESENT='false',CHECKSUMS_ISSUES_PRESENT='false'),capture_output=True,text=True)
   self.assertEqual(result.returncode,wanted,result.stdout+result.stderr)
 def test_all_discovery_issues_are_red_without_acceptance_and_print_as_safe_json(self):
  job=JOBS['publication_status']
  for stage in ('check','plan','checksums'):
   for key in ('issues_present','issues_json'):
    self.assertEqual(JOBS['publication_'+stage]['outputs'][key],'${{ steps.'+stage+'.outputs.'+key+' }}')
    self.assertEqual(job['env'][stage.upper()+'_'+key.upper()],'${{ needs.publication_'+stage+'.outputs.'+key+' }}')
  diagnostics=['custom-arm64: blocked\n::error title=injected::not a workflow annotation']
  for stage in ('check','plan','checksums'):
   for acceptance in ('success','failure','skipped'):
    context={'needs.publication_'+stage+'.result':'success','needs.publication_'+stage+'.outputs.issues_present':'true',
      'needs.publication_acceptance.result':acceptance}
    self.assertTrue(expression(job['if'],context))
    self.assertFalse(expression(job['if'],context,cancelled=True))
  for check,plan,checksums in ((diagnostics,[],[]),([],diagnostics,[]),([],[],diagnostics),
      (diagnostics,['official-amd64: source unavailable'],['custom-amd64: checksum conflict'])):
   for complete in ('true','false',''):
    result=subprocess.run(['bash','-e','-c',job['steps'][0]['run']],env=dict(os.environ,
      ALL_REQUESTED_PASSED=complete,CHECK_ISSUES_PRESENT=str(bool(check)).lower(),CHECK_ISSUES_JSON=json.dumps(check) if check else '',
      PLAN_ISSUES_PRESENT=str(bool(plan)).lower(),PLAN_ISSUES_JSON=json.dumps(plan) if plan else '',
      CHECKSUMS_ISSUES_PRESENT=str(bool(checksums)).lower(),CHECKSUMS_ISSUES_JSON=json.dumps(checksums) if checksums else ''),capture_output=True,text=True)
    self.assertEqual(result.returncode,1,result.stdout+result.stderr)
    self.assertEqual(json.loads(result.stdout.splitlines()[0]),check+plan+checksums)
    self.assertNotIn('\n::error title=injected',result.stdout)
if __name__=='__main__':unittest.main()
