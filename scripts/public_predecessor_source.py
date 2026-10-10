#!/usr/bin/env python3
"""Resolve a public predecessor's original source, independent of current tags.

Lower receipt authentication precedes this module. Its pinned offline manifest
supplies expected raw bytes and contract identity; upper CI/public validators
independently authenticate the original producer. Retained assets are transport
only, never a substitute for missing producer controls or native acceptance.
"""
import copy
import re
import json

from resolved_inventory import ARCHES, file_facts, hash_value, require, vendor_base
from resolved_transport import ControlGitHub, UPSTREAM, public_controls, ci_controls
from public_predecessor import upstream_input_claim
from validate_resolved_custom import archive_bytes, byte_facts
from validate_upstream import validate_manifest


def related_name(name, base):
    return isinstance(name, str) and (name == base or
        re.fullmatch(re.escape(base) + r'\.backup-[0-9a-f]{12}', name) is not None)


class OriginalPublicControls:
    """Present exact retained assets to the existing public-controls validator.

    Canonical names here describe authenticated logical files. Every physical
    ID/name/digest is separately pinned and rechecked against complete live API
    lists, including backups. No bytes are renamed or written on GitHub.
    """
    def __init__(self, client, release, assets, selected):
        self.client, self.repo, self.tag = client, client.repo, client.tag
        self.initial_release = release
        self.selected = selected
        self.initial_assets = {a['id']: copy.deepcopy(a) for a in assets}

    def run(self, *args):
        # Legacy validators enumerate assets through the API rather than only
        # release(). Serve the same authenticated logical view on both paths.
        endpoint = args[-1]
        base = f'repos/{self.repo}/releases/{self.initial_release["id"]}'
        if endpoint == base:
            return json.dumps(self.release())
        match = re.fullmatch(re.escape(base) + r'/assets\?per_page=100&page=([1-9][0-9]*)', endpoint)
        if match:
            start = (int(match[1]) - 1) * 100
            return json.dumps(self.release()['assets'][start:start + 100])
        return self.client.run(*args)

    def asset_bytes(self, asset, **kwargs):
        return self.client.asset_bytes(asset, **kwargs)

    def release(self):
        from native_upgrade_input import public_release, release_identity, asset_pin
        fresh = public_release(self.client, self.initial_release['id'])
        require(release_identity(fresh, repository=UPSTREAM) ==
                release_identity(self.initial_release, repository=UPSTREAM),
                'Original upper release identity changed')
        live = {a['id']: a for a in fresh['assets']}
        require(len(live) == len(fresh['assets']), 'Duplicate original upper asset ID')
        result = []
        for logical, selected in self.selected.items():
            original = self.initial_assets[selected['id']]
            actual = live.get(selected['id'])
            require(isinstance(actual, dict) and
                    asset_pin(actual, original['name'], UPSTREAM) == asset_pin(original, original['name'], UPSTREAM),
                    'Original upper control/asset changed during authentication')
            result.append(dict(actual, name=logical))
        return dict(fresh, assets=result)


def original_public_view(version, mode, record):
    """Select receipt-bound transports; the caller authenticates its protocol."""
    from native_upgrade_input import array_pages, asset_pin, json_object, positive
    client = ControlGitHub(UPSTREAM, version)
    release = client.release()
    require(positive(release.get('id')) and release.get('tag_name') == version and
            release.get('draft') is False and release.get('prerelease') == (mode != 'stable'),
            'Original upper release unavailable')
    assets = array_pages(client, f'repos/{UPSTREAM}/releases/{release["id"]}/assets')
    require(len({a.get('name') for a in assets}) == len(assets) and
            len({a.get('id') for a in assets}) == len(assets), 'Ambiguous original upper assets')
    raw_pin = {'bytes': record['size'], 'sha256': record['sha256']}
    candidates = [a for a in assets if related_name(a.get('name'), 'release-validation.json')]
    require(0 < len(candidates) <= 32, 'Original upper publication receipt unavailable or ambiguous')
    matching = []
    for asset in candidates:
        asset_pin(asset, asset['name'], UPSTREAM)
        raw = client.asset_bytes(asset)
        proof = json_object(raw)
        files = proof.get('files')
        if not isinstance(files, dict) or files.get(record['file']) != raw_pin:
            continue
        if 'resolved_contract_sha256' in record:
            matches_contract = isinstance(files.get('resolved-source.json'), dict) and \
                files['resolved-source.json'].get('sha256') == record['resolved_contract_sha256']
        else:
            matches_contract = 'resolved-source.json' not in files and proof.get('schema') == 1 and \
                proof.get('contract') == 'upstream7'
        if matches_contract:
            matching.append((asset, proof))
    require(matching, 'Authenticated original upper publication controls unavailable')
    canonical = [row for row in matching if row[0]['name'] == 'release-validation.json']
    if canonical:
        asset, proof = canonical[0]
    else:
        require(len({row[0]['digest'] for row in matching}) == 1,
                'Ambiguous original upper publication receipts')
        asset, proof = min(matching, key=lambda row: row[0]['id'])
    selected = {'release-validation.json': asset}
    optional_transport = {f'1panel-{version}-linux-{arch}.tar.gz' for arch in ARCHES if arch != record['architecture']}
    optional_transport |= {name + '.sha256' for name in optional_transport}
    for name, pin in proof['files'].items():
        file_facts(pin)
        matches = [a for a in assets if related_name(a.get('name'), name) and
                   a.get('digest') == 'sha256:' + pin['sha256'] and a.get('size') == pin['bytes']]
        if not matches and name in optional_transport:
            # No synthetic asset metadata: the original control validators have
            # an explicit selected-product mode for absent unrelated raw/sidecar bytes.
            continue
        require(matches, 'Original upper receipt-bound asset unavailable: ' + name)
        exact = [a for a in matches if a['name'] == name]
        chosen = exact[0] if exact else min(matches, key=lambda row: row['id'])
        asset_pin(chosen, chosen['name'], UPSTREAM)
        selected[name] = chosen
    return OriginalPublicControls(client, release, assets, selected), proof


def retained_public_source(version, mode, arch, record, work, original_ci=None):
    """Authenticate an original receipt and all of its pinned controls first."""
    from native_upgrade_input import asset_pin, PredecessorGitHub
    view, proof = original_public_view(version, mode, record)
    release, selected = view.initial_release, view.selected
    asset = selected['release-validation.json']
    contract, contract_sha, upstream = public_controls(version, mode, view, selected_arch=arch)
    require(contract_sha == record['resolved_contract_sha256'] and upstream['records'].get(arch) == record,
            'Original upper producer differs from predecessor source pins')
    if original_ci is not None:
        original, authentication = original_ci
        if 'upstream_input' in proof:
            require(upstream_input_claim(proof['upstream_input']) == original,
                    'Retained upper receipt differs from original CI descriptor')
        matrix = upstream['matrix_manifest']
        if matrix is not None and matrix.get('schema_version') == 3:
            matrix = matrix['generations'][matrix['artifact_generations'][arch]]
        if matrix is not None:
            require(matrix['producer_run_id'] == original['run_id'] and
                    matrix['producer_run_attempt'] == authentication['run_attempt'] and
                    matrix['producer_head_sha'] == authentication['run']['head_sha'],
                    'Retained upper producer differs from original CI upload')
        else:
            require(upstream['validation_run_id'] == original['run_id'] and
                    upstream['validation_commit'] == original['build_repository_commit'] and
                    authentication['run']['event'] != 'pull_request' and
                    authentication['run']['conclusion'] == 'success',
                    'Retained upper receipt lacks original CI producer identity')
        require(record['build_repository_commit'] == original['build_repository_commit'],
                'Retained raw producer differs from original CI input')
    raw_asset = selected[record['file']]
    path = work / 'source.tar.gz'
    PredecessorGitHub(UPSTREAM, version).download_asset(raw_asset, path)
    view.release()
    acquisition = {'kind': 'exact-retained-public-source', 'release_id': release['id'],
        'asset': asset_pin(raw_asset, raw_asset['name'], UPSTREAM),
        'receipt': asset_pin(asset, asset['name'], UPSTREAM),
        'controls': {name: asset_pin(row, row['name'], UPSTREAM) for name, row in selected.items()
                     if name in ('resolved-source.json', 'build-manifest.json', 'checksums.txt')}}
    if original_ci is not None:
        acquisition['original_ci_authentication'] = original_ci[1]
    return path, contract, contract_sha, upstream, acquisition


def source_from_public_predecessor(archive, binding, receipt, source, arch, work, root):
    """Caller must have authenticated binding/receipt against the public run log."""
    from native_upgrade_input import (json_object, download_url,
                                      validate_custom_source, SHARD_LIMIT)
    from manual_publication import fetch_ci_bundle, OriginalCIUnavailable, ci_run_identity, github_json
    try:
        version, mode = binding['version'], binding['mode']
        package_name = binding['archive']['name']
        pin = {key: binding['archive'][key] for key in ('bytes', 'sha256')}
        require(receipt['files'].get(package_name) == pin, 'Public source archive is not receipt-bound')
        selected = archive_bytes(archive, pin, package_name.removesuffix('.tar.gz'),
                                 selected={'offline-manifest.json', 'manifest.json'})
        manifest = json_object(selected['offline-manifest.json'])
        require(manifest.get('schema') == 1 and manifest.get('app_version') == version and
                manifest.get('source') == source and manifest.get('architecture') == arch,
                'Original offline source manifest identity mismatch')
        require(isinstance(manifest.get('inputs'), dict) and isinstance(manifest['inputs'].get('app'), dict),
                'Original source input evidence unavailable')
        app = manifest['inputs']['app']
        raw_pin = {key: app[key] for key in ('bytes', 'sha256')}
        file_facts(raw_pin)
        require(raw_pin['bytes'] <= SHARD_LIMIT and app.get('version') == version,
                'Original source version/size mismatch')
        name = f'1panel-{version}-linux-{arch}.tar.gz'
        if source == 'official':
            expected_url = vendor_base(version, mode, 'official') + name
            require(app.get('url') == expected_url, 'Original official source URL is not canonical')
            source_pin = dict(raw_pin, url=expected_url)
            path = work / 'source.tar.gz'
            download_url(source_pin, path)
            body = archive_bytes(path, raw_pin, name.removesuffix('.tar.gz'))
            return body, {'kind': 'canonical-vendor', 'pin': source_pin,
                          'selection': 'exact authenticated public predecessor manifest; no current discovery'}
        require(source == 'custom', 'Only community packages use native upgrades')
        upstream_manifest = validate_manifest(json_object(selected['manifest.json']), arch, version)
        require(manifest.get('upstream_provenance') == upstream_manifest and
                manifest.get('upstream_manifest_sha256') == byte_facts(selected['manifest.json'])['sha256'] and
                app.get('upstream_sha256') == raw_pin['sha256'], 'Original custom source manifest binding mismatch')
        contract_sha = upstream_manifest.get('resolved_contract_sha256')
        record = {'architecture': arch, 'file': name, 'sha256': raw_pin['sha256'], 'size': raw_pin['bytes'],
                  **{key: upstream_manifest[key] for key in ('source_commit', 'installer_commit',
                     'build_repository_commit')}}
        if contract_sha is None:
            # The authenticated predecessor's original manifest selects legacy,
            # never a failed modern validation or the current tag's protocol.
            from legacy_predecessor_source import materialize_legacy
            from native_upgrade_input import PredecessorGitHub, asset_pin
            view, original_proof = original_public_view(version, mode, record)
            if 'upstream_input' in receipt:
                original = upstream_input_claim(receipt['upstream_input'])
                if 'run_id' in original:
                    require('upstream_input' in original_proof and
                            upstream_input_claim(original_proof['upstream_input']) == original,
                            'Legacy receipt lacks original CI descriptor')
            downloader = PredecessorGitHub(UPSTREAM, version)
            def download(logical, destination):
                downloader.download_asset(view.selected[logical['name']], destination)
            body, proof = materialize_legacy(version, mode, arch, work, client=view, download=download, selected_arch=arch)
            require(proof.get('pin') == raw_pin and proof.get('record') == record and
                    body.get('manifest.json') == selected['manifest.json'],
                    'Authenticated legacy source differs from predecessor original bytes')
            proof['acquisition'] = {'kind': 'exact-retained-public-source',
                'asset': asset_pin(view.selected[name], view.selected[name]['name'], UPSTREAM),
                'receipt': asset_pin(view.selected['release-validation.json'],
                                    view.selected['release-validation.json']['name'], UPSTREAM)}
            if 'upstream_input' in receipt:
                proof['original_upstream_input'] = original
            proof['offline_manifest_sha256'] = byte_facts(selected['offline-manifest.json'])['sha256']
            return body, proof
        hash_value(contract_sha)
        record['resolved_contract_sha256'] = contract_sha
        original = upstream_input_claim(receipt['upstream_input'])
        if 'run_id' in original:
            require(app.get('source_kind') == 'github-actions-artifact' and app.get('url') == original['run_url'] and
                    app.get('checksum_url') == original['run_url'] and app.get('artifact_name') == name and
                    app.get('expected_build_repository_commit') == original['build_repository_commit'] ==
                    record['build_repository_commit'], 'Original CI receipt/manifest provenance mismatch')
            directory = work / 'original-ci-source'
            ci_evidence = {}
            try:
                actual = fetch_ci_bundle(directory, version, original['run_id'], original['artifact_id'],
                    original['artifact_sha256'], original['build_repository_commit'], 'downstream17', evidence=ci_evidence)
            except OriginalCIUnavailable as unavailable:
                path, contract, checked_sha, upstream, acquisition = retained_public_source(
                    version, mode, arch, record, work, (original, unavailable.authentication))
                auth = unavailable.authentication
                require(ci_run_identity(github_json(f'repos/{UPSTREAM}/actions/runs/{original["run_id"]}'),
                                        original['run_id']) == auth['latest_run'] and
                        ci_run_identity(github_json(f'repos/{UPSTREAM}/actions/runs/{original["run_id"]}/attempts/{auth["run_attempt"]}'),
                                        original['run_id']) == auth['run'], 'Original CI producer changed during retained acquisition')
            else:
                require(actual == original, 'Original CI provenance changed')
                contract, checked_sha, upstream = ci_controls(directory, version, mode, actual)
                path = directory / name
                acquisition = {'kind': 'exact-verified-ci-artifact', 'provenance': actual,
                               'original_ci_authentication': ci_evidence}
        else:
            expected_url = f'https://github.com/{UPSTREAM}/releases/download/{version}/{name}'
            require(app.get('source_kind') == 'release' and app.get('url') == expected_url and
                    app.get('checksum_url') == expected_url + '.sha256', 'Original public source provenance mismatch')
            path, contract, checked_sha, upstream, acquisition = retained_public_source(
                version, mode, arch, record, work)
        require(checked_sha == contract_sha and upstream['records'].get(arch) == record,
                'Authenticated original source differs from public predecessor')
        body, proof = validate_custom_source(path, version, mode, arch, contract, checked_sha,
                                             upstream, acquisition, work)
        require(body.get('manifest.json') == selected['manifest.json'], 'Original raw manifest differs from predecessor')
        proof['original_upstream_input'] = original
        proof['offline_manifest_sha256'] = byte_facts(selected['offline-manifest.json'])['sha256']
        return body, proof
    except (KeyError, TypeError, OSError) as error:
        raise ValueError('Original public predecessor source evidence unavailable or malformed') from error
