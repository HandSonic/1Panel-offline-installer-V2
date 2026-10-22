"""Package outcomes survive stale API state without accepting stale executions."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import package_matrix
from publication_outcomes import collect_recorded,products,REPOSITORY
from resolved_inventory import canonical,digest


class PackageReports(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name);self.shards=self.root/'shards';self.shards.mkdir()
        self.rows=products({'official':['amd64'],'custom':['arm64']})
        self.plan={'version':'v2.99.0','repository':REPOSITORY,'tag':'v2.99.0',
                   'workflow_run_id':'91','workflow_commit':'a'*40,'rows':self.rows,
                   'native_rows':[],'resolved':{'inventory':{'matrix':{'official':['amd64'],'custom':['arm64']},
                        'rows':self.rows,'native_rows':[]}}}
        self.jobs=[]
        for index,row in enumerate(self.rows):self.report(row,1,'success',100+index)

    def report(self,row,attempt,status,job_id):
        folder=self.shards/f'package-shard-{attempt}-{row["key"]}';folder.mkdir()
        value={'identity':{k:self.plan[k] for k in ('version','repository','tag','workflow_commit','workflow_run_id')},
               'row':row,'files':{},'companions':{},'plan_sha256':digest(self.plan),
               'outcome':{'status':status,'stage':'package','reason':'' if status=='success' else 'Package build or validation command failed',
                          'producer_run_attempt':attempt}}
        (folder/'shard.json').write_text(json.dumps(value))
        self.jobs.append({'id':job_id,'name':f'publication_packages ({row["source"]}, {row["arch"]})',
            'run_id':91,'run_attempt':attempt,'head_sha':'a'*40,'status':'queued','conclusion':None,
            'html_url':f'https://github.com/{REPOSITORY}/actions/runs/91/job/{job_id}'})
        return folder

    def collect(self,attempt=2):
        return collect_recorded(self.jobs,self.rows,self.shards,self.plan,attempt)

    def test_recorded_failure_overrides_old_success_while_api_still_says_queued(self):
        failed=self.report(self.rows[1],2,'failure',202)
        result=self.collect()
        self.assertEqual([row['status'] for row in result],['success','failure'])
        self.assertEqual(result[1]['job_id'],202)
        (failed/'unvalidated.tar.gz').write_bytes(b'partial invalid package')
        with self.assertRaisesRegex(ValueError,'unvalidated payloads'):self.collect()

    def test_missing_new_report_cannot_reuse_an_old_success(self):
        self.jobs.append(dict(self.jobs[-1],id=202,run_attempt=2))
        with self.assertRaisesRegex(ValueError,'another workflow execution'):self.collect()

    def test_copied_job_does_not_replace_the_original_upload_execution(self):
        original=self.jobs[0]
        original.update(status='completed',conclusion='success',created_at='2026-10-20T01:00:00Z',
            started_at='2026-10-20T01:00:01Z',completed_at='2026-10-20T01:01:00Z',runner_id=1,
            runner_name='runner',runner_group_id=0,runner_group_name='GitHub Actions',labels=['ubuntu'],
            steps=[{'name':'Upload','conclusion':'success'}])
        self.jobs.append(dict(copy.deepcopy(original),id=303,run_attempt=2,created_at='2026-10-20T02:00:00Z',runner_group_id=None))
        result=self.collect()
        self.assertEqual(result[0]['job_id'],original['id'])
        self.assertEqual(result[0]['producer_run_attempt'],1)

    def test_recorded_identity_plan_and_attempt_cannot_be_changed(self):
        folder=self.shards/f'package-shard-1-{self.rows[0]["key"]}';path=folder/'shard.json'
        original=json.loads(path.read_text())
        for field in ('plan','commit','attempt'):
            value=copy.deepcopy(original)
            if field=='plan':value['plan_sha256']='b'*64
            elif field=='commit':value['identity']['workflow_commit']='b'*40
            else:value['outcome']['producer_run_attempt']=2
            path.write_text(json.dumps(value))
            with self.subTest(field=field),self.assertRaises(ValueError):self.collect()

    def test_native_matrix_gate_and_missing_identity_are_handled_without_status_polling(self):
        with patch.dict(os.environ,PACKAGE_MATRIX_RESULT='failure'),patch('native_candidate_input.CandidateGitHub') as client:
            with self.assertRaisesRegex(ValueError,'infrastructure'):package_matrix.package_outcomes(self.plan,self.shards)
            client.assert_not_called()
        with patch.dict(os.environ,PACKAGE_MATRIX_RESULT='success',GITHUB_RUN_ATTEMPT='2'),\
             patch('native_candidate_input.listed',side_effect=[[],self.jobs]) as listed,\
             patch.object(package_matrix.time,'sleep') as sleep:
            self.assertEqual(len(package_matrix.package_outcomes(self.plan,self.shards)),2)
        self.assertEqual(listed.call_count,2);sleep.assert_called_once()
        with patch.dict(os.environ,PACKAGE_MATRIX_RESULT='success',GITHUB_RUN_ATTEMPT='2'),\
             patch('native_candidate_input.listed',return_value=[]) as listed,\
             patch.object(package_matrix.time,'sleep') as sleep:
            with self.assertRaisesRegex(ValueError,'not yet visible'):
                package_matrix.package_outcomes(self.plan,self.shards)
        self.assertEqual(listed.call_count,13);self.assertEqual(sleep.call_count,12)

    def test_record_failure_discards_half_built_bytes_and_skipped_steps_cannot_record(self):
        work=self.root/'plan';work.mkdir();(work/'plan.json').write_bytes(canonical(self.plan))
        output=self.root/'package-shard';output.mkdir();(output/'incomplete.tar.gz').write_bytes(b'unvalidated')
        row=self.rows[0]
        args=SimpleNamespace(source=row['source'],arch=row['arch'],output=str(output),result='failure')
        with patch.object(package_matrix,'load_plan',return_value=(work,self.plan)),patch.dict(os.environ,GITHUB_RUN_ATTEMPT='2'):
            package_matrix.record(args)
            self.assertEqual({p.name for p in output.iterdir()},{'shard.json'})
            value=json.loads((output/'shard.json').read_text())
            self.assertEqual(value['files'],{});self.assertEqual(value['outcome']['status'],'failure')
            args.result='skipped'
            with self.assertRaises(ValueError):package_matrix.record(args)


if __name__=='__main__':unittest.main()
