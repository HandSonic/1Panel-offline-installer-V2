#!/usr/bin/env python3
"""Read and revalidate one existing package without changing its release."""
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from publication_outcomes import REPOSITORY, filename
from release_asset_repair import digest
from resolved_inventory import require


def discard(output, version, row):
    """Remove only this attempt's downloaded candidate and verification companion."""
    output = Path(output)
    paths = [output / row['source'] / filename(version, row)]
    if row['source'] == 'enterprise-docker':
        paths.append(output / 'enterprise-original' / filename(version, dict(row, source='enterprise-original')))
    for path in paths:
        path.unlink(missing_ok=True)
        if path.parent.is_dir() and not any(path.parent.iterdir()):
            path.parent.rmdir()


def fetch_existing(facts, row, output, root, client=None):
    """Acquire immutable API-pinned bytes while the caller still has a read token.

    An absent checksum may be completed by the normal writer. An existing
    checksum must already describe the exact archive; it is never ignored.
    Failed downloads are only a cache miss and never authorize remote changes.
    """
    from native_upgrade_input import PredecessorGitHub, asset_pin
    version = facts['version']
    require(facts['repository'] == REPOSITORY, 'Wrong package retry repository')
    client = client or PredecessorGitHub(REPOSITORY, facts['tag'], allow_drafts=True)
    require(client.repo == REPOSITORY and client.tag == facts['tag'], 'Wrong package retry destination')
    output = Path(output)
    name = filename(version, row)
    try:
        release = client.release()
        require(type(release.get('id')) is int and release['id'] > 0 and
                release.get('tag_name') == facts['tag'] and type(release.get('draft')) is bool and
                release.get('prerelease') == (facts['mode'] != 'stable'), 'Existing package release identity changed')
        assets = {asset['name']: asset for asset in release['assets']}
        require(len(assets) == len(release['assets']) and
                len({a['id'] for a in release['assets']}) == len(assets), 'Ambiguous existing package assets')
        if name not in assets:
            return None
        names = [name] + ([name + '.sha256'] if name + '.sha256' in assets else [])
        pins = {key: asset_pin(assets[key], key, REPOSITORY) for key in names}
        if len(names) == 2:
            require(pins[names[1]]['bytes'] <= 4096, 'Existing package checksum is oversized')
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='package-retry-', dir=output.parent) as temporary:
            temporary = Path(temporary)
            for key in names:
                client.download_asset(assets[key], temporary / key)
                require(digest(temporary / key) == {k: pins[key][k] for k in ('bytes', 'sha256')},
                        'Existing package download differs from immutable API bytes')
            if len(names) == 2:
                require((temporary / names[1]).read_bytes() ==
                        (pins[name]['sha256'] + '  ' + name + '\n').encode('ascii'),
                        'Existing package checksum differs from archive')
            after = client.release()
            current = {asset['name']: asset for asset in after['assets']}
            require(all(after.get(key) == release.get(key) for key in ('id', 'tag_name', 'draft', 'prerelease')) and
                    len(current) == len(after['assets']) and
                    [key for key in (name, name + '.sha256') if key in current] == names and
                    all(asset_pin(current[key], key, REPOSITORY) == pins[key] for key in names),
                    'Existing selected package changed during download')
            destination = output / row['source'] / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            require(not destination.exists() and not destination.is_symlink(), 'Existing retry output is not empty')
            shutil.move(temporary / name, destination)
        if row['source'] == 'enterprise-docker':
            from prepare_enterprise import acquire
            original = output / 'enterprise-original' / filename(version, dict(row, source='enterprise-original'))
            acquire(facts['resolved']['inventory']['enterprise']['archives'][row['arch']], original)
        return pins[name]
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        discard(output, version, row)
        return None


def validate_existing(facts, row, output, pin, root, checker):
    """Current static validation runs without credentials, then native gates still run."""
    from offline_public_source import package_manifest
    from runtime_contract import product_policy
    runtime = facts['resolved']
    path = Path(output) / row['source'] / filename(facts['version'], row)
    require(digest(path) == {key: pin[key] for key in ('bytes', 'sha256')}, 'Retry package bytes changed')
    if row['source'] == 'enterprise-original':
        expected = runtime['inventory']['enterprise']['archives'][row['arch']]
        require(all(pin[key] == expected[key] for key in ('bytes', 'sha256')),
                'Existing enterprise original differs from current vendor')
    else:
        manifest = package_manifest(path, pin, facts['version'], facts['tag'], row['source'], row['arch'])
        require(manifest['publication']['policy_fingerprint'] ==
                product_policy(runtime, row['source'], row['arch'], root), 'Existing package policy changed')
        for component in ('docker', 'compose'):
            expected = runtime['inventory'][component][row['arch']]
            require(all(manifest['inputs'][component].get(key) == expected[key]
                        for key in ('url', 'version', 'bytes', 'sha256')), 'Existing dependency source changed')
        if row['source'] == 'custom':
            expected = runtime['upstream']['records'][row['arch']]
            require(manifest['inputs']['app'].get('sha256') == expected['sha256'] and
                    manifest['inputs']['app'].get('bytes') == expected['size'], 'Existing custom source changed')
        else:
            vendor = 'official' if row['source'] == 'official' else 'enterprise'
            expected = runtime['inventory'][vendor]['archives'][row['arch']]
            require(all(manifest['inputs']['app'].get(key) == value for key, value in expected.items()),
                    'Existing vendor source changed')
    checker(Path(output), facts['version'], row['source'], [row['arch']])


VALIDATION_ERRORS = (OSError, ValueError, KeyError, TypeError, EOFError, tarfile.TarError, subprocess.SubprocessError)
