"""Removed transition entrypoints cannot publish or bless obsolete receipts."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from publication_contract import verify_historical_receipt
import test_publication_contract


class RetiredReceiptTransitions(unittest.TestCase):
 def test_transition_commands_have_no_writer(self):
  for script,operation in [('manual_publication.py','refresh-receipt'),('manual_publication.py','publish'),('package_matrix.py','revalidate')]:
   with self.subTest(script=script,operation=operation),tempfile.TemporaryDirectory() as td:
    result=subprocess.run([sys.executable,str(ROOT/'scripts'/script),operation,'--work',td],capture_output=True,text=True)
    self.assertNotEqual(result.returncode,0);self.assertEqual(list(Path(td).iterdir()),[])
  for name in ('receipt_migration.py','repair_release.py','create_validation_receipt.py','check_release_state.py'):
   self.assertFalse((ROOT/'scripts'/name).exists())
 def test_historical_checksum_format_is_verified_without_rewriting_bytes(self):
  case=test_publication_contract.ReceiptTests()
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);proof,checksums,assets,run=case.fixture(root)
   raw=b'\r\n'.join(reversed(checksums.rstrip(b'\n').splitlines()))
   import hashlib
   fact={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()};proof['files']['checksums.txt']=fact
   next(a for a in assets if a['name']=='checksums.txt').update(size=len(raw),digest='sha256:'+fact['sha256'])
   self.assertTrue(case.check(root,(proof,raw,assets,run)))
   self.assertEqual(raw,b'\r\n'.join(reversed(checksums.rstrip(b'\n').splitlines())))
 def test_historical_input_needs_explicit_inventory_and_policy(self):
  for expected,policy in [(set(), 'b'*64),({'checksums.txt'},'old'),([], 'b'*64)]:
   with self.subTest(expected=expected),self.assertRaises(ValueError):
    verify_historical_receipt({},b'',[],{},'downstream17','v2.99.0','v2.99.0','HandSonic/1Panel-offline-installer-V2',expected=expected,expected_policy=policy)
 def test_historical_receipts_cannot_be_current_preparation(self):
  from publication_outcomes import validate_preparation
  case=test_publication_contract.ReceiptTests()
  with tempfile.TemporaryDirectory() as td:
   proof,*_=case.fixture(Path(td))
   with self.assertRaises(ValueError):validate_preparation(proof,{})

if __name__=='__main__':unittest.main()
