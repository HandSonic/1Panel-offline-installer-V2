#!/usr/bin/env python3
"""Explicit, recoverable same-release repair. No deletions, no automatic CI use."""
import argparse
import json
import os
import re
import subprocess
import tempfile
import uuid
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
    def release(self):return json.loads(self.run('api',f'repos/{self.repo}/releases/tags/{self.tag}'))
    def upload(self,path):self.run('release','upload',self.tag,str(path),'--repo',self.repo)
    def rename(self,asset_id,name):self.run('api','--method','PATCH',f'repos/{self.repo}/releases/assets/{asset_id}','-f',f'name={name}')
    def download(self,name,directory):self.run('release','download',self.tag,'--repo',self.repo,'--pattern',name,'--dir',str(directory))
    def notes(self,body):self.run('release','edit',self.tag,'--repo',self.repo,'--notes',body)

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

def repair(client, files, journal_path):
    before=client.release();assets={a['name']:a for a in before['assets']}
    if len(assets)!=len(before['assets']):raise ValueError('Duplicate remote asset names')
    expected={p.name:digest(p) for p in files}
    if len(expected)!=len(files):raise ValueError('Duplicate local publication filenames')
    check_asset_names(before['assets'],expected)
    token=uuid.uuid4().hex[:12]
    state={'repo':client.repo,'tag':client.tag,'phase':'staging','old_notes':before.get('body') or '',
           'operations':[],'rollback_errors':[],'warning':'GitHub multi-asset changes are not atomic; a brief naming transition can occur.'}
    journal=Journal(journal_path,state)
    try:
        with tempfile.TemporaryDirectory() as temporary:
            temp=Path(temporary)
            for source in files:
                facts=digest(source);old=assets.get(source.name)
                if old and old.get('digest')=='sha256:'+facts['sha256'] and old['size']==facts['bytes']:
                    continue
                old_facts=None
                if old:
                    # Verify legacy bytes too; old GitHub assets may lack server digests.
                    old_readback=temp/('old-'+str(old['id']));old_readback.mkdir()
                    client.download(source.name,old_readback)
                    old_facts=digest(old_readback/source.name)
                    if old_facts['bytes']!=old['size'] or (old.get('digest') and old['digest']!='sha256:'+old_facts['sha256']):
                        raise ValueError('Original asset failed readback verification')
                    (old_readback/source.name).unlink()
                staged_name=f'{source.name}.staged-{token}';backup_name=f'{source.name}.backup-{token}'
                if staged_name in assets or backup_name in assets:raise ValueError('Staging name collision')
                stage=temp/staged_name
                try:os.link(source,stage)
                except OSError:
                    import shutil;shutil.copyfile(source,stage)
                state['staging_intent']={'name':staged_name,'sha256':facts['sha256'],'bytes':facts['bytes']};journal.save()
                client.upload(stage)
                fresh={a['name']:a for a in client.release()['assets']}
                uploaded=fresh[staged_name]
                if uploaded.get('digest')!='sha256:'+facts['sha256'] or uploaded['size']!=facts['bytes']:
                    raise ValueError('Staged server digest/size mismatch')
                readback=temp/('readback-'+str(uploaded['id']));readback.mkdir()
                client.download(staged_name,readback)
                if digest(readback/staged_name)!=facts:raise ValueError('Staged bytes failed download verification')
                operation={'canonical':source.name,'staged':staged_name,'backup':backup_name,
                           'old_id':old['id'] if old else None,'new_id':uploaded['id'],'old_digest':'sha256:'+old_facts['sha256'] if old else None,
                           'new_digest':'sha256:'+facts['sha256'],'old_size':old['size'] if old else None,
                           'old_renamed':False,'new_renamed':False}
                state['operations'].append(operation);state.pop('staging_intent',None);journal.save()
                (readback/staged_name).unlink();stage.unlink()
            state['phase']='switching';journal.save()
            for operation in state['operations']:
                current={a['id']:a for a in client.release()['assets']}
                staged=current.get(operation['new_id'])
                if not staged or staged['name']!=operation['staged'] or staged.get('digest')!=operation['new_digest']:
                    raise ValueError('Staged identity changed before switch')
                if operation['old_id'] is not None:
                    previous=current.get(operation['old_id'])
                    if not previous or previous['name']!=operation['canonical'] or previous['size']!=operation['old_size']:
                        raise ValueError('Original identity changed before switch')
                    if previous.get('digest') and previous['digest']!=operation['old_digest']:
                        raise ValueError('Original digest changed before switch')
                elif any(a['name']==operation['canonical'] for a in current.values()):
                    raise ValueError('A new canonical asset appeared concurrently')
                # Record intent first so a process interruption is reviewable.
                if operation['old_id'] is not None:
                    state['pending']={'asset_id':operation['old_id'],'name':operation['backup']};journal.save()
                    client.rename(operation['old_id'],operation['backup']);operation['old_renamed']=True;journal.save()
                state['pending']={'asset_id':operation['new_id'],'name':operation['canonical']};journal.save()
                client.rename(operation['new_id'],operation['canonical']);operation['new_renamed']=True;journal.save()
            state.pop('pending',None)
            state['phase']='verifying';journal.save()
            final_assets=client.release()['assets']
            check_asset_names(final_assets,expected)
            after={a['name']:a for a in final_assets}
            for operation in state['operations']:
                if after[operation['canonical']]['id']!=operation['new_id'] or after[operation['canonical']].get('digest')!=operation['new_digest']:
                    raise ValueError('Canonical asset verification failed')
                if operation['old_id'] is not None and after[operation['backup']]['id']!=operation['old_id']:
                    raise ValueError('Original backup verification failed')
            # Include unchanged assets and additions in the final canonical checksum contract.
            for name,facts in expected.items():
                asset=after.get(name)
                if not asset or asset.get('digest')!='sha256:'+facts['sha256'] or asset['size']!=facts['bytes']:
                    raise ValueError(f'Final canonical matrix/digest mismatch: {name}')
            if state['operations']:
                note='\n\nRelease asset repair: verified replacements uploaded; previous bytes remain as recoverable .backup assets and are not endorsed as validated installations.\n'
                note+='\n'.join(f"- {op['canonical']}: {op['new_digest']}; " + (f"backup {op['backup']} ({op['old_digest']})" if op['old_id'] is not None else 'new asset') for op in state['operations'])
                if (client.release().get('body') or '') != state['old_notes']:
                    raise ValueError('Release notes changed concurrently; refusing to overwrite')
                state['intended_notes']=state['old_notes']+note
                state['notes_update_attempted']=True;journal.save()
                client.notes(state['intended_notes'])
            state['phase']='complete';journal.save()
    except Exception as error:
        state['failure']=str(error);state['phase']='rolling_back';journal.save()
        # Resolve uncertain PATCH outcomes by reading current asset IDs before rollback.
        try:
            observed={a['id']:a['name'] for a in client.release()['assets']}
            for operation in state['operations']:
                operation['new_renamed']=observed.get(operation['new_id'])==operation['canonical']
                operation['old_renamed']=observed.get(operation['old_id'])==operation['backup']
        except Exception as inspect_error:
            state['rollback_errors'].append('Cannot establish remote state: '+str(inspect_error))
            state['phase']='rollback_incomplete';journal.save()
            raise
        # Preserve all staged bytes. Never delete either old or new assets.
        for operation in reversed(state['operations']):
            try:
                if operation['new_renamed']:client.rename(operation['new_id'],operation['staged']);operation['new_renamed']=False
                if operation['old_renamed']:client.rename(operation['old_id'],operation['canonical']);operation['old_renamed']=False
                journal.save()
            except Exception as rollback_error:
                state['rollback_errors'].append(str(rollback_error));journal.save()
        if state.get('notes_update_attempted'):
            try:
                current_notes=client.release().get('body') or ''
                if current_notes==state.get('intended_notes'):
                    client.notes(state['old_notes'])
                elif current_notes!=state['old_notes']:
                    state['concurrent_notes_preserved']=True
            except Exception as rollback_error:state['rollback_errors'].append(str(rollback_error))
        state['phase']='rollback_incomplete' if state['rollback_errors'] else 'rolled_back';journal.save()
        raise
    return state

