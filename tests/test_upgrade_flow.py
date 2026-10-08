"""Bounded upgrade simulation. No downloaded binary or host service is executed."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class UpgradeFlow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pkg = self.root/'package'; self.pkg.mkdir()
        self.bin = self.root/'usr/local/bin'; self.bin.mkdir(parents=True)
        self.units = self.root/'etc/systemd/system'; self.units.mkdir(parents=True)
        (self.root/'etc/init.d').mkdir(parents=True)
        self.base = self.root/'data'; self.db = self.base/'1panel/db'; self.db.mkdir(parents=True)
        (self.base/'1panel/geo').mkdir()
        self.commands = self.root/'commands'; self.commands.mkdir()
        for cmd in ('python3','flock','mktemp','dirname','tee','bash'):
            (self.commands/cmd).symlink_to(shutil.which(cmd))
        self.write('sleep','#!/bin/bash\nexit 0\n', self.commands)
        self.write('systemctl', '''#!/usr/bin/python3
import sys, pathlib, os, json
root=pathlib.Path(os.environ['TEST_ROOT'])
a=sys.argv[1:]
if pathlib.Path(sys.argv[0]).name in ('rc-service','service'):a=[a[1],a[0]];a[0]='is-active' if a[0]=='status' else a[0]
action=a[0]; name=a[-1].replace('.service','')
with (root/'events').open('a') as f:f.write(' '.join(a)+'\\n')
if action=='daemon-reload':
 if os.environ.get('FAIL_RELOAD') and not (root/'reload-failed').exists():(root/'reload-failed').touch();sys.exit(1)
 sys.exit(0)
state=root/(name+'.state')
fail=os.environ.get('FAIL_SERVICE','')
marker=root/'failed-once'
if fail==action+':'+name and not marker.exists():
 marker.touch();sys.exit(1)
if action=='stop':state.write_text('stopped')
if action=='start':
 if os.environ.get('MIGRATE') and not (root/'migrated').exists():
  import sqlite3
  with sqlite3.connect(root/'data/1panel/db/core.db') as c:c.execute('CREATE TABLE migrated (value TEXT)')
  (root/'migrated').touch()
 state.write_text('running')
if action=='is-active':
 if os.environ.get('FAIL_HEALTH') and (root/'migrated').exists() and not (root/'health-failed').exists():
  count=root/'count';n=int(count.read_text())+1 if count.exists() else 1;count.write_text(str(n))
  if n>=30:(root/'health-failed').touch()
  sys.exit(1)
 sys.exit(0 if state.read_text()=='running' else 3)
''', self.commands)
        self.old_password = "'secret\\1|&$value'"
        conf=f"#!/bin/bash\nBASE_DIR='{self.base}'\nORIGINAL_VERSION=v2.0.0\nORIGINAL_PASSWORD={self.old_password}\nORIGINAL_PORT=10086\nORIGINAL_USERNAME='old user'\nORIGINAL_ENTRANCE=secret_entry\nLANGUAGE=zh\nCHANGE_USER_INFO=false\n"
        self.write('1pctl',conf,self.bin)
        self.write('1pctl',conf.replace('v2.0.0','v2.3.2').replace(self.old_password,"'new-secret'"),self.pkg)
        elf=bytearray(64); elf[:6]=b'\x7fELF\x02\x01';elf[18:20]=struct.pack('<H',62)
        for service in ('1panel-core','1panel-agent'):
            (self.pkg/service).write_bytes(elf)
            (self.bin/service).write_text('old '+service)
            (self.root/(service+'.state')).write_text('running')
            (self.units/(service+'.service')).write_text('old unit')
            (self.pkg/(service+'.service')).write_text('new unit')
        for parent in (self.pkg,self.bin):
            (parent/'lang').mkdir();(parent/'lang/zh.sh').write_text('new' if parent==self.pkg else 'old')
        (self.bin/'lang/stale.sh').write_text('stale')
        (self.pkg/'GeoIP.mmdb').write_text('new geo')
        (self.base/'1panel/geo/GeoIP.mmdb').write_text('old geo')
        for name in ('core.db','agent.db'):
            with sqlite3.connect(self.db/name) as c:
                c.execute('CREATE TABLE settings (key TEXT, value TEXT)')
                c.execute("INSERT INTO settings VALUES ('SystemVersion','v2.2.0')")
        self.before=self.snapshot()

    def tearDown(self):self.temp.cleanup()
    def write(self,name,text,parent):
        p=parent/name;p.write_text(text);p.chmod(0o755)
    def snapshot(self):
        return {str(p.relative_to(self.root)):p.read_bytes() for base in (self.bin,self.units,self.root/'etc/init.d',self.base) for p in base.rglob('*') if p.is_file() and p.name!='.1panel-upgrade.lock'}
    def run_upgrade(self, fail_stage='', **env):
        script=(ROOT/'upgrade_offline.sh').read_text().replace('[[ $EUID -eq 0 ]]', '[[ 0 -eq 0 ]]')
        script=script.replace('/usr/local/bin',str(self.bin)).replace('/etc/systemd/system',str(self.units)).replace('/etc/init.d',str(self.root/'etc/init.d'))
        script=script.replace('platform.machine()', "os.environ.get('TEST_ARCH', platform.machine())").replace('sys.byteorder', "os.environ.get('TEST_ENDIAN', sys.byteorder)")
        # Fault injection exists only in the temporary copy, never in production.
        script=script.replace("ctx=json.loads((work/'context.json').read_text())", "ctx=json.loads((work/'context.json').read_text())\n        if mode==os.environ.get('FAIL_STAGE'): raise OSError('injected')")
        script=script.replace("if name=='db': continue", "if name=='db': continue\n                if os.environ.get('FAIL_STAGE')=='partial-install' and name=='1panel-agent': raise OSError('injected')\n                if os.environ.get('FAIL_FILE')==name: raise OSError('injected')")
        script=script.replace("with db_connect(pathlib.Path(db)) as con:", "if os.environ.get('FAIL_STAGE')=='partial-database' and db.endswith('agent.db'): raise OSError('injected')\n                with db_connect(pathlib.Path(db)) as con:")
        (self.pkg/'upgrade.sh').write_text(script)
        return subprocess.run(['/bin/bash',str(self.pkg/'upgrade.sh')],env={**os.environ,'PATH':str(self.commands),'TEST_ROOT':str(self.root),'FAIL_STAGE':fail_stage,**env},text=True,capture_output=True,timeout=15)
    def assert_failed_restored(self,result):
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertNotIn('finished successfully',result.stdout)
        self.assertEqual(self.before,self.snapshot())
        self.assertNotIn('secret',result.stdout+result.stderr)
    def test_enterprise_manifest_rejected_before_stop(self):
        (self.pkg/'offline-manifest.json').write_text(json.dumps({'source':'enterprise-docker'}))
        result=self.run_upgrade()
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'events').exists())
        self.assertEqual(self.before,self.snapshot())

    def test_success(self):
        result=self.run_upgrade();self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('finished successfully',result.stdout)
        self.assertIn('ORIGINAL_PASSWORD='+self.old_password,(self.bin/'1pctl').read_text())
        self.assertFalse((self.bin/'lang/stale.sh').exists())
        for name in ('core.db','agent.db'):
            with sqlite3.connect(self.db/name) as c:self.assertEqual(c.execute('SELECT value FROM settings').fetchone()[0],'v2.3.2')
        backup=next(self.pkg.glob('upgrade-backup.*/backup'))
        self.assertEqual(backup.parent.stat().st_mode & 0o777,0o700)
        self.assertNotIn('secret',result.stdout+result.stderr)
    def test_filesystem_failures(self):
        for stage in ('backup','install','partial-install','database','partial-database'):
            with self.subTest(stage=stage):
                result=self.run_upgrade(stage);self.assert_failed_restored(result)
    def test_service_managers(self):
        for manager,command,suffix in [('openrc','rc-service','openrc'),('sysvinit','service','init')]:
            with self.subTest(manager=manager):
                systemctl=self.commands/'systemctl'
                if systemctl.exists():systemctl.rename(self.commands/command)
                elif (self.commands/'rc-service').exists():(self.commands/'rc-service').rename(self.commands/command)
                for service in ('1panel-core','1panel-agent'):
                    (self.pkg/(service+'.'+suffix)).write_text('new init')
                result=self.run_upgrade();self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                for name in ('core.db','agent.db'):
                    with sqlite3.connect(self.db/name) as c:c.execute("UPDATE settings SET value='v2.2.0'")
    def test_reload_failure(self):self.assert_failed_restored(self.run_upgrade(FAIL_RELOAD='1'))
    def test_verify_failure(self):self.assert_failed_restored(self.run_upgrade('verify'))
    def test_restore_failure_keeps_services_stopped(self):
        result=self.run_upgrade('restore',FAIL_SERVICE='start:1panel-core')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Restore failed',result.stdout)
        self.assertEqual((self.root/'1panel-agent.state').read_text(),'stopped')
        self.assertEqual((self.root/'1panel-core.state').read_text(),'stopped')
    def test_resource_and_unit_copy_failures(self):
        for name in ('1pctl','lang','GeoIP.mmdb','1panel-core.service','1panel-agent.service'):
            with self.subTest(name=name):self.assert_failed_restored(self.run_upgrade(FAIL_FILE=name))
    def test_absent_resource_removed_on_rollback(self):
        (self.base/'1panel/geo/GeoIP.mmdb').unlink()
        self.before=self.snapshot()
        self.assert_failed_restored(self.run_upgrade('partial-database'))
    def test_stop_failure(self):self.assert_failed_restored(self.run_upgrade(FAIL_SERVICE='stop:1panel-core'))
    def test_core_start_failure(self):self.assert_failed_restored(self.run_upgrade(FAIL_SERVICE='start:1panel-core'))
    def test_agent_start_failure(self):self.assert_failed_restored(self.run_upgrade(FAIL_SERVICE='start:1panel-agent'))
    def test_migration_health_failure_restores_database(self):
        self.assert_failed_restored(self.run_upgrade(MIGRATE='1',FAIL_HEALTH='1'))
        with sqlite3.connect(self.db/'core.db') as c:self.assertFalse(c.execute("SELECT name FROM sqlite_master WHERE name='migrated'").fetchall())
    def test_all_seven_architectures_and_endianness(self):
        cases=[('x86_64',2,62,'little'),('aarch64',2,183,'little'),('armv7l',1,40,'little'),('ppc64le',2,21,'little'),('s390x',2,22,'big'),('riscv64',2,243,'little'),('loongarch64',2,258,'little'),('loong64',2,258,'little')]
        for arch,bits,machine,endian in cases:
            with self.subTest(arch=arch):
                elf=bytearray(64);elf[:6]=b'\x7fELF'+bytes([bits,1 if endian=='little' else 2]);elf[18:20]=struct.pack(('<' if endian=='little' else '>')+'H',machine)
                for name in ('1panel-core','1panel-agent'):(self.pkg/name).write_bytes(elf)
                result=self.run_upgrade('backup',TEST_ARCH=arch,TEST_ENDIAN=endian)
                self.assertIn('Package staged and validated',result.stdout)
                self.assert_failed_restored(result)
                result=self.run_upgrade(TEST_ARCH=arch,TEST_ENDIAN='big' if endian=='little' else 'little')
                self.assertNotIn('Package staged and validated',result.stdout)
                self.assert_failed_restored(result)
    def test_armv6_is_not_armv7(self):
        elf=bytearray(64);elf[:6]=b'\x7fELF\x01\x01';elf[18:20]=struct.pack('<H',40)
        for name in ('1panel-core','1panel-agent'):(self.pkg/name).write_bytes(elf)
        result=self.run_upgrade(TEST_ARCH='armv6l')
        self.assert_failed_restored(result);self.assertFalse((self.root/'events').exists())
    def test_reject_architecture_before_stop(self):
        (self.pkg/'1panel-core').write_bytes(b'not ELF')
        self.assert_failed_restored(self.run_upgrade());self.assertFalse((self.root/'events').exists())
    def test_version_guards(self):
        path=self.pkg/'1pctl'; original=path.read_text()
        for version in ('v2.1.0','v2.2.0','v2.3.2-alpha.1','v2.nightly','bad'):
            with self.subTest(version=version):
                path.write_text(original.replace('v2.3.2',version));self.assert_failed_restored(self.run_upgrade())
        self.assertFalse((self.root/'events').exists())
    def test_missing_version_row(self):
        with sqlite3.connect(self.db/'agent.db') as c:c.execute('DELETE FROM settings')
        self.before=self.snapshot();self.assert_failed_restored(self.run_upgrade())
        self.assertFalse((self.root/'events').exists())

if __name__=='__main__':unittest.main()
