"""Synthetic source schemas retain exact bytes without a version registry."""
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from embedded_configuration import expected_bytes,validate_binary
from semantic_configuration import production
from test_runtime_contract import runtime
from runtime_fixtures import activate,refresh


class EmbeddedConfigurationTests(unittest.TestCase):
 def fixture(self,td,optional='  extra_optional: keep\n',trailing=True):
  value=runtime()
  for component in ('core','agent'):
   text='base:\n  mode: development\n'+('  version: development\n' if component=='core' else '')+'  is_demo: true\n'+optional+'log:\n  level: debug'+('\n' if trailing else '')
   value['configuration_sources'][component]=text
   raw=text.encode();facts={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
   contract=value['source_contract'];path=component+'/config/config.yaml'
   contract['source']['files'][path]=facts
   contract['configuration'][component]={'path':path,'source_bytes':len(raw),'source_sha256':facts['sha256'],'normalized_sha256':hashlib.sha256(production(raw,value['version'],component,'stable')).hexdigest()}
  activate(self,td,refresh(value));return value['version']
 def test_component_configuration_is_exact_and_rejects_mixed_development_bytes(self):
  with tempfile.TemporaryDirectory() as td:
   version=self.fixture(td)
   for component in ('core','agent'):
    with self.subTest(component=component):
     old,new,commit=expected_bytes(version,component)
     self.assertNotEqual(old,new);validate_binary(b'ELF fixture'+new,version,component,commit)
     for changed in (old,new+old,new.replace(b'keep',b'changed')):
      with self.assertRaises(ValueError):validate_binary(changed,version,component,commit)
     self.assertEqual(b'  version:' in new,component=='core')
 def test_optional_fields_and_trailing_newline_are_preserved(self):
  for trailing in (False,True):
   with self.subTest(trailing=trailing),tempfile.TemporaryDirectory() as td:
    version=self.fixture(td,'  remote_url: https://fixture.invalid\n  is_intl: false\n',trailing)
    for component in ('core','agent'):
     old,new,_=expected_bytes(version,component)
     self.assertIn(b'remote_url:',new);self.assertIn(b'  is_intl: false',new)
     self.assertNotIn(b'is_enterprise:',new);self.assertNotIn(b'is_fxplay:',new)
     self.assertEqual(old.endswith(b'\n'),new.endswith(b'\n'))
 def test_wrong_source_version_and_absent_plan_fail(self):
  with tempfile.TemporaryDirectory() as td:
   version=self.fixture(td);_,new,_=expected_bytes(version,'core')
   with self.assertRaises(ValueError):validate_binary(new,version,'core','0'*40)
   with self.assertRaises(ValueError):expected_bytes('v2.99.1','core')
  from unittest.mock import patch
  with patch.dict('os.environ',{},clear=True),self.assertRaisesRegex(ValueError,'Authenticated runtime plan'):
   expected_bytes('v2.99.0','core')

if __name__=='__main__':unittest.main()
