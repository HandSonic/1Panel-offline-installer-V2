"""Generic authenticated source contracts; historical samples are test data only."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from official_source import source,verify
from package_matrix import matrix_rows
from publication_contract import policy_fingerprint
from runtime_fixtures import activate,dependencies,refresh,vendor_pins
from test_runtime_contract import runtime


class HistoricalSourceContracts(unittest.TestCase):
 def test_runtime_source_matrices_have_no_version_registry(self):
  for version in ('v2.99.0','v2.100.1'):
   with self.subTest(version=version),tempfile.TemporaryDirectory() as td:
    value=runtime(version=version);activate(self,td,value);rows=matrix_rows(version)
    self.assertEqual(len(rows),17)
    self.assertEqual({r['arch'] for r in rows if r['source']=='official'},set(value['inventory']['official']['archives']))
    for row in rows:
     if row['source']=='official':self.assertEqual(source(version,row['arch'])['version'],version)
 def test_unknown_architecture_and_absent_runtime_are_not_guessed(self):
  with tempfile.TemporaryDirectory() as td:
   activate(self,td,runtime())
   with self.assertRaises(ValueError):source('v2.99.0','loong64')
   with self.assertRaises(ValueError):source('../../escape','amd64')
  with patch.dict('os.environ',{},clear=True),self.assertRaisesRegex(ValueError,'Authenticated runtime plan'):
   matrix_rows('v2.3.2')
 def test_official_bytes_size_url_and_version_are_enforced(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);package=root/'app.tgz';package.write_bytes(b'synthetic input fixture')
   value=runtime();pin={'bytes':package.stat().st_size,'sha256':hashlib.sha256(package.read_bytes()).hexdigest()}
   value['inventory']['official']=vendor_pins(value['version'],'official',{'amd64':pin})
   refresh(value);activate(self,td,value);url=source(value['version'],'amd64')['url']
   verify(package,value['version'],'amd64',url)
   with self.assertRaises(ValueError):verify(package,value['version'],'amd64',url.replace('stable','beta'))
   package.write_bytes(b'corrupt input')
   with self.assertRaises(ValueError):verify(package,value['version'],'amd64',url)
   for update in ({'bytes':-1},{'version':'v2.98.0'},{'url':'https://unreviewed.invalid/archive'}):
    wrong=copy.deepcopy(value);wrong['inventory']['official']['archives']['amd64'].update(update)
    activate(self,td,wrong)
    with self.assertRaises(ValueError):source(value['version'],'amd64')
 def test_policy_binds_generator_code_and_current_inputs_only(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td)/'repo';shutil.copytree(ROOT,root,ignore=shutil.ignore_patterns('__pycache__','*.pyc','build','.git'))
   value=runtime();activate(self,td,value);before=policy_fingerprint('downstream17',value['version'],root)
   (root/'unrelated-history.json').write_text('{"historical":true}')
   self.assertEqual(before,policy_fingerprint('downstream17',value['version'],root))
   for name in ('prepare_offline.sh','scripts/prepare_enterprise.py','scripts/package_matrix.py','scripts/release_asset_repair.py','scripts/publication_contract.py','scripts/release_inventory.py','scripts/legacy_predecessor_source.py','scripts/import_ci_artifact.py','requirements-validation.txt','.github/workflows/build-offline-v2.yml'):
    path=root/name;raw=path.read_bytes();path.write_bytes(raw+b'\n# security fix\n')
    self.assertNotEqual(before,policy_fingerprint('downstream17',value['version'],root),name);path.write_bytes(raw)
 def test_ci_pins_semantic_validation_dependency(self):
  self.assertEqual((ROOT/'requirements-validation.txt').read_text().strip(),'PyYAML==6.0.3')
  workflow=(ROOT/'.github/workflows/build-offline-v2.yml').read_text()
  for job,end in [('regression','build_plan'),('publication_plan','publication_packages')]:
   section=workflow.split('  '+job+':',1)[1].split('  '+end+':',1)[0]
   for required in ('python3 -m venv','-r requirements-validation.txt','GITHUB_PATH'):self.assertIn(required,section)
 def test_both_custom_input_routes_pass_producer_gate_before_extraction(self):
  script=(ROOT/'prepare_offline.sh').read_text();gate=script.index('scripts/validate_upstream_package.py')
  self.assertLess(script.index('scripts/import_ci_artifact.py'),gate)
  self.assertLess(script.index('scripts/validate_upstream.py" checksum'),gate)
  self.assertLess(gate,script.index('tar -xf "${app_tar}"'))
 def test_archive_gate_uses_authenticated_contract_without_vendor_code_import(self):
  import validate_upstream_package as gate
  with tempfile.TemporaryDirectory() as td:
   value=runtime();activate(self,td,value)
   with patch('validate_resolved_custom.verify_archive',return_value='verified') as verify_archive:
    self.assertEqual(gate.validate('/tmp/fixture.tgz',value['version'],'arm64'),'verified')
   args=verify_archive.call_args.args
   self.assertEqual(args[4],value['source_contract']);self.assertEqual(args[5],value['source_contract_sha256'])
   self.assertEqual(args[7],value['upstream']['producer_commit'])
 def test_producer_toolchain_cannot_be_replaced_after_resolution(self):
  from runtime_contract import manifest_contract
  from test_validate_resolved_custom import Fixture
  from resolved_inventory import digest
  value=runtime();fixture=Fixture();contract=value['source_contract']
  data={'schema_version':1,'edition':'community','version':value['version'],'architecture':'amd64',
        'source_commit':contract['source']['commit'],'installer_commit':contract['installer']['commit'],
        'mode':value['mode'],'build_repository_commit':value['upstream']['producer_commit'],
        'resolved_contract_sha256':digest(contract),**{k+'_version':v for k,v in contract['toolchain'].items()},
        'files':{name:{'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()} for name,raw in fixture.files.items()}}
  with tempfile.TemporaryDirectory() as td:
   activate(self,td,value);manifest_contract(data,fixture.files['1pctl'],value['version'],'amd64')
   for field in ('node_version','npm_version','go_version'):
    with self.subTest(field=field),self.assertRaises(ValueError):
     manifest_contract(dict(data,**{field:'0.0.1'}),fixture.files['1pctl'],value['version'],'amd64')

if __name__=='__main__':unittest.main()
