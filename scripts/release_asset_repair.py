#!/usr/bin/env python3
"""Add missing canonical assets; stop for explicit deletion approval on conflicts."""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
import hashlib
from urllib.parse import quote


class ProductConflicts(ValueError):
    publication_stage='product-preflight'
    publication_error_code='PRODUCT_CONFLICT'

def digest(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""):h.update(chunk)
    return {"bytes":Path(path).stat().st_size,"sha256":h.hexdigest()}

class GitHub:
    def __init__(self, repo, tag, *, allow_drafts=False):
        if type(allow_drafts) is not bool:raise ValueError('Invalid draft discovery policy')
        self.repo=repo;self.tag=tag;self.allow_drafts=allow_drafts;self._release_id=None
    def run(self,*args):return subprocess.run(['gh',*args],check=True,text=True,capture_output=True).stdout
    def pin_release(self,release_id):
        if type(release_id) is not int or release_id<=0:raise ValueError('Invalid pinned release identity')
        if self._release_id not in (None,release_id):raise ValueError('Pinned release identity changed')
        self._release_id=release_id
    def _draft_release_id(self):
        # Only an explicitly enabled writer may discover unpublished releases.
        # Read every page before choosing so duplicate tags cannot select a draft.
        matches=[];seen=set()
        for page in range(1,101):
            rows=json.loads(self.run('api',f'repos/{self.repo}/releases?per_page=100&page={page}'))
            if not isinstance(rows,list) or len(rows)>100:raise ValueError('Invalid release discovery page')
            for row in rows:
                if not isinstance(row,dict) or type(row.get('id')) is not int or row['id']<=0 or not isinstance(row.get('tag_name'),str):
                    raise ValueError('Invalid release discovery identity')
                if row['id'] in seen:raise ValueError('Duplicate release discovery identity')
                seen.add(row['id'])
                if row['tag_name']==self.tag:matches.append(row)
            if len(rows)<100:break
        else:raise ValueError('Release discovery page limit exceeded')
        if not matches:return None
        if len(matches)!=1 or matches[0].get('draft') is not True:
            raise ValueError('Missing or ambiguous exact draft release')
        return matches[0]['id']
    def release(self):
        if self._release_id is None:
            try:
                release=json.loads(self.run('api',f'repos/{self.repo}/releases/tags/{self.tag}'))
            except subprocess.CalledProcessError as error:
                if not self.allow_drafts or not re.search(r'\bHTTP 404\b',error.stderr or ''):raise
                release_id=self._draft_release_id()
                if release_id is None:raise
                self.pin_release(release_id)
                release=json.loads(self.run('api',f'repos/{self.repo}/releases/{self._release_id}'))
        else:
            release=json.loads(self.run('api',f'repos/{self.repo}/releases/{self._release_id}'))
        if not isinstance(release,dict) or type(release.get('id')) is not int or release['id']<=0 or release.get('tag_name')!=self.tag or (self._release_id is not None and release['id']!=self._release_id):
            raise ValueError('Release identity mismatch')
        if release.get('draft') is True and not self.allow_drafts:raise ValueError('Public release reader cannot accept a draft')
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
    def upload(self,path):
        if self._release_id is None:raise ValueError('Release upload requires a pinned identity')
        self.release()  # Recheck the bound ID/tag; never resolve the mutable tag for a write.
        path=Path(path);facts=digest(path)
        endpoint=f'https://uploads.github.com/repos/{self.repo}/releases/{self._release_id}/assets?name={quote(path.name,safe="")}'
        asset=json.loads(self.run('api','--hostname','github.com','--method','POST',endpoint,
            '-H','Content-Type: application/octet-stream','--input',str(path)))
        if not isinstance(asset,dict) or type(asset.get('id')) is not int or asset['id']<=0 or asset.get('name')!=path.name or asset.get('size')!=facts['bytes'] or asset.get('digest')!='sha256:'+facts['sha256']:
            raise ValueError('Uploaded asset identity/hash differs from exact local bytes')
        return asset
    def download(self,name,directory):self.run('release','download',self.tag,'--repo',self.repo,'--pattern',name,'--dir',str(directory))
    def download_id(self,asset_id,destination,size):
        # A name can change and change back between catalogue reads. Missing
        # server digests therefore require bytes from the pinned immutable ID.
        if type(asset_id) is not int or asset_id<=0 or type(size) is not int or size<=0:
            raise ValueError('Invalid immutable asset ID/size')
        with Path(destination).open('xb') as output, tempfile.TemporaryFile() as errors:
            with subprocess.Popen(['gh','api','-H','Accept: application/octet-stream',
                    f'repos/{self.repo}/releases/assets/{asset_id}'],
                    stdout=subprocess.PIPE,stderr=errors) as process:
                total=0
                try:
                    while True:
                        block=process.stdout.read(min(1024*1024,size+1-total))
                        if not block:break
                        total+=len(block)
                        if total>size:raise ValueError('Immutable asset exceeds pinned size')
                        output.write(block)
                    if process.wait(timeout=90)!=0 or total!=size:
                        raise ValueError('Immutable asset download failed or changed size')
                except BaseException:
                    process.kill();process.wait()
                    raise

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

def repair(client, files, journal_path, *, update_notes=True, retire_names=(), retire_version=None, retire_controls=False,isolate_products=False):
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
    if type(isolate_products) is not bool:raise ValueError('Invalid product isolation policy')
    product_pattern=re.compile(r'1panel-'+re.escape(retire_version or '')+r'-(official|custom|enterprise-original|enterprise-docker)-offline-linux-(amd64|arm64|armv7|ppc64le|s390x|loong64|riscv64)\.tar\.gz')
    def product_name(name):
        archive=name.removesuffix('.sha256')
        return archive if product_pattern.fullmatch(archive) else None
    if isolate_products:
        if client.repo!='HandSonic/1Panel-offline-installer-V2' or update_notes or not retire_controls or not isinstance(retire_version,str):
            raise ValueError('Product isolation requires the exact downstream package writer')
        products={product_name(name) for name in expected}
        if None in products or set(expected)!={name+suffix for name in products for suffix in ('','.sha256')}:
            raise ValueError('Product isolation requires complete archive/checksum pairs')
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
        client.download_id(asset['id'],target/asset['name'],asset['size'])
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
                old=assets[name]
                try:
                    facts=existing_facts(old,temp)
                except (OSError,ValueError,subprocess.SubprocessError):
                    if not isolate_products or product_name(name) is None or type(old.get('id')) is not int or old['id']<=0:raise
                    # Unreadable old bytes have no authority over a sibling.
                    # Preserve their exact ID/metadata and authorize no change.
                    checksum=old.get('digest')
                    state['conflicts'].append({'asset_id':old['id'],'name':name,
                        'size':old.get('size') if type(old.get('size')) is int and old['size']>=0 else None,
                        'sha256':checksum[7:] if isinstance(checksum,str) and re.fullmatch(r'sha256:[0-9a-f]{64}',checksum) else None,
                        'reason':'existing-content-unverifiable'})
                    continue
                if name in retired or facts!=expected[name]:
                    state['conflicts'].append({'asset_id':old['id'],'name':name,'size':facts['bytes'],
                        'sha256':facts['sha256'],'reason':'retired-canonical' if name in retired else 'different-content'})
            known=snapshot(before)
            if snapshot(client.release())!=known:raise ValueError('Release changed during publication preflight')
            # Old shared controls choose the public read protocol and cannot be
            # migrated partially. Product conflicts only block their own pair.
            shared_conflict=any(product_name(row['name']) is None for row in state['conflicts'])
            if state['conflicts'] and (not isolate_products or shared_conflict):
                state['phase']='confirmation_required';journal.save()
                raise ValueError('Explicit deletion confirmation required for the exact assets listed in the journal')
            blocked={product_name(row['name']) for row in state['conflicts']} if isolate_products else set()
            if isolate_products:state['blocked_products']=sorted(blocked)
            publishable={name:facts for name,facts in expected.items() if product_name(name) not in blocked}
            state['phase']='uploading';journal.save()
            ordered=sorted(files,key=lambda p:(p.name=='checksums.txt' or p.name.endswith('.sha256'),p.name))
            for source in ordered:
                if source.name not in publishable:continue
                if source.name in assets:continue
                if snapshot(client.release())!=known:raise ValueError('Release changed before canonical upload')
                facts=expected[source.name]
                if digest(source)!=facts:raise ValueError('Local publication bytes changed after preflight')
                state['upload_intent']={'name':source.name,**facts};journal.save()
                # No clobber, rename, backup, staged name or delete. If an upload
                # response is uncertain, stop; a later run can verify its bytes.
                response=client.upload(source)
                fresh=client.release();check_asset_names(fresh['assets'],set(expected)|retired)
                current={a['name']:a for a in fresh['assets']}
                uploaded=current.get(source.name)
                if not isinstance(response,dict) or not uploaded or type(response.get('id')) is not int or response['id']<=0 or response['id']!=uploaded.get('id') or response.get('name')!=source.name or response.get('digest')!='sha256:'+facts['sha256'] or response.get('size')!=facts['bytes'] or uploaded.get('digest')!='sha256:'+facts['sha256'] or uploaded.get('size')!=facts['bytes']:
                    raise ValueError('Canonical upload server digest/size mismatch')
                without_new=dict(fresh,assets=[a for a in fresh['assets'] if a['name']!=source.name])
                if snapshot(without_new)!=known:raise ValueError('Existing release assets changed during upload')
                readback=temp/('uploaded-'+str(uploaded['id']));readback.mkdir()
                client.download_id(response['id'],readback/source.name,facts['bytes'])
                if digest(readback/source.name)!=facts:raise ValueError('Canonical upload failed byte verification')
                (readback/source.name).unlink()
                state['operations'].append({'canonical':source.name,'new_id':uploaded['id'],
                                            'new_digest':'sha256:'+facts['sha256'],'bytes':facts['bytes']})
                state.pop('upload_intent',None);journal.save()
                known=snapshot(fresh);assets=current
            final=client.release()
            if snapshot(final)!=known:raise ValueError('Release changed during final verification')
            retained_conflicts={row['name'] for row in state['conflicts']} if isolate_products else set()
            if (retired&set(assets))-retained_conflicts or not set(publishable)<=set(assets):
                raise ValueError('Final canonical publication inventory mismatch')
            # Existing equal bytes were established during preflight. New bytes
            # were independently downloaded and verified after their upload.
            if state['conflicts']:
                state['phase']='partial'
                state['completed_products']=sorted({product_name(name) for name in publishable})
                journal.save()
                raise ProductConflicts('Conflicting products retained; independently safe products published')
            state['phase']='complete';journal.save()
    except Exception as error:
        state['failure_code']='PRODUCT_CONFLICT' if isinstance(error,ProductConflicts) else ('GITHUB_API_FAILED' if isinstance(error,subprocess.SubprocessError) else 'PUBLICATION_FAILED')
        if state['phase'] not in ('confirmation_required','partial'):state['phase']='failed'
        journal.save()
        raise
    return state
