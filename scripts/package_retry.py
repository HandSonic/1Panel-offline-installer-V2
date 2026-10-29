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


def expected_installer(runtime, version, source, arch, work, cache, *, original=None):
    """Render the current recipe from authenticated original bytes, never old output.

    Custom installer bytes have their own immutable Git source pin. Vendor
    installers only have a whole-archive pin, so they must be read from that
    archive. One invocation's cache shares identical sources across products.
    """
    from resolved_inventory import file_facts, commit_value
    from validate_resolved_custom import archive_bytes, byte_facts
    from patch_installer import render
    if source == 'custom':
        from runtime_contract import custom_inputs
        installer = custom_inputs(runtime, arch)[0]['installer']
        require(installer['repository'] == '1Panel-dev/installer', 'Unexpected installer source')
        commit_value(installer['commit'])
        pin = installer['resources']['install.sh']
        file_facts(pin)
        require(pin['bytes'] <= 2 * 1024 * 1024, 'Original installer exceeds bounded size')
        key = ('custom', pin['bytes'], pin['sha256'])
    else:
        require(source in ('official', 'enterprise-docker'), 'Unexpected installer product')
        vendor = 'official' if source == 'official' else 'enterprise'
        pin = runtime['inventory'][vendor]['archives'][arch]
        file_facts({key: pin[key] for key in ('bytes', 'sha256')})
        require(pin['version'] == version, 'Original installer version changed')
        key = ('vendor', version, arch, pin['bytes'], pin['sha256'])
    if key not in cache:
        if source == 'custom':
            from resolved_transport import canonical_read
            response = canonical_read('https://raw.githubusercontent.com/1Panel-dev/installer/' +
                                      installer['commit'] + '/install.sh', limit=2 * 1024 * 1024)
            require(response['status'] == 200 and byte_facts(response['body']) == pin,
                    'Original custom installer differs from immutable source')
            raw = response['body']
        else:
            # Existing enterprise shards already carry their exact vendor
            # companion. Other current-package checks authenticate it once.
            with tempfile.TemporaryDirectory(prefix='installer-source-', dir=work) as temporary:
                path = Path(original) if original is not None else Path(temporary) / 'source.tar.gz'
                if original is None:
                    from native_upgrade_input import download_url
                    download_url(pin, path)
                members = archive_bytes(path, {key: pin[key] for key in ('bytes', 'sha256')},
                                        f'1panel-{version}-linux-{arch}', selected={'install.sh'})
                raw = members.get('install.sh')
            require(isinstance(raw, bytes) and 0 < len(raw) <= 2 * 1024 * 1024,
                    'Original vendor installer missing or oversized')
        # Match Path.read_text's universal newline handling in the builder.
        text = raw.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
        cache[key] = render(text).encode('utf-8')
    return cache[key]


def pending_rows(facts, root, client=None):
    """Build only missing archives; checksum-only gaps use the small-file writer."""
    rows = facts['rows']
    if os.environ.get('PUBLICATION_OPERATION') != 'build':
        return rows
    require(facts['repository'] == REPOSITORY, 'Wrong package retry repository')
    from publication_check import inspect_publication
    from release_asset_repair import GitHub
    client = client or GitHub(REPOSITORY, facts['tag'], allow_drafts=True)
    return inspect_publication(client, facts['version'], facts['tag'], facts['mode'], rows)['missing_rows']


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
        from runtime_contract import custom_inputs
        require(provenance.get('resolved_contract_sha256') == custom_inputs(runtime, arch)[1],
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
        original = (Path(output) / 'enterprise-original' / filename(facts['version'], dict(row, source='enterprise-original'))
                    if row['source'] == 'enterprise-docker' else None)
        with tempfile.TemporaryDirectory(prefix='retry-installer-', dir=Path(output).parent) as temporary:
            installer = expected_installer(runtime, facts['version'], row['source'], row['arch'],
                                           temporary, {}, original=original)
        manifest = package_manifest(path, pin, facts['version'], facts['tag'], row['source'], row['arch'],
                                    expected_installer=installer)
        validate_inputs(manifest, runtime, row['source'], row['arch'], root)
    checker(Path(output), facts['version'], row['source'], [row['arch']])


VALIDATION_ERRORS = (OSError, ValueError, KeyError, TypeError, EOFError, tarfile.TarError, subprocess.SubprocessError)
