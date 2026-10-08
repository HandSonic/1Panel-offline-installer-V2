#!/usr/bin/env python3
"""Verify custom upstream's immutable-source manifest before downstream edits."""
import json
import re
import sys
from pathlib import Path, PurePosixPath
from validate_payload import APP_REQUIRED, digest

def validate_manifest(data, arch, version):
    if data.get('schema_version')!=1 or data.get('architecture')!=arch or data.get('version')!=version or data.get('edition')!='community':
        raise ValueError('Upstream manifest identity mismatch')
    for field in ['source_commit','installer_commit','build_repository_commit']:
        if not re.fullmatch(r'[0-9a-f]{40}',data.get(field,'')):raise ValueError(f'Invalid upstream {field}')
    files=data.get('files',{})
    if not set(APP_REQUIRED+['install.sh','1pctl']).issubset(files):raise ValueError('Upstream manifest missing required application files')
    for name,facts in files.items():
        p=PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or str(p)!=name or not isinstance(facts.get('size'),int) or facts['size']<=0 or not re.fullmatch(r'[0-9a-f]{64}',facts.get('sha256','')):
            raise ValueError('Unsafe or invalid upstream manifest entry')
    return data

def verify_directory(directory,arch,version,expected_commit=None):
    path=Path(directory)/'manifest.json'
    data=validate_manifest(json.loads(path.read_text()),arch,version)
    if expected_commit and data['build_repository_commit']!=expected_commit:raise ValueError('Upstream CI build commit mismatch')
    for name,facts in data['files'].items():
        actual=digest(Path(directory)/name)
        if actual!={'bytes':facts['size'],'sha256':facts['sha256']}:raise ValueError(f'Upstream manifest hash mismatch: {name}')
    actual_files={str(p.relative_to(directory)) for p in Path(directory).rglob('*') if p.is_file() and p!=path}
    if actual_files!=set(data['files']):raise ValueError('Upstream manifest does not enumerate every input file')
    return data

def checksum_sidecar(path, expected_name):
    text=Path(path).read_text()
    match=re.fullmatch(r'([0-9a-f]{64})  '+re.escape(expected_name)+r'\n?',text)
    if not match:raise ValueError('Invalid upstream SHA-256 sidecar or filename')
    return match.group(1)

if __name__=='__main__':
    try:
        if sys.argv[1]=='checksum':print(checksum_sidecar(sys.argv[2],sys.argv[3]))
        else:verify_directory(Path(sys.argv[1]),sys.argv[2],sys.argv[3],sys.argv[4] if len(sys.argv)>4 else None);print('Verified upstream source manifest')
    except Exception as exc:sys.exit(str(exc))
