#!/usr/bin/env python3
"""Independently validate new-version archives against resolved immutable inputs.

Expected contract/archive digests and producer commit must come from the
authenticated upstream release transport, not from the archive being checked.
No packaged program is executed; native acceptance remains a separate gate.
"""
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile

from resolved_inventory import canonical, file_facts, source_contract, commit_value, require
from semantic_configuration import production
from validate_payload import elf

EXPANDED_LIMIT = 2 * 1024 ** 3
FILE_LIMIT = 512 * 1024 ** 2
LOGICAL_LIMIT = EXPANDED_LIMIT


def byte_facts(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def archive_bytes(path, pin, root, selected=None, modes=None):
    """Read every pass through one descriptor and reject replacement/mutation."""
    require(modes is None or (isinstance(modes, dict) and not modes),
            'Archive mode inventory must be an empty dictionary')
    def identity(value):
        return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError('Cannot open stable regular custom archive') from exc
    with os.fdopen(descriptor, 'rb') as stream:
        initial = os.fstat(stream.fileno())
        require(stat.S_ISREG(initial.st_mode) and initial.st_size == pin['bytes'],
                'Custom input archive size/type mismatch')
        def unchanged():
            try:
                require(identity(os.fstat(stream.fileno())) == identity(initial) ==
                        identity(os.stat(path, follow_symlinks=False)),
                        'Custom archive changed during validation')
            except OSError as exc:
                raise ValueError('Custom archive changed during validation') from exc
        unchanged()
        checksum = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
        require(checksum.hexdigest() == pin['sha256'], 'Custom input archive SHA mismatch')
        unchanged()
        stream.seek(0)
        expanded = 0
        with gzip.GzipFile(fileobj=stream, mode='rb') as compressed:
            for block in iter(lambda: compressed.read(1024 * 1024), b''):
                expanded += len(block)
                require(expanded <= EXPANDED_LIMIT, 'Expanded custom archive exceeds limit')
        unchanged()
        stream.seek(0)
        data, permissions, seen, logical = {}, {}, set(), 0
        with tarfile.open(fileobj=stream, mode='r:gz') as archive:
            for member in archive:
                name = PurePosixPath(member.name)
                require(not name.is_absolute() and '..' not in name.parts and name.parts and
                        name.parts[0] == root and '\\' not in member.name and str(name) == member.name and
                        member.name not in seen, 'Unsafe or duplicate custom archive entry')
                seen.add(member.name)
                require(member.sparse is None and member.type != tarfile.GNUTYPE_SPARSE and
                        not any(key.startswith('GNU.sparse') or key in ('SCHILY.realsize', 'SCHILY.holes')
                                for key in member.pax_headers) and
                        member.pax_headers.get('SCHILY.filetype') != 'sparse',
                        'Sparse custom archive members are prohibited')
                if member.isdir():
                    continue
                require(member.isfile() and len(name.parts) > 1 and 0 < member.size <= FILE_LIMIT,
                        'Custom archive contains nonregular, empty or oversized entry')
                logical += member.size
                require(logical <= LOGICAL_LIMIT, 'Custom archive cumulative logical size exceeds limit')
                relative = name.relative_to(root).as_posix()
                if relative in ('1panel-core', '1panel-agent', '1pctl', 'install.sh'):
                    require(bool(member.mode & 0o111), 'Required custom executable is not executable')
                if selected is None or relative in selected:
                    if selected is not None:
                        require(member.size <= 64 * 1024 ** 2, 'Selected GeoIP member exceeds limit')
                    data[relative] = archive.extractfile(member).read()
                    require(len(data[relative]) == member.size, 'Truncated custom archive member')
                    # Preserve ordinary access/execute bits only; never restore
                    # setuid, setgid, or sticky bits from a package archive.
                    permissions[relative] = member.mode & 0o777
        unchanged()
    if modes is not None:
        modes.update(permissions)
    return data


def verify_archive(path, version, mode, arch, contract, contract_digest, archive_pin,
                   producer_commit, configuration_sources, aggregate_record, geoip_origin=None, capability_check=None):
    contract = source_contract(canonical(contract), contract_digest, version, mode)
    from resolved_inventory import installer_resource_layout
    resource_layout = installer_resource_layout(contract['installer']['resources'])
    require(arch in contract['architectures'], 'Unrequested custom architecture')
    commit_value(producer_commit)
    file_facts(archive_pin)
    expected_record = {'architecture': arch, 'file': f'1panel-{version}-linux-{arch}.tar.gz',
                       'sha256': archive_pin['sha256'], 'size': archive_pin['bytes'],
                       'source_commit': contract['source']['commit'],
                       'installer_commit': contract['installer']['commit'],
                       'build_repository_commit': producer_commit,
                       'resolved_contract_sha256': contract_digest}
    require(isinstance(aggregate_record, dict) and type(aggregate_record.get('size')) is int and
            aggregate_record == expected_record, 'Aggregate record differs from resolved archive/producer contract')
    path = Path(path)
    root = f'1panel-{version}-linux-{arch}'
    data = archive_bytes(path, archive_pin, root)
    required = set(contract['installer']['resources']) | {
        '1panel-core', '1panel-agent', '1panel-core.service', '1panel-agent.service', 'GeoIP.mmdb', 'manifest.json'}
    # The producer also copies these optional, non-runtime source documents.
    # Their bytes remain bound by the authenticated archive and full manifest.
    allowed = required | {'LICENSE', 'README.md'}
    require(required <= set(data) <= allowed,
            'Custom archive file set differs from resolved input contract: missing=' +
            repr(sorted(required - set(data))) + ', unexpected=' + repr(sorted(set(data) - allowed)))
    if capability_check is not None:
        capability_check(data['install.sh'],set(data),'custom')
    raw_manifest = data.pop('manifest.json')
    from resolved_inventory import object_bytes
    manifest = object_bytes(raw_manifest)
    expected = {'schema_version': 1, 'version': version, 'architecture': arch, 'edition': 'community',
                'source_commit': contract['source']['commit'], 'installer_commit': contract['installer']['commit'],
                'build_repository_commit': producer_commit, 'mode': mode,
                'resolved_contract_sha256': contract_digest,
                **{k + '_version': value for k, value in contract['toolchain'].items()}}
    require(type(manifest.get('schema_version')) is int and all(manifest.get(k) == value for k, value in expected.items()),
            'Custom manifest differs from immutable producer contract')
    require(isinstance(manifest.get('files'), dict) and set(manifest['files']) == set(data),
            'Custom manifest does not enumerate every archive member')
    for name, raw in data.items():
        require(isinstance(manifest['files'][name], dict) and
                type(manifest['files'][name].get('size')) is int and
                manifest['files'][name] == {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()},
                'Custom manifest member mismatch: ' + name)
    for name, expected_pin in contract['installer']['resources'].items():
        raw = data[name]
        if name == '1pctl':
            require(len(re.findall(rb'(?m)^ORIGINAL_VERSION=' + re.escape(version.encode()) + rb'$', raw)) == 1,
                    'Custom control utility does not contain exactly the requested version')
            raw, count = re.subn(rb'(?m)^ORIGINAL_VERSION=[^\n]*',
                                b'ORIGINAL_VERSION=' + contract['installer']['original_version'].encode(), raw)
            require(count == 1, 'Ambiguous custom control version')
        require(byte_facts(raw) == expected_pin, 'Custom installer resource mismatch: ' + name)
    if resource_layout == 'root-systemd':
        from service_layout import service_layout
        require(service_layout(data['install.sh'], set(data))['layout'] == resource_layout,
                'Custom installer interface differs from its resource contract')
    require(byte_facts(data['GeoIP.mmdb']) == {k: contract['resources']['geoip'][k] for k in ('sha256', 'bytes')},
            'Custom GeoIP resource mismatch')
    geoip = contract['resources']['geoip']
    if 'archive' in geoip:
        require(geoip_origin is not None, 'Pinned GeoIP origin archive required')
        origin = geoip['archive']
        origin_data = archive_bytes(geoip_origin, {k: origin[k] for k in ('sha256', 'bytes')},
                                    f'1panel-{version}-linux-amd64', selected={'GeoIP.mmdb'})
        require(set(origin_data) == {'GeoIP.mmdb'} and byte_facts(origin_data['GeoIP.mmdb']) ==
                {k: geoip[k] for k in ('sha256', 'bytes')}, 'GeoIP origin member mismatch')
    require(isinstance(configuration_sources, dict) and set(configuration_sources) == {'core', 'agent'},
            'Both immutable configuration sources required')
    binaries = {}
    for component in ('core', 'agent'):
        original = configuration_sources[component]
        profile = contract['configuration'][component]
        require(isinstance(original, bytes) and byte_facts(original) ==
                {'sha256': profile['source_sha256'], 'bytes': profile['source_bytes']},
                'Configuration source bytes differ from immutable source contract')
        normalized = production(original, version, component, mode)
        require(hashlib.sha256(normalized).hexdigest() == profile['normalized_sha256'],
                'Resolved production configuration differs from semantic normalization')
        binary = data['1panel-' + component]
        elf(binary, arch)
        require(normalized in binary and (original == normalized or original not in binary),
                'Custom binary contains wrong production configuration')
        if resource_layout == 'initscript':
            require(data.get('initscript/1panel-' + component + '.service') == data['1panel-' + component + '.service'],
                    'Custom installed service differs from pinned installer service')
        binaries['1panel-' + component] = hashlib.sha256(binary).hexdigest()
    return {'version': version, 'architecture': arch, 'contract_sha256': contract_digest,
            'archive': dict(archive_pin), 'manifest_sha256': hashlib.sha256(raw_manifest).hexdigest(),
            'source_commit': contract['source']['commit'], 'installer_commit': contract['installer']['commit'],
            'producer_commit': producer_commit, 'binaries': binaries}
