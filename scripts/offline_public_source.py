#!/usr/bin/env python3
"""Package-only lower releases: fixed publisher, API identity, SHA and manifest.

This is a public read protocol, not writer admission or historical native proof.
Old canonical receipts always select the existing strict reader. New publication
metadata lives only in repacked archives; enterprise originals stay untouched.
"""
import os
from pathlib import Path
import re
import subprocess
import tarfile

from public_predecessor import REPOSITORY, RECEIPT, release_identity
from resolved_inventory import ARCHES, digest as object_digest, file_facts, hash_value, object_bytes, require
from validate_resolved_custom import archive_bytes, byte_facts

FORMAT = 'package-only-v1'
CONTROLS = (RECEIPT, 'native-acceptance.json', 'checksums.txt')


def publication_metadata(version, root, source, arch, payloads):
    """Builders use the exact authenticated plan; standalone historical fixtures do not."""
    from runtime_contract import selected, product_policy, PLAN_PATH
    runtime = selected(version, root)
    if runtime is None:
        return {}
    plan = object_bytes(Path(os.environ[PLAN_PATH]).read_bytes())
    require(plan.get('repository') == REPOSITORY and plan.get('version') == version,
            'Package publication plan identity mismatch')
    return {'publication': {'format': FORMAT, 'repository': REPOSITORY,
            'release_tag': plan['tag'],
            'policy_fingerprint': product_policy(runtime, source, arch, payloads)}}


def package_pattern(version):
    return re.compile(r'1panel-' + re.escape(version) +
        r'-(official|custom|enterprise-original|enterprise-docker)-offline-linux-(' + '|'.join(ARCHES) + r')\.tar\.gz')


def package_only(release):
    """Presence chooses protocol; failure never downgrades a canonical old receipt."""
    names = {asset.get('name') for asset in release['assets']}
    if RECEIPT in names:
        return False
    require(not set(CONTROLS[1:]) & names, 'Incomplete legacy public controls')
    return True


def publication_snapshot(release):
    """Pin complete API state without making sibling payload validity a gate."""
    from native_candidate_input import positive
    assets = []
    for asset in release['assets']:
        require(isinstance(asset, dict) and positive(asset.get('id')) and
                isinstance(asset.get('name'), str), 'Malformed public asset identity')
        assets.append({key: asset.get(key) for key in ('id', 'name', 'state', 'url', 'size', 'digest')})
    require(len({a['id'] for a in assets}) == len(assets) and len({a['name'] for a in assets}) == len(assets),
            'Ambiguous package publication snapshot')
    return {'release': release_identity(release), 'assets': sorted(assets, key=lambda a: a['id'])}


def public_pair(client, assets, name, work):
    """Bind one archive and its exact individual SHA sidecar by immutable IDs."""
    from native_upgrade_input import asset_pin
    checksum = name + '.sha256'
    require(name in assets and checksum in assets, 'Selected public package/sidecar unavailable')
    pins = {key: asset_pin(assets[key], key) for key in (name, checksum)}
    require(pins[checksum]['bytes'] <= 4096, 'Oversized public package checksum')
    path = Path(work) / checksum
    client.download_asset(assets[checksum], path)
    raw = path.read_bytes()
    require(byte_facts(raw) == {key: pins[checksum][key] for key in ('bytes', 'sha256')} and
            raw == (pins[name]['sha256'] + '  ' + name + '\n').encode('ascii'),
            'Public package checksum differs from exact archive/API identity')
    return pins


def public_inventory(client, release, version, mode, work, selected_name=None):
    from native_upgrade_input import public_release
    require(client.repo == REPOSITORY and package_only(release), 'Package-only publisher required')
    identity = release_identity(release)
    require(identity['draft'] is False and identity['prerelease'] == (mode != 'stable'),
            'Package publication channel/state mismatch')
    pattern = package_pattern(version)
    publication_snapshot(release)
    pins, assets, archives, sidecars = {}, {}, set(), set()
    for asset in release['assets']:
        name = asset['name']; assets[name] = asset
        if pattern.fullmatch(name):
            archives.add(name)
        elif name.endswith('.sha256') and pattern.fullmatch(name[:-7]):
            sidecars.add(name[:-7])
        else:
            base = re.fullmatch(r'(.+)\.backup-[0-9a-f]{12}', name)
            require(base is not None and (base[1] in CONTROLS or pattern.fullmatch(base[1]) or
                    (base[1].endswith('.sha256') and pattern.fullmatch(base[1][:-7]))),
                    'Unexpected or staged package publication asset')
    require(archives or sidecars, 'Public package inventory unavailable')
    if selected_name is None:
        require(archives == sidecars, 'Public package/sidecar inventory mismatch')
        wanted = sorted(archives)
    else:
        # Individual sidecars make checksum and payload failures product-local.
        require((selected_name in archives) == (selected_name in sidecars),
                'Selected public product package/sidecar presence differs')
        wanted = [selected_name] if selected_name in archives else []
    for name in wanted:
        pins.update(public_pair(client, assets, name, work))
    require(publication_snapshot(public_release(client, release['id'])) == publication_snapshot(release),
            'Package publication changed during checksum validation')
    return pins, assets, archives


def package_manifest(path, pin, version, tag, source, arch, *, expected_installer=None, verify_scripts=False):
    inner = (f'1panel-{version}-linux-{arch}' if source == 'enterprise-docker' else
             f'1panel-{version}-{source}-offline-linux-{arch}')
    scripts = {'install.sh', 'docker.service'} | ({'upgrade.sh'} if source in ('official', 'custom') else set())
    try:
        selected = {'offline-manifest.json', 'install.sh'} if expected_installer is not None else {'offline-manifest.json'}
        if verify_scripts:
            selected |= scripts
        members = archive_bytes(path, {k: pin[k] for k in ('bytes', 'sha256')}, inner, selected=selected)
        raw = members.get('offline-manifest.json')
    except (OSError, EOFError, tarfile.TarError) as error:
        raise ValueError('Invalid package-only archive') from error
    require(isinstance(raw, bytes), 'Package-only offline manifest unavailable')
    manifest = object_bytes(raw)
    require(manifest.get('schema') == 1 and manifest.get('app_version') == version and
            manifest.get('source') == source and manifest.get('architecture') == arch,
            'Package-only manifest product mismatch')
    publication = manifest.get('publication')
    require(isinstance(publication, dict) and set(publication) ==
            {'format', 'repository', 'release_tag', 'policy_fingerprint'} and
            publication['format'] == FORMAT and publication['repository'] == REPOSITORY and
            publication['release_tag'] == tag, 'Package-only publication marker mismatch')
    hash_value(publication['policy_fingerprint'])
    if verify_scripts:
        payloads = manifest.get('payloads')
        require(isinstance(payloads, dict), 'Published script facts unavailable')
        for name in sorted(scripts):
            raw = members.get(name)
            require(isinstance(raw, bytes) and payloads.get(name) == byte_facts(raw),
                    'Published script differs from its manifest: ' + name)
    if expected_installer is not None:
        installer = members.get('install.sh')
        payloads = manifest.get('payloads')
        require(isinstance(installer, bytes) and isinstance(payloads, dict) and
                payloads.get('install.sh') == byte_facts(installer), 'Current offline installer differs from manifest')
        require(isinstance(expected_installer, bytes) and installer == expected_installer,
                'Existing offline installer differs from current source and generation recipe')
    return manifest


def download_manifest(client, asset, pin, version, tag, source, arch, work, *, expected_installer=None, verify_scripts=False):
    path = Path(work) / pin['name']
    client.download_asset(asset, path)
    return package_manifest(path, pin, version, tag, source, arch,
                            expected_installer=expected_installer, verify_scripts=verify_scripts)


def verify_format(client, release, pins, assets, archives, version, mode, work, preferred=None):
    """A marker establishes new format, never a missing old receipt alone."""
    pattern = package_pattern(version)
    names = sorted(name for name in archives if
                   '-enterprise-original-' not in name)
    if preferred in names:
        # A broken selected product never substitutes another marker/product.
        source, arch = pattern.fullmatch(preferred).groups()
        return preferred, download_manifest(client, assets[preferred], pins[preferred], version,
                                           release['tag_name'], source, arch, work)
    if names:
        # For a genuine selected-product absence, any intact marked package can
        # establish the format. A damaged sibling cannot veto another's marker.
        for name in names:
            try:
                pin = public_pair(client, assets, name, work)[name]
                source, arch = pattern.fullmatch(name).groups()
                manifest = download_manifest(client, assets[name], pin, version,
                                             release['tag_name'], source, arch, work)
                return name, manifest
            except (ValueError, KeyError, OSError, tarfile.TarError, subprocess.SubprocessError):
                continue
        raise ValueError('No intact package-only publication marker available')
    # Originals cannot carry a marker without violating vendor passthrough.
    # For an originals-only release, independently known vendor hashes are the
    # format/product authority instead; no old Actions receipt is substituted.
    require(archives, 'No package archive can establish the publication format')
    from resolved_transport import discover_vendor
    inventory = discover_vendor(version, mode, 'enterprise')
    for name in sorted(assets):
        match = pattern.fullmatch(name)
        if match is None:
            continue
        source, arch = match.groups()
        pin = public_pair(client, assets, name, work)[name]
        require(source == 'enterprise-original' and
                all(inventory['archives'].get(arch, {}).get(k) == pin[k] for k in ('bytes', 'sha256')),
                'Enterprise original differs from canonical vendor bytes')
    return None, None


def public_noop(client, release, plan, root, work):
    from package_retry import validate_inputs
    from native_upgrade_input import public_release
    version = plan['version']
    require(release_identity(release)['tag_name'] == plan['tag'] and plan['repository'] == REPOSITORY,
            'Public package release/tag mismatch')
    pins, assets, _ = public_inventory(client, release, version, plan['mode'], work)
    expected = {f'1panel-{version}-{source}-offline-linux-{arch}.tar.gz'
                for source, arches in plan['resolved']['inventory']['matrix'].items() for arch in arches}
    if set(pins) != expected | {name + '.sha256' for name in expected}:
        return False
    for name in sorted(expected):
        source, arch = package_pattern(version).fullmatch(name).groups()
        if source == 'enterprise-original':
            vendor = plan['resolved']['inventory']['enterprise']['archives'].get(arch, {})
            require(all(vendor.get(k) == pins[name][k] for k in ('bytes', 'sha256')),
                    'Public enterprise original differs from current vendor pin')
            path = Path(work) / name
            client.download_asset(assets[name], path)
            from release_asset_repair import digest
            require(digest(path) == {k: pins[name][k] for k in ('bytes', 'sha256')},
                    'Public enterprise original bytes changed')
        else:
            manifest = download_manifest(client, assets[name], pins[name], version, plan['tag'], source, arch, work,
                                         verify_scripts=True)
            validate_inputs(manifest, plan['resolved'], source, arch, root, current_templates=False)
        (Path(work) / name).unlink()
    require(publication_snapshot(public_release(client, release['id'])) == publication_snapshot(release),
            'Public packages changed during no-op validation')
    return True


def predecessor_inputs(archive, binding, source, arch, work):
    """Validate the new package's own immutable source and dependency pins.

    Fixed publisher authority replaces historical run/log lookup. The original
    source archive is still hash-pinned and compared by unpack_predecessor; no
    current dependency lock or different source generation may replace it.
    """
    from native_upgrade_input import download_url, PredecessorGitHub, asset_pin
    from public_predecessor_source import related_name
    from public_predecessor_dependencies import official_pin
    from resolved_inventory import vendor_base
    from validate_upstream import validate_manifest
    require(binding.get('publication_format') == FORMAT and binding.get('repository') == REPOSITORY,
            'Trusted public package binding required')
    version, mode = binding['version'], binding['mode']
    manifest = package_manifest(archive, binding['archive'], version, version, source, arch)
    require(object_digest(manifest) ==
            binding['manifest_content_sha256'], 'Selected package manifest changed')
    app = manifest['inputs']['app']
    pin = {key: app[key] for key in ('bytes', 'sha256')}
    file_facts(pin)
    name = f'1panel-{version}-linux-{arch}.tar.gz'
    path = Path(work) / 'source.tar.gz'
    source_proof = {'kind': 'package-pinned-original-source', 'pin': pin,
                    'authority': 'trusted lower publisher and SHA-covered offline manifest'}
    if source == 'official':
        url = vendor_base(version, mode, 'official') + name
        require(app.get('url') == url and app.get('version') == version,
                'Package official source is not canonical')
        download_url(dict(pin, url=url), path)
    else:
        require(source == 'custom' and app.get('version') == version and app.get('upstream_sha256') == pin['sha256'],
                'Package custom source identity mismatch')
        from resolved_transport import ControlGitHub, UPSTREAM
        upper = ControlGitHub(UPSTREAM, version)
        release = upper.release()
        require(release_identity(release, UPSTREAM)['tag_name'] == version and release['draft'] is False and
                release['prerelease'] == (mode != 'stable'), 'Pinned upper source release mismatch')
        candidates = [asset for asset in release['assets'] if related_name(asset.get('name'), name) and
                      asset.get('digest') == 'sha256:' + pin['sha256'] and asset.get('size') == pin['bytes']]
        require(candidates, 'Original package-pinned custom source unavailable')
        asset = min(candidates, key=lambda row: (row['name'] != name, row['id']))
        transport = asset_pin(asset, asset['name'], UPSTREAM)
        PredecessorGitHub(UPSTREAM, version).download_asset(asset, path)
        after = upper.release()
        require(release_identity(after, UPSTREAM) == release_identity(release, UPSTREAM) and
                [asset_pin(row, row['name'], UPSTREAM) for row in after['assets'] if row.get('id') == asset['id']] == [transport],
                'Pinned custom source changed during download')
        source_proof['asset'] = transport
    body = archive_bytes(path, pin, name.removesuffix('.tar.gz'))
    if source == 'custom':
        raw = body['manifest.json']
        upstream = validate_manifest(object_bytes(raw), arch, version)
        require(upstream == manifest.get('upstream_provenance') and
                byte_facts(raw)['sha256'] == manifest.get('upstream_manifest_sha256'),
                'Original custom source manifest differs from published package')
        for member, facts in upstream['files'].items():
            require(byte_facts(body[member]) == {'bytes': facts['size'], 'sha256': facts['sha256']},
                    'Original custom source member differs from embedded manifest')
    dependencies = {component: {arch: official_pin(component, {arch: manifest['inputs'][component]}, arch)}
                    for component in ('docker', 'compose')}
    dependency_proof = {'kind': 'trusted-public-package-dependencies',
                        'archive': binding['archive'], 'pins': dependencies}
    return body, source_proof, dependencies, dependency_proof
