"""Resolved inventories and immutable installer interfaces without version tables."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from package_matrix import load_plan,matrix_rows,plan
from publication_contract import expected_names
from release_inventory import native_rows,resolved_matrix
from runtime_contract import selected
from runtime_fixtures import activate,environment
from test_runtime_contract import runtime


class ResolvedInventoryRegressions(unittest.TestCase):
 def test_exact_packages_shards_and_native_rows_follow_discovery(self):
  for enterprise in (False,True):
   with self.subTest(enterprise=enterprise),tempfile.TemporaryDirectory() as td:
    value=runtime(enterprise=enterprise);activate(self,td,value);version=value['version']
    self.assertEqual(len(matrix_rows(version)),17 if enterprise else 13)
    self.assertEqual(len(expected_names('downstream17',version))-1,17 if enterprise else 13)
    self.assertEqual(len(native_rows(version)),12 if enterprise else 8)
    self.assertNotIn('loong64',resolved_matrix(version)['official'])
    self.assertNotIn('enterprise-original',{r['source'] for r in native_rows(version)})
 def test_absence_requires_complete_canonical_observations(self):
  for fault in ('missing','incomplete','server-error','success','wrong-url','duplicate','wrong-version'):
   with self.subTest(fault=fault),tempfile.TemporaryDirectory() as td:
    value=runtime(enterprise=False);evidence=value['inventory']['enterprise']['evidence']
    if fault=='missing':evidence.pop('checksums')
    elif fault=='incomplete':evidence['archives'].pop('arm64')
    elif fault=='server-error':evidence['checksums']['status']=503
    elif fault=='success':evidence['checksums']['status']=200
    elif fault=='wrong-url':evidence['archives']['amd64']['url']='https://fixture.invalid'
    elif fault=='duplicate':evidence['archives']['amd64']=evidence['archives']['arm64']
    else:value['inventory']['version']='v2.98.0'
    with self.assertRaises(ValueError):selected(value['version'],env=environment(td,value))
 def test_requested_inventory_cannot_be_narrowed_or_expanded(self):
  for enterprise in (False,True):
   for fault in ('official','custom','enterprise','unpaired','duplicate','unknown','empty','empty-source','unsupported-arch'):
    with self.subTest(enterprise=enterprise,fault=fault),tempfile.TemporaryDirectory() as td:
     value=runtime(enterprise=enterprise);matrix=value['inventory']['matrix']
     if fault in ('official','custom'):matrix[fault].pop()
     elif fault=='enterprise':
      if enterprise:matrix.pop('enterprise-original');matrix.pop('enterprise-docker')
      else:matrix.update({'enterprise-original':['amd64'],'enterprise-docker':['amd64']})
     elif fault=='unpaired':matrix['enterprise-docker']=['amd64']
     elif fault=='duplicate':matrix['official'].append(matrix['official'][0])
     elif fault=='unknown':matrix['unreviewed']=['amd64']
     elif fault=='empty':matrix.clear()
     elif fault=='empty-source':matrix['custom']=[]
     else:matrix['official'].append('unknown64')
     activate(self,td,value)
     with self.assertRaises(ValueError):matrix_rows(value['version'])
     with self.assertRaises(ValueError):expected_names('downstream17',value['version'])
 def test_missing_inventory_and_duplicate_json_fields_fail_closed(self):
  for field in ('official','enterprise'):
   with self.subTest(field=field),tempfile.TemporaryDirectory() as td:
    value=runtime();value['inventory'].pop(field)
    with self.assertRaises(ValueError):selected(value['version'],env=environment(td,value))
  with tempfile.TemporaryDirectory() as td:
   value=runtime();env=environment(td,value);path=Path(env['ONEPANEL_RESOLVED_PLAN'])
   raw=path.read_bytes().replace(b'{',b'{"resolved":{},',1);path.write_bytes(raw)
   env['ONEPANEL_RESOLVED_PLAN_SHA256']=hashlib.sha256(raw).hexdigest()
   with self.assertRaises(ValueError):selected(value['version'],env=env)
 def test_installer_samples_preserve_cli_interactive_and_appstore_boundaries(self):
  from installer_capabilities import inspect_installer
  fixtures=ROOT/'tests/fixtures/historical-installers'
  interfaces=set()
  for row in json.loads((fixtures/'index.json').read_text())['fixtures']:
   data=(fixtures/row['file']).read_bytes();self.assertEqual(hashlib.sha256(data).hexdigest(),row['source_sha256'])
   names={'install.sh','upgrade.sh'}|({'appstore.tar.gz'} if row['appstore_install'] else set())
   actual=inspect_installer(data,names,'enterprise')
   self.assertEqual(actual['adapter']=='cli',row['non_interactive_cli'])
   self.assertEqual(actual['appstore_required'],row['appstore_install'])
   interfaces.add((actual['adapter'],actual['appstore_required'],actual['edition_selection']))
  self.assertGreaterEqual(len(interfaces),4)
 def test_plan_exports_and_binds_exact_native_expectations(self):
  for enterprise in (False,True):
   with self.subTest(enterprise=enterprise),tempfile.TemporaryDirectory() as td,patch.dict(os.environ,GITHUB_SHA='a'*40,GITHUB_RUN_ID='123',GITHUB_OUTPUT=str(Path(td)/'outputs')):
    value=runtime(enterprise=enterprise);version=value['version']
    args=SimpleNamespace(version=version,repository='HandSonic/1Panel-offline-installer-V2',tag=version,work=str(Path(td)/'plan'),upstream_source='release')
    with patch('resolved_transport.resolve',return_value=value):plan(args)
    path=Path(args.work)/'plan.json';facts=json.loads(path.read_text())
    outputs=dict(line.split('=',1) for line in (Path(td)/'outputs').read_text().splitlines())
    self.assertEqual(json.loads(outputs['native_matrix']),{'include':native_rows(version)})
    for fault in ('missing','duplicate','extra','scenario','empty','unsupported'):
     altered=copy.deepcopy(facts)
     if fault=='missing':altered['native_rows'].pop()
     elif fault=='duplicate':altered['native_rows'].append(altered['native_rows'][0])
     elif fault=='extra':altered['native_rows'].append({'source':'enterprise-original','arch':'amd64','scenario':'fresh'})
     elif fault=='scenario':altered['native_rows'][0]['scenario']='optional'
     elif fault=='empty':altered['native_rows']=[]
     else:altered['native_rows'][0]['arch']='unknown64'
     path.write_text(json.dumps(altered))
     with self.assertRaises(ValueError):load_plan(args)

if __name__=='__main__':unittest.main()
