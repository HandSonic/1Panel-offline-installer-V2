#!/usr/bin/env python3
"""Add missing canonical assets; stop for explicit deletion approval on conflicts."""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
import hashlib

def digest(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""):h.update(chunk)
    return {"bytes":Path(path).stat().st_size,"sha256":h.hexdigest()}

class GitHub:
    def __init__(self, repo, tag):self.repo=repo;self.tag=tag
    def run(self,*args):return subprocess.run(['gh',*args],check=True,text=True,capture_output=True).stdout
    def release(self):
        release=json.loads(self.run('api',f'repos/{self.repo}/releases/tags/{self.tag}'))
        if type(release.get('id')) is not int or release['id']<=0 or release.get('tag_name')!=self.tag:
            raise ValueError('Release identity mismatch')
        assets=[]
        for page in range(1,101):
            rows=json.loads(self.run('api',f'repos/{self.repo}/releases/{release["id"]}/assets?per_page=100&page={page}'))
            if not isinstance(rows,list) or len(rows)>100:raise ValueError('Invalid release asset page')
            assets.extend(rows)
            if len(rows)<100:break
        else:raise ValueError('Release asset page limit exceeded')
        if any(not isinstance(a,dict) or type(a.get('id')) is not int or a['id']<=0 or not isinstance(a.get('name'),str) for a in assets):
            raise ValueError('Invalid release asset identity')
        if len({a['id'] for a in assets})!=len(assets) or len({a['name'] for a in assets})!=len(assets):
            raise ValueError('Duplicate release assets across pages')
        release['assets']=assets
        return release
    def upload(self,path):self.run('release','upload',self.tag,str(path),'--repo',self.repo)
    def download(self,name,directory):self.run('release','download',self.tag,'--repo',self.repo,'--pattern',name,'--dir',str(directory))

class Journal:
    def __init__(self,path,state):self.path=Path(path);self.state=state;self.save()
    def save(self):
        temporary=self.path.with_name(self.path.name+'.part')
        with temporary.open('w') as stream:
            json.dump(self.state,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        temporary.replace(self.path)

def check_asset_names(assets,expected):
    names=[a['name'] for a in assets]
    if len(names)!=len(set(names)):raise ValueError('Duplicate remote asset names')
    for name in set(names)-set(expected):
        if not any(re.fullmatch(re.escape(base)+r'\.(backup|staged)-[0-9a-f]{12}',name) for base in expected):
            raise ValueError(f'Unexpected canonical asset requires review: {name}')

def repair(client, files, journal_path, *, update_notes=True, retire_names=(), retire_version=None, retire_controls=False):
    before=client.release();assets={a['name']:a for a in before['assets']}
    if len(assets)!=len(before['assets']):raise ValueError('Duplicate remote asset names')
    expected={p.name:digest(p) for p in files}
    if len(expected)!=len(files):raise ValueError('Duplicate local publication filenames')
    retired=set(retire_names)
    if len(retired)!=len(retire_names) or retired&set(expected):raise ValueError('Invalid failed-branch retirement set')
    if retired:
        # Archive names use the authenticated application version, even when
        # the destination release tag has a separate publication suffix.
        if not isinstance(retire_version,str) or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?',retire_version) or not (
                client.tag==retire_version or re.fullmatch(re.escape(retire_version)+r'-[A-Za-z0-9._-]+',client.tag)):
            raise ValueError('Authenticated retirement version/target required')
        if any(not re.fullmatch(
                r'1panel-'+re.escape(retire_version)+r'-(official|custom|enterprise-original|enterprise-docker)-offline-linux-(amd64|arm64|armv7|ppc64le|s390x|loong64|riscv64)\.tar\.gz',name)
                for name in retired):raise ValueError('Invalid failed-branch retirement set')
    if retire_controls:
        if client.repo != 'HandSonic/1Panel-offline-installer-V2' or update_notes:
            raise ValueError('Control migration requires the lower package-only writer')
        controls={'release-validation.json','native-acceptance.json','checksums.txt'}
        if controls&set(expected):raise ValueError('Cannot publish and retire the same controls')
        retired |= {name+'.sha256' for name in retired} | controls
    if retired and update_notes:raise ValueError('Branch failures belong in Actions; preserve release notes')
    check_asset_names(before['assets'],set(expected)|retired)
    if any('.staged-' in name for name in assets):
        raise ValueError('Existing staged assets require review before publication')
    state={'repo':client.repo,'tag':client.tag,'phase':'preflight','operations':[],
           'retired_names':sorted(retired),'conflicts':[]}
    journal=Journal(journal_path,state)

    def snapshot(release):
        rows=release['assets']
        if len({a['id'] for a in rows})!=len(rows) or len({a['name'] for a in rows})!=len(rows):
            raise ValueError('Duplicate release asset identity')
        return {'release':{k:release.get(k) for k in ('id','tag_name','draft','prerelease','url')},
                'assets':sorted(({k:a.get(k) for k in ('id','name','size','digest','state','url')}
                                 for a in rows),key=lambda a:a['id'])}

    def existing_facts(asset,temporary):
        if type(asset.get('id')) is not int or asset['id']<=0 or type(asset.get('size')) is not int or asset['size']<=0:
            raise ValueError('Invalid existing asset identity/size')
        checksum=asset.get('digest')
        if isinstance(checksum,str) and re.fullmatch(r'sha256:[0-9a-f]{64}',checksum):
            return {'bytes':asset['size'],'sha256':checksum[7:]}
        if checksum not in (None,''):
            raise ValueError('Malformed existing server digest')
        # Historical assets may lack a server digest; read their bytes without
        # mutating them before deciding equality or listing exact conflicts.
        target=temporary/('existing-'+str(asset['id']));target.mkdir()
        client.download(asset['name'],target)
        facts=digest(target/asset['name'])
        if facts['bytes']!=asset['size']:raise ValueError('Existing asset size changed during readback')
        (target/asset['name']).unlink()
        return facts

    try:
        with tempfile.TemporaryDirectory() as temporary:
            temp=Path(temporary)
            # Whole-release preflight finishes before the first upload. Neither
            # replacement nor retired controls grant permission to delete.
            for name in sorted(set(assets)&(set(expected)|retired)):
                old=assets[name];facts=existing_facts(old,temp)
                if name in retired or facts!=expected[name]:
                    state['conflicts'].append({'asset_id':old['id'],'name':name,'size':facts['bytes'],
                        'sha256':facts['sha256'],'reason':'retired-canonical' if name in retired else 'different-content'})
            known=snapshot(before)
            if snapshot(client.release())!=known:raise ValueError('Release changed during publication preflight')
            if state['conflicts']:
                state['phase']='confirmation_required';journal.save()
                raise ValueError('Explicit deletion confirmation required for the exact assets listed in the journal')
            state['phase']='uploading';journal.save()
            ordered=sorted(files,key=lambda p:(p.name=='checksums.txt' or p.name.endswith('.sha256'),p.name))
            for source in ordered:
                if source.name in assets:continue
                if snapshot(client.release())!=known:raise ValueError('Release changed before canonical upload')
                facts=expected[source.name]
                if digest(source)!=facts:raise ValueError('Local publication bytes changed after preflight')
                state['upload_intent']={'name':source.name,**facts};journal.save()
                # No clobber, rename, backup, staged name or delete. If an upload
                # response is uncertain, stop; a later run can verify its bytes.
                client.upload(source)
                fresh=client.release();check_asset_names(fresh['assets'],set(expected)|retired)
                current={a['name']:a for a in fresh['assets']}
                uploaded=current.get(source.name)
                if not uploaded or uploaded.get('digest')!='sha256:'+facts['sha256'] or uploaded.get('size')!=facts['bytes']:
                    raise ValueError('Canonical upload server digest/size mismatch')
                without_new=dict(fresh,assets=[a for a in fresh['assets'] if a['name']!=source.name])
                if snapshot(without_new)!=known:raise ValueError('Existing release assets changed during upload')
                readback=temp/('uploaded-'+str(uploaded['id']));readback.mkdir()
                client.download(source.name,readback)
                if digest(readback/source.name)!=facts:raise ValueError('Canonical upload failed byte verification')
                (readback/source.name).unlink()
                state['operations'].append({'canonical':source.name,'new_id':uploaded['id'],
                                            'new_digest':'sha256:'+facts['sha256'],'bytes':facts['bytes']})
                state.pop('upload_intent',None);journal.save()
                known=snapshot(fresh);assets=current
            final=client.release()
            if snapshot(final)!=known:raise ValueError('Release changed during final verification')
            if retired&set(assets) or not set(expected)<=set(assets):
                raise ValueError('Final canonical publication inventory mismatch')
            # Existing equal bytes were established during preflight. New bytes
            # were independently downloaded and verified after their upload.
            state['phase']='complete';journal.save()
    except Exception as error:
        state['failure']=str(error)
        if state['phase']!='confirmation_required':state['phase']='failed'
        journal.save()
        raise
    return state
