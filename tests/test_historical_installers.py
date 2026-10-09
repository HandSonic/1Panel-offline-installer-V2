"""Compatibility of real immutable installer cohorts; no installer execution."""
import hashlib,json,re,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from patch_installer import patch
FIXTURES=ROOT/'tests/fixtures/historical-installers'

def features(text):
 return {'non_interactive_cli':'NON_INTERACTIVE=' in text,'edition_selection':'.selected_edition' in text,'appstore_install':'function Install_AppStore' in text}

def functions(text):
 return dict(re.findall(r'(?ms)^function (\w+)\s*\(\)\s*\{(.*?)^}[^\S\n]*(?:\n|$)',text))

class HistoricalInstallers(unittest.TestCase):
 def test_all_real_cohorts_preserve_install_behavior(self):
  rows=json.loads((FIXTURES/'index.json').read_text())['fixtures'];self.assertEqual(len(rows),12)
  for row in rows:
   with self.subTest(installer=row['installer_commit']),tempfile.TemporaryDirectory() as t:
    data=(FIXTURES/row['file']).read_bytes();self.assertEqual(hashlib.sha256(data).hexdigest(),row['source_sha256'])
    original=data.decode();self.assertEqual(features(original),{k:row[k] for k in features(original)})
    p=Path(t)/'install.sh';p.write_bytes(data);patch(p);once=p.read_bytes();patch(p);self.assertEqual(once,p.read_bytes())
    transformed=once.decode();self.assertEqual(features(original),features(transformed))
    before,after=functions(original),functions(transformed)
    for name,body in before.items():
     if name not in ['Install_Docker','Install_Docker_Offline','Install_Iptables_Offline','Get_Ip']:self.assertEqual(body,after.get(name),name)
    result=subprocess.run(['bash','-n',str(p)],capture_output=True,text=True);self.assertEqual(result.returncode,0,result.stderr)
 def test_comments_and_unrelated_settings_need_no_version_override(self):
  row=json.loads((FIXTURES/'index.json').read_text())['fixtures'][0]
  original=(FIXTURES/row['file']).read_text()
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'install.sh';p.write_text(original+'\n# Future optional setting\nOPTIONAL_FEATURE="keep this value"\n')
   patch(p);self.assertIn('OPTIONAL_FEATURE="keep this value"',p.read_text())

if __name__=='__main__':unittest.main()
