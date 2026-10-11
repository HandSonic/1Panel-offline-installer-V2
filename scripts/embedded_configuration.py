#!/usr/bin/env python3
"""Exact runtime-resolved Go-embedded configuration checks; never execute binaries."""
import hashlib,json,sys,tarfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def expected_bytes(version,component,root=ROOT):
    from runtime_contract import require_selected, configuration
    return configuration(require_selected(version, root), component)

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
