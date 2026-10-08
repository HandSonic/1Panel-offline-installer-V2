#!/usr/bin/env python3
"""Exact per-version Go-embedded configuration checks; never execute binaries."""
import hashlib,json,re,sys,tarfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def expected_bytes(version,component,root=ROOT):
    registry=json.loads((root/'config/embedded-configs.json').read_text())
    if version not in registry:raise ValueError(f'Unreviewed embedded-config version: {version}')
    entry=registry[version];profile=entry['components'][component]
    original=(root/profile['source_file']).read_bytes()
    if hashlib.sha256(original).hexdigest()!=profile['source_sha256']:raise ValueError('Pinned source YAML changed')
    text=original.decode()
    for key,value in profile['normalized_fields'].items():
        value=version if value=='$VERSION' else entry['mode'] if value=='$MODE' else value
        text,count=re.subn(r'(?m)^(  '+re.escape(key)+r':) [^\r\n]+$',lambda m:m[1]+' '+value,text)
        if count!=1:raise ValueError(f'{version}/{component}: expected exactly one {key}, found {count}')
    normalized=text.encode()
    if hashlib.sha256(normalized).hexdigest()!=profile['normalized_sha256']:raise ValueError('Reviewed normalized YAML changed')
    return original,normalized,entry['source_commit']

def validate_binary(data,version,component,source_commit,root=ROOT):
    original,normalized,expected_commit=expected_bytes(version,component,root)
    if source_commit!=expected_commit:raise ValueError('Binary configuration source commit mismatch')
    if normalized not in data:raise ValueError(f'{version}/{component}: exact normalized production configuration not embedded')
    if original!=normalized and original in data:raise ValueError(f'{version}/{component}: unmodified development configuration remains embedded')
    return hashlib.sha256(normalized).hexdigest()

def validate_archive(path,version,arch,root=ROOT):
    prefix=f'1panel-{version}-linux-{arch}/'
    with tarfile.open(path,'r:gz') as archive:
        manifest=json.load(archive.extractfile(prefix+'manifest.json'))
        return {c:validate_binary(archive.extractfile(prefix+'1panel-'+c).read(),version,c,manifest['source_commit'],root) for c in ['core','agent']}

if __name__=='__main__':
    print(json.dumps(validate_archive(Path(sys.argv[1]),sys.argv[2],sys.argv[3]),sort_keys=True))
