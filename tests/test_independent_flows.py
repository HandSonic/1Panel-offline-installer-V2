"""Independent generated Docker-function tests in a temporary fake root.
No downloaded executable is run; systemctl/docker/network are command stubs.
Only absolute installation paths are redirected, leaving control flow unchanged.
"""
import io, os, subprocess, sys, tarfile, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import patch_installer

class InstallFlowTests(unittest.TestCase):
 def run_flow(self, scenario='fresh', fail=None, service=True, missing=None, iptables=True, docker_host='', docker_context='', active_context='default'):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);pkg=root/'package';pkg.mkdir();(root/'usr/local/bin').mkdir(parents=True);(root/'etc/systemd/system').mkdir(parents=True)
   (pkg/'docker-compose').write_text('fake-compose');(pkg/'docker.service').write_text('service')
   with tarfile.open(pkg/'docker.tgz','w:gz') as t:
    for name in ['docker','dockerd','containerd','containerd-shim-runc-v2','ctr','runc','docker-init','docker-proxy']:
     m=tarfile.TarInfo('docker/'+name);data=('fake-'+name).encode();m.size=len(data);m.mode=0o755;t.addfile(m,io.BytesIO(data))
   if missing:(pkg/missing).unlink()
   helper=patch_installer.HELPERS.replace('/usr/local/',str(root)+'/usr/local/').replace('/etc/systemd/',str(root)+'/etc/systemd/')
   stub=r'''
set -u
log(){ printf '%s\n' "$*" >> "$LOG"; }
command(){
 if [[ "$1" == -v && "$2" == iptables ]]; then [[ "$IPTABLES" == yes ]]; return $?; fi
 if [[ "$1" == -v && "$2" == docker && "$SCENARIO" == fresh ]]; then return 1; fi
 if [[ "$1" == -v && "$2" == systemctl && "$SERVICE" == no ]]; then return 1; fi
 builtin command "$@"
}
systemctl(){ echo "systemctl $*" >> "$LOG"; [[ "${FAIL:-}" != "systemctl:$1" ]]; }
docker(){
 echo "docker $*" >> "$LOG"
 if [[ "$1" == context && "$2" == show ]]; then echo "$ACTIVE_CONTEXT";return 0;fi
 [[ "${FAIL:-}" != docker ]] || return 1
 if [[ "$SCENARIO" == stopped && ! -f "$COUNT" ]]; then touch "$COUNT";return 1;fi
 return 0
}
cp(){
 echo "cp $*" >> "$LOG"
 if [[ "${FAIL:-}" == cp ]]; then return 1; fi
 /bin/cp "$@"
}
curl(){ echo NETWORK >> "$LOG";return 99; }
wget(){ echo NETWORK >> "$LOG";return 99; }
'''
   env=dict(os.environ,CURRENT_DIR=str(pkg),LOG=str(root/'calls'),COUNT=str(root/'count'),SCENARIO=scenario,SERVICE='yes' if service else 'no',IPTABLES='yes' if iptables else 'no',DOCKER_HOST=docker_host,DOCKER_CONTEXT=docker_context,ACTIVE_CONTEXT=active_context,FAIL=fail or '',TXT_DOCKER_INSTALL_FAIL='Docker failed')
   r=subprocess.run(['bash','-c',stub+'\n'+helper+'\nInstall_Docker\n'],env=env,text=True,capture_output=True)
   logs=(root/'calls').read_text() if (root/'calls').exists() else ''
   files={str(p.relative_to(root)):p.read_bytes() for p in (root/'usr').rglob('*') if p.is_file()};files.update({str(p.relative_to(root)):p.read_bytes() for p in (root/'etc').rglob('*') if p.is_file()})
   self.assertNotIn('NETWORK',logs)
   return r,logs,files
 def test_fresh_offline_copy_and_services(self):
  r,log,files=self.run_flow();self.assertEqual(r.returncode,0,r.stderr)
  for name in ['docker','dockerd','containerd','containerd-shim-runc-v2','ctr','runc','docker-init','docker-proxy']:
   self.assertEqual(files['usr/local/bin/'+name],('fake-'+name).encode())
  self.assertEqual(files['usr/local/lib/docker/cli-plugins/docker-compose'],b'fake-compose')
  self.assertEqual(files['etc/systemd/system/docker.service'],b'service')
  self.assertIn('systemctl daemon-reload',log);self.assertIn('systemctl enable docker',log)
 def test_existing_engine_not_replaced(self):
  r,log,files=self.run_flow('existing');self.assertEqual(r.returncode,0)
  self.assertNotIn('usr/local/bin/dockerd',files);self.assertNotIn('daemon-reload',log)
 def test_existing_stopped_engine_started(self):
  r,log,files=self.run_flow('stopped');self.assertEqual(r.returncode,0);self.assertIn('systemctl start docker',log)
 def test_missing_payloads_abort(self):
  for name in ['docker.tgz','docker-compose','docker.service']:
   with self.subTest(name=name):self.assertNotEqual(self.run_flow(missing=name)[0].returncode,0)
 def test_copy_failure_aborts(self): self.assertNotEqual(self.run_flow(fail='cp')[0].returncode,0)
 def test_daemon_reload_failure_aborts(self): self.assertNotEqual(self.run_flow(fail='systemctl:daemon-reload')[0].returncode,0)
 def test_enable_failure_aborts(self): self.assertNotEqual(self.run_flow(fail='systemctl:enable')[0].returncode,0)
 def test_unhealthy_engine_fails(self): self.assertNotEqual(self.run_flow(fail='docker')[0].returncode,0)
 def test_fresh_no_systemd_fails(self):
  r,log,files=self.run_flow(service=False);self.assertNotEqual(r.returncode,0);self.assertEqual(files,{});self.assertNotIn('cp ',log)
 def test_stopped_no_systemd_has_no_install_writes(self):
  r,log,files=self.run_flow('stopped',service=False);self.assertNotEqual(r.returncode,0);self.assertEqual(files,{});self.assertNotIn('cp ',log)
 def test_existing_healthy_without_systemd_is_supported(self):
  r,log,files=self.run_flow('existing',service=False);self.assertEqual(r.returncode,0);self.assertNotIn('usr/local/bin/dockerd',files)
 def test_client_only_without_service_has_no_install_writes(self):
  r,log,files=self.run_flow('stopped',fail='systemctl:cat');self.assertNotEqual(r.returncode,0);self.assertEqual(files,{});self.assertNotIn('cp ',log)
 def test_systemd_unavailable_has_no_install_writes(self):
  r,log,files=self.run_flow(fail='systemctl:show-environment');self.assertNotEqual(r.returncode,0);self.assertEqual(files,{});self.assertNotIn('cp ',log)
 def test_fresh_missing_iptables_fails_before_any_install_write(self):
  r,log,files=self.run_flow(iptables=False);self.assertNotEqual(r.returncode,0);self.assertEqual(files,{});self.assertNotIn('cp ',log);self.assertIn('requires iptables',log)
 def test_healthy_existing_engine_without_iptables_is_preserved(self):
  r,log,files=self.run_flow('existing',service=False,iptables=False);self.assertEqual(r.returncode,0);self.assertNotIn('usr/local/bin/dockerd',files);self.assertNotIn('requires iptables',log)
 def test_remote_host_fails_before_any_install_write(self):
  r,log,files=self.run_flow('existing',docker_host='tcp://remote.example:2375');self.assertNotEqual(r.returncode,0);self.assertEqual(files,{});self.assertNotIn('cp ',log);self.assertNotIn('docker --host',log)
 def test_remote_context_environment_fails_before_writes(self):
  r,log,files=self.run_flow('existing',docker_context='remote');self.assertNotEqual(r.returncode,0);self.assertEqual(files,{});self.assertNotIn('cp ',log)
 def test_nondefault_selected_context_fails_before_writes(self):
  r,log,files=self.run_flow('existing',active_context='custom');self.assertNotEqual(r.returncode,0);self.assertEqual(files,{});self.assertNotIn('cp ',log)
 def test_default_local_host_probe_is_explicit(self):
  r,log,files=self.run_flow('existing',docker_host='unix:///var/run/docker.sock',docker_context='default');self.assertEqual(r.returncode,0);self.assertIn('docker --host unix:///var/run/docker.sock version',log)
if __name__=='__main__':unittest.main()
