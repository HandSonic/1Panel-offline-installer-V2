#!/usr/bin/env python3
"""Check release completeness from small discovery responses and GitHub metadata.

This check selects work; it does not certify old payloads against today's build
recipe. Archive validation remains in candidate and predecessor consumers.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from publication_outcomes import REPOSITORY, filename, products
from public_predecessor import release_identity
from release_asset_repair import GitHub
from resolved_inventory import ARCHES, require, version_identity


def discover_rows(version, mode):
    """Vendor checksums and archive HEAD requests; never read package bodies."""
    from resolved_transport import discover_vendor
    official = discover_vendor(version, mode, 'official')
    enterprise = discover_vendor(version, mode, 'enterprise')
    require(official['availability'] == 'present' and
            enterprise['availability'] in ('present', 'absent'),
            'Cannot determine the current package inventory; retry discovery without building')
    matrix = {'official': list(official['archives']), 'custom': list(ARCHES)}
    if enterprise['availability'] == 'present':
        matrix.update({source: list(enterprise['archives'])
                       for source in ('enterprise-original', 'enterprise-docker')})
    return products(matrix)


def asset_facts(asset, name, repository):
    require(isinstance(asset, dict) and type(asset.get('id')) is int and asset['id'] > 0 and
            asset.get('name') == name and
            asset.get('url') == f'https://api.github.com/repos/{repository}/releases/assets/{asset["id"]}',
            'Invalid release asset identity: ' + name)
    require(asset.get('state') == 'uploaded' and type(asset.get('size')) is int and asset['size'] > 0,
            'Incomplete existing asset requires review: ' + name)
    value = asset.get('digest')
    require(isinstance(value, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', value),
            'Existing asset has no usable GitHub digest: ' + name)
    return {'bytes': asset['size'], 'sha256': value[7:]}


def checksum_bytes(name, asset):
    return (asset['digest'][7:] + '  ' + name + '\n').encode('ascii')


def inspect_publication(client, version, tag, mode, rows):
    """Separate missing archives from missing checksums without downloading either."""
    version_identity(version, mode)
    require(client.repo == REPOSITORY and client.tag == tag and
            (tag == version or re.fullmatch(re.escape(version) + r'-[A-Za-z0-9._-]+', tag)),
            'Unexpected publication destination')
    try:
        release = client.release()
    except subprocess.CalledProcessError as error:
        if (not re.search(r'\bHTTP 404\b', error.stderr or '') or
                not isinstance(error.cmd, (list, tuple)) or not error.cmd or
                error.cmd[-1] != f'repos/{client.repo}/releases/tags/{tag}' or
                getattr(client, '_release_id', None) is not None):
            raise
        return {'release': None, 'missing_rows': list(rows), 'checksum_names': []}
    identity = release_identity(release, client.repo)
    require(identity['tag_name'] == tag and identity['prerelease'] == (mode != 'stable'),
            'Publication release identity/channel mismatch')
    assets = release['assets']
    require(isinstance(assets, list) and all(isinstance(a, dict) and
            isinstance(a.get('name'), str) and type(a.get('id')) is int for a in assets) and
            len({a['name'] for a in assets}) == len(assets) and
            len({a['id'] for a in assets}) == len(assets), 'Ambiguous release asset inventory')
    assets = {a['name']: a for a in assets}
    missing, checksums = [], []
    for row in rows:
        name = filename(version, row)
        archive, sidecar = assets.get(name), assets.get(name + '.sha256')
        if archive is None:
            if sidecar is not None:
                asset_facts(sidecar, name + '.sha256', client.repo)
            missing.append(row)
            continue
        asset_facts(archive, name, client.repo)
        if sidecar is None:
            checksums.append(name)
            continue
        facts = asset_facts(sidecar, name + '.sha256', client.repo)
        raw = checksum_bytes(name, archive)
        require(facts == {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()},
                'Existing checksum conflicts with the published archive: ' + name)
    if identity['draft']:
        # Even a complete draft still needs the existing admission/publication
        # path. It is not a completed public release or a checksum-only repair.
        return {'release': release, 'missing_rows': list(rows), 'checksum_names': []}
    require(not checksums or not {'release-validation.json', 'native-acceptance.json', 'checksums.txt'} & set(assets),
            'Legacy shared checksums require explicit recovery; individual sidecars are not added automatically')
    return {'release': release, 'missing_rows': missing, 'checksum_names': checksums}


def complete_checksums(client, version, tag, mode, rows):
    """Add only missing sidecars of unchanged uploaded archives under the release lock."""
    state = inspect_publication(client, version, tag, mode, rows)
    release = state['release']
    require(release is not None and release.get('draft') is False,
            'Checksum completion requires an existing public release')
    client.pin_release(release['id'])
    with tempfile.TemporaryDirectory(prefix='publication-checksums-') as temporary:
        for name in state['checksum_names']:
            current = inspect_publication(client, version, tag, mode, rows)
            require(release_identity(current['release'], client.repo) == release_identity(release, client.repo),
                    'Release identity changed before checksum completion')
            before = {a['name']: a for a in release['assets']}
            assets = {a['name']: a for a in current['release']['assets']}
            require(name in assets and assets[name]['id'] == before[name]['id'] and
                    asset_facts(assets[name], name, client.repo) == asset_facts(before[name], name, client.repo),
                    'Archive changed before checksum completion: ' + name)
            if name not in current['checksum_names']:
                continue
            raw = checksum_bytes(name, assets[name])
            path = Path(temporary) / (name + '.sha256')
            path.write_bytes(raw)
            response = client.upload(path)
            expected = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
            require(isinstance(response, dict) and type(response.get('id')) is int and response['id'] > 0 and
                    response.get('name') == path.name and response.get('size') == len(raw) and
                    response.get('digest') in (None, '', 'sha256:' + expected['sha256']),
                    'Uploaded checksum differs from the expected bytes')
            for attempt in range(7):
                observed = client.release()
                require(release_identity(observed, client.repo) == release_identity(release, client.repo),
                        'Release identity changed after checksum completion')
                found = {a['name']: a for a in observed['assets']}
                require(name in found and found[name]['id'] == assets[name]['id'] and
                        asset_facts(found[name], name, client.repo) == asset_facts(assets[name], name, client.repo),
                        'Archive changed during checksum completion: ' + name)
                checksum = found.get(path.name)
                if checksum is not None and checksum.get('digest') == 'sha256:' + expected['sha256']:
                    require(checksum.get('id') == response['id'] and
                            asset_facts(checksum, path.name, client.repo) == expected,
                            'Uploaded checksum identity changed')
                    break
                require(attempt < 6, 'Uploaded checksum is not visible yet; retry the metadata check')
                time.sleep(5)
    return inspect_publication(client, version, tag, mode, rows)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('check', 'checksums'))
    parser.add_argument('--repository', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--mode', default='stable', choices=('stable', 'beta', 'dev'))
    args = parser.parse_args(argv)
    require(args.repository == REPOSITORY, 'Unexpected publication repository')
    version_identity(args.version, args.mode)
    rows = discover_rows(args.version, args.mode)
    client = GitHub(args.repository, args.tag, allow_drafts=True)
    if args.command == 'checksums':
        require(os.environ.get('GITHUB_ACTIONS') == 'true' and
                os.environ.get('GITHUB_REPOSITORY') == args.repository and
                os.environ.get('GITHUB_EVENT_NAME') in ('schedule', 'workflow_dispatch') and
                os.environ.get('PUBLICATION_OPERATION') == 'build' and
                not os.environ.get('CANDIDATE_PREDECESSOR') and not os.environ.get('REPAIR_PREDECESSOR'),
                'Checksum completion requires the ordinary build workflow')
        result = complete_checksums(client, args.version, args.tag, args.mode, rows)
    else:
        result = inspect_publication(client, args.version, args.tag, args.mode, rows)
    outputs = {'version': args.version, 'release_tag': args.tag, 'mode': args.mode,
               'build_required': str(bool(result['missing_rows'])).lower(),
               'checksum_required': str(bool(result['checksum_names'])).lower()}
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write(''.join(key + '=' + value + '\n' for key, value in outputs.items()))
    print(json.dumps(dict(outputs, missing_products=[row['key'] for row in result['missing_rows']],
                          missing_checksums=result['checksum_names'])))
    return outputs


if __name__ == '__main__':
    main()
