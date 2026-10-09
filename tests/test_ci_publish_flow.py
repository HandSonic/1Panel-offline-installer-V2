"""Run the checked-in publish shell with a fake gh; no GitHub mutations."""
import hashlib,json,os,subprocess,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def publish_script():
 s=(ROOT/'.github/workflows/build-offline-v2.yml').read_text();s=s.split('- name: Stage verified assets in a draft, then publish',1)[1].split('        run: |\n',1)[1]
 lines=[]
 for line in s.splitlines():
  if line and not line.startswith('          '):break
  lines.append(line[10:] if line.startswith('          ') else line)
 return '\n'.join(lines)+'\n'
class PublishTests(unittest.TestCase):
 def run_publish(self, case, version='v2.3.2', tag=None):
  tag=tag or version+'-offline-r1'
  with tempfile.TemporaryDirectory() as t:
   t=Path(t);bindir=t/'bin';bindir.mkdir();out=t/f'build/{version}/official';out.mkdir(parents=True)
   name=f'1panel-{version}-official-offline-linux-amd64.tar.gz';p=out/name;p.write_bytes(b'fake archive prevalidated')
   sums=t/f'build/{version}/checksums.txt';sums.write_text(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+name+'\n')
   receipt=t/f'build/{version}/release-validation.json';receipt.write_text('verified fixture receipt')
   (t/'matrix.json').write_text(json.dumps({'official':['amd64']}))
   assets=[{'name':name,'size':p.stat().st_size,'digest':'sha256:'+hashlib.sha256(p.read_bytes()).hexdigest()},{'name':'checksums.txt','size':sums.stat().st_size,'digest':'sha256:'+hashlib.sha256(sums.read_bytes()).hexdigest()}]
   assets.append({'name':'release-validation.json','size':receipt.stat().st_size,'digest':'sha256:'+hashlib.sha256(receipt.read_bytes()).hexdigest()})
   if case=='missing':assets.pop(0)
   if case=='wrong_digest':assets[0]['digest']='sha256:'+'0'*64
   if case=='zero_size':assets[0]['size']=0
   if case=='extra':assets.append({'name':'extra','size':1,'digest':'sha256:'+'0'*64})
   remote={'id':123,'tag_name':tag,'draft':True,'assets':assets}
   if case=='wrong_tag':remote['tag_name']='v2.2.1'
   if case=='published_during_prepare':remote['draft']=False
   if case=='invalid_id':remote['id']=True
   (t/'remote.json').write_text(json.dumps(remote))
   gh=bindir/'gh';gh.write_text('''#!/usr/bin/env python3
import os,sys,pathlib
args=sys.argv[1:];p=pathlib.Path(os.environ['MOCK_ROOT']);c=os.environ['CASE']
with (p/'calls').open('a') as f:f.write(' '.join(args)+'\\n')
if args[:2]==['release','view']:
 if c=='new':sys.exit(1)
 print('false' if c=='public' else 'true')
elif args[:2]==['release','upload'] and c=='upload_fail':sys.exit(1)
elif args[:3]==['api','--method','PATCH']:
 if c=='publish_fail':sys.exit(1)
 assert args[3:] == ['repos/example/repo/releases/123','-f','tag_name='+os.environ['RELEASE_TAG'],'-F','draft=false','-f','make_latest=legacy'], args
elif args[0]=='api':
 if c=='api_fail':sys.exit(1)
 print((p/'remote.json').read_text())
''');gh.chmod(0o755)
   e=dict(os.environ,PATH=str(bindir)+':'+os.environ['PATH'],MOCK_ROOT=str(t),CASE=case,VERSION=version,RELEASE_TAG=tag,MATRIX='matrix.json',GITHUB_REPOSITORY='example/repo')
   r=subprocess.run(['bash','-c',publish_script()],cwd=t,env=e,text=True,capture_output=True)
   return r,(t/'calls').read_text()
 def test_verified_new_draft_publishes(self):
  r,c=self.run_publish('new');self.assertEqual(r.returncode,0,r.stderr);self.assertIn('release create v2.3.2-offline-r1 --draft',c);self.assertIn('api --method PATCH repos/example/repo/releases/123 -f tag_name=v2.3.2-offline-r1 -F draft=false -f make_latest=legacy',c)
 def test_existing_draft_resume(self):
  r,c=self.run_publish('draft');self.assertEqual(r.returncode,0,r.stderr);self.assertNotIn('release create',c)
 def test_public_release_never_modified(self):
  r,c=self.run_publish('public');self.assertNotEqual(r.returncode,0);self.assertNotIn('upload',c);self.assertNotIn('api --method PATCH',c)
 def test_bad_remote_assets_do_not_publish(self):
  for case in ['missing','wrong_digest','zero_size','extra','upload_fail','api_fail','wrong_tag','published_during_prepare','invalid_id']:
   with self.subTest(case=case):
    r,c=self.run_publish(case);self.assertNotEqual(r.returncode,0);self.assertNotIn('api --method PATCH',c)
 def test_historical_and_current_normal_publish_use_server_latest_selection(self):
  for version in ['v2.2.1','v2.3.2']:
   for tag in [version,version+'-offline-r1']:
    with self.subTest(version=version,tag=tag):
     r,c=self.run_publish('new',version,tag);self.assertEqual(r.returncode,0,r.stderr)
     self.assertIn('tag_name='+tag+' -F draft=false -f make_latest=legacy',c)
     self.assertNotIn('make_latest=true',c);self.assertNotIn('--latest',c)
 def test_publish_api_failure_fails_job(self):
  r,c=self.run_publish('publish_fail');self.assertNotEqual(r.returncode,0);self.assertEqual(c.count('api --method PATCH'),1)
if __name__=='__main__':unittest.main()
