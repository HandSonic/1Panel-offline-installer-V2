"""Every writer uses the same authenticated final artifact and native gate."""
import os,subprocess,unittest
from test_workflow_concurrency import JOBS,expression
class PublishWorkflowTests(unittest.TestCase):
 def test_normal_and_manual_writer_use_exact_final_artifact_and_native_markers(self):
  for name in ('build','publication_repair'):
   job=JOBS[name];self.assertIn('publication_acceptance',job['needs'])
   step=next(s for s in job['steps'] if 'runtime_publication.py publish' in s.get('run',''))
   self.assertEqual(step['env']['ACCEPTED_ARTIFACT_ID'],'${{ needs.publication_acceptance.outputs.artifact_id }}')
   self.assertEqual(step['env']['EXPECTED_RECEIPT_SHA256'],'${{ needs.publication_acceptance.outputs.receipt_sha256 }}')
   self.assertEqual(step['env']['EXPECTED_NATIVE_SHA256'],'${{ needs.publication_acceptance.outputs.native_acceptance_sha256 }}')
   self.assertNotIn('gh release',step['run']);self.assertNotIn('release-matrix-',step['run'])
   self.assertIn('--journal publication-work/control/repair-journal.json',step['run'])
 def test_only_writer_jobs_can_modify_releases(self):
  writers={n for n,j in JOBS.items() if j['permissions']['contents']=='write'}
  self.assertEqual(writers,{'build','publication_repair'})
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
  self.assertEqual(set(job['needs']),{'publication_acceptance','build','publication_repair'})
  self.assertEqual(JOBS['publication_acceptance']['outputs']['all_requested_passed'],'${{ steps.admit.outputs.all_requested_passed }}')
  self.assertEqual(job['env']['ALL_REQUESTED_PASSED'],'${{ needs.publication_acceptance.outputs.all_requested_passed }}')
  for result,cancelled,wanted in (('success',False,True),('failure',False,False),('skipped',False,False),('success',True,False)):
   self.assertEqual(expression(job['if'],{'needs.publication_acceptance.result':result},cancelled),wanted)
  for complete,wanted in (('true',0),('false',1),('',1)):
   result=subprocess.run(['bash','-c',job['steps'][0]['run']],env=dict(os.environ,ALL_REQUESTED_PASSED=complete),capture_output=True,text=True)
   self.assertEqual(result.returncode,wanted,result.stdout+result.stderr)
if __name__=='__main__':unittest.main()
