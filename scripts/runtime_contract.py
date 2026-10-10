#!/usr/bin/env python3
"""Carry authenticated, per-run inputs without committing new version records.

The planner authenticates transport before creating this document. Every later
job supplies its exact plan SHA through a bound workflow output and rechecks the
whole contract. Merely placing a JSON file in a checkout never activates it.
"""
import hashlib
import json
import os
from pathlib import Path
import re

from resolved_inventory import (ARCHES, MAX_CONTROL, canonical, commit_value,
    digest, exact_keys, file_facts, hash_value, object_bytes, package_plan,
    require, source_contract)
from semantic_configuration import production

PLAN_PATH = 'ONEPANEL_RESOLVED_PLAN'
PLAN_SHA = 'ONEPANEL_RESOLVED_PLAN_SHA256'
ROOT = Path(__file__).resolve().parents[1]


def byte_facts(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def validate(value, version, root=ROOT):
    exact_keys(value, ('schema', 'kind', 'version', 'mode', 'source_contract',
                      'source_contract_sha256', 'inventory', 'upstream',
                      'configuration_sources') + (('custom_failure',) if value.get('source_contract') is None else ()), 'runtime contract')
    require(type(value['schema']) is int and value['schema'] == 1 and
            value['kind'] == '1panel-resolved-offline-inputs' and value['version'] == version,
            'Runtime contract identity mismatch')
    from resolved_inventory import version_identity
    from runtime_inventory import plan as runtime_plan
    version_identity(version,value['mode'])
    contract = None
    if value['source_contract'] is not None:
        contract = source_contract(canonical(value['source_contract']), value['source_contract_sha256'], version, value['mode'])
    else:
        require(value['source_contract_sha256'] is None and value['upstream'] is None and value['configuration_sources'] == {} and
                isinstance(value.get('custom_failure'), str) and 0 < len(value['custom_failure']) <= 512, 'Unresolved custom origin needs explicit failure')
    inventory = value['inventory']
    require(isinstance(inventory, dict), 'Missing resolved inventory')
    dependencies = {name: object_bytes((root / (name + '-sources.json')).read_bytes())
                    for name in ('docker', 'compose')}
    expected, _ = runtime_plan(version, value['mode'], contract, value['source_contract_sha256'],
                               inventory.get('official'), inventory.get('enterprise'),
                               dependencies['docker'], dependencies['compose'])
    require(inventory == expected, 'Runtime matrix/dependencies differ from complete discovery')
    if contract is None:
        return value
    upstream = value['upstream']
    exact_keys(upstream, ('repository', 'source_kind', 'validation_sha256', 'validation_run_id',
                         'validation_commit', 'producer_commit', 'records', 'matrix_manifest'), 'runtime upstream')
    require(upstream['repository'] == 'HandSonic/1Panel-Build-v2' and
            upstream['source_kind'] in ('verified-public-release', 'verified-ci-artifact') and
            type(upstream['validation_run_id']) is int and upstream['validation_run_id'] > 0,
            'Unexpected upstream receipt identity')
    hash_value(upstream['validation_sha256'])
    commit_value(upstream['validation_commit'])
    lineage = isinstance(upstream['matrix_manifest'], dict) and upstream['matrix_manifest'].get('schema_version') == 3
    if lineage:
        require(upstream['producer_commit'] is None, 'Mixed generations cannot claim a single producer')
    else:
        commit_value(upstream['producer_commit'])
    if upstream['matrix_manifest'] is None:
        accepted = list(ARCHES)
    else:
        if lineage:
            from upstream_lineage import validate as validate_outcomes
        else:
            from upstream_outcomes import validate as validate_outcomes
        accepted = validate_outcomes(upstream['matrix_manifest'], version)
        require(upstream['records'] == {row['architecture']: row for row in upstream['matrix_manifest']['artifacts']},
                'Upper accepted records differ from outcome manifest')
    require(isinstance(upstream['records'], dict) and set(upstream['records']) == set(accepted),
            'Upstream record set differs from explicit accepted architecture outcomes')
    for arch, row in upstream['records'].items():
        exact_keys(row, ('architecture', 'file', 'sha256', 'size', 'source_commit',
                         'installer_commit', 'build_repository_commit',
                         'resolved_contract_sha256'), 'resolved upstream row')
        file_facts({'bytes': row['size'], 'sha256': row['sha256']})
        require(row['architecture'] == arch and row['file'] == f'1panel-{version}-linux-{arch}.tar.gz' and
                row['source_commit'] == contract['source']['commit'] and
                row['installer_commit'] == contract['installer']['commit'] and
                (lineage or row['build_repository_commit'] == upstream['producer_commit']) and
                row['resolved_contract_sha256'] == value['source_contract_sha256'],
                'Upstream archive record differs from authenticated source contract')
    sources = value['configuration_sources']
    exact_keys(sources, ('core', 'agent'), 'runtime configuration sources')
    for part, text in sources.items():
        require(isinstance(text, str), 'Configuration source must be UTF-8 text')
        profile = contract['configuration'][part]
        raw = text.encode('utf-8')
        require(byte_facts(raw) == {'bytes': profile['source_bytes'], 'sha256': profile['source_sha256']},
                'Runtime configuration source changed')
        require(hashlib.sha256(production(raw, version, part, value['mode'])).hexdigest() ==
                profile['normalized_sha256'], 'Runtime normalized configuration changed')
    return value


def selected(version, root=ROOT, env=None):
    env = os.environ if env is None else env
    path, expected = env.get(PLAN_PATH), env.get(PLAN_SHA)
    if not path and not expected:
        return None
    require(bool(path) and bool(expected), 'Runtime plan path and authenticated SHA are both required')
    hash_value(expected)
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_CONTROL,
            'Unsafe or oversized runtime plan')
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == expected, 'Runtime plan bytes changed')
    document = object_bytes(raw)
    value = document.get('resolved')
    require(value is not None, 'Expected runtime contract is absent from plan')
    return validate(value, version, root)


def require_selected(version, root=ROOT, env=None):
    value = selected(version, root, env)
    require(value is not None, 'Authenticated runtime plan required; complete source/edition/architecture discovery first')
    return value


def configuration(value, component):
    original = value['configuration_sources'][component].encode('utf-8')
    normalized = production(original, value['version'], component, value['mode'])
    return original, normalized, value['source_contract']['source']['commit']


def manifest_contract(data, control, version, arch, root=ROOT):
    value = selected(version, root)
    require(value is not None, 'Resolved producer contract required')
    contract = value['source_contract']
    expected = {'schema_version': 1, 'edition': 'community', 'version': version,
                'architecture': arch, 'source_commit': contract['source']['commit'],
                'installer_commit': contract['installer']['commit'], 'mode': value['mode'],
                'build_repository_commit': value['upstream']['records'][arch]['build_repository_commit'],
                'resolved_contract_sha256': value['source_contract_sha256'],
                **{key + '_version': v for key, v in contract['toolchain'].items()}}
    require(all(data.get(key) == v for key, v in expected.items()),
            'Repacked upstream manifest differs from resolved producer contract')
    facts = data.get('files', {})
    pins = dict(contract['installer']['resources'])
    pins['GeoIP.mmdb'] = {k: contract['resources']['geoip'][k] for k in ('bytes', 'sha256')}
    for name, pin in pins.items():
        if name == '1pctl':
            normalized, count = re.subn(rb'(?m)^ORIGINAL_VERSION=[^\n]*',
                b'ORIGINAL_VERSION=' + contract['installer']['original_version'].encode(), control)
            require(count == 1 and len(re.findall(rb'(?m)^ORIGINAL_VERSION=' + re.escape(version.encode()) + rb'$', control)) == 1
                    and byte_facts(normalized) == pin, 'Repacked control utility differs from resolved source')
        else:
            require(facts.get(name) == {'size': pin['bytes'], 'sha256': pin['sha256']},
                    'Repacked producer resource differs from resolved source: ' + name)
    return data


def policy(value, root=ROOT):
    # Run IDs and receipt refreshes are transport facts, not source semantics.
    # The authenticated source/inventory and current implementation define policy.
    paths = ['scripts/runtime_contract.py', 'scripts/resolved_inventory.py', 'scripts/upstream_lineage.py',
             'scripts/resolved_frontend_lock.py', 'scripts/frontend_lock_origins.py',
             'scripts/resolved_transport.py', 'scripts/validate_resolved_custom.py',
             'scripts/runtime_publication.py', 'scripts/runtime_native_acceptance.py',
             'scripts/native_upgrade_input.py', 'scripts/native_upgrade_smoke.py',
             'scripts/public_predecessor_source.py',
             'scripts/public_predecessor_selection.py', 'scripts/public_predecessor_dependencies.py',
             'scripts/initial_release_applicability.py', 'scripts/initial_install_input.py',
             'scripts/legacy_predecessor_source.py', 'scripts/import_ci_artifact.py',
             'scripts/native_candidate_input.py', 'scripts/native_install_smoke.py',
             'scripts/public_predecessor.py', 'scripts/runtime_inventory.py', 'scripts/upstream_outcomes.py', 'scripts/publication_outcomes.py',
             'scripts/semantic_configuration.py', 'scripts/installer_capabilities.py', 'scripts/service_layout.py',
             'scripts/validate_release.py', 'scripts/validate_payload.py',
             'scripts/patch_installer.py', 'scripts/validate_upstream.py',
             'scripts/embedded_configuration.py', 'scripts/enterprise_contract.py',
             'scripts/validate_upstream_package.py', 'scripts/upstream_validation_contract.py',
             'scripts/official_source.py', 'prepare_offline.sh', 'scripts/prepare_enterprise.py',
             'scripts/package_matrix.py', 'scripts/release_asset_repair.py', 'scripts/publication_contract.py',
             'scripts/release_inventory.py', 'scripts/manual_publication.py',
             'upgrade_offline.sh', 'docker.service', 'requirements-validation.txt',
             '.github/workflows/build-offline-v2.yml']
    code = {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths}
    return digest({'source_contract': value['source_contract'], 'inventory': value['inventory'],
                   'upstream_archives': value['upstream']['records'] if value['upstream'] else {}, 'validator_code': code})
