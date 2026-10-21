#!/usr/bin/env python3
"""Read and revalidate one existing package without changing its release."""
from pathlib import Path
import os
import shutil
import subprocess
import tarfile
import tempfile

from publication_outcomes import REPOSITORY, filename
from release_asset_repair import digest
from resolved_inventory import require

ROOT = Path(__file__).resolve().parents[1]


def pending_rows(facts, root, client=None):
    """Select only products lacking a current, intact public publication.

    Discovery uncertainty requests the full matrix. Product-local read or input
    failures request just that product; only exact, public package-only bytes
    qualify for skipping the existing build and native gates.
    """
    rows = facts['rows']
    if os.environ.get('PUBLICATION_OPERATION') != 'build':
        return rows
    from native_upgrade_input import PredecessorGitHub
    from offline_public_source import (package_only, public_inventory, public_pair,
        download_manifest, publication_snapshot)
    try:
        require(facts['repository'] == REPOSITORY, 'Wrong package retry repository')
        client = client or PredecessorGitHub(REPOSITORY, facts['tag'], allow_drafts=True)
        require(client.repo == REPOSITORY and client.tag == facts['tag'], 'Wrong package retry destination')
        release = client.release()
        require(release.get('tag_name') == facts['tag'] and release.get('draft') is False and
                package_only(release), 'A public package-only release is required for retry selection')
        before = publication_snapshot(release)
        with tempfile.TemporaryDirectory(prefix='package-scope-') as temporary:
            # Empty selection inventories the directory once without requiring
            # missing/broken sibling archive-sidecar pairs to be complete.
            _, assets, _ = public_inventory(client, release, facts['version'], facts['mode'],
                                            Path(temporary), selected_name='')
            pending = []
            for row in rows:
                try:
                    name = filename(facts['version'], row)
                    with tempfile.TemporaryDirectory(dir=temporary, prefix='product-') as product:
                        work = Path(product)
                        pin = public_pair(client, assets, name, work)[name]
                        if row['source'] == 'enterprise-original':
                            vendor = facts['resolved']['inventory']['enterprise']['archives'][row['arch']]
                            require(all(vendor[key] == pin[key] for key in ('bytes', 'sha256')),
                                    'Public enterprise original differs from current vendor pin')
                            path = work / name
                            client.download_asset(assets[name], path)
                            require(digest(path) == {key: pin[key] for key in ('bytes', 'sha256')},
                                    'Public enterprise original bytes changed')
                        else:
                            manifest = download_manifest(client, assets[name], pin, facts['version'],
                                facts['tag'], row['source'], row['arch'], work, current_installer=True)
                            validate_inputs(manifest, facts['resolved'], row['source'], row['arch'], root)
                except VALIDATION_ERRORS:
                    pending.append(row)
            require(publication_snapshot(client.release()) == before,
                    'Public packages changed during retry selection')
        return pending
    except VALIDATION_ERRORS:
        return rows


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


def validate_inputs(manifest, runtime, source, arch, root=ROOT):
    """Compare immutable product inputs without tying old bytes to publisher code."""
    inventory = runtime['inventory']
    require(isinstance(inventory.get('matrix'), dict) and arch in inventory['matrix'].get(source, []),
            'Existing product is not requested by the current matrix')
    inputs = manifest.get('inputs')
    require(isinstance(inputs, dict), 'Existing package inputs must be an object')
    for component in ('docker', 'compose'):
        expected = inventory[component][arch]
        actual = inputs.get(component)
        require(isinstance(actual, dict), 'Existing dependency input must be an object: ' + component)
        require(all(actual.get(key) == expected[key]
                    for key in ('url', 'version', 'bytes', 'sha256')), 'Existing dependency source changed')
    app = inputs.get('app')
    require(isinstance(app, dict), 'Existing app input must be an object')
    if source == 'custom':
        upstream = runtime.get('upstream')
        require(isinstance(runtime.get('source_contract'), dict) and isinstance(upstream, dict) and
                isinstance(upstream.get('records'), dict) and isinstance(upstream['records'].get(arch), dict),
                'Current custom product inputs are unavailable')
        expected = upstream['records'][arch]
        require(app.get('sha256') == expected['sha256'] and
                app.get('bytes') == expected['size'], 'Existing custom source changed')
        provenance = manifest.get('upstream_provenance')
        require(isinstance(provenance, dict), 'Existing custom provenance must be an object')
        require(provenance.get('resolved_contract_sha256') == runtime['source_contract_sha256'],
                'Existing custom source contract changed')
    else:
        vendor = 'official' if source == 'official' else 'enterprise'
        expected = inventory[vendor]['archives'][arch]
        require(all(app.get(key) == expected[key] for key in ('url', 'version', 'bytes', 'sha256')),
                'Existing vendor source changed')
    if source != 'enterprise-original':
        payloads = manifest.get('payloads')
        require(isinstance(payloads, dict), 'Existing package payloads must be an object')
        templates = {'docker.service': Path(root) / 'docker.service'}
        if source in ('official', 'custom'):
            templates['upgrade.sh'] = Path(root) / 'upgrade_offline.sh'
        for name, path in templates.items():
            require(payloads.get(name) == digest(path), 'Existing shipped template changed: ' + name)


def validate_existing(facts, row, output, pin, root, checker):
    """Current static validation runs without credentials, then native gates still run."""
    from offline_public_source import package_manifest
    runtime = facts['resolved']
    path = Path(output) / row['source'] / filename(facts['version'], row)
    if row['source'] == 'enterprise-original':
        require(digest(path) == {key: pin[key] for key in ('bytes', 'sha256')}, 'Retry package bytes changed')
        expected = runtime['inventory']['enterprise']['archives'][row['arch']]
        require(all(pin[key] == expected[key] for key in ('bytes', 'sha256')),
                'Existing enterprise original differs from current vendor')
    else:
        # The protected manifest reader verifies the complete archive SHA on
        # its stable descriptor; do not scan the same archive first.
        manifest = package_manifest(path, pin, facts['version'], facts['tag'], row['source'], row['arch'],
                                    current_installer=True)
        validate_inputs(manifest, runtime, row['source'], row['arch'], root)
    checker(Path(output), facts['version'], row['source'], [row['arch']])


VALIDATION_ERRORS = (OSError, ValueError, KeyError, TypeError, EOFError, tarfile.TarError, subprocess.SubprocessError)
