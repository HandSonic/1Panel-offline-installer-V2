#!/usr/bin/env python3
"""Validate release bytes without executing packaged binaries or installer scripts."""
import gzip,hashlib,json,pathlib,re,struct,sys,tarfile
from resolve_inputs import resolve
from embedded_configuration import validate_binary
ARCHES={'amd64':(2,1,62),'arm64':(2,1,183),'armv7':(1,1,40),'ppc64le':(2,1,21),'s390x':(2,2,22),'loong64':(2,1,258),'riscv64':(2,1,243)}
def architectures(value):
    arches=value.replace(',',' ').replace(';',' ').split()
    if not arches or len(arches)!=len(set(arches)) or any(a not in ARCHES for a in arches): raise ValueError('Invalid or duplicate architecture list')
    return arches

def validate_elf(data, arch):
    if len(data)<52 or data[:4]!=b'\x7fELF': raise ValueError('Missing ELF header')
    found=(data[4],data[5],int.from_bytes(data[18:20],'little' if data[5]==1 else 'big'))
    if found!=ARCHES[arch]: raise ValueError(f'Wrong ELF target {found}; expected {ARCHES[arch]}')

def validate_package(path, version, arch):
    entry=resolve(version); root=f'1panel-{version}-linux-{arch}'
    required=set(entry['installer_sha256'])|{'1panel-core','1panel-agent','1panel-core.service','1panel-agent.service','GeoIP.mmdb','manifest.json'}
    # tar readers may stop at end-of-archive before checking the gzip trailer.
    with gzip.open(path,'rb') as stream:
        while stream.read(1024*1024): pass
    with tarfile.open(path,'r:gz') as tf:
        data={}
        for member in tf.getmembers():
            p=pathlib.PurePosixPath(member.name)
            if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0]!=root: raise ValueError('Unsafe archive path')
            if member.isdir(): continue
            if not member.isfile() or len(p.parts)<2: raise ValueError('Links/nonregular archive member prohibited')
            rel=str(p.relative_to(root))
            if rel in data: raise ValueError('Duplicate archive member')
            if member.size<=0: raise ValueError(f'Empty file {rel}')
            if rel in ('1panel-core','1panel-agent','1pctl','install.sh') and not member.mode & 0o111: raise ValueError(f'Not executable: {rel}')
            data[rel]=tf.extractfile(member).read()
        if not required<=data.keys(): raise ValueError(f'Missing files: {required-data.keys()}')
        manifest=json.loads(data.pop('manifest.json'))
        expected={'schema_version':1,'version':version,'architecture':arch,'edition':'community','source_commit':entry['source_commit'],'installer_commit':entry['installer_commit'],'mode':entry['mode'],'go_version':entry['go_version'],'node_version':entry['node_version'],'npm_version':entry['npm_version']}
        for key,value in expected.items():
            if manifest.get(key)!=value: raise ValueError(f'Manifest mismatch {key}')
        if not re.fullmatch('[0-9a-f]{40}',manifest.get('build_repository_commit','')): raise ValueError('Missing build commit')
        if set(manifest['files'])!=set(data): raise ValueError('Manifest file set mismatch')
        for rel,content in data.items():
            if manifest['files'][rel]!={'sha256':hashlib.sha256(content).hexdigest(),'size':len(content)}: raise ValueError(f'Manifest digest mismatch {rel}')
        for binary in ('1panel-core','1panel-agent'):
            validate_elf(data[binary],arch)
            validate_binary(data[binary],version,binary.removeprefix('1panel-'),entry['source_commit'])
        if hashlib.sha256(data['GeoIP.mmdb']).hexdigest()!=entry['geoip_sha256']: raise ValueError('GeoIP mismatch')
        for rel,digest in entry['installer_sha256'].items():
            if rel=='1pctl':
                normalized,count=re.subn(rb'(?m)^ORIGINAL_VERSION=[^\n]*',b'ORIGINAL_VERSION='+entry['installer_original_version'].encode(),data[rel])
                if count!=1 or hashlib.sha256(normalized).hexdigest()!=digest: raise ValueError('1pctl content mismatch')
                continue
            if hashlib.sha256(data[rel]).hexdigest()!=digest: raise ValueError(f'Installer resource mismatch {rel}')
        if f'ORIGINAL_VERSION={version}'.encode() not in data['1pctl']: raise ValueError('1pctl version mismatch')
        for part in ('core','agent'):
            if data[f'1panel-{part}.service'] != data[f'initscript/1panel-{part}.service']: raise ValueError('Service copy mismatch')
        return manifest

def validate_dist(dist,version,arches):
    expected=set(); records=[]
    for arch in arches:
        name=f'1panel-{version}-linux-{arch}.tar.gz'; expected|={name,name+'.sha256'}
        path=dist/name; digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if (dist/(name+'.sha256')).read_text()!=f'{digest}  {name}\n': raise ValueError('Checksum must match bytes and relative basename')
        manifest=validate_package(path,version,arch)
        records.append({'architecture':arch,'file':name,'sha256':digest,'size':path.stat().st_size,'source_commit':manifest['source_commit'],'installer_commit':manifest['installer_commit'],'build_repository_commit':manifest['build_repository_commit']})
    expected|={'checksums.txt','build-manifest.json'}
    # CI may attach the resolved inputs alongside the immutable manifests.
    if (dist/'build-inputs.env').is_file(): expected.add('build-inputs.env')
    if {p.name for p in dist.iterdir()}!=expected: raise ValueError('Unexpected or missing release files')
    aggregate=''.join((dist/(r['file']+'.sha256')).read_text() for r in records)
    if (dist/'checksums.txt').read_text()!=aggregate: raise ValueError('Aggregate checksums mismatch')
    if json.loads((dist/'build-manifest.json').read_text())!={'schema_version':1,'version':version,'artifacts':records}: raise ValueError('Release manifest mismatch')
    return records
if __name__=='__main__':
    validate_dist(pathlib.Path(sys.argv[1]),sys.argv[2],architectures(' '.join(sys.argv[3:])))
    print('All requested artifacts verified')
