import json,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from embedded_configuration import expected_bytes,validate_binary

class EmbeddedConfigurationTests(unittest.TestCase):
 def test_reviewed_per_version_component_schemas(self):
  registry=json.loads((ROOT/'config/embedded-configs.json').read_text())
  for version,entry in registry.items():
   for component in ['core','agent']:
    with self.subTest(version=version,component=component):
     old,new,commit=expected_bytes(version,component)
     self.assertNotEqual(old,new);validate_binary(b'ELF fixture'+new,version,component,commit)
     with self.assertRaises(ValueError):validate_binary(b'ELF fixture'+old,version,component,commit)
     with self.assertRaises(ValueError):validate_binary(new+old,version,component,commit)
     if component=='core':self.assertIn(('  version: '+version).encode(),new)
     else:self.assertNotIn(b'  version:',new)
 def test_old_optional_fields_and_trailing_newline_are_preserved(self):
  old,new,commit=expected_bytes('v2.0.14','core');self.assertIn(b'remote_url:',new);self.assertIn(b'  is_intl: false',new);self.assertNotIn(b'is_enterprise:',new);self.assertEqual(old.endswith(b'\n'),new.endswith(b'\n'))
  old,new,commit=expected_bytes('v2.1.5','agent');self.assertNotIn(b'is_fxplay:',new);self.assertNotIn(b'is_enterprise:',new)
 def test_wrong_source_or_unknown_version_is_not_success(self):
  old,new,commit=expected_bytes('v2.3.2','core')
  with self.assertRaises(ValueError):validate_binary(new,'v2.3.2','core','0'*40)
  with self.assertRaises(ValueError):expected_bytes('v9.9.9','core')

if __name__=='__main__':unittest.main()
