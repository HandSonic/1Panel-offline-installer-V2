#!/usr/bin/env python3
"""Package-only transport trusts the named publisher and verifies bytes, not CI history."""
import hashlib
from pathlib import Path
import subprocess
import tempfile
import tarfile
from resolved_inventory import ARCHES, MAX_CONTROL, canonical, commit_value, file_facts, object_bytes, require, source_contract
from resolved_frontend_lock import required_sidecars, validate_payload, MAX_LOCK
from validate_resolved_custom import archive_bytes

FORMAT = 'package-only-v1'
SOURCE_KIND = 'trusted-package-release'
PREFIX = 'build-metadata/'


def pin(asset):
    require(type(asset.get('id')) is int and asset['id'] > 0 and isinstance(asset.get('digest'), str) and
            asset['digest'].startswith('sha256:'), 'Package asset requires an immutable API identity and digest')
    value = {'bytes': asset.get('size'), 'sha256': asset['digest'][7:]}
    file_facts(value)
    require(value['bytes'] <= 512 * 1024 ** 2, 'Package asset exceeds bounded size')
    return dict(asset_id=asset['id'], **value)


def metadata(path, version, mode, arch, expected):
    modes = {}
    data = archive_bytes(path, expected, f'1panel-{version}-linux-{arch}', modes=modes)
    require('manifest.json' in data, 'Package manifest missing')
    manifest = object_bytes(data.pop('manifest.json'))
    require(manifest.get('publication_format') == FORMAT, 'Package-only marker missing or unsupported')
    require(type(manifest.get('schema_version')) is int and manifest['schema_version'] == 1 and
            manifest.get('version') == version and manifest.get('mode') == mode and
            manifest.get('architecture') == arch and manifest.get('edition') == 'community', 'Package manifest identity mismatch')
    commit_value(manifest.get('build_repository_commit'))
    raw = data.get(PREFIX + 'resolved-source.json')
    require(isinstance(raw, bytes) and len(raw) <= MAX_CONTROL, 'Embedded source contract missing or oversized')
    sha = hashlib.sha256(raw).hexdigest()
    contract = source_contract(raw, sha, version, mode)
    require(canonical(contract) == raw and manifest.get('resolved_contract_sha256') == sha and
            manifest.get('source_commit') == contract['source']['commit'] and
            manifest.get('installer_commit') == contract['installer']['commit'], 'Embedded source contract differs from package identity')
    wanted = {PREFIX + 'resolved-source.json'} | {PREFIX + n for n in required_sidecars(contract)}
    require({name for name in data if name.startswith(PREFIX)} == wanted and
            all(not modes[name] & 0o111 for name in wanted), 'Unexpected or executable package metadata')
    for name in required_sidecars(contract):
        require(len(data[PREFIX + name]) <= MAX_LOCK, 'Embedded frontend lock oversized')
        validate_payload(data[PREFIX + name], contract)
    require(isinstance(manifest.get('files'), dict) and set(manifest['files']) == set(data), 'Package manifest must cover every regular member')
    for name, body in data.items():
        require(manifest['files'][name] == {'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}, 'Package member hash/size mismatch: ' + name)
    from validate_payload import elf
    for component in ('1panel-core', '1panel-agent'):
        require(component in data, 'Package binary missing')
        elf(data[component], arch)
    return contract, sha


def asset_map(release):
    assets = {a['name']: a for a in release['assets']}
    require(len(assets) == len(release['assets']) and len({a['id'] for a in release['assets']}) == len(assets), 'Duplicate package asset identity')
    return assets


def selected_assets(client, release, arch):
    version = client.tag
    assets = asset_map(release)
    name = f'1panel-{version}-linux-{arch}.tar.gz'
    require(name in assets and name + '.sha256' in assets, 'Package or SHA sidecar missing for ' + arch)
    archive_pin, sidecar_pin = pin(assets[name]), pin(assets[name + '.sha256'])
    require(sidecar_pin['bytes'] <= 4096, 'Package SHA sidecar oversized')
    sidecar = client.asset_bytes(assets[name + '.sha256'])
    require(len(sidecar) == sidecar_pin['bytes'] and hashlib.sha256(sidecar).hexdigest() == sidecar_pin['sha256'] and
            sidecar == (archive_pin['sha256'] + '  ' + name + '\n').encode('ascii'), 'Package SHA sidecar mismatch')
    return assets[name], archive_pin, sidecar_pin


def read_package(client, release, arch, mode, work):
    version = client.tag
    name = f'1panel-{version}-linux-{arch}.tar.gz'
    asset, archive_pin, sidecar_pin = selected_assets(client, release, arch)
    path = Path(work) / (arch + '.tar.gz')
    if hasattr(client, 'download_asset'):
        client.download_asset(asset, path)
    else:
        from native_upgrade_input import PredecessorGitHub
        PredecessorGitHub(client.repo, version).download_asset(asset, path)
    contract, sha = metadata(path, version, mode, arch, {k: archive_pin[k] for k in ('bytes', 'sha256')})
    after = client.release()
    require(after.get('id') == release['id'] and after.get('tag_name') == version and
            after.get('draft') is False and after.get('prerelease') == (mode != 'stable'), 'Package release identity changed during reading')
    current = asset_map(after)
    require(name in current and name + '.sha256' in current and pin(current[name]) == archive_pin and
            pin(current[name + '.sha256']) == sidecar_pin, 'Selected package assets changed during reading')
    return contract, sha


def controls(client, release, mode, selected_arch=None):
    """One working architecture supplies metadata; each shard checks itself."""
    assets = asset_map(release)
    choices = [selected_arch] if selected_arch else list(ARCHES)
    failure = None
    with tempfile.TemporaryDirectory(prefix='public-package-') as work:
        for arch in choices:
            try:
                contract, sha = read_package(client, release, arch, mode, work)
                break
            except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.SubprocessError) as error:
                failure = error
        else:
            raise ValueError('No usable package metadata: ' + str(failure))
    records, snapshots = {}, {}
    for arch in contract['architectures']:
        name = f'1panel-{client.tag}-linux-{arch}.tar.gz'
        try:
            raw, sidecar = pin(assets[name]), pin(assets[name + '.sha256'])
        except (KeyError, ValueError):
            continue  # Inventory still requests it; the absent shard fails alone.
        records[arch] = {'architecture': arch, 'file': name, 'sha256': raw['sha256'], 'size': raw['bytes'],
                        'source_commit': contract['source']['commit'], 'installer_commit': contract['installer']['commit'],
                        'build_repository_commit': None, 'resolved_contract_sha256': sha}
        snapshots[arch] = {'archive': raw, 'checksum': sidecar}
    return contract, sha, {'repository': client.repo, 'source_kind': SOURCE_KIND,
                           'release_id': release['id'], 'records': records, 'assets': snapshots}


def verify_selected(runtime, arch, client):
    """API/sidecar recheck only; the packaging validator checks full package bytes."""
    require(client.repo == runtime['upstream']['repository'] and client.tag == runtime['version'],
            'Wrong selected package target')
    release = client.release()
    expected = runtime['upstream']
    require(release.get('id') == expected['release_id'] and release.get('tag_name') == runtime['version'] and
            release.get('draft') is False and release.get('prerelease') == (runtime['mode'] != 'stable'),
            'Selected package release identity changed')
    asset, raw, checksum = selected_assets(client, release, arch)
    require(arch in expected['records'] and expected['assets'].get(arch) == {'archive': raw, 'checksum': checksum},
            'Selected package changed after planning')
    after = client.release()
    current = asset_map(after)
    name = asset['name']
    require(after.get('id') == release['id'] and after.get('tag_name') == runtime['version'] and
            after.get('draft') is False and after.get('prerelease') == release['prerelease'] and
            name in current and name + '.sha256' in current and pin(current[name]) == raw and
            pin(current[name + '.sha256']) == checksum, 'Selected package changed during verification')
    return expected['records'][arch]
