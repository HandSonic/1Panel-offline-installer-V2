#!/usr/bin/env python3
"""Gate a complete, explicitly specified release matrix before publishing."""
import gzip
import hashlib
import io
import json
import re
import sys
import tarfile
from pathlib import Path
from validate_payload import ARCHES, members, docker, elf, digest, PAYLOAD_REQUIRED, APP_REQUIRED
from patch_installer import HELPERS
from validate_upstream import validate_manifest
from enterprise_contract import validate_layout

def enterprise_inventory(path, version, arch, lock_root):
    lock=json.loads((lock_root/f'enterprise-sources-{version}.json').read_text())[arch]
    if digest(path)['sha256'] != lock['sha256']:
        raise ValueError('Enterprise original differs from upstream checksum')
    with gzip.open(path,'rb') as stream:
        while stream.read(1024*1024): pass
    inventory={};prefix=f'1panel-{version}-linux-{arch}/'
    with tarfile.open(path,'r:gz') as archive:
        entries=members(archive)
        validate_layout(entries, prefix, archive.extractfile(prefix+'install.sh').read(), version, lock_root)
        for name,member in entries.items():
            if member.isdir() and name.rstrip('/')==prefix.rstrip('/'):continue
            if not name.startswith(prefix): raise ValueError('Unexpected original archive root')
            if not member.isfile():continue
            relative=name[len(prefix):];h=hashlib.sha256();size=0
            stream=archive.extractfile(member)
            head=stream.read(20);h.update(head);size+=len(head)
            if relative in ['1panel-core','1panel-agent']:elf(head,arch)
            for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk);size+=len(chunk)
            inventory[relative]={'bytes':size,'sha256':h.hexdigest()}
    return inventory

def validate(root, version, matrix, lock_root=None):
    lock_root = lock_root or Path(__file__).resolve().parents[1]
    expected = json.loads(Path(matrix).read_text())
    checksums = {}
    for line in (root / 'checksums.txt').read_text().splitlines():
        checksum, name = line.split('  ', 1)
        if '/' in name or name in checksums:
            raise ValueError('Checksums must contain unique flat asset names')
        checksums[name] = checksum
    wanted = {f'1panel-{version}-{source}-offline-linux-{arch}.tar.gz' for source, arches in expected.items() for arch in arches}
    files = list(root.glob('*/*.tar.gz'))
    if {p.name for p in files} != wanted or len(files) != len(wanted) or set(checksums) != wanted:
        raise ValueError('Incomplete or unexpected release matrix')
    for path in files:
        # tarfile can stop at tar end blocks before validating the gzip trailer.
        try:
            with gzip.open(path, 'rb') as stream:
                while stream.read(1024 * 1024): pass
        except (OSError, EOFError) as exc:
            raise ValueError(f'Invalid outer gzip stream: {path.name}: {exc}') from exc
        if digest(path)['sha256'] != checksums[path.name]:
            raise ValueError(f'Asset checksum mismatch: {path.name}')
        if '-enterprise-original-offline-' in path.name:
            arch=path.name.removesuffix('.tar.gz').rsplit('-linux-',1)[1]
            enterprise_inventory(path,version,arch,lock_root)
            continue
        with tarfile.open(path, 'r:gz') as archive:
            entries = members(archive)
            prefix = path.name.removesuffix('.tar.gz') + '/'
            if '-enterprise-docker-offline-' in path.name:
                arch=path.name.removesuffix('.tar.gz').rsplit('-linux-',1)[1]
                prefix=f'1panel-{version}-linux-{arch}/'
            m = json.load(archive.extractfile(prefix + 'offline-manifest.json'))
            if path.name != f"1panel-{m['app_version']}-{m['source']}-offline-linux-{m['architecture']}.tar.gz":
                raise ValueError('Manifest identity mismatch')
            for component in ['docker', 'compose']:
                lock = json.loads((lock_root / (component + '-sources.json')).read_text())[m['architecture']]
                actual = m['inputs'][component]
                bundled = m['payloads']['docker.tgz' if component == 'docker' else 'docker-compose']
                if actual['sha256'] != bundled['sha256'] or actual['bytes'] != bundled['bytes']:
                    raise ValueError(f'{component} provenance does not describe bundled bytes')
                if any(actual.get(k) != lock[k] for k in ['url', 'version', 'bytes', 'sha256']):
                    raise ValueError(f'Unreviewed {component} source: update the source lock before release')
            if m['source']=='custom':
                app_input=m['inputs']['app']
                from_ci=app_input.get('source_kind')=='github-actions-artifact'
                checksum_url=app_input.get('url','') if from_ci else app_input.get('url','')+'.sha256'
                if app_input.get('upstream_sha256')!=app_input.get('sha256') or app_input.get('checksum_url')!=checksum_url:
                    raise ValueError('Custom source was not verified against upstream checksum')
                raw=archive.extractfile(prefix+'manifest.json').read()
                upstream=validate_manifest(json.loads(raw),m['architecture'],version)
                from upstream_validation_contract import validate_manifest_contract
                validate_manifest_contract(upstream,version,m['architecture'],archive.extractfile(prefix+'1pctl').read(),lock_root)
                if from_ci:
                    if not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+',app_input['url']):raise ValueError('Invalid CI source URL')
                    if upstream['build_repository_commit']!=app_input.get('expected_build_repository_commit') or app_input.get('artifact_name')!=f"1panel-{version}-linux-{m['architecture']}.tar.gz":raise ValueError('CI artifact provenance mismatch')
                if hashlib.sha256(raw).hexdigest()!=m.get('upstream_manifest_sha256') or upstream!=m.get('upstream_provenance'):
                    raise ValueError('Upstream provenance changed during repack')
                for name,facts in upstream['files'].items():
                    if name in ['install.sh','upgrade.sh']:continue
                    body=archive.extractfile(prefix+name).read()
                    if name in ['1panel-core','1panel-agent']:
                        from embedded_configuration import validate_binary
                        validate_binary(body,version,name.removeprefix('1panel-'),upstream['source_commit'],root=lock_root)
                    if len(body)!=facts['size'] or hashlib.sha256(body).hexdigest()!=facts['sha256']:
                        raise ValueError(f'Upstream member changed during repack: {name}')
            if m['source']=='official':
                from official_source import source
                pin=source(version,m['architecture'],lock_root)
                if any(m['inputs']['app'].get(k)!=v for k,v in pin.items()):
                    raise ValueError('Official input provenance differs from reviewed source')
            if m['source']=='enterprise-docker':
                original=root/'enterprise-original'/f"1panel-{version}-enterprise-original-offline-linux-{m['architecture']}.tar.gz"
                original_inventory=enterprise_inventory(original,version,m['architecture'],lock_root)
                original_inventory.pop('install.sh')
                if original_inventory != m.get('preserved_enterprise_files'):
                    raise ValueError('Enterprise preserved-file inventory differs from original')
                for name,facts in original_inventory.items():
                    if m['payloads'].get(name) != facts: raise ValueError(f'Enterprise file changed: {name}')
                app_lock=json.loads((lock_root/f'enterprise-sources-{version}.json').read_text())[m['architecture']]
                if any(m['inputs']['app'].get(k)!=v for k,v in app_lock.items()):
                    raise ValueError('Enterprise input provenance mismatch')
            required=list(PAYLOAD_REQUIRED)
            if m['source']=='enterprise-docker': required=list(m['payloads'])
            for name in required:
                data = archive.extractfile(prefix + name).read()
                if len(data) != m['payloads'][name]['bytes'] or hashlib.sha256(data).hexdigest() != m['payloads'][name]['sha256']:
                    raise ValueError(f'Payload checksum mismatch: {name}')
                if name == 'docker.tgz':
                    if docker(io.BytesIO(data), m['architecture']) != m['docker_binaries']:
                        raise ValueError('Docker binary inventory mismatch')
                elif name in ['docker-compose','1panel-core','1panel-agent']: elf(data[:20], m['architecture'])
                elif name == 'install.sh' and HELPERS not in data.decode():
                    raise ValueError('Offline patch missing')
    return len(files)

if __name__ == '__main__':
    try:
        print(f'Validated {validate(Path(sys.argv[1]), sys.argv[2], sys.argv[3])} complete offline assets')
    except Exception as exc:
        sys.exit(str(exc))
