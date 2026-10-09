#!/usr/bin/env python3
"""Preserve enterprise originals and stream-repack reviewed Docker enhancements."""
import argparse
import copy
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from patch_installer import patch
from validate_payload import APP_REQUIRED, digest, docker, elf, members
from enterprise_contract import validate_layout

ROOT = Path(__file__).resolve().parents[1]

def acquire(pin, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() or digest(dest)['sha256'] != pin['sha256']:
        part=dest.with_name(dest.name+'.part')
        try:
            subprocess.run(['curl','--retry','3','--retry-delay','2','-fL',pin['url'],'-o',str(part)],check=True)
            if digest(part)['sha256'] != pin['sha256']: raise ValueError('Downloaded source checksum mismatch')
            part.replace(dest)
        finally:
            part.unlink(missing_ok=True)
    facts=digest(dest)
    if 'bytes' in pin and facts['bytes'] != pin['bytes']: raise ValueError('Source byte count mismatch')
    return dict(pin, **facts)

class HashingReader:
    def __init__(self, reader): self.reader=reader; self.hash=hashlib.sha256(); self.size=0
    def read(self, size=-1):
        data=self.reader.read(size);self.hash.update(data);self.size+=len(data);return data
    def fact(self): return {'bytes':self.size,'sha256':self.hash.hexdigest()}

def build(version, arch, cache, output):
    enterprise=json.loads((ROOT/f'enterprise-sources-{version}.json').read_text())[arch]
    source=cache/f'enterprise-{version}-{arch}.tar.gz'
    app=acquire(enterprise,source)
    pins={c:json.loads((ROOT/f'{c}-sources.json').read_text())[arch] for c in ['docker','compose']}
    files={c:cache/f"{c}-{pins[c]['version']}-{arch}{'.tgz' if c=='docker' else ''}" for c in pins}
    inputs={c:acquire(pins[c],files[c]) for c in pins};inputs['app']=app
    inventory=docker(files['docker'],arch);elf(files['compose'].read_bytes()[:20],arch)
    original_dir=output/'enterprise-original';original_dir.mkdir(parents=True,exist_ok=True)
    original=original_dir/f'1panel-{version}-enterprise-original-offline-linux-{arch}.tar.gz'
    # A hard link (or byte copy) preserves the exact upstream archive, including installer.
    if not original.exists():
        try:os.link(source,original)
        except OSError:shutil.copyfile(source,original)
    if digest(original)['sha256'] != app['sha256']: raise ValueError('Original is not byte-identical')
    asset_name=f'1panel-{version}-enterprise-docker-offline-linux-{arch}'
    # Official enterprise upgrade.sh requires the original inner directory name.
    folder=f'1panel-{version}-linux-{arch}'
    enhanced_dir=output/'enterprise-docker';enhanced_dir.mkdir(parents=True,exist_ok=True)
    destination=enhanced_dir/(asset_name+'.tar.gz');partial=destination.with_name(destination.name+'.part')
    original_prefix=f'1panel-{version}-linux-{arch}/'
    payloads={};preserved={}
    with tarfile.open(source,'r:gz') as source_tar:
        entries=members(source_tar)
        installer_source = source_tar.extractfile(original_prefix+'install.sh').read()
        validate_layout(entries, original_prefix, installer_source, version, ROOT)
        for name in ['1panel-core','1panel-agent']:
            elf(source_tar.extractfile(original_prefix+name).read(20),arch)
        with tempfile.TemporaryDirectory() as temp:
            installer=Path(temp)/'install.sh';installer.write_bytes(installer_source)
            patch(installer)
            with tarfile.open(partial,'w:gz') as target:
                for name, member in entries.items():
                    if member.isdir() and name.rstrip('/')==original_prefix.rstrip('/'):continue
                    if not name.startswith(original_prefix): raise ValueError('Unexpected enterprise archive root')
                    relative=name[len(original_prefix):]
                    if not relative: continue
                    if relative in ['docker.tgz','docker-compose','docker.service','offline-manifest.json']:
                        raise ValueError('Unexpected preexisting enterprise Docker bundle; review required')
                    clone=copy.copy(member);clone.name=folder+'/'+relative
                    if member.isdir():target.addfile(clone);continue
                    if relative=='install.sh':
                        data=installer.read_bytes();clone.size=len(data);target.addfile(clone,io.BytesIO(data))
                        payloads[relative]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
                    else:
                        reader=HashingReader(source_tar.extractfile(member));target.addfile(clone,reader)
                        payloads[relative]=reader.fact();preserved[relative]=reader.fact()
                for name,path in [('docker.tgz',files['docker']),('docker-compose',files['compose']),('docker.service',ROOT/'docker.service')]:
                    info=target.gettarinfo(str(path.resolve()),folder+'/'+name)
                    if not info.isfile():raise ValueError('Bundled Docker payload must be a regular file')
                    if name=='docker-compose':info.mode=0o755
                    with path.open('rb') as data:target.addfile(info,data)
                    payloads[name]=digest(path)
                manifest={'schema':1,'architecture':arch,'source':'enterprise-docker','app_version':version,
                          'inputs':inputs,'payloads':payloads,'docker_binaries':inventory,'preserved_enterprise_files':preserved}
                data=(json.dumps(manifest,indent=2)+'\n').encode();info=tarfile.TarInfo(folder+'/offline-manifest.json');info.size=len(data)
                target.addfile(info,io.BytesIO(data))
    partial.replace(destination)
    print(f'Built enterprise original and Docker-enhanced {arch}: {destination}')
    return original,destination

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--version',required=True);parser.add_argument('--arch',required=True,choices=['amd64','arm64'])
    parser.add_argument('--cache',type=Path,default=ROOT/'build/cache')
    parser.add_argument('--output',type=Path,help='Output version directory (default: build/<version>)')
    args=parser.parse_args()
    build(args.version,args.arch,args.cache,args.output or ROOT/'build'/args.version)
