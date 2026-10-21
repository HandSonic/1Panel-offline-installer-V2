"""Every writer uses the same authenticated final artifact and native gate."""
import unittest
from test_workflow_concurrency import JOBS
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
if __name__=='__main__':unittest.main()
