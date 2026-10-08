#!/usr/bin/env python3
"""Inspect payload bytes without executing downloaded binaries."""
import argparse
import gzip
import hashlib
import io
import json
import struct
import tarfile
from pathlib import Path, PurePosixPath

ARCHES = {'amd64': (2, 1, 62), 'arm64': (2, 1, 183), 'armv7': (1, 1, 40),
          'ppc64le': (2, 1, 21), 's390x': (2, 2, 22), 'riscv64': (2, 1, 243), 'loong64': (2, 1, 258)}
REQUIRED = {'docker', 'dockerd', 'containerd', 'containerd-shim-runc-v2', 'ctr', 'runc', 'docker-proxy', 'docker-init'}

APP_REQUIRED = ['1panel-core', '1panel-agent', '1pctl', 'GeoIP.mmdb'] + [
    f'initscript/1panel-{role}.{kind}' for role in ['core', 'agent']
    for kind in ['service', 'init', 'openrc', 'procd']] + [
    f'lang/{lang}.sh' for lang in ['en', 'fa', 'pt-BR', 'ru', 'zh']]
PAYLOAD_REQUIRED = ['docker.tgz', 'docker-compose', 'docker.service', 'install.sh', 'upgrade.sh'] + APP_REQUIRED

def elf(data, arch):
    if len(data) < 20 or data[:4] != b'\x7fELF':
        raise ValueError('Not an ELF binary')
    actual = (data[4], data[5], struct.unpack(('<' if data[5] == 1 else '>') + 'H', data[18:20])[0])
    if actual != ARCHES[arch]:
        raise ValueError(f'Wrong ELF architecture: {actual}, expected {ARCHES[arch]} ({arch})')
    return {'class': actual[0], 'endianness': actual[1], 'machine': actual[2]}

def members(archive):
    result = {}
    for member in archive.getmembers():
        name = PurePosixPath(member.name)
        if name.is_absolute() or '..' in name.parts or member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
            raise ValueError(f'Unsafe archive member: {member.name}')
        key = str(name)
        if key in result:
            raise ValueError(f'Duplicate archive member: {key}')
        result[key] = member
    return result

def docker(path, arch):
    result = {}
    if hasattr(path, 'read'):
        raw = path.getvalue()
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
            while compressed.read(1024 * 1024): pass
        archive_context = tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz')
    else:
        with gzip.open(path, 'rb') as compressed:
            while compressed.read(1024 * 1024): pass
        archive_context = tarfile.open(path, 'r:gz')
    with archive_context as archive:
        entries = members(archive)
        for binary in REQUIRED:
            key = 'docker/' + binary
            if key not in entries or not entries[key].isfile() or entries[key].size == 0:
                raise ValueError(f'Missing Docker binary: {key}')
        for name, member in entries.items():
            if member.isfile():
                if not member.mode & 0o111:
                    raise ValueError(f'Docker binary is not executable: {name}')
                if len(PurePosixPath(name).parts) != 2 or not name.startswith('docker/'):
                    raise ValueError(f'Unexpected Docker payload: {name}')
                data = archive.extractfile(member).read()
                result[name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'elf': elf(data, arch)}
    return result

def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return {'bytes': Path(path).stat().st_size, 'sha256': h.hexdigest()}

def manifest(directory, arch, source, version, docker_version, compose_version, app, docker_file, compose):
    directory = Path(directory)
    docker_binaries = docker(docker_file, arch)
    elf(Path(compose).read_bytes()[:20], arch)
    payloads = {}
    for name in PAYLOAD_REQUIRED:
        path = directory / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f'Required payload missing: {name}')
        payloads[name] = digest(path)
    for name in ['1panel-core', '1panel-agent']:
        elf((directory / name).read_bytes()[:20], arch)
    if '# OFFLINE_INSTALLER_PATCH_V2' not in (directory / 'install.sh').read_text():
        raise ValueError('Offline patch missing')
    inputs = {}
    for name, path, ver in [('app', app, version), ('docker', docker_file, docker_version), ('compose', compose, compose_version)]:
        origin = Path(str(path) + '.source.json')
        if not origin.is_file():
            raise ValueError(f'Missing source provenance: {path}')
        provenance = json.loads(origin.read_text())
        if provenance['sha256'] != digest(path)['sha256']:
            raise ValueError(f'Source provenance checksum mismatch: {path}')
        inputs[name] = dict(provenance, version=ver)
    output = {'schema': 1, 'architecture': arch, 'source': source, 'app_version': version,
              'inputs': inputs, 'payloads': payloads, 'docker_binaries': docker_binaries}
    if source == 'custom':
        from validate_upstream import checksum_sidecar
        upstream_sha=checksum_sidecar(str(app)+'.upstream.sha256', f'1panel-{version}-linux-{arch}.tar.gz')
        if upstream_sha!=inputs['app']['sha256']:raise ValueError('Custom source checksum no longer matches')
        inputs['app']['upstream_sha256']=upstream_sha
        inputs['app']['checksum_url']=inputs['app']['url']+'.sha256'
        origin_path=Path(str(app)+'.origin.json')
        if origin_path.exists():inputs['app'].update(json.loads(origin_path.read_text()))
        upstream=directory/'manifest.json'
        output['upstream_manifest_sha256']=digest(upstream)['sha256']
        output['upstream_provenance']=json.loads(upstream.read_text())
    (directory / 'offline-manifest.json').write_text(json.dumps(output, indent=2) + '\n')

if __name__ == '__main__':
    import sys
    try:
        mode, *args = sys.argv[1:]
        if mode == 'docker': print(json.dumps(docker(*args), indent=2))
        elif mode == 'elf': print(json.dumps(elf(Path(args[0]).read_bytes()[:20], args[1])))
        elif mode == 'archive':
            with tarfile.open(args[0]) as a:
                entries=members(a)
                if len(args)>1 and any(PurePosixPath(name).parts[0] != args[1] for name in entries):
                    raise ValueError('Unexpected application archive root')
        elif mode == 'manifest': manifest(*args)
        elif mode == 'cached':
            path, url = args
            recorded = json.loads(Path(path + '.source.json').read_text())
            if recorded != dict(digest(path), url=url): raise ValueError('Cache provenance mismatch')
        elif mode == 'provenance':
            path, url = args
            Path(path + '.source.json').write_text(json.dumps(dict(digest(path), url=url)) + '\n')
        else: raise ValueError('Unknown mode')
    except (ValueError, OSError, tarfile.TarError, KeyError) as exc:
        sys.exit(str(exc))
